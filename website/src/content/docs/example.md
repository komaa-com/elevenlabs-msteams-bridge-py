---
title: "Run the Example"
description: "A guided walkthrough of examples/basic-bridge: what each line does, how the vision hook works, and how to grow it into your own service."
---

The repository ships one example, [`examples/basic-bridge`](https://github.com/komaa-com/elevenlabs-msteams-bridge-py/tree/main/examples/basic-bridge) - a complete, working embedding in about 30 lines. This page walks through it so you understand every moving part before writing your own.

## What the example is

A single `main.py` that:

1. loads a `.env` file,
2. starts the bridge with `start_server()`,
3. plugs a **custom vision hook** into the agent's `look` tool,
4. shuts down gracefully on Ctrl-C / SIGTERM.

## Run it

```bash
pip install elevenlabs-msteams-bridge
git clone https://github.com/komaa-com/elevenlabs-msteams-bridge-py
cd elevenlabs-msteams-bridge-py/examples/basic-bridge
cp .env.example .env    # fill in the three required values
python main.py
```

It prints the WebSocket URL to give StandIn:

```text
Point your StandIn identity's agent WebSocket URL at ws://<this-host>:8080/voice/msteams/stream
```

Expose port 8080 with a tunnel (see [Getting Started](/elevenlabs-msteams-bridge-py/getting-started/)), set your StandIn identity's **agent WebSocket URL** to the `wss://` form, and place a Teams call - your ElevenLabs agent answers.

The three values in `.env`:

| Variable | What to put there |
|---|---|
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key (server-side only; never sent to the Teams side). |
| `ELEVENLABS_AGENT_ID` | The agent that should answer calls, from the Agents dashboard. |
| `WORKER_SHARED_SECRET` | The shared secret from StandIn pairing - both sides must match exactly. |

## The code, line by line

```python
from elevenlabs_msteams_bridge import load_config, load_dotenv, start_server

async def describe(frame: dict, question: str) -> str:
    # frame["dataBase64"] is the JPEG/PNG frame, frame["mime"] its type,
    # frame["source"] is "camera" or "screenshare".
    return f"(stub) I received a {frame.get('mime')} frame from the {frame.get('source')}"

async def main() -> None:
    cfg = load_config()
    server = await start_server(cfg, vision=describe)
    ...
    await stop.wait()      # run until SIGTERM / Ctrl-C
    await server.close()   # drain live calls gracefully
```

- **`load_dotenv()`** reads `.env` from the working directory (existing environment always wins), so the example runs the same way the CLI does.
- **`load_config()`** reads every setting from environment variables and fails loud on a missing required variable or a non-numeric number - a typo stops startup with a clear message instead of silently misbehaving.
- **`start_server(cfg, vision=describe)`** starts the WebSocket server and returns a handle. The `vision` argument is the interesting part, below.
- **`await server.close()`** ends every live call with a spoken-protocol `session.end` (not a hard socket drop) before the process exits.

## The vision hook

When your agent calls its `look` client tool ("what do you see?"), the bridge hands **your** function the latest camera or screen-share frame and the agent's question. Whatever text you return is what the agent gets as the tool result - **the raw frame never leaves your process**.

The stub just proves the wiring. Replace it with any vision-capable model:

```python
async def describe(frame: dict, question: str) -> str:
    # e.g. call OpenAI, Azure OpenAI, Claude, or a local VLM here with
    # the data URL f"data:{frame['mime']};base64,{frame['dataBase64']}"
    return await my_vision_model(frame, question)
```

Prefer configuration over code? Leave `vision=` out and set `VISION_API_URL` / `VISION_MODEL` instead - the built-in describer calls any OpenAI-compatible chat-completions endpoint. Pass `vision=None` to disable path-2 vision entirely. The trade-offs between the two vision paths are covered in [Vision and Tools](/elevenlabs-msteams-bridge-py/vision-and-tools/).

## From example to your own service

The example **is** the recommended embedding shape. To grow it:

- add your own logic around `start_server()` (it is just an awaitable in your event loop);
- swap the vision stub for a real model;
- set the [governor variables](/elevenlabs-msteams-bridge-py/governors-and-privacy/) (`MAX_CALL_MINUTES`, `EL_TTS_VOICE_ID`, `GOODBYE_TEXT`) before going to production;
- for tests, inject a fake agent with the `connect_el` argument - see [Library API](/elevenlabs-msteams-bridge-py/library-api/).

If you only need the stock behavior, skip the embedding entirely and run the `elevenlabs-msteams-bridge` CLI.
