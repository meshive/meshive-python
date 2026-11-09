"""Clients sharing one transport (`transport=`), and the SSL context default clients reuse."""
import asyncio
import ssl
import threading
import types

import certifi
import httpx
import pytest

from meshive import AsyncMeshive, Meshive
from meshive import _client
from tests.test_sdk import (DOWNLOAD_URLS, WHOAMI, _download_handler, isolate_config_dir, slept)  # noqa: F401


class RecordingTransport(httpx.MockTransport):
    """A mock transport that records every request and counts close calls."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.closed = 0

        def record(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json=WHOAMI)
        super().__init__(record)

    def close(self):
        self.closed += 1

    async def aclose(self):
        self.closed += 1


def _client_for(cls, key, transport, **kwargs):
    return cls(api_key=key, base_url="https://api.test", transport=transport, **kwargs)


# --- the caller owns the transport --------------------------------------------

def test_close_leaves_a_shared_transport_open():
    transport = RecordingTransport()
    first = _client_for(Meshive, "meshive_a", transport)
    first.me()
    first.close()
    with _client_for(Meshive, "meshive_b", transport) as second:
        second.me()
    assert transport.closed == 0
    _client_for(Meshive, "meshive_c", transport).me()          # still usable after both clients closed
    assert len(transport.requests) == 3


def test_async_close_leaves_a_shared_transport_open():
    transport = RecordingTransport()

    async def run():
        first = _client_for(AsyncMeshive, "meshive_a", transport)
        await first.me()
        await first.close()
        async with _client_for(AsyncMeshive, "meshive_b", transport) as second:
            await second.me()
        await _client_for(AsyncMeshive, "meshive_c", transport).me()

    asyncio.run(run())
    assert transport.closed == 0 and len(transport.requests) == 3


# --- only connections are shared ----------------------------------------------

def test_clients_sharing_a_transport_keep_their_own_key_and_headers():
    transport = RecordingTransport()
    a = _client_for(Meshive, "meshive_a", transport, headers={"X-Meshive-Client": "agent-a"})
    b = _client_for(Meshive, "meshive_b", transport, headers={"X-Meshive-Client": "agent-b"})
    a.me(); b.me(); a.me(); b.me()
    sent = [(r.headers["authorization"], r.headers["x-meshive-client"]) for r in transport.requests]
    assert sent == [("Bearer meshive_a", "agent-a"), ("Bearer meshive_b", "agent-b")] * 2


def test_async_clients_sharing_a_transport_keep_their_own_key_and_headers():
    transport = RecordingTransport()

    async def run():
        a = _client_for(AsyncMeshive, "meshive_a", transport, headers={"X-Meshive-Client": "agent-a"})
        b = _client_for(AsyncMeshive, "meshive_b", transport, headers={"X-Meshive-Client": "agent-b"})
        await asyncio.gather(a.me(), b.me(), a.me(), b.me())

    asyncio.run(run())
    sent = sorted((r.headers["authorization"], r.headers["x-meshive-client"]) for r in transport.requests)
    assert sent == [("Bearer meshive_a", "agent-a")] * 2 + [("Bearer meshive_b", "agent-b")] * 2


def test_cookies_set_for_one_client_never_reach_another():
    def handler(request):
        response = httpx.Response(200, json=WHOAMI)
        if request.headers["authorization"] == "Bearer meshive_a":
            response.headers["set-cookie"] = "session=for-a; Path=/"
        return response

    transport = RecordingTransport(handler)
    a = _client_for(Meshive, "meshive_a", transport)
    b = _client_for(Meshive, "meshive_b", transport)
    a.me(); b.me(); a.me()
    cookies = [r.headers.get("cookie") for r in transport.requests]
    assert cookies == [None, None, "session=for-a"]   # a keeps its cookie (as before), b never sees it


def test_downloads_through_a_shared_transport_carry_no_key(tmp_path):
    seen = {}
    transport = RecordingTransport(_download_handler(DOWNLOAD_URLS, seen))
    client = _client_for(Meshive, "meshive_a", transport)
    client.download_asset("asset_abc", tmp_path)
    assert (tmp_path / "model.safetensors").read_bytes() == b"hello"
    assert seen["storage_auth"] == [None, None]


def test_each_client_applies_its_own_timeout_on_a_shared_transport():
    transport = RecordingTransport()
    _client_for(Meshive, "meshive_a", transport, timeout=5.0).me()
    _client_for(Meshive, "meshive_b", transport, timeout=42.0).me()

    async def run():
        await _client_for(AsyncMeshive, "meshive_c", transport, timeout=7.0).me()

    asyncio.run(run())
    timeouts = [r.extensions["timeout"] for r in transport.requests]
    assert timeouts == [dict.fromkeys(("connect", "read", "write", "pool"), t) for t in (5.0, 42.0, 7.0)]


def test_a_shared_transport_sends_the_same_request_as_a_default_client():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=WHOAMI)

    default_client = Meshive(api_key="meshive_test", base_url="https://api.test")
    default_client._client._transport = httpx.MockTransport(handler)   # keeps the default client's own settings
    default_client.me()
    _client_for(Meshive, "meshive_test", RecordingTransport(handler)).me()
    default, shared = seen
    assert (shared.method, shared.url) == (default.method, default.url)
    assert dict(shared.headers) == dict(default.headers)
    assert shared.extensions["timeout"] == default.extensions["timeout"]


# --- default clients ------------------------------------------------------------

@pytest.fixture
def ssl_builds(monkeypatch):
    """Starts from an empty per-thread cache and records every SSL context httpx builds, whether the SDK asks for it
    or an httpx transport makes its own. A context passed in as verify= is handed back as is, so it isn't counted."""
    monkeypatch.setattr(_client, "_ssl_contexts", threading.local())
    for name in _client._SSL_ENV:
        monkeypatch.delenv(name, raising=False)
    built = []

    def counting(real):
        def build(*args, **kwargs):
            ctx = real(*args, **kwargs)
            if not isinstance(kwargs.get("verify", True), ssl.SSLContext):
                built.append(ctx)
            return ctx
        return build

    monkeypatch.setattr(httpx, "create_ssl_context", counting(httpx.create_ssl_context))
    monkeypatch.setattr(httpx._transports.default, "create_ssl_context",
                        counting(httpx._transports.default.create_ssl_context))
    return built


