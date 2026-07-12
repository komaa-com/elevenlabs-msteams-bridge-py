from __future__ import annotations

import asyncio
from typing import Any

import pytest

from elevenlabs_msteams_bridge.config import BridgeConfig


def make_config(**overrides: Any) -> BridgeConfig:
    base: dict[str, Any] = dict(
        port=8080,
        host="127.0.0.1",
        worker_shared_secret="test-secret",
        elevenlabs_api_key="xi-test",
        elevenlabs_agent_id="agent_test",
        el_host="api.elevenlabs.io",
        el_environment=None,
        el_first_message=None,
        el_agent_branch_id=None,
        el_tts_voice_id=None,
        el_tts_model_id="eleven_turbo_v2_5",
        vision_api_url=None,
        vision_api_key=None,
        vision_model=None,
        max_call_minutes=0,
        goodbye_text="goodbye",
        goodbye_grace_ms=100,
        hmac_freshness_ms=60_000,
        max_connections=0,
        max_connections_per_ip=0,
        pre_start_timeout_ms=0,
        worker_idle_timeout_ms=0,
        trust_proxy=False,
        tls_cert_path=None,
        tls_key_path=None,
        log_transcripts=False,
    )
    base.update(overrides)
    return BridgeConfig(**base)


class FakeWorkerPort:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self.buffered = 0

    @property
    def is_open(self) -> bool:
        return self.closed is None

    @property
    def buffered_bytes(self) -> int:
        return self.buffered

    def send_text(self, payload: str) -> None:
        import json

        self.sent.append(json.loads(payload))

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def of_type(self, mtype: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == mtype]


class FakeAgentPort:
    def __init__(self) -> None:
        self.conversation_id: str | None = "conv_test"
        self.audio: list[str] = []
        self.messages: list[tuple[str, Any]] = []
        self.closed = False

    @property
    def is_open(self) -> bool:
        return not self.closed

    def send_audio_chunk(self, b64: str) -> None:
        self.audio.append(b64)

    def send_conversation_init(self, init: dict) -> None:
        self.messages.append(("init", init))

    def send_pong(self, event_id: int) -> None:
        self.messages.append(("pong", event_id))

    def send_contextual_update(self, text: str) -> None:
        self.messages.append(("context", text))

    def send_user_message(self, text: str) -> None:
        self.messages.append(("user_message", text))

    def send_client_tool_result(self, tool_call_id: str, result: str, is_error: bool) -> None:
        self.messages.append(("tool_result", (tool_call_id, result, is_error)))

    async def attach_image(self, data: bytes, mime: str, question: str) -> None:
        self.messages.append(("attach_image", (mime, question)))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_worker() -> FakeWorkerPort:
    return FakeWorkerPort()


@pytest.fixture
def fake_agent() -> FakeAgentPort:
    return FakeAgentPort()


async def settle() -> None:
    """Let pending callbacks/tasks run."""
    for _ in range(5):
        await asyncio.sleep(0)
