"""ElevenLabs Agent WebSocket client + the REST calls the bridge needs
(signed URL minting, conversation file upload, standalone TTS for the governor
goodbye).

Wire reference (validated against the live agent WebSocket spec):
client->server messages are user_audio_chunk, pong,
conversation_initiation_client_data, contextual_update, user_message,
client_tool_result, multimodal_message; server->client are
conversation_initiation_metadata, audio, interruption, ping, vad_score,
user_transcript, agent_response, client_tool_call, ...
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import quote, urlencode

import aiohttp

from .config import BridgeConfig
from .log import Logger

# Time bound on REST calls (signed URL, file upload) and the WS open, so a hung
# ElevenLabs API can't wedge the call open (the governor is only armed after connect).
EL_REST_TIMEOUT_MS = 10_000

# Hard time bound on the goodbye-TTS request so a hung endpoint can't hold the
# governor's mute/goodbye open (the call's hard teardown deadline still fires).
GOODBYE_TTS_TIMEOUT_MS = 10_000

_EXT_FOR_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def build_conversation_init(
    dynamic_variables: dict[str, str],
    first_message: str | None = None,
    environment: str | None = None,
    user_id: str | None = None,
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Build conversation_initiation_client_data.

    user_id is a stable per-person id for ElevenLabs analytics/memory. Pass the
    caller's AAD id when present; NEVER a shared default - distinct anonymous
    callers must not collide on one id (cross-caller memory bleed). Omitted when None.
    """
    msg: dict[str, Any] = {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": dynamic_variables,
    }
    # conversation_config_override fields are rejected unless allowlisted in the
    # agent's security settings - only send when configured.
    if first_message:
        msg["conversation_config_override"] = {"agent": {"first_message": first_message}}
    if environment:
        msg["environment"] = environment
    if user_id:
        msg["user_id"] = user_id
    if branch_id:
        msg["branch_id"] = branch_id
    return msg


async def get_signed_url(cfg: BridgeConfig) -> str:
    """Mint a short-lived signed URL for a private agent. Endpoint is
    get-signed-url (hyphens). Expires in ~15 min: call per session.start, never cache."""
    params = {"agent_id": cfg.elevenlabs_agent_id}
    if cfg.el_environment:
        params["environment"] = cfg.el_environment
    url = f"https://{cfg.el_host}/v1/convai/conversation/get-signed-url?{urlencode(params)}"
    timeout = aiohttp.ClientTimeout(total=EL_REST_TIMEOUT_MS / 1000)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"xi-api-key": cfg.elevenlabs_api_key}) as res:
            if res.status != 200:
                raise RuntimeError(f"get-signed-url failed: HTTP {res.status} {await res.text()}")
            body = await res.json()
    signed = body.get("signed_url")
    if not signed:
        raise RuntimeError("get-signed-url returned no signed_url")
    return signed


async def upload_conversation_file(cfg: BridgeConfig, conversation_id: str, data: bytes, mime: str) -> str:
    """Vision path 1: upload a frame to the LIVE conversation and get a file_id
    for multimodal_message. Note: this persists the frame with ElevenLabs -
    callers must gate it on Teams recording being active."""
    ext = _EXT_FOR_MIME.get(mime.lower())
    if not ext:
        raise ValueError(f"unsupported image mime for upload: {mime}")
    url = f"https://{cfg.el_host}/v1/convai/conversations/{quote(conversation_id, safe='')}/files"
    form = aiohttp.FormData()
    form.add_field("file", data, filename=f"frame.{ext}", content_type=mime)
    timeout = aiohttp.ClientTimeout(total=EL_REST_TIMEOUT_MS / 1000)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=form, headers={"xi-api-key": cfg.elevenlabs_api_key}) as res:
            if res.status != 200:
                raise RuntimeError(f"file upload failed: HTTP {res.status} {await res.text()}")
            body = await res.json()
    file_id = body.get("file_id")
    if not file_id:
        raise RuntimeError("file upload returned no file_id")
    return file_id


