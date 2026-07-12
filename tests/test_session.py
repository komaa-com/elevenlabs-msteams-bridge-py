import base64
import json

from elevenlabs_msteams_bridge.session import MAX_OUTBOUND_BUFFER_BYTES, CallSession

from conftest import FakeAgentPort, FakeWorkerPort, make_config, settle


def make_session(cfg=None, worker=None, agent=None):
    worker = worker or FakeWorkerPort()
    agent = agent or FakeAgentPort()

    async def connector(cfg_, log, handlers):
        connector.handlers = handlers  # type: ignore[attr-defined]
        return agent

    session = CallSession(cfg or make_config(), worker, "call-1", connect_el=connector, vision=None)
    return session, worker, agent, connector


def start_msg(**kw):
    msg = {
        "type": "session.start",
        "callId": "call-1",
        "threadId": "t",
        "caller": {"displayName": "Alice", "tenantId": "ten", "aadId": "aad-1"},
        "direction": "inbound",
    }
    msg.update(kw)
    return json.dumps(msg)


async def test_session_start_connects_and_inits():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    assert session.has_started
    kinds = [k for k, _ in agent.messages]
    assert kinds[0] == "init"
    init = agent.messages[0][1]
    assert init["dynamic_variables"]["caller_name"] == "Alice"
    assert init["user_id"] == "aad-1"
    session.end_call("test-done")


async def test_anonymous_caller_gets_no_user_id():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(start_msg(caller={}))
    await settle()
    init = agent.messages[0][1]
    assert "user_id" not in init
    assert init["dynamic_variables"]["caller_name"] == "caller"
    session.end_call("test-done")


async def test_audio_buffered_until_agent_open_then_flushed():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(start_msg())
    # session.start handling is async; frames sent before the connect resolves buffer
    session.handle_worker_message(json.dumps({"type": "audio.frame", "seq": 1, "timestampMs": 0, "payloadBase64": "QUJD"}))
    await settle()
    session.handle_worker_message(json.dumps({"type": "audio.frame", "seq": 2, "timestampMs": 20, "payloadBase64": "REVG"}))
    assert agent.audio == ["QUJD", "REVG"]
    session.end_call("test-done")


async def test_callid_mismatch_ends_call():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(start_msg(callId="other-call"))
    await settle()
    assert session.closed
    ends = worker.of_type("session.end")
    assert ends and ends[0]["reason"] == "callid-mismatch"


async def test_agent_audio_relayed_with_seq_and_timestamp():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    handlers = connector.handlers
    pcm = base64.b64encode(b"\x00" * 640).decode()
    handlers.on_message({"type": "audio", "audio_event": {"audio_base_64": pcm, "event_id": 1}})
    handlers.on_message({"type": "audio", "audio_event": {"audio_base_64": pcm, "event_id": 2}})
    frames = worker.of_type("audio.frame")
    assert [f["seq"] for f in frames] == [0, 1]
    assert frames[0]["timestampMs"] == 0
    assert frames[1]["timestampMs"] == 20  # 640 bytes = 20 ms
    session.end_call("test-done")


async def test_interruption_drops_ghost_audio():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    handlers = connector.handlers
    pcm = base64.b64encode(b"\x00" * 640).decode()
    handlers.on_message({"type": "interruption", "interruption_event": {"event_id": 5}})
    cancels = worker.of_type("assistant.cancel")
    assert cancels and cancels[0]["turnId"] == 5
    handlers.on_message({"type": "audio", "audio_event": {"audio_base_64": pcm, "event_id": 4}})  # ghost
    handlers.on_message({"type": "audio", "audio_event": {"audio_base_64": pcm, "event_id": 6}})  # fresh
    frames = worker.of_type("audio.frame")
    assert len(frames) == 1
    session.end_call("test-done")


async def test_el_ping_gets_pong():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    connector.handlers.on_message({"type": "ping", "ping_event": {"event_id": 42}})
    assert ("pong", 42) in agent.messages
    session.end_call("test-done")


async def test_worker_ping_gets_pong():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(json.dumps({"type": "ping", "ts": 777}))
    pongs = worker.of_type("pong")
    assert pongs and pongs[0]["ts"] == 777
    session.end_call("test-done")


async def test_end_call_tool():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    connector.handlers.on_message(
        {"type": "client_tool_call", "client_tool_call": {"tool_name": "end_call", "tool_call_id": "t1"}}
    )
    await settle()
    assert session.closed
    assert agent.closed
    ends = worker.of_type("session.end")
    assert ends and ends[0]["reason"] == "agent-ended-call"


async def test_express_tool_maps_to_expression():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    connector.handlers.on_message(
        {
            "type": "client_tool_call",
            "client_tool_call": {"tool_name": "express", "tool_call_id": "t2", "parameters": {"emotion": "joy"}},
        }
    )
    expr = worker.of_type("expression")
    assert expr and expr[0]["emotion"] == "joy"
    session.end_call("test-done")


