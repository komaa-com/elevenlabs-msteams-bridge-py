"""Integration tests for the guarded fetch path (resolver + caps + redirect
policy) against a local HTTP server. is_forbidden_ip is patched to admit
loopback so the guarded connect path itself can be exercised."""

import socket

import pytest
from aiohttp import web

from elevenlabs_msteams_bridge import ssrf
from elevenlabs_msteams_bridge.ssrf import _GuardedResolver, fetch_public_image

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _lookup_dual(host):
    return ["93.184.216.34", "2606:4700::1111"]


async def test_resolver_honors_requested_family():
    r = _GuardedResolver(_lookup_dual)
    v4 = await r.resolve("h.example", 443, socket.AF_INET)
    assert [e["host"] for e in v4] == ["93.184.216.34"]
    assert all(e["family"] == socket.AF_INET for e in v4)
    v6 = await r.resolve("h.example", 443, socket.AF_INET6)
    assert [e["host"] for e in v6] == ["2606:4700::1111"]
    assert all(e["family"] == socket.AF_INET6 for e in v6)
    both = await r.resolve("h.example", 443, socket.AF_UNSPEC)
    assert len(both) == 2


async def test_resolver_raises_when_family_has_no_addresses():
    async def v4_only(host):
        return ["93.184.216.34"]

    r = _GuardedResolver(v4_only)
    with pytest.raises(OSError, match="requested address family"):
        await r.resolve("h.example", 443, socket.AF_INET6)


async def test_resolver_blocks_private_answer():
    async def rebind(host):
        return ["93.184.216.34", "10.0.0.5"]

    r = _GuardedResolver(rebind)
    with pytest.raises(OSError, match="rebind"):
        await r.resolve("h.example", 443, socket.AF_UNSPEC)


@pytest.fixture
async def image_server():
    async def img(request):
        return web.Response(body=PNG_BYTES, content_type="image/png")

    async def big(request):
        return web.Response(body=b"\x00" * (512 * 1024), content_type="image/png")

    async def redirect(request):
        raise web.HTTPFound("http://example.com/elsewhere")

    app = web.Application()
    app.router.add_get("/img", img)
    app.router.add_get("/big", big)
    app.router.add_get("/redir", redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield port
    await runner.cleanup()


@pytest.fixture
def allow_loopback(monkeypatch):
    # admit 127.0.0.1 through the guard so the connect path can be exercised
    monkeypatch.setattr(ssrf, "is_forbidden_ip", lambda ip: False)


async def _loopback(host):
    return ["127.0.0.1"]


async def test_fetch_public_image_roundtrip(image_server, allow_loopback):
    data, mime = await fetch_public_image(f"http://localhost:{image_server}/img", 1024 * 1024, lookup=_loopback)
    assert data == PNG_BYTES
    assert mime == "image/png"


async def test_fetch_rejects_oversized_body(image_server, allow_loopback):
    with pytest.raises(ValueError, match="too large|exceeded"):
        await fetch_public_image(f"http://localhost:{image_server}/big", 1024, lookup=_loopback)


async def test_fetch_rejects_redirects(image_server, allow_loopback):
    with pytest.raises(ValueError, match="HTTP 302"):
        await fetch_public_image(f"http://localhost:{image_server}/redir", 1024 * 1024, lookup=_loopback)
