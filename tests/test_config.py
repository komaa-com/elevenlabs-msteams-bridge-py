import pytest

from elevenlabs_msteams_bridge.config import load_config


REQUIRED = {
    "WORKER_SHARED_SECRET": "s",
    "ELEVENLABS_API_KEY": "k",
    "ELEVENLABS_AGENT_ID": "a",
}


def _set_required(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)


def test_defaults(monkeypatch):
    for k in list(REQUIRED) + ["PORT", "EL_HOST", "MAX_CALL_MINUTES"]:
        monkeypatch.delenv(k, raising=False)
    _set_required(monkeypatch)
    cfg = load_config()
    assert cfg.port == 8080
    assert cfg.el_host == "api.elevenlabs.io"
    assert cfg.max_call_minutes == 0
    assert cfg.el_tts_model_id == "eleven_turbo_v2_5"


def test_missing_required(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
        load_config()


def test_non_numeric_fails_loud(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MAX_CALL_MINUTES", "abc")
    with pytest.raises(ValueError, match="MAX_CALL_MINUTES"):
        load_config()


def test_negative_fails_loud(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MAX_CALL_MINUTES", "-1")
    with pytest.raises(ValueError, match="negative"):
        load_config()


def test_el_host_restricted(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("EL_HOST", "evil.example.com")
    monkeypatch.delenv("EL_HOST_ALLOW_ANY", raising=False)
    with pytest.raises(ValueError, match="elevenlabs.io"):
        load_config()


def test_el_host_allow_any_override(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("EL_HOST", "proxy.internal")
    monkeypatch.setenv("EL_HOST_ALLOW_ANY", "true")
    assert load_config().el_host == "proxy.internal"


def test_el_host_suffix_spoof_rejected(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("EL_HOST", "notelevenlabs.io")
    monkeypatch.delenv("EL_HOST_ALLOW_ANY", raising=False)
    with pytest.raises(ValueError):
        load_config()
