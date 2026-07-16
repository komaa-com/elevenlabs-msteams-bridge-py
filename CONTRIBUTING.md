# Contributing

Thanks for helping improve `elevenlabs-msteams-bridge`.

## Local setup

```bash
git clone https://github.com/komaa-com/elevenlabs-msteams-bridge-py
cd elevenlabs-msteams-bridge-py
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q            # the full suite runs with no network and no ElevenLabs account
ruff check src tests
ruff format --check src tests
```

## Conventions

- **One runtime dependency** (`aiohttp`); everything else is dev-only. Please do not add runtime dependencies without discussing first.
- **Audio is relayed verbatim at 16 kHz** - both sides speak base64 PCM16K and the worker re-aligns variable-length chunks, so the hot path is copy-only. Keep that boundary; the provider adapter (`elevenlabs.py`) owns the ElevenLabs event framing.
- **Tests use a fake `AgentPort`** (see `tests/conftest.py`), so the suite runs without an ElevenLabs account - including the barge-in ghost filter, backpressure drop, and goodbye paths.
- The wire contract with the StandIn media bridge (`protocol.py`, `hmac_auth.py`) is shared with the sibling bridges; changes there need to stay interoperable.
- Error paths matter: a malformed frame from either peer must never escape a WebSocket read loop (that would take down every live call).

## Parity with the Node.js sibling

This package mirrors [`@komaa/elevenlabs-msteams-bridge`](https://github.com/komaa-com/elevenlabs-msteams-bridge): same wire contract, same environment variables, same behaviors. When you change observable behavior here, check whether the Node implementation needs the same change (and vice versa) so the two stay drop-in interchangeable.

## Release flow (maintainers)

1. Bump `version` in `pyproject.toml` and commit.
2. Tag `vX.Y.Z` and push the tag; the publish workflow verifies tag == version, runs the suite, and publishes to PyPI via trusted publishing.

## Documentation policy

Document how to **connect to** the hosted StandIn service and how the bridge behaves on the wire. Do not document the internals of the hosted media bridge - this repository only depends on its published wire contract.
