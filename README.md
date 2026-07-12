# elevenlabs-msteams-bridge (Python)

Bridge **Microsoft Teams voice/video calls** to an **ElevenLabs Agent**.

This is the Python sibling of [`@komaa/elevenlabs-msteams-bridge`](https://www.npmjs.com/package/@komaa/elevenlabs-msteams-bridge)
(Node.js) - same wire contract, same environment variables, drop-in interchangeable behind the same
`.env` file. It terminates the StandIn media bridge wire protocol on one side and the ElevenLabs
Agent WebSocket on the other:

- **No transcoding**: both sides speak base64 PCM 16 kHz mono - the hot path is copy-only.
- **Barge-in**: ElevenLabs interruptions map to playback flushes, with ghost-audio filtering.
- **On-demand vision**: the agent's `look` client tool answers from the caller's camera or
  screen-share, via your own OpenAI-compatible vision endpoint (frames never persisted) or via
  ElevenLabs multimodal upload (gated on Teams recording).
- **Call governors**: a bridge-side hard time cap with a deterministic TTS goodbye, plus the
  worker-side governor.
- **Hardened**: HMAC-signed upgrades with replay guard, connection caps, SSRF-guarded image
  fetches, dead-peer detection, graceful SIGTERM drain, Prometheus `/metrics`.

[StandIn](https://standin.komaa.com) is the hosted media bridge that joins the Teams call and dials
this bridge - you run no Teams media stack yourself. Docs: [docs.komaa.com](https://docs.komaa.com/elevenlabs/installation).

## Install

```bash
pip install elevenlabs-msteams-bridge
```

Requires Python 3.10+.

## Run

```bash
ELEVENLABS_API_KEY=sk_... \
ELEVENLABS_AGENT_ID=agent_... \
WORKER_SHARED_SECRET=... \
elevenlabs-msteams-bridge
```

A `.env` file in the working directory is loaded automatically (existing environment wins). The
bridge listens on `ws://0.0.0.0:8080/voice/msteams/stream` by default; StandIn appends `/{callId}`
per call. Expose the port with a tunnel and register the `wss://` URL as your identity's
**Agent voice URL** in the StandIn dashboard.

Your ElevenLabs agent's audio input **and** output format must be **PCM 16000 Hz** - the bridge
ends the call with a clear error if the agent negotiates anything else.

## Embed

```python
import asyncio
from elevenlabs_msteams_bridge import load_config, start_server

async def main():
    server = await start_server(load_config())
    await asyncio.Event().wait()  # run until cancelled

asyncio.run(main())
```

Pass your own async `vision` callable to answer the agent's `look` tool with any model you like -
the raw frame never leaves your process:

```python
async def describe(frame: dict, question: str) -> str:
    ...  # call your vision model with frame["dataBase64"] / frame["mime"]
    return "a person holding a badge"

server = await start_server(load_config(), vision=describe)
```

## Configuration

Everything is environment variables; names are identical to the Node package.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `ELEVENLABS_API_KEY` | yes | - | Server-side ElevenLabs key (signed URLs, file upload, TTS). |
| `ELEVENLABS_AGENT_ID` | yes | - | The agent that answers calls. |
| `WORKER_SHARED_SECRET` | yes | - | Must equal the shared secret from StandIn pairing (HMAC upgrade check). |
| `PORT` / `BIND` | no | `8080` / `0.0.0.0` | Listen port / bind address. |
| `MAX_CALL_MINUTES` | no | `0` (off) | Bridge-side hard cap per call; on expiry a goodbye is spoken, then the call ends. |
| `EL_TTS_VOICE_ID` | no | - | Voice for the deterministic goodbye via standalone TTS. |
| `EL_TTS_MODEL_ID` | no | `eleven_turbo_v2_5` | TTS model for the goodbye line. |
| `GOODBYE_TEXT` / `GOODBYE_GRACE_MS` | no | (default line) / `8000` | Goodbye wording and playout grace. |
| `EL_FIRST_MESSAGE` | no | - | Greeting/disclosure override (must be allowlisted on the agent). |
| `EL_HOST` | no | `api.elevenlabs.io` | Regional pins: `api.us` / `api.eu.residency` / `api.in.residency` / `api.sg.residency` `.elevenlabs.io`. Restricted to elevenlabs.io hosts. |
| `EL_ENVIRONMENT` / `EL_AGENT_BRANCH_ID` | no | - | Staging environment / pinned agent branch. |
| `VISION_API_URL` / `VISION_API_KEY` / `VISION_MODEL` | no | - | OpenAI-compatible chat-completions endpoint for the `look` tool (describe-then-inject). |
| `HMAC_FRESHNESS_MS` | no | `60000` | Allowed clock skew + replay window for the signed upgrade. |
| `MAX_CONNECTIONS` / `MAX_CONNECTIONS_PER_IP` | no | `64` / = total | Connection caps. |
| `PRE_START_TIMEOUT_MS` | no | `10000` | Drop a worker that authenticates but never sends `session.start`. |
| `WORKER_IDLE_TIMEOUT_MS` | no | `90000` | Dead-peer window (the worker heartbeats every 30 s). |
| `TRUST_PROXY_XFF` | no | `false` | Trust the first `X-Forwarded-For` hop for the per-IP cap. |
| `TLS_CERT_PATH` / `TLS_KEY_PATH` | no | - | Serve native TLS (`wss`). Otherwise front the plain WS with a TLS terminator. |
| `LOG_TRANSCRIPTS` | no | `false` | Log transcripts - still gated on Teams recording being active. |
| `LOG_LEVEL` | no | `info` | `debug` / `info` / `warn` / `error`. |

## Endpoints

- `GET /healthz` - liveness.
- `GET /metrics` - Prometheus counters (calls, rejections, relayed/dropped frames).
- `GET /{...}/{callId}` + WebSocket upgrade - the worker wire, HMAC-signed with
  `X-OpenClawTeamsBridge-Timestamp` / `X-OpenClawTeamsBridge-Signature` over
  `"{timestampMs}.{callId}"`.

## Vision and recording

The `look` tool prefers your `VISION_API_URL` endpoint: the frame is described transiently and only
the **text** enters the conversation. Without one, the bridge falls back to uploading the frame to
ElevenLabs (multimodal) - that persists the frame with a third party, so it is only allowed while
Teams recording is active. Note that even path-2 descriptions become ElevenLabs conversation
content, which ElevenLabs retains per your agent's settings; enable the agent's zero-retention mode
if callers' surroundings must not be stored.

## License

MIT (c) Alaaeldin Elhenawy