async def synthesize_goodbye(cfg: BridgeConfig, text: str) -> bytes:
    """Standalone TTS for the deterministic governor goodbye: synthesize the
    exact text as raw PCM16K and return the bytes."""
    if not cfg.el_tts_voice_id:
        raise RuntimeError("EL_TTS_VOICE_ID not configured")
    url = f"https://{cfg.el_host}/v1/text-to-speech/{quote(cfg.el_tts_voice_id, safe='')}?output_format=pcm_16000"
    timeout = aiohttp.ClientTimeout(total=GOODBYE_TTS_TIMEOUT_MS / 1000)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            json={"text": text, "model_id": cfg.el_tts_model_id},
            headers={"xi-api-key": cfg.elevenlabs_api_key},
        ) as res:
            if res.status != 200:
                raise RuntimeError(f"TTS failed: HTTP {res.status} {await res.text()}")
            return await res.read()


class ElSessionHandlers:
    """Callbacks the session wires into the agent socket."""

    __slots__ = ("on_message", "on_close", "on_error")

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_close: Callable[[int, str], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        self.on_message = on_message
        self.on_close = on_close
        self.on_error = on_error


class AgentPort(Protocol):
    """What the relay needs from an agent connection; ElAgentSocket is the real
    one, tests fake it."""

    conversation_id: str | None

    @property
    def is_open(self) -> bool: ...

    def send_audio_chunk(self, base64_pcm: str) -> None: ...
    def send_conversation_init(self, init: dict[str, Any]) -> None: ...
    def send_pong(self, event_id: int) -> None: ...
    def send_contextual_update(self, text: str) -> None: ...
    def send_user_message(self, text: str) -> None: ...
    def send_client_tool_result(self, tool_call_id: str, result: str, is_error: bool) -> None: ...
    async def attach_image(self, data: bytes, mime: str, question: str) -> None: ...
    def close(self) -> None: ...


# Async factory signature tests can substitute for a fake agent.
ElConnector = Callable[[BridgeConfig, Logger, ElSessionHandlers], Awaitable[AgentPort]]


class ElAgentSocket:
    """One agent conversation socket. Thin: parsing + send helpers only; relay
    logic lives in session.py."""

    def __init__(self, cfg: BridgeConfig, log: Logger) -> None:
        self._cfg = cfg
        self._log = log
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._read_task: asyncio.Task | None = None
        self.conversation_id: str | None = None

    @classmethod
    async def connect(cls, cfg: BridgeConfig, log: Logger, handlers: ElSessionHandlers) -> "ElAgentSocket":
        """Open the agent WS and wire handlers. Resolves once the socket is open.
        One retry with a fresh signed URL (refresh-on-failure): signed URLs are
        short-lived and a transient mint/connect failure should not hang up."""
        sock = cls(cfg, log)
        try:
            await sock._open_once()
        except Exception as err:
            log.warn(f"EL connect failed ({err}); retrying with a fresh signed URL")
            await sock._dispose_transport()
            await asyncio.sleep(0.25)
            try:
                await sock._open_once()
            except Exception:
                await sock._dispose_transport()
                raise
        sock._read_task = asyncio.create_task(sock._read_loop(handlers))
        return sock

    async def _open_once(self) -> None:
        signed_url = await get_signed_url(self._cfg)
        # Bound the WS open like the REST calls: a blackholed TCP connect or a
        # stalled TLS/upgrade handshake must not hang session.start forever.
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(signed_url, max_msg_size=16 * 1024 * 1024),
            timeout=EL_REST_TIMEOUT_MS / 1000,
        )

    async def _dispose_transport(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _read_loop(self, handlers: ElSessionHandlers) -> None:
        ws = self._ws
        assert ws is not None
        close_code = 1000
        close_reason = ""
        try:
            async for frame in ws:
                if frame.type == aiohttp.WSMsgType.TEXT:
                    try:
                        msg = json.loads(frame.data)
                    except ValueError:
                        self._log.warn("EL sent unparseable frame; dropping")
                        continue
                    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                        self._log.warn("EL sent non-object frame; dropping")
                        continue
                    if msg["type"] == "conversation_initiation_metadata":
                        if not self._handle_init_metadata(msg):
                            break  # fatal format mismatch; close and end the call
                    try:
                        handlers.on_message(msg)
                    except Exception as err:
                        # Never let a handler error escape the read loop - it
                        # would silently kill the relay for this call.
                        self._log.error(f"error handling EL {msg.get('type')}: {err}")
                elif frame.type == aiohttp.WSMsgType.ERROR:
                    handlers.on_error(ws.exception() or RuntimeError("EL websocket error"))
                    break
        except Exception as err:  # transport-level failure
            handlers.on_error(err if isinstance(err, Exception) else RuntimeError(str(err)))
        finally:
            close_code = ws.close_code or close_code
            await self._dispose_transport()
            handlers.on_close(close_code, close_reason)

    def _handle_init_metadata(self, msg: dict[str, Any]) -> bool:
        meta = msg.get("conversation_initiation_metadata_event") or {}
        if isinstance(meta.get("conversation_id"), str):
            self.conversation_id = meta["conversation_id"]
        # pcm_16000 both ways is the no-transcode contract; anything else is an
        # agent misconfig. Close the agent socket so the session tears the call
        # down cleanly and the operator sees one unambiguous error - log-only
        # would leave the call "up" with garbled/dead audio for its whole duration.
        out_fmt = meta.get("agent_output_audio_format")
        in_fmt = meta.get("user_input_audio_format")
        bad = (out_fmt if out_fmt and out_fmt != "pcm_16000" else None) or (
            in_fmt if in_fmt and in_fmt != "pcm_16000" else None
        )
        if bad:
            self._log.error(
                f"agent audio format is {bad}, expected pcm_16000 both ways - "
                "fix the agent's audio settings; ending the call"
            )
            return False
        return True

    @property
    def is_open(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _send(self, obj: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        # fire-and-forget like the hot path requires; errors surface on the read loop
        asyncio.ensure_future(self._send_safe(ws, json.dumps(obj)))

    @staticmethod
    async def _send_safe(ws: aiohttp.ClientWebSocketResponse, payload: str) -> None:
        try:
            await ws.send_str(payload)
        except Exception:
            pass  # socket died mid-send; the read loop reports the close

    def send_audio_chunk(self, base64_pcm: str) -> None:
        """Caller audio -> agent. Payload is base64 PCM16K, forwarded verbatim
        (no "type" field on this one)."""
        self._send({"user_audio_chunk": base64_pcm})

    def send_conversation_init(self, init: dict[str, Any]) -> None:
        self._send(init)

    def send_pong(self, event_id: int) -> None:
        self._send({"type": "pong", "event_id": event_id})

    def send_contextual_update(self, text: str) -> None:
        """Non-interrupting background context (participants, dtmf, vision descriptions)."""
        self._send({"type": "contextual_update", "text": text})

    def send_user_message(self, text: str) -> None:
        """Interrupting user-turn text (governor-goodbye fallback path)."""
        self._send({"type": "user_message", "text": text})

    def send_client_tool_result(self, tool_call_id: str, result: str, is_error: bool) -> None:
        self._send({"type": "client_tool_result", "tool_call_id": tool_call_id, "result": result, "is_error": is_error})

    async def attach_image(self, data: bytes, mime: str, question: str) -> None:
        """Vision path 1: upload the frame to the live conversation, then inject
        it as a multimodal user turn."""
        if not self.conversation_id:
            raise RuntimeError("no conversation_id yet (conversation_initiation_metadata not received)")
        file_id = await upload_conversation_file(self._cfg, self.conversation_id, data, mime)
        self._send(
            {
                "type": "multimodal_message",
                "text": {"type": "user_message", "text": question},
                "file": {"type": "file_input", "file_id": file_id},
            }
        )

    def close(self) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            asyncio.ensure_future(self._close_async())

    async def _close_async(self) -> None:
        try:
            if self._ws is not None:
                await self._ws.close(code=1000, message=b"session-end")
        except Exception:
            pass
