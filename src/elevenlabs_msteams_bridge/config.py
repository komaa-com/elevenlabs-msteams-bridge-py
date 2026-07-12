"""Bridge configuration, entirely from environment variables.

The worker-side contract (HMAC secret, wire protocol) must match the StandIn
media bridge; the ElevenLabs side needs an API key and agent id. Environment
variable names are identical to the Node package (@komaa/elevenlabs-msteams-bridge),
so the two are drop-in interchangeable behind the same .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_GOODBYE = "I'm sorry, we've reached the time limit for this call. Thank you for calling, goodbye!"


@dataclass(frozen=True)
class BridgeConfig:
    port: int
    """TCP port the bridge listens on for worker WebSocket upgrades."""
    host: str
    """Bind address."""
    worker_shared_secret: str
    """Must equal the shared secret the StandIn media bridge signs with (HMAC upgrade check)."""
    elevenlabs_api_key: str
    """Server-side ElevenLabs key; mints signed URLs, uploads files, calls TTS."""
    elevenlabs_agent_id: str
    """Default agent id."""
    el_host: str
    """ElevenLabs API host. Regional pins: api.us / api.eu.residency / api.in.residency / api.sg.residency."""
    el_environment: str | None
    """Optional environment passed to get-signed-url and conversation_initiation_client_data."""
    el_first_message: str | None
    """Optional localized greeting / spoken disclosure sent as a first_message override
    (must be allowlisted in the agent's security settings)."""
    el_agent_branch_id: str | None
    """Optional agent branch id pinned per deployment."""
    el_tts_voice_id: str | None
    """Voice id for the deterministic governor goodbye via standalone TTS.
    None = fall back to user_message injection."""
    el_tts_model_id: str
    """TTS model for the goodbye line."""
    vision_api_url: str | None
    """Vision path 2: OpenAI-compatible chat-completions URL for describe-then-inject. None = disabled."""
    vision_api_key: str | None
    """Bearer key for the vision endpoint (optional - local endpoints may not need one)."""
    vision_model: str | None
    """Vision model name (required when vision_api_url is set)."""
    max_call_minutes: float
    """Bridge-side call governor: hard cap on call duration in minutes (fractional allowed).
    0 = disabled. ElevenLabs doesn't know about your billing; on limit the bridge
    speaks a goodbye and ends the call."""
    goodbye_text: str
    """Goodbye line the governor speaks (deterministic via TTS when EL_TTS_VOICE_ID is set)."""
    goodbye_grace_ms: float
    """How long to let the goodbye play out before session.end when its duration is unknown."""
    hmac_freshness_ms: float
    """Allowed clock skew for the HMAC timestamp, in ms (the worker documents +/-60s)."""
    max_connections: int
    """Max concurrent worker connections (0 = default 64)."""
    max_connections_per_ip: int
    """Max concurrent connections from one remote IP (0 = default: same as max_connections)."""
    pre_start_timeout_ms: float
    """Drop a worker that authenticates but never sends session.start after this many ms (0 = default 10s)."""
    worker_idle_timeout_ms: float
    """Dead-peer window: end the call after this many ms without ANY worker message
    (0 = default 90s; the worker heartbeats every 30s)."""
    trust_proxy: bool
    """Trust X-Forwarded-For for the per-IP cap (only behind a proxy you control)."""
    tls_cert_path: str | None
    """PEM cert path for native TLS (wss). When cert + key are both set the bridge serves
    TLS itself; otherwise it is plain WS and MUST be fronted by a TLS terminator."""
    tls_key_path: str | None
    """PEM key path for native TLS (wss)."""
    log_transcripts: bool
    """Log agent transcripts (still gated on Teams recording.status == "active")."""


def _validate_el_host(host: str) -> str:
    """ELEVENLABS_API_KEY is sent as `xi-api-key` to `https://{EL_HOST}/...`, so an
    attacker-influenced or fat-fingered EL_HOST would exfiltrate the key. Restrict
    it to ElevenLabs' own hosts (the default + the documented regional pins). Set
    EL_HOST_ALLOW_ANY=true only for a deliberate proxy/test host."""
    if os.environ.get("EL_HOST_ALLOW_ANY") == "true":
        return host
    h = host.lower()
    if h == "elevenlabs.io" or h.endswith(".elevenlabs.io"):
        return host
    raise ValueError(
        f'EL_HOST "{host}" is not an elevenlabs.io host; the API key must not be sent elsewhere. '
        "Set EL_HOST_ALLOW_ANY=true to override for a trusted proxy."
    )


def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ValueError(f"Missing required env var {name}")
    return v


def _optional(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def _num_from_env(name: str, fallback: float) -> float:
    """Parse a numeric env var, failing LOUD on a non-numeric value. float("abc")
    raising is the point: a typo must stop startup with a clear message, not
    silently disable the governor (MAX_CALL_MINUTES) or misbind (PORT).
    Negatives fail too: all these knobs are counts/durations where a negative is
    never meaningful and would silently disable checks guarded by `> 0`."""
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return fallback
    try:
        n = float(raw)
    except ValueError:
        raise ValueError(f'Env var {name}="{raw}" is not a number') from None
    if n != n or n in (float("inf"), float("-inf")):
        raise ValueError(f'Env var {name}="{raw}" is not a number')
    if n < 0:
        raise ValueError(f'Env var {name}="{raw}" must not be negative')
    return n


def load_config() -> BridgeConfig:
    return BridgeConfig(
        port=int(_num_from_env("PORT", 8080)),
        host=os.environ.get("BIND", "").strip() or "0.0.0.0",
        worker_shared_secret=_required("WORKER_SHARED_SECRET"),
        elevenlabs_api_key=_required("ELEVENLABS_API_KEY"),
        elevenlabs_agent_id=_required("ELEVENLABS_AGENT_ID"),
        el_host=_validate_el_host(os.environ.get("EL_HOST", "").strip() or "api.elevenlabs.io"),
        el_environment=_optional("EL_ENVIRONMENT"),
        el_first_message=_optional("EL_FIRST_MESSAGE"),
        el_agent_branch_id=_optional("EL_AGENT_BRANCH_ID"),
        el_tts_voice_id=_optional("EL_TTS_VOICE_ID"),
        el_tts_model_id=os.environ.get("EL_TTS_MODEL_ID") or "eleven_turbo_v2_5",
        max_call_minutes=_num_from_env("MAX_CALL_MINUTES", 0),
        goodbye_text=os.environ.get("GOODBYE_TEXT") or DEFAULT_GOODBYE,
        goodbye_grace_ms=_num_from_env("GOODBYE_GRACE_MS", 8000),
        vision_api_url=_optional("VISION_API_URL"),
        vision_api_key=_optional("VISION_API_KEY"),
        vision_model=_optional("VISION_MODEL"),
        hmac_freshness_ms=_num_from_env("HMAC_FRESHNESS_MS", 60_000),
        max_connections=int(_num_from_env("MAX_CONNECTIONS", 0)),
        max_connections_per_ip=int(_num_from_env("MAX_CONNECTIONS_PER_IP", 0)),
        pre_start_timeout_ms=_num_from_env("PRE_START_TIMEOUT_MS", 0),
        worker_idle_timeout_ms=_num_from_env("WORKER_IDLE_TIMEOUT_MS", 0),
        trust_proxy=os.environ.get("TRUST_PROXY_XFF") == "true",
        tls_cert_path=_optional("TLS_CERT_PATH"),
        tls_key_path=_optional("TLS_KEY_PATH"),
        log_transcripts=os.environ.get("LOG_TRANSCRIPTS") == "true",
    )
