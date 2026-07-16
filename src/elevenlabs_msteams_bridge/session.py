"""One Teams call: pairs the worker WebSocket with one ElevenLabs Agent
conversation and relays between them.

Audio is relayed verbatim in both directions - both sides speak base64 PCM16K
and the worker re-aligns variable-length chunks itself, so the hot path is
copy-only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from collections import deque
from typing import Any, Protocol

from .config import BridgeConfig
from .elevenlabs import ElAgentSocket, ElConnector, ElSessionHandlers, build_conversation_init, synthesize_goodbye
from .log import logger
from .metrics import metric_inc
from .protocol import parse_worker_message, pcm16k_bytes_to_ms
from .ssrf import fetch_public_image
from .vision import VisionDescriber, make_vision_describer

# show_image fetch cap: display.image goes to a 640x360 tile; 5 MB is generous.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Pending caller-audio cap while EL connects: 250 x 20 ms = 5 s.
MAX_PENDING_AUDIO_FRAMES = 250

# 20 ms of PCM 16 kHz mono 16-bit = 16000 * 0.02 * 2 = 640 bytes (one hot-path frame).
PCM16K_FRAME_BYTES = 640

# Outbound (bridge->worker) send-buffer cap. Above this, drop realtime frames
# instead of letting a stalled worker balloon memory.
MAX_OUTBOUND_BUFFER_BYTES = 1 * 1024 * 1024

# Pending contextual-update cap while EL connects (participants/dtmf).
MAX_PENDING_CONTEXT = 32

# Extra headroom on top of the goodbye grace before the governor force-ends the
# call, so a hung TTS synth can never wedge a time-limited call open.
GOODBYE_HARD_CAP_MS = 8_000

# Min gap between "now speaking" contextual updates (group calls), so VAD
# flapping between speakers cannot spam the agent.
SPEAKER_UPDATE_MIN_INTERVAL_MS = 5_000

# Dead-peer window: worker heartbeats every 30 s -> 3 missed pings ends the call.
DEFAULT_WORKER_IDLE_TIMEOUT_MS = 90_000

# Inline show_image dataBase64 cap - same 5 MB bound as the URL path,
# expressed in base64 characters (4 chars per 3 bytes).
MAX_INLINE_IMAGE_B64_CHARS = -(-MAX_IMAGE_BYTES // 3) * 4

# Length caps on agent-supplied control-frame strings. expression is undroppable,
# so an unbounded emotion would bypass the outbound memory bound; caption/mode are
# bounded for the same reason. Mirrors OpenAI/Deepgram.
MAX_EMOTION_CHARS = 64
MAX_CAPTION_CHARS = 500
MAX_MODE_CHARS = 32

_IMAGE_MIME_RE = re.compile(r"^image/(jpeg|png)$")


def _now_ms() -> float:
    return time.monotonic() * 1000


class WorkerPort(Protocol):
    """What the session needs from the worker connection; the server provides
    the real one, tests fake it."""

    @property
    def is_open(self) -> bool: ...

    @property
    def buffered_bytes(self) -> int: ...

    def send_text(self, payload: str) -> None: ...
    def close(self, code: int, reason: str) -> None: ...


class CallSession:
    """Relay for a single authenticated worker connection.

    The server feeds inbound worker frames via handle_worker_message() and
    signals disconnect via handle_worker_close(); everything outbound goes
    through the WorkerPort.
    """

    def __init__(
        self,
        cfg: BridgeConfig,
        worker: WorkerPort,
        call_id: str,
        connect_el: ElConnector | None = None,
        vision: VisionDescriber | None | str = "auto",
        on_closed: Any = None,
    ) -> None:
        self.cfg = cfg
        self.worker = worker
        self.call_id = call_id
        self.log = logger(f"call:{call_id[:12]}")
        self._connect_el: ElConnector = connect_el or ElAgentSocket.connect
        self.vision: VisionDescriber | None = make_vision_describer(cfg) if vision == "auto" else vision  # type: ignore[assignment]
        self._on_closed = on_closed

        self.el: Any = None
        self.closed = False
        self.session_started = False

        # outbound audio bookkeeping (bridge -> worker)
        self._out_seq = 0
        self._out_timestamp_ms = 0.0
        # backpressure-warn throttle (avoid ~50 warn lines/s when a worker stalls)
        self._dropped_frames = 0
        self._last_backpressure_warn_ms = 0.0

        # barge-in ghost filter: drop EL audio with event_id <= the last interruption
        self._last_interrupt_event_id = -1
        # highest event_id relayed so far - the flush point for goodbye ghost-dropping
        self._last_seen_audio_event_id = 0
        # hard mute: set ONLY while a deterministic TTS goodbye plays (never for the
        # user_message fallback, where the agent itself must stay audible)
        self._mute_agent_audio = False
        # first goodbye wins: both governors (worker assistant.say + bridge time limit) can race
        self._goodbye_in_progress = False
        # group-call speaker attribution + rate limit
        self._last_speaker_name: str | None = None
        self._last_speaker_update_ms = 0.0
        self._participant_count = 1
        # caller audio arriving while the EL socket is still connecting
        self._pending_audio: deque[str] = deque(maxlen=MAX_PENDING_AUDIO_FRAMES)
        # contextual updates (participants/dtmf) that arrived before the EL socket was open
        self._pending_context: deque[str] = deque(maxlen=MAX_PENDING_CONTEXT)

        # Teams recording gate: transcripts may be logged/persisted only when "active"
        self._recording_active = False

        # vision groundwork: latest inbound frame per source, memory only
        self._latest_video_frame: dict[str, dict[str, Any]] = {}

        # bridge-side call governor
        self._governor_handle: asyncio.TimerHandle | None = None
        self._goodbye_handle: asyncio.TimerHandle | None = None

        # Dead-peer detection: a half-open TCP socket (NAT timeout, peer crash)
        # delivers nothing and never closes - the session would stay "live" for
        # hours, holding the billed EL conversation open AND blocking every
        # reconnect for this callId with a 409.
        self._last_worker_activity_ms = _now_ms()
        idle_ms = cfg.worker_idle_timeout_ms if cfg.worker_idle_timeout_ms > 0 else DEFAULT_WORKER_IDLE_TIMEOUT_MS
        self._idle_ms = idle_ms
        self._idle_task = asyncio.create_task(self._idle_watchdog(max(0.02, min(idle_ms / 6000, 15.0))))

    # ---- lifecycle wiring (called by the server's read loop) ----

    @property
    def has_started(self) -> bool:
        """Whether session.start has arrived (the server's pre-start timer asks)."""
        return self.session_started

    def handle_worker_message(self, raw: str | bytes) -> None:
        self._last_worker_activity_ms = _now_ms()  # any inbound frame proves the peer is alive
        try:
            self._on_worker_message(raw)
        except Exception as err:
            # a handler error must never escape into the server's read loop
            self.log.error(f"error handling worker message: {err}")

    def handle_worker_close(self) -> None:
        self._teardown("worker-closed")

    def handle_worker_error(self, err: Exception) -> None:
        self.log.warn(f"worker socket error: {err}")
        self._teardown("worker-error")

    async def _idle_watchdog(self, interval_s: float) -> None:
        while not self.closed:
            await asyncio.sleep(interval_s)
            if self.closed:
                return
            if _now_ms() - self._last_worker_activity_ms > self._idle_ms:
                self.log.warn(f"no worker message in {int(self._idle_ms)}ms (dead peer?); ending the call")
                self.end_call("worker-idle-timeout")
                return

    # ---- worker -> bridge ----

    def _on_worker_message(self, raw: str | bytes) -> None:
        msg = parse_worker_message(raw)
        if msg is None:
            self.log.warn("unparseable worker frame; dropping")
            return
        mtype = msg["type"]
        if mtype == "session.start":
            if self.session_started:
                # A second session.start would orphan the first EL socket; the worker
                # sends exactly one per connection, so treat a repeat as a protocol error.
                self.log.warn("duplicate session.start ignored")
                return
            # Mark started SYNCHRONOUSLY: audio frames can arrive between this
            # message and the scheduled coroutine's first step, and they must be
            # buffered (not dropped) for the flush after connect.
            self.session_started = True
            asyncio.ensure_future(self._on_session_start_safe(msg))
        elif mtype == "audio.frame":
            # hot path: caller audio -> agent, verbatim. While the EL socket is
            # still connecting, buffer (bounded) instead of dropping the caller's
            # first words; flushed right after conversation init.
            payload = msg.get("payloadBase64")
            if not isinstance(payload, str):
                return
            if self.el is not None:
                self.el.send_audio_chunk(payload)
                metric_inc("bridge_frames_to_agent_total")
                self._note_speaker(msg.get("speakerName"))
            elif self.session_started:
                self._pending_audio.append(payload)  # deque drops the oldest at cap
        elif mtype == "ping":
            self._send_to_worker({"type": "pong", "ts": msg.get("ts")})
        elif mtype == "participants":
            count = msg.get("count")
            if isinstance(count, (int, float)):
                self._participant_count = int(count)
                self._push_context(
                    "This is a 1:1 call with a single human caller."
                    if count <= 1
                    else f"There are {int(count)} human participants on this call. Stay quiet unless directly addressed."
                )
        elif mtype == "dtmf":
            digit = msg.get("digit")
            if isinstance(digit, str) and digit:
                self._push_context(f'The caller pressed the "{digit}" key on their keypad.')
        elif mtype == "recording.status":
            self._recording_active = msg.get("status") == "active"
            self.log.info(f"recording.status = {msg.get('status')}")
        elif mtype == "video.frame":
            # Known sources only (camera/screenshare): the key comes from the peer,
            # so an unexpected value must not grow the map unbounded.
            source = msg.get("source")
            if source in ("camera", "screenshare"):
                self._latest_video_frame[source] = msg  # buffered for on-demand vision; not persisted
            else:
                self.log.debug(f'ignoring video.frame with unknown source "{source}"')
        elif mtype == "assistant.say":
            # worker-side governor: speak, the worker tears down afterwards
            asyncio.ensure_future(self._perform_goodbye_safe(str(msg.get("text") or "")))
        elif mtype == "session.end":
            self.log.info(f"session.end from worker: {msg.get('reason')}")
            self._teardown("worker-session-end")
        else:
            self.log.debug(f"ignoring worker message type {mtype}")

    async def _on_session_start_safe(self, msg: dict[str, Any]) -> None:
        try:
            await self._on_session_start(msg)
        except Exception as err:
            self.log.error(f"session.start handling failed: {err}")

    async def _on_session_start(self, msg: dict[str, Any]) -> None:
        msg_call_id = msg.get("callId")
        if msg_call_id and msg_call_id != self.call_id:
            # must match the HMAC-authenticated callId in the URL path (wire contract).
            self.log.error(f"session.start callId {msg_call_id} != URL callId {self.call_id}; closing")
            self.end_call("callid-mismatch")
            return
        direction = msg.get("direction") or "inbound"
        recording = msg.get("recordingStatus") or "unknown"
        self.log.info(f"session.start (direction={direction}, recording={recording})")
        self._recording_active = recording == "active"

        handlers = ElSessionHandlers(
            on_message=self._on_el_message,
            on_close=self._on_el_close,
            on_error=lambda err: self.log.warn(f"EL socket error: {err}"),
        )
        try:
            el = await self._connect_el(self.cfg, self.log, handlers)
        except Exception as err:
            metric_inc("bridge_el_connect_failures_total")
            self.log.error(f"could not open ElevenLabs session: {err}")
            self.end_call("agent-unavailable")
            return

        # The worker may have dropped (ring cancelled, rollout) DURING the connect
        # above. If so, teardown already ran with self.el still None - keeping the
        # just-opened socket would orphan a live, billed ElevenLabs conversation.
        if self.closed:
            self.log.info("worker closed during EL connect; closing the orphaned agent socket")
            try:
                el.close()
            except Exception:
                pass
            return
        self.el = el

        # Per-call personalization. Caller fields are all nullable - default, never send null.
        caller = msg.get("caller") or {}
        el.send_conversation_init(
            build_conversation_init(
                dynamic_variables={
                    "caller_name": (caller.get("displayName") or "").strip() or "caller",
                    "tenant_id": (caller.get("tenantId") or "").strip() or "unknown-tenant",
                    "call_direction": (msg.get("direction") or "").strip() or "inbound",
                },
                environment=self.cfg.el_environment,
                first_message=self.cfg.el_first_message,
                # per-person memory: AAD id when known; omitted for guests/anonymous
                # so distinct callers never share an identity
                user_id=(caller.get("aadId") or "").strip() or None,
                branch_id=self.cfg.el_agent_branch_id,
            )
        )
        # flush caller audio buffered while the socket was connecting
        while self._pending_audio:
            el.send_audio_chunk(self._pending_audio.popleft())
        # flush contextual updates (participants/dtmf) that arrived during the
        # connect window - the "N humans, stay quiet" signal often lands right at join
        while self._pending_context:
            el.send_contextual_update(self._pending_context.popleft())
        self.log.info("ElevenLabs agent session open; relaying")

        # Bridge-side governor: ElevenLabs doesn't know about your billing.
        if self.cfg.max_call_minutes > 0:
            limit_s = self.cfg.max_call_minutes * 60
            loop = asyncio.get_running_loop()
            self._governor_handle = loop.call_later(
                limit_s, lambda: asyncio.ensure_future(self._on_governor_limit_safe())
            )
            self.log.info(f"governor armed: max {self.cfg.max_call_minutes:g} min")

    async def _on_governor_limit_safe(self) -> None:
        try:
            await self._on_governor_limit()
        except Exception as err:
            self.log.error(f"governor error: {err}")

    async def _on_governor_limit(self) -> None:
        """Time limit hit: speak the goodbye, let it play out, then tear the call down."""
        if self.closed:
            return
        self.log.info("governor: call time limit reached")
        # Guarantee teardown regardless of the goodbye. Arm a HARD-bounded
        # deadline BEFORE awaiting the goodbye - a hung/slow TTS must never
        # wedge the call open past its limit.
        hard_ms = self.cfg.goodbye_grace_ms + GOODBYE_HARD_CAP_MS
        loop = asyncio.get_running_loop()
        self._goodbye_handle = loop.call_later(hard_ms / 1000, lambda: self.end_call("time-limit"))
        played_ms = await self._perform_goodbye(self.cfg.goodbye_text)
        if self.closed:
            return  # the hard deadline (or another path) already tore down
        # Deterministic TTS reports its real duration; the agent-side fallback does
        # not. Reschedule to the real grace, but never later than the hard cap.
        grace_ms = min(played_ms if played_ms is not None else self.cfg.goodbye_grace_ms, hard_ms)
        if self._goodbye_handle:
            self._goodbye_handle.cancel()
        self._goodbye_handle = loop.call_later((grace_ms + 500) / 1000, lambda: self.end_call("time-limit"))

    def _note_speaker(self, name: Any) -> None:
        """Group-call speaker attribution: the worker tags audio.frame with the
        active speaker's display name. Surface it to the agent as a
        non-interrupting contextual update - only in group calls (1:1 attribution
        is noise), only when the name CHANGES, and rate-limited so VAD flapping
        between speakers cannot spam the agent."""
        if not name or not isinstance(name, str) or self._participant_count <= 1:
            return
        now = _now_ms()
        if name == self._last_speaker_name or now - self._last_speaker_update_ms < SPEAKER_UPDATE_MIN_INTERVAL_MS:
            return
        self._last_speaker_name = name
        self._last_speaker_update_ms = now
        if self.el is not None:
            self.el.send_contextual_update(f"The person now speaking is {name}.")

    def _push_context(self, text: str) -> None:
        """Queue a contextual update (participants/dtmf). While the EL socket is
        still connecting it is buffered and flushed after conversation init, so a
        "N humans on the call" signal that lands right at join is not lost."""
        if self.el is not None:
            self.el.send_contextual_update(text)
        elif self.session_started and not self.closed:
            self._pending_context.append(text)  # deque drops the oldest at cap

    # ---- EL -> bridge ----

    def _on_el_close(self, code: int, reason: str) -> None:
        self.log.info(f"EL socket closed ({code} {reason})")
        self.end_call("agent-disconnected")

    def _on_el_message(self, msg: dict[str, Any]) -> None:
        # Defensive: one malformed EL frame must never raise out of the read
        # loop. Guard the nested event objects like parse_worker_message guards
        # the worker side.
        mtype = msg.get("type")
        if mtype == "audio":
            ev = msg.get("audio_event")
            if (
                not isinstance(ev, dict)
                or not isinstance(ev.get("event_id"), (int, float))
                or not isinstance(ev.get("audio_base_64"), str)
            ):
                self.log.warn("EL audio frame missing audio_event/event_id/audio_base_64; dropping")
                return
            event_id = int(ev["event_id"])
            self._last_seen_audio_event_id = max(self._last_seen_audio_event_id, event_id)
            if self._mute_agent_audio:
                self.log.debug(f"dropping agent audio {event_id} (deterministic goodbye playing)")
                return
            if event_id <= self._last_interrupt_event_id:
                self.log.debug(
                    f"dropping ghost audio event {event_id} (interrupted at {self._last_interrupt_event_id})"
                )
                return
            self._emit_audio_to_worker(ev["audio_base_64"])
        elif mtype == "interruption":
            ev = msg.get("interruption_event")
            if not isinstance(ev, dict) or not isinstance(ev.get("event_id"), (int, float)):
                self.log.warn("EL interruption missing interruption_event/event_id; dropping")
                return
            event_id = int(ev["event_id"])
            self._last_interrupt_event_id = max(self._last_interrupt_event_id, event_id)
            # turnId = EL event_id; the worker's playback flush ignores the value
            # but the field must serialize
            self._send_to_worker({"type": "assistant.cancel", "turnId": event_id})
            self.log.info(f"barge-in: interruption at event {event_id}")
        elif mtype == "ping":
            ev = msg.get("ping_event")
            if not isinstance(ev, dict) or not isinstance(ev.get("event_id"), (int, float)):
                self.log.warn("EL ping missing ping_event/event_id; dropping")
                return
            if self.el is not None:
                self.el.send_pong(int(ev["event_id"]))
        elif mtype in ("user_transcript", "agent_response"):
            # Recording gate: never log/persist transcripts unless Teams recording is active.
            if self.cfg.log_transcripts and self._recording_active:
                self.log.info(str(mtype), msg)
        elif mtype == "client_tool_call":
            call = msg.get("client_tool_call")
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("tool_name"), str)
                or not isinstance(call.get("tool_call_id"), str)
            ):
                self.log.warn("EL client_tool_call missing tool_name/tool_call_id; dropping")
                return
            self._on_client_tool_call(call)
        elif mtype in ("conversation_initiation_metadata", "vad_score"):
            pass  # metadata handled in ElAgentSocket; vad is informational
        else:
            self.log.debug(f"ignoring EL message type {mtype}")

    def _on_client_tool_call(self, call: dict[str, Any]) -> None:
        """Map agent client tools -> worker capabilities:
        end_call -> session.end, express -> expression, show_image -> display.image,
        look -> vision."""
        params = call.get("parameters") or {}
        if not isinstance(params, dict):
            params = {}
        tool = call["tool_name"]
        tool_call_id = call["tool_call_id"]
        if tool == "end_call":
            if self.el is not None:
                self.el.send_client_tool_result(tool_call_id, "call ended", False)
            self.log.info("agent requested end_call")
            self.end_call("agent-ended-call")
        elif tool == "express":
            emotion = params.get("emotion").strip() if isinstance(params.get("emotion"), str) else ""
            if not emotion:
                if self.el is not None:
                    self.el.send_client_tool_result(tool_call_id, "express requires an 'emotion' parameter", True)
                return
            if len(emotion) > MAX_EMOTION_CHARS:
                if self.el is not None:
                    self.el.send_client_tool_result(
                        tool_call_id, f"express: 'emotion' must be at most {MAX_EMOTION_CHARS} characters", True
                    )
                return
            self._send_to_worker({"type": "expression", "emotion": emotion})
            if self.el is not None:
                self.el.send_client_tool_result(tool_call_id, f"expressing {emotion}", False)
        elif tool == "show_image":
            asyncio.ensure_future(self._on_show_image(tool_call_id, params))
        elif tool == "look":
            asyncio.ensure_future(self._on_look(tool_call_id, params))
        else:
            if self.el is not None:
                self.el.send_client_tool_result(tool_call_id, f'tool "{tool}" is not implemented by this bridge', True)
            self.log.warn(f"unmapped client tool: {tool}")

    async def _on_show_image(self, tool_call_id: str, params: dict[str, Any]) -> None:
        """show_image -> display.image on the bot's video tile. Accepts either
        inline base64 ({dataBase64, mime}) or a URL the bridge fetches server-side."""
        try:
            data_base64 = params.get("dataBase64") if isinstance(params.get("dataBase64"), str) else None
            if data_base64 and len(data_base64) > MAX_INLINE_IMAGE_B64_CHARS:
                raise ValueError(
                    f"inline image too large ({len(data_base64)} base64 chars, max {MAX_INLINE_IMAGE_B64_CHARS})"
                )
            mime = params.get("mime") if isinstance(params.get("mime"), str) else None
            url = params.get("url") if isinstance(params.get("url"), str) else None
            if not data_base64 and url:
                # SSRF guard: the URL is agent-(LLM-)controlled, i.e. indirectly
                # caller-controlled. fetch_public_image validates the host, then
                # PINS the connect-time DNS resolution through the same
                # private-range check. No redirects, bounded time and size.
                img_bytes, mime = await fetch_public_image(url, MAX_IMAGE_BYTES, 10_000)
                data_base64 = base64.b64encode(img_bytes).decode("ascii")
            if not data_base64 or not mime or not _IMAGE_MIME_RE.match(mime):
                raise ValueError("show_image needs {dataBase64, mime} or {url} resolving to image/jpeg or image/png")
            delivered = self._send_to_worker(
                {
                    "type": "display.image",
                    "dataBase64": data_base64,
                    "mime": mime,
                    "durationMs": params.get("durationMs")
                    if isinstance(params.get("durationMs"), (int, float))
                    else None,
                    "mode": params.get("mode")[:MAX_MODE_CHARS] if isinstance(params.get("mode"), str) else None,
                    "ts": 0,
                    "caption": params.get("caption")[:MAX_CAPTION_CHARS]
                    if isinstance(params.get("caption"), str)
                    else None,
                }
            )
            # Tell the agent the truth: a frame dropped under backpressure was
            # NOT shown - claiming success would leave it talking about an image
            # the caller never saw.
            if not delivered:
                raise ValueError("image could not be delivered (worker connection is congested); try again")
            if self.el is not None:
                self.el.send_client_tool_result(tool_call_id, "image is being shown to the caller", False)
        except Exception as err:
            self.log.warn(f"show_image failed: {err}")
            if self.el is not None:
                self.el.send_client_tool_result(tool_call_id, f"show_image failed: {err}", True)

    async def _on_look(self, tool_call_id: str, params: dict[str, Any]) -> None:
        """Vision on demand - agent client tool `look`
        ({source?: "camera"|"screenshare", question?: string}).

        Route: prefer path 2 (describe via YOUR vision model -> answer in the
        tool result; frames are processed transiently, not persisted). Fall back
        to path 1 (upload to ElevenLabs + multimodal_message) - that one
        PERSISTS the frame with a third party, so it is gated on Teams recording
        being active."""
        requested = params.get("source") if isinstance(params.get("source"), str) else None
        frame = (
            (self._latest_video_frame.get(requested) if requested else None)
            or self._latest_video_frame.get("screenshare")
            or self._latest_video_frame.get("camera")
        )
        if frame is None:
            if self.el is not None:
                self.el.send_client_tool_result(
                    tool_call_id,
                    "no video is available - the caller has not shared their camera or screen",
                    True,
                )
            return
        question = params.get("question") if isinstance(params.get("question"), str) else ""
        question = question.strip() or "Describe what is visible."
        try:
            if self.vision is not None:
                # Path 2 is INTENTIONALLY NOT recording-gated. The raw frame never
                # leaves the bridge - only a text description does - but note that
                # description becomes ElevenLabs conversation content, which EL
                # persists by default. Operators who need "no vision until
                # recording is on" should enable EL zero-retention or use
                # recording-gated path 1. See "Vision and recording" in the README.
                description = await self.vision(frame, question)
                if self.el is not None:
                    self.el.send_client_tool_result(tool_call_id, description, False)
                return
            if not self._recording_active:
                if self.el is not None:
                    self.el.send_client_tool_result(
                        tool_call_id,
                        "cannot inspect video: Teams recording is not active, so frames may not be shared "
                        "(and no local vision endpoint is configured)",
                        True,
                    )
                return
            el = self.el
            if el is None:
                raise RuntimeError("agent connection is not open")
            who = (
                f"screen shared by {frame.get('participantName') or 'a participant'}"
                if frame.get("source") == "screenshare"
                else f"camera of {frame.get('participantName') or 'the caller'}"
            )
            mime = frame.get("mime")
            if not isinstance(mime, str) or not mime:
                raise ValueError("video frame carries no mime type")
            try:
                frame_bytes = base64.b64decode(frame.get("dataBase64") or "", validate=False)
            except (binascii.Error, ValueError) as err:
                raise ValueError(f"video frame is not valid base64: {err}") from None
            await el.attach_image(frame_bytes, mime, f"[live Teams frame: {who}] {question}")
            if self.el is not None:
                self.el.send_client_tool_result(
                    tool_call_id, "the frame was attached to the conversation - answer based on it", False
                )
        except Exception as err:
            self.log.warn(f"look failed: {err}")
            if self.el is not None:
                self.el.send_client_tool_result(tool_call_id, f"look failed: {err}", True)

    # ---- governor goodbye ----

    async def _perform_goodbye_safe(self, text: str) -> None:
        try:
            await self._perform_goodbye(text)
        except Exception as err:
            self.log.error(f"goodbye failed: {err}")

    async def _perform_goodbye(self, text: str) -> float | None:
        """Speak a goodbye line (both governors: worker assistant.say and the
        bridge-side time limit). Flushes buffered playback first (assistant.cancel
        + drop in-flight ghosts up to the last seen event_id) so stale agent audio
        cannot delay the goodbye.

        Preferred: deterministic, the exact text via standalone TTS - the agent
        is hard-muted while it plays and the real duration (ms) is returned.
        Fallback: the agent itself says it via user_message - its audio MUST keep
        relaying (mute stays off), duration unknown (None)."""
        if self._goodbye_in_progress:
            # Both governors can race; running twice would double-speak and leave
            # the mute latch in an ambiguous state - first one wins.
            self.log.info("goodbye already in progress; ignoring duplicate")
            return None
        self._goodbye_in_progress = True
        self.log.info("speaking goodbye")
        self._send_to_worker({"type": "assistant.cancel", "turnId": 0})
        self._last_interrupt_event_id = max(self._last_interrupt_event_id, self._last_seen_audio_event_id)
        if self.cfg.el_tts_voice_id:
            try:
                self._mute_agent_audio = True  # only the deterministic goodbye may speak now
                pcm = await synthesize_goodbye(self.cfg, text)
                # Emit as 20 ms frames like the hot path, rather than one
                # multi-second frame, so playback does not depend on the worker
                # re-aligning a giant chunk.
                for off in range(0, len(pcm), PCM16K_FRAME_BYTES):
                    chunk = pcm[off : off + PCM16K_FRAME_BYTES]
                    # undroppable: the goodbye is the deterministic compliance line at
                    # call-end; it must play even under worker backpressure (parity w/ deepgram).
                    self._emit_audio_to_worker(base64.b64encode(chunk).decode("ascii"), undroppable=True)
                played_ms = pcm16k_bytes_to_ms(len(pcm))
                # Unmute once the goodbye has played out. Normally the call ends
                # first (time-limit teardown, or the worker hangs up after its
                # assistant.say) - but if a peer fails to tear down, the agent
                # must not stay silently muted for the rest of the call.
                asyncio.get_running_loop().call_later(
                    (played_ms + 250) / 1000, lambda: setattr(self, "_mute_agent_audio", False)
                )
                return played_ms
            except Exception as err:
                self._mute_agent_audio = False  # fallback: the agent must stay audible
                self.log.warn(f"goodbye TTS failed ({err}); falling back to user_message")
        if self.el is not None:
            self.el.send_user_message(
                f'[system: the call is about to end due to a time limit. Say a brief goodbye now: "{text}"]'
            )
        return None

    # ---- plumbing ----

    def _emit_audio_to_worker(self, base64_pcm: str, undroppable: bool = False) -> None:
        frame = {
            "type": "audio.frame",
            "seq": self._out_seq,
            "timestampMs": round(self._out_timestamp_ms),
            "payloadBase64": base64_pcm,
        }
        self._out_seq += 1
        # advance the timeline by the actual PCM duration - exact decoded length
        # (frames are <=1 KB, so the decode is cheap and is correct for unpadded
        # base64 where arithmetic on the string length drifts)
        self._out_timestamp_ms += pcm16k_bytes_to_ms(len(base64.b64decode(base64_pcm)))
        metric_inc("bridge_frames_to_worker_total")
        self._send_to_worker(frame, undroppable)

    def _send_to_worker(self, msg: dict[str, Any], undroppable: bool = False) -> bool:
        """Send one frame; False when the frame was dropped (socket closed or
        realtime backpressure), True when it was queued for delivery."""
        if not self.worker.is_open:
            return False
        # Backpressure guard: if the worker stalls, the outbound buffer grows
        # unbounded (50 audio.frames/s) and leaks memory. Above the cap, drop
        # this frame rather than queue it. ONLY the continuous realtime audio.frame
        # is droppable. display.image is a ONE-SHOT the agent is told succeeded, so
        # dropping it desyncs the agent's belief from what the caller sees; control
        # frames (assistant.cancel, session.end, pong, expression) are tiny and
        # load-bearing; and the deterministic goodbye TTS passes undroppable so a
        # backpressured call-end still plays the compliance line.
        droppable = msg.get("type") == "audio.frame" and not undroppable
        if droppable and self.worker.buffered_bytes > MAX_OUTBOUND_BUFFER_BYTES:
            self._dropped_frames += 1
            metric_inc("bridge_frames_dropped_total")
            now = _now_ms()
            # Throttle the log: warn at most once per second with the total.
            if now - self._last_backpressure_warn_ms >= 1000:
                self.log.warn(
                    f"worker send backpressure: dropped {self._dropped_frames} frame(s) "
                    f"(buffered {self.worker.buffered_bytes} bytes)"
                )
                self._last_backpressure_warn_ms = now
                self._dropped_frames = 0
            return False
        self.worker.send_text(json.dumps(msg))
        return True

    def shutdown(self, reason: str) -> None:
        """Graceful external shutdown (e.g. SIGTERM drain): tell the worker the
        call is ending, then close both sockets. Idempotent via the closed flag."""
        self.end_call(reason)

    def end_call(self, reason: str) -> None:
        """Ask the worker to tear the call down, then close both sockets."""
        if not self.closed:
            self._send_to_worker({"type": "session.end", "reason": reason})
        self._teardown(reason)

    def _teardown(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.log.info(f"teardown: {reason}")
        if self._governor_handle:
            self._governor_handle.cancel()
            self._governor_handle = None
        if self._goodbye_handle:
            self._goodbye_handle.cancel()
            self._goodbye_handle = None
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            if self.el is not None:
                self.el.close()
        except Exception:
            pass
        try:
            self.worker.close(1000, reason)
        except Exception:
            pass
        self._latest_video_frame.clear()
        self._pending_audio.clear()
        self._pending_context.clear()
        # let the server de-register this call (registry eviction, dup-callId dedup)
        try:
            if self._on_closed is not None:
                self._on_closed()
        except Exception:
            pass  # registry callback must never raise back into teardown
