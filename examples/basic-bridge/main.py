"""Minimal embedding of elevenlabs-msteams-bridge with a custom vision hook.

Run: `python main.py` (reads .env from this directory; see .env.example).
The custom `describe` function answers the agent's `look` tool - the raw frame
never leaves your process. Replace the stub with a call to any vision model.
"""

from __future__ import annotations

import asyncio
import signal

from elevenlabs_msteams_bridge import load_config, start_server
from elevenlabs_msteams_bridge.cli import _load_dotenv


async def describe(frame: dict, question: str) -> str:
    # frame["dataBase64"] is the JPEG/PNG frame, frame["mime"] its type,
    # frame["source"] is "camera" or "screenshare".
    # Call your vision model here (OpenAI, Azure OpenAI, Claude, a local VLM...).
    return f"(stub) I received a {frame.get('mime')} frame from the {frame.get('source')} and the question: {question}"


async def main() -> None:
    cfg = load_config()
    server = await start_server(cfg, vision=describe)
    print(f"Point your StandIn identity's agent WebSocket URL at ws://<this-host>:{cfg.port}/voice/msteams/stream")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.close()


if __name__ == "__main__":
    _load_dotenv()
    asyncio.run(main())
