# basic-bridge (Python)

Minimal, working embedding of `elevenlabs-msteams-bridge`: `load_config()` + `start_server()` with
a custom vision hook for the agent's `look` tool.

## Run

```bash
pip install elevenlabs-msteams-bridge
cp .env.example .env   # fill in ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID, WORKER_SHARED_SECRET
python main.py
```

It prints the WebSocket URL to give StandIn. Expose port 8080 with a tunnel (Tailscale Funnel,
cloudflared, ngrok, ...), set your StandIn identity's **Agent voice URL** to the `wss://` URL, and
place a Teams call - your ElevenLabs agent answers.

The `describe` stub in `main.py` shows where to plug in your own vision model: it receives the
latest camera/screen-share frame and the agent's question, and whatever text it returns is what the
agent speaks from. The raw frame never leaves your process.

Full setup walkthrough: https://docs.komaa.com/elevenlabs/example