def test_default_clients_own_and_close_their_transport(ssl_builds):
    sync = Meshive(api_key="meshive_a", base_url="https://api.test")
    assert isinstance(sync._client._transport, httpx.HTTPTransport)
    sync.close()
    assert sync._client.is_closed

    async def run():
        client = AsyncMeshive(api_key="meshive_a", base_url="https://api.test")
        assert isinstance(client._client._transport, httpx.AsyncHTTPTransport)
        await client.close()
        return client

    assert asyncio.run(run())._client.is_closed


def test_default_clients_reuse_one_verifying_ssl_context_per_thread(ssl_builds):
    clients = [Meshive(api_key="meshive_a", base_url="https://api.test") for _ in range(3)]
    clients += [AsyncMeshive(api_key="meshive_a", base_url="https://api.test") for _ in range(2)]
    assert len(ssl_builds) == 1
    ctx = ssl_builds[0]
    assert all(c._client._transport._pool._ssl_context is ctx for c in clients)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname

    other = []
    worker = threading.Thread(target=lambda: other.append(_client._default_ssl_context()))
    worker.start()
    worker.join()
    assert len(ssl_builds) == 2 and other[0] is not ctx      # a client created on another thread gets its own


def test_default_ssl_context_follows_the_certificate_variables(ssl_builds, monkeypatch):
    before = _client._default_ssl_context()
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    changed = Meshive(api_key="meshive_a", base_url="https://api.test")
    assert len(ssl_builds) == 2 and changed._client._transport._pool._ssl_context is not before
    assert _client._default_ssl_context() is ssl_builds[1]  # and is kept again under the new value

    # truststore.inject_into_ssl() replaces ssl.SSLContext. Swapping the real one breaks the ssl module on 3.12 (its
    # setters look the name up), so the SDK's view of it is swapped instead.
    monkeypatch.setattr(_client, "ssl", types.SimpleNamespace(SSLContext=type("Injected", (), {})))
    assert _client._default_ssl_context() is ssl_builds[2] and len(ssl_builds) == 3


def test_shared_transport_builds_no_ssl_context(ssl_builds):
    Meshive(api_key="meshive_a", base_url="https://api.test", transport=RecordingTransport())
    AsyncMeshive(api_key="meshive_a", base_url="https://api.test", transport=RecordingTransport())
    assert ssl_builds == []
    httpx.Client()                                          # the fixture does see a context httpx builds itself
    assert len(ssl_builds) == 1