async def test_unknown_tool_reports_error():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    connector.handlers.on_message(
        {"type": "client_tool_call", "client_tool_call": {"tool_name": "teleport", "tool_call_id": "t3"}}
    )
    results = [m for k, m in agent.messages if k == "tool_result"]
    assert results and results[-1][2] is True  # is_error
    session.end_call("test-done")


async def test_backpressure_drops_audio_keeps_control():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    worker.buffered = MAX_OUTBOUND_BUFFER_BYTES + 1
    pcm = base64.b64encode(b"\x00" * 640).decode()
    connector.handlers.on_message({"type": "audio", "audio_event": {"audio_base_64": pcm, "event_id": 1}})
    assert worker.of_type("audio.frame") == []  # dropped
    connector.handlers.on_message({"type": "interruption", "interruption_event": {"event_id": 2}})
    assert worker.of_type("assistant.cancel")  # control frames always pass
    session.end_call("test-done")


async def test_goodbye_fallback_user_message_and_dedup():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    session.handle_worker_message(json.dumps({"type": "assistant.say", "text": "bye now"}))
    await settle()
    session.handle_worker_message(json.dumps({"type": "assistant.say", "text": "bye again"}))
    await settle()
    user_msgs = [m for k, m in agent.messages if k == "user_message"]
    assert len(user_msgs) == 1 and "bye now" in user_msgs[0]
    session.end_call("test-done")


async def test_worker_close_tears_down_agent():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    session.handle_worker_close()
    assert session.closed
    assert agent.closed


async def test_worker_dropped_during_connect_closes_orphan_agent():
    import asyncio

    worker = FakeWorkerPort()
    agent = FakeAgentPort()
    release = asyncio.Event()

    async def slow_connector(cfg_, log, handlers):
        await release.wait()
        return agent

    session = CallSession(make_config(), worker, "call-1", connect_el=slow_connector, vision=None)
    session.handle_worker_message(start_msg())
    await settle()
    session.handle_worker_close()  # worker drops while EL is still connecting
    release.set()
    await settle()
    assert agent.closed  # the orphaned, billed conversation is closed


async def test_recording_gate_updates():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(start_msg(recordingStatus="active"))
    await settle()
    assert session._recording_active
    session.handle_worker_message(json.dumps({"type": "recording.status", "status": "stopped"}))
    assert not session._recording_active
    session.end_call("test-done")


async def test_look_without_video_reports_error():
    session, worker, agent, connector = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    connector.handlers.on_message(
        {"type": "client_tool_call", "client_tool_call": {"tool_name": "look", "tool_call_id": "t4"}}
    )
    await settle()
    results = [m for k, m in agent.messages if k == "tool_result"]
    assert results and results[-1][2] is True and "no video" in results[-1][1]
    session.end_call("test-done")


async def test_look_uses_vision_describer():
    async def describer(frame, question):
        return f"I see a cat ({frame['source']}, q={question})"

    worker = FakeWorkerPort()
    agent = FakeAgentPort()

    async def connector(cfg_, log, handlers):
        connector.handlers = handlers  # type: ignore[attr-defined]
        return agent

    session = CallSession(make_config(), worker, "call-1", connect_el=connector, vision=describer)
    session.handle_worker_message(start_msg())
    await settle()
    frame_b64 = base64.b64encode(b"jpegbytes").decode()
    session.handle_worker_message(
        json.dumps(
            {
                "type": "video.frame",
                "source": "camera",
                "ts": 0,
                "width": 640,
                "height": 360,
                "mime": "image/jpeg",
                "dataBase64": frame_b64,
            }
        )
    )
    connector.handlers.on_message(
        {
            "type": "client_tool_call",
            "client_tool_call": {"tool_name": "look", "tool_call_id": "t5", "parameters": {"question": "what?"}},
        }
    )
    await settle()
    results = [m for k, m in agent.messages if k == "tool_result"]
    assert results and results[-1][2] is False and "I see a cat" in results[-1][1]
    session.end_call("test-done")


async def test_unknown_video_source_ignored():
    session, worker, agent, _ = make_session()
    session.handle_worker_message(start_msg())
    await settle()
    session.handle_worker_message(
        json.dumps(
            {"type": "video.frame", "source": "evil", "ts": 0, "width": 1, "height": 1, "mime": "image/jpeg", "dataBase64": ""}
        )
    )
    assert session._latest_video_frame == {}
    session.end_call("test-done")


async def test_junk_frames_dropped():
    session, worker, agent, _ = make_session()
    session.handle_worker_message("not json at all")
    session.handle_worker_message(json.dumps({"noType": True}))
    assert not session.closed
    session.end_call("test-done")
