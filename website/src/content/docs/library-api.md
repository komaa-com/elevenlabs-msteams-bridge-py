---
title: "Library API"
description: "Embed the bridge in your own asyncio app: start_server, custom vision hooks, custom agent transports, HMAC helpers, and protocol helpers."
---

The package is both a CLI and an importable Python library. Everything below is exported from the package root.

```python
from elevenlabs_msteams_bridge import load_config, start_server
```

## Run the bridge in your own service

`load_config()` reads the same environment variables as the CLI and raises a clear `ValueError` when a required variable is missing or a numeric one is not a number. `start_server(cfg)` is a coroutine that starts listening and returns a `BridgeServer` handle.

```python
import asyncio
from elevenlabs_msteams_bridge import load_config, start_server

async def main():
    server = await start_server(load_config())
    print("bridge up")
    try:
        await asyncio.Event().wait()   # run until cancelled
    finally:
        await server.close()           # drains live calls (session.end + close)

asyncio.run(main())
```

`server.drain()` ends every live call gracefully without stopping the listener; `server.close()` drains and stops. The CLI wires SIGTERM/SIGINT to this for you - in your own app, hook your shutdown path to `server.close()` so a rolling deploy never hard-drops a call.

## Custom vision hook

The `vision` argument to `start_server` is your own answer to the agent's `look` tool. The raw frame never leaves your process; only the string you return is sent to the agent. This example uses OpenAI's vision API (`pip install openai`):

```python
from openai import AsyncOpenAI
from elevenlabs_msteams_bridge import load_config, start_server

openai = AsyncOpenAI()  # reads OPENAI_API_KEY

async def describe(frame: dict, question: str) -> str:
    # frame: {"source": "camera" | "screenshare", "mime": ..., "dataBase64": ...,
    #         "width": ..., "height": ..., "participantName": ...}
    who = "the caller's shared screen" if frame["source"] == "screenshare" else "the caller's camera"
    res = await openai.chat.completions.create(
        model="gpt-4o",  # any vision-capable model
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"This is {who}. {question}"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{frame['mime']};base64,{frame['dataBase64']}", "detail": "low"},
                    },
                ],
            }
        ],
    )
    return res.choices[0].message.content or "I could not make out the image."

server = await start_server(load_config(), vision=describe)
```

Pass `vision=None` to disable path-2 vision entirely (the agent then falls back to the recording-gated ElevenLabs multimodal upload). Omit the argument to use the built-in describer, which is driven by `VISION_API_URL` / `VISION_MODEL`.

## Custom agent transport (testing)

The `connect_el` argument to `start_server` is an async factory that returns an `AgentPort`. The default opens a real ElevenLabs Agent socket; tests substitute a fake so no network is needed.

```python
from elevenlabs_msteams_bridge import load_config, start_server

async def fake_connector(cfg, log, handlers):
    class FakePort:
        conversation_id = "conv_test"
        is_open = True
        def send_audio_chunk(self, b64): ...
        def send_conversation_init(self, init): ...
        def send_pong(self, event_id): ...
        def send_contextual_update(self, text): ...
        def send_user_message(self, text): ...
        def send_client_tool_result(self, tool_call_id, result, is_error): ...
        async def attach_image(self, data, mime, question): ...
        def close(self): ...
    # push agent->bridge events at any time with handlers.on_message({...})
    return FakePort()

server = await start_server(load_config(), connect_el=fake_connector, vision=None)
```

The repository's own [test suite](https://github.com/komaa-com/elevenlabs-msteams-bridge-py/tree/main/tests) uses exactly this shape - `tests/conftest.py` has a reusable `FakeAgentPort`.

## HMAC helpers

Useful if you build tools that talk to the bridge, or want to test the upgrade.

```python
import time
from elevenlabs_msteams_bridge import sign, verify, is_fresh, TIMESTAMP_HEADER, SIGNATURE_HEADER

ts = int(time.time() * 1000)
signature = sign(secret, ts, call_id)   # HMAC-SHA256(secret, f"{ts}.{call_id}") hex
# send as headers X-StandIn-Timestamp / -Signature
verify(secret, ts, call_id, signature)  # constant-time, False on any missing input
is_fresh(ts, 60_000)                    # within the freshness window?
```

## Protocol helpers

Wire messages are plain dicts (they arrive and leave as JSON). `parse_worker_message(raw)` is the guarded parser (returns `None` on junk), and `pcm16k_bytes_to_ms(n)` converts PCM byte counts to milliseconds. See the [Wire Protocol](/elevenlabs-msteams-bridge-py/wire-protocol/) for the full contract.

## Also exported

- `authorize_upgrade`, `call_id_from_path`, `ReplayGuard` - the upgrade-authorization primitives.
- `CallSession`, `WorkerPort` - the per-call relay class and its transport protocol (advanced embedding).
- `assert_public_http_url`, `is_forbidden_ip`, `fetch_public_image` - the SSRF-guard primitives.
- `ElAgentSocket`, `get_signed_url`, `synthesize_goodbye`, `build_conversation_init`, `upload_conversation_file` - the ElevenLabs-side helpers.
- `load_dotenv` - the tiny `.env` loader the CLI uses.
- `render_metrics`, `logger` - metrics text and the minimal leveled logger.
