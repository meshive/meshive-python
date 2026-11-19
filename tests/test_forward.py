"""`meshive port-forward`: the local forwarder, tested over real sockets against a stand-in for a pod port's public
address (TLS with a test certificate, the lock's key check, and a small app behind it), and the command."""
import asyncio
import base64
import contextlib
import errno
import functools
import hashlib
import importlib
import json
import os
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from meshive.cli import _forward as forward_cli
from meshive._client import _build_ssl_context
from meshive.cli._forward import Forwarder
from tests.test_sdk import isolate_config_dir, sync_client  # noqa: F401  (autouse fixture)

cli = importlib.import_module("meshive.cli.main")

DATA = Path(__file__).parent / "data"
HOST = "0123456789abcdef.test-cluster.meshive.test"       # the name the test certificate is issued for
KEY = "mshv_" + "Zq7" * 14 + "x"                           # "mshv_" + 43 characters
OTHER_KEY = "mshv_" + "Ab3" * 14 + "y"
KEY_COMMAND = "meshive port-key-create ws p-0 8188 --name NAME"


@pytest.fixture(autouse=True)
def no_key_in_the_environment(monkeypatch):
    monkeypatch.delenv(forward_cli.ENV_ACCESS_KEY, raising=False)


def scenario(test):
    """Runs an async test on a fresh event loop (the platform's default: the proactor loop on Windows)."""
    @functools.wraps(test)
    def run(*args, **kwargs):
        async def whole():
            try:
                await test(*args, **kwargs)
            finally:                    # no connection is left for the garbage collector to close after the loop
                while Raw.opened:
                    Raw.opened.pop().close()
                for _ in range(5):
                    await asyncio.sleep(0)

        tls_is_inspected()      # found out once, before any loop runs
        asyncio.run(asyncio.wait_for(whole(), 90))
    return run


# =================================================================================================
# The stand-in endpoint
# =================================================================================================

def frame(opcode, payload, *, mask=None):
    """One WebSocket frame (a client's frames are masked, a server's are not)."""
    head = bytes([0x80 | opcode])
    size = len(payload)
    bit = 0x80 if mask else 0
    if size < 126:
        head += bytes([bit | size])
    elif size < 65536:
        head += bytes([bit | 126]) + struct.pack("!H", size)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", size)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        head += mask
    return head + payload


async def read_frame(reader):
    first, second = await reader.readexactly(2)
    size = second & 0x7F
    if size == 126:
        size, = struct.unpack("!H", await reader.readexactly(2))
    elif size == 127:
        size, = struct.unpack("!Q", await reader.readexactly(8))
    mask = await reader.readexactly(4) if second & 0x80 else None
    payload = await reader.readexactly(size)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return first & 0x0F, payload


def _answer(status, headers=(), body=b"", *, length=True, version="HTTP/1.1"):
    lines = [f"{version} {status} Answer"] + [f"{name}: {value}" for name, value in headers]
    if length:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class Endpoint:
    """What a locked port's public address does, as far as the forwarder can tell: it speaks HTTP/1.1 over TLS,
    turns a request without the key away with 401 before reading its body, and passes the others on to the app
    without the key."""

    def __init__(self, *, locked=True, tls=True):
        self.locked = locked
        self.tls = tls
        self.arrivals = []      # (method, target) of every request that reached the endpoint
        self.keys = []          # the access keys each of them carried
        self.requests = []      # what the app behind the lock saw: method, target, headers, body
        self.opened = 0         # connections accepted so far
        self.open = set()       # connections open now
        self.drop_once = set()  # targets answered, once, by closing the connection
        self.drop_reused_once = False   # the next request on a connection that has served one: close instead
        self.port = 0
        self._server = None

    async def start(self):
        context = None
        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(DATA / "forward_endpoint.pem")
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=context, limit=1 << 20)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self):
        self._server.close()
        self.close_connections()

    def close_connections(self):
        """What a server does to connections that sat idle for too long."""
        for writer in list(self.open):
            writer.close()

    def seen(self, name):
        """The values of a header in the last request the app saw."""
        return [value for key, value in self.requests[-1]["headers"] if key.lower() == name.lower()]

    async def _serve(self, reader, writer):
        self.opened += 1
        self.open.add(writer)
        try:
            served = 0
            while await self._one(reader, writer, served):
                served += 1
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.open.discard(writer)
            writer.close()

    async def _body(self, reader, headers):
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body, self.last_chunk_lines = b"", []
            while True:
                line = await reader.readuntil(b"\r\n")
                self.last_chunk_lines.append(line)
                size = int(line.strip(), 16)        # a chunk extension or stray text fails here
                if size == 0:
                    assert await reader.readuntil(b"\r\n") == b"\r\n"       # no trailers
                    return body
                body += await reader.readexactly(size)
                assert await reader.readexactly(2) == b"\r\n"
        return await reader.readexactly(int(headers.get("content-length", "0")))

    async def _close_unread(self, reader, writer, then="linger"):
        """Answered without reading the body: the connection is closed on it, as servers do, a moment later (so
        that the answer is not lost to a reset). Real servers wait about as long (Go 0.5 s after half-closing,
        nginx up to 5 s between reads); the wait ends as soon as the forwarder closes its side. A shorter wait
        made tests fail on a busy computer: the reset could reach the forwarder before it had read the answer's
        body, and an answer that loses its body is (rightly) broken off rather than passed on.

        `then` is what the server does after answering: "linger" reads and drops what still comes, as above;
        "close" reads nothing more and closes a moment later; "keep" leaves the connection open without reading
        for a while, then reads and drops what comes until the other side closes."""
        await writer.drain()
        if then == "close":
            await asyncio.sleep(1.0)
            return False
        if then == "keep":
            await asyncio.sleep(1.5)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(), 2.0 if then == "linger" else 10.0)
        return False

    async def _one(self, reader, writer, served):
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw[:-4].decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ")
        pairs = [(name, value.strip()) for name, value in (line.split(":", 1) for line in lines[1:])]
        headers = {name.lower(): value for name, value in pairs}
        path = target.split("?")[0]
        self.arrivals.append((method, target))
        keys = [value for name, value in pairs if name.lower() == "x-meshive-key"]
        keys += [value[7:] for name, value in pairs if name.lower() == "authorization" and value[7:12] == "mshv_"]
        self.keys.append(keys)
        if self.drop_reused_once and served:
            self.drop_reused_once = False
            return False
        if target in self.drop_once:
            self.drop_once.discard(target)
            return False
        has_body = "transfer-encoding" in headers or int(headers.get("content-length", "0")) > 0

        if self.locked and KEY not in keys:
            writer.write(_answer(401, [("X-Meshive-Lock-Denied", "1"), ("Cache-Control", "no-store"),
                                       ("Content-Type", "application/json")], b'{"error":"unauthorized"}'))
            return not has_body or await self._close_unread(reader, writer)
        if self.locked:             # the app never sees the key
            pairs = [(name, value) for name, value in pairs if name.lower() != "x-meshive-key"
                     and not (name.lower() == "authorization" and "mshv_" in value)]
        if path == "/early":        # an app that answers before it has read the body
            then = target.partition("then=")[2] or "linger"
            writer.write(_answer(413, [] if then == "keep" else [("Connection", "close")], b"too large"))
            return await self._close_unread(reader, writer, then)
        if "100-continue" in headers.get("expect", "").lower():
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        body = await self._body(reader, headers)
        self.requests.append({"method": method, "target": target, "version": version, "headers": pairs,
                              "body": body})

        if path == "/ws" and "upgrade" in headers.get("connection", "").lower():
            accept = base64.b64encode(hashlib.sha1(
                (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            while True:                                     # an echo server
                opcode, payload = await read_frame(reader)
                writer.write(frame(opcode, payload))
                await writer.drain()
                if opcode == 8:                             # close
                    return False
        if path == "/sse":
            writer.write(_answer(200, [("Content-Type", "text/event-stream"), ("Transfer-Encoding", "chunked")],
                                 length=False))
            for number in range(4):
                event = b"data: %d\n\n" % number
                writer.write(b"%x\r\n%s\r\n" % (len(event), event))
                await writer.drain()
                await asyncio.sleep(0.15)
            writer.write(b"0\r\n\r\n")
        elif path == "/range":
            first, last = (int(n) for n in headers["range"].split("=")[1].split("-"))
            content = bytes(range(256)) * 4
            writer.write(_answer(206, [("Content-Range", f"bytes {first}-{last}/{len(content)}")],
                                 content[first:last + 1]))
        elif path == "/redirect":
            writer.write(_answer(302, [
                ("Location", f"https://{HOST}/next?x=1"),
                ("Set-Cookie", f"sid=abc; Domain={HOST}; Path=/; Secure; HttpOnly; SameSite=None"),
                ("Set-Cookie", "__Host-tok=1; Path=/; Secure; SameSite=None")]))
        elif path == "/elsewhere":
            writer.write(_answer(302, [("Location", f"https://{HOST}.example.com/x")]))
        elif path == "/until-close":
            writer.write(_answer(200, body=b"runs until the connection closes", length=False))
            return False
        elif path == "/says-close":         # with ?slowly, the connection is closed only when the other side goes
            writer.write(_answer(200, [("Connection", "close")], b"last one"))
            return "slowly" in target and await reader.read(1) != b""
        elif path == "/old-server":
            writer.write(_answer(200, body=b"from an old server", version="HTTP/1.0"))
            return "slowly" in target and await reader.read(1) != b""
        elif path == "/truncated":
            writer.write(_answer(200, [("Content-Length", "100")], b"only this", length=False))
            return False
        elif path == "/folded":
            writer.write(b"HTTP/1.1 200 OK\r\nX-One: a\r\n folded\r\nContent-Length: 0\r\n\r\n")
        elif path == "/two-lengths":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nabc")
        elif path == "/hints":
            writer.write(b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n")
            writer.write(_answer(200, body=b"after the hints"))
        elif path == "/trailers":
            writer.write(_answer(200, [("Transfer-Encoding", "chunked"), ("Trailer", "X-Sum")], length=False))
            writer.write(b"5;name=value\r\nhello\r\n6\r\n world\r\n0\r\nX-Sum: 1\r\n\r\n")
        elif path == "/download":
            size = int(target.split("n=")[1])
            writer.write(_answer(200, [("Content-Length", str(size))], length=False))
            block = bytes(range(256)) * 256
            for start in range(0, size, len(block)):
                writer.write(block[:min(len(block), size - start)])
                await writer.drain()
        elif path == "/no-content":
            writer.write(_answer(204, length=False))
        elif path == "/app-401":
            writer.write(_answer(401, [("WWW-Authenticate", "Basic")], b"sign in"))
        elif path == "/says-too-much":      # an answer, and then one more that nobody asked for
            writer.write(_answer(200, body=b"asked for") + _answer(200, body=b"not asked for"))
        elif path == "/slow":               # an app that takes its time (the test never waits it out)
            return await reader.read(1) != b""      # ... until the other side leaves
        elif path == "/silent-stream":      # a stream that has nothing to say yet
            writer.write(_answer(200, [("Transfer-Encoding", "chunked")], length=False))
            await writer.drain()
            return await reader.read(1) != b""
        elif path == "/until-reset":        # an answer that runs until the connection closes, and breaks off
            writer.write(_answer(200, body=b"the first part", length=False))
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0)
                                                       if sys.platform == "win32" else struct.pack("ii", 1, 0))
            writer.transport.abort()
            return False
        elif path == "/hop":
            writer.write(_answer(200, [("Connection", "X-Internal"), ("X-Internal", "1"), ("Keep-Alive", "timeout=5"),
                                       ("Proxy-Authenticate", "Basic"), ("X-Kept", "1")], b"ok"))
        else:
            seen = json.dumps({"method": method, "target": target, "headers": pairs, "length": len(body),
                               "sha256": hashlib.sha256(body).hexdigest()}).encode()
            head = [("Content-Type", "application/json")]
            if method == "HEAD":
                writer.write(_answer(200, head + [("Content-Length", str(len(seen)))], length=False))
            else:
                writer.write(_answer(200, head, seen))
        return True


class EndingEndpoint:
    """A server that turns every request away at once, without reading its body, ends the session from its side
    (a TLS close_notify, or a half close without TLS), and then goes on reading and dropping whatever the client
    still sends until the client closes. A front server that refuses an upload may do just that. It runs in a
    thread with blocking sockets, so that it behaves the same on every event loop."""

    def __init__(self, *, tls=True):
        self.tls = tls
        self.taken = []         # bytes read and dropped after the session ended, per connection (None: reset)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = False
        self._context = None
        if tls:
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(DATA / "forward_endpoint.pem")
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        self._thread.join(5)
        self._listener.close()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        raw = conn
        try:
            conn.settimeout(20)
            sock = self._context.wrap_socket(conn, server_side=True) if self.tls else conn
            raw = sock
            head = b""
            while b"\r\n\r\n" not in head:
                data = sock.recv(65536)
                if not data:
                    raise ConnectionError("no request")
                head += data
            time.sleep(0.5)     # deciding takes a moment, while the body piles up in the buffers on the way
            sock.sendall(_answer(401, [("X-Meshive-Lock-Denied", "1"), ("Content-Type", "application/json")],
                                 b'{"error":"unauthorized"}'))
            if self.tls:
                sock.setblocking(False)
                with contextlib.suppress(ssl.SSLError):
                    # Sends close_notify. Waiting for the client's own then fails (the client is still sending)
                    # or would block: either way the session is over from here.
                    sock.unwrap()
                raw = socket.socket(fileno=sock.detach())
                raw.setblocking(True)
                raw.settimeout(20)
            else:
                sock.shutdown(socket.SHUT_WR)
            taken = 0
            while data := raw.recv(64 * 1024):    # at about the pace of a network, not of a loopback
                taken += len(data)
                time.sleep(0.002)
            self.taken.append(taken)
        except OSError:
            self.taken.append(None)
        finally:
            raw.close()


def _trusting_the_test_ca():
    return ssl.create_default_context(cafile=str(DATA / "forward_ca.pem"))


@functools.lru_cache(maxsize=None)
def tls_is_inspected():
    """True when something on this computer answers TLS connections in the endpoint's place, even on 127.0.0.1.
    An antivirus that inspects HTTPS does: the client is shown a certificate the antivirus made up, and the
    connection goes nowhere, because the antivirus doesn't trust the test certificate either. The forwarder
    refuses such a connection, which is right, but no test could reach the stand-in endpoint. On such a computer
    the stand-in is reached without TLS (see PlainForwarder), so that everything but TLS is still tested there,
    and the tests about the certificate are skipped."""
    async def shown():
        endpoint = Endpoint()
        await endpoint.start()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", endpoint.port, ssl=context, server_hostname=HOST)
            certificate = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            writer.close()
            return certificate
        except OSError:
            return None
        finally:
            await endpoint.close()

    text = (DATA / "forward_endpoint.pem").read_text()
    end = "-----END CERTIFICATE-----"
    own = ssl.PEM_cert_to_DER_cert(text[text.index("-----BEGIN CERTIFICATE-----"):text.index(end) + len(end)])
    return asyncio.run(asyncio.wait_for(shown(), 30)) != own


def needs_untouched_traffic(test):
    @functools.wraps(test)
    def run(*args, **kwargs):
        if tls_is_inspected():
            pytest.skip("traffic on this computer is inspected (an antivirus answers in the endpoint's place)")
        return test(*args, **kwargs)
    return run


class PlainForwarder(Forwarder):
    """Only for a computer whose TLS is inspected: reaches the stand-in endpoint without TLS. The command never
    makes one of these."""

    async def _open(self):
        return await asyncio.open_connection(*self._connect_to, limit=1 << 18)


@contextlib.asynccontextmanager
async def forwarding(*, locked=True, key=KEY, send_key=True, host=HOST, trust=True, allow_origins=(), **settings):
    """An endpoint and a forwarder in front of it. `settings` override the forwarder's time limits."""
    endpoint = Endpoint(locked=locked, tls=not tls_is_inspected())
    await endpoint.start()
    said = []
    make = PlainForwarder if tls_is_inspected() else Forwarder
    forwarder = make(host, listen_port=0, key=key, send_key=send_key, allow_origins=allow_origins,
                     connect_to=("127.0.0.1", endpoint.port),
                     ssl_context=_trusting_the_test_ca() if trust else None,
                     verbose=True, log=said.append, key_command=KEY_COMMAND, port_label="Port 8188 of pod p-0")
    for name, value in settings.items():
        assert hasattr(forwarder, name)
        setattr(forwarder, name, value)
    await forwarder.start()
    try:
        yield forwarder, endpoint, said
    finally:
        await forwarder.close()
        await endpoint.close()


def web(forwarder, **options):
    """A real HTTP client pointed at the forwarder, as an app that only takes an address would be."""
    return httpx.AsyncClient(base_url=forwarder.local_url, trust_env=False, timeout=30, **options)


class Answer:
    def __init__(self, status, headers):
        self.status, self.headers, self.body, self.chunks = status, headers, b"", None

    def all(self, name):
        return [value for key, value in self.headers if key.lower() == name.lower()]

    def get(self, name):
        return (self.all(name) or [None])[0]

    def json(self):
        return json.loads(self.body)


class Raw:
    """A client that sends exactly the bytes it is given: for requests no well-behaved client would send."""

    opened = []

    def __init__(self, reader, writer, port):
        self.reader, self.writer, self.port = reader, writer, port

    @classmethod
    async def connect(cls, forwarder):
        reader, writer = await asyncio.open_connection("127.0.0.1", forwarder.listen_port, limit=1 << 22)
        client = cls(reader, writer, forwarder.listen_port)
        cls.opened.append(client)
        return client

    def close(self):
        self.writer.close()

    def send(self, data):
        self.writer.write(data if isinstance(data, bytes) else data.encode("latin-1"))

    def request(self, method, target, headers=(), body=b"", *, host=None):
        lines = [f"{method} {target} HTTP/1.1", f"Host: {host or f'127.0.0.1:{self.port}'}"]
        lines += [f"{name}: {value}" for name, value in headers]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        self.send("\r\n".join(lines) + "\r\n\r\n")
        self.send(body)

    async def answer(self, *, head=False):
        raw = await self.reader.readuntil(b"\r\n\r\n")
        lines = raw[:-4].decode("latin-1").split("\r\n")
        answer = Answer(int(lines[0].split(" ")[1]),
                        [(name, value.strip()) for name, value in (line.split(":", 1) for line in lines[1:])])
        if head or answer.status < 200 or answer.status in (204, 304):
            return answer
        if answer.get("transfer-encoding") == "chunked":
            answer.chunks = []
            while True:
                line = await self.reader.readuntil(b"\r\n")
                size = int(line.strip(), 16)
                if size == 0:
                    assert await self.reader.readuntil(b"\r\n") == b"\r\n"
                    break
                answer.chunks.append(line)
                answer.body += await self.reader.readexactly(size)
                assert await self.reader.readexactly(2) == b"\r\n"
        elif answer.get("content-length") is not None:
            answer.body = await self.reader.readexactly(int(answer.get("content-length")))
        else:
            answer.body = await self.reader.read()
        return answer

    async def closed(self, within=5.0):
        """True when the forwarder closes the connection (with nothing more on it) within the time."""
        try:
            return await asyncio.wait_for(self.reader.read(), within) == b""
        except (ConnectionError, OSError):
            return True


async def ask(forwarder, method, target, headers=(), body=b"", **options):
    client = await Raw.connect(forwarder)
    try:
        client.request(method, target, headers, body, **options)
        return await client.answer(head=method == "HEAD")
    finally:
        client.close()


async def until(condition, within=5.0):
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


# =================================================================================================
# What the prototype's functional checks covered: clients that can't send the key themselves
# =================================================================================================

@scenario
async def test_a_request_without_any_credentials_reaches_the_app():
    async with forwarding() as (forwarder, endpoint, _):
        async with web(forwarder) as client:
            answer = await client.get("/echo?a=1")
        assert answer.status_code == 200 and answer.json()["target"] == "/echo?a=1"
        assert endpoint.seen("host") == [HOST]              # the app sees its own address, not 127.0.0.1
        assert endpoint.keys == [[KEY]]                     # the lock was shown the key, once
        assert "mshv_" not in json.dumps(endpoint.requests[-1]["headers"])      # and the app was not
        assert endpoint.requests[-1]["version"] == "HTTP/1.1"


@scenario
async def test_the_clients_own_authorization_and_cookies_pass_unchanged():
    async with forwarding() as (forwarder, endpoint, _):
        async with web(forwarder) as client:
            await client.get("/echo", headers={"Authorization": "Bearer app-token", "Cookie": "a=1; b=2"})
        assert endpoint.seen("authorization") == ["Bearer app-token"]
        assert endpoint.seen("cookie") == ["a=1; b=2"]
        assert endpoint.keys == [[KEY]]


@scenario
async def test_a_post_with_a_body():
    async with forwarding() as (forwarder, endpoint, _):
        async with web(forwarder) as client:
            answer = await client.post("/echo", content=b"x" * 5000)
        assert answer.json()["sha256"] == hashlib.sha256(b"x" * 5000).hexdigest()
        assert endpoint.seen("content-length") == ["5000"] and endpoint.seen("transfer-encoding") == []


@scenario
async def test_an_api_client_with_its_own_key_for_the_app():
    """An OpenAI-style client: base URL = the forwarder, its API key in Authorization. The app's key comes from
    the client, the access key from the forwarder, and both arrive."""
    async with forwarding() as (forwarder, endpoint, _):
        async with web(forwarder, headers={"Authorization": "Bearer sk-app"}) as client:
            answer = await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
        assert answer.status_code == 200 and answer.json()["method"] == "POST"
        assert json.loads(endpoint.requests[-1]["body"]) == {"model": "m", "messages": []}
        assert endpoint.seen("authorization") == ["Bearer sk-app"] and endpoint.keys == [[KEY]]


@scenario
async def test_a_websocket_upgrade_becomes_a_two_way_pipe():
    async with forwarding() as (forwarder, endpoint, said):
        client = await Raw.connect(forwarder)
        client.request("GET", "/ws", [("Connection", "keep-alive, Upgrade"), ("Upgrade", "websocket"),
                                      ("Origin", forwarder.local_url),
                                      ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="),
                                      ("Sec-WebSocket-Version", "13")])
        answer = await client.answer()
        assert answer.status == 101 and answer.get("upgrade") == "websocket"
        assert answer.get("connection").lower() == "upgrade"
        assert answer.get("sec-websocket-accept") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        # The upgrade request carries the key too, and the page's origin as the endpoint's (apps compare the two).
        assert endpoint.keys == [[KEY]] and endpoint.seen("origin") == [f"https://{HOST}"]
        assert endpoint.seen("upgrade") == ["websocket"] and endpoint.seen("connection") == ["Upgrade"]
        assert endpoint.seen("sec-websocket-key") == ["dGhlIHNhbXBsZSBub25jZQ=="]
        # Text, a 300 KB binary frame, a ping, and several frames sent at once.
        for opcode, payload in ((1, b"small"), (2, os.urandom(300_000)), (9, b"ping"), (1, b"")):
            client.send(frame(opcode, payload, mask=os.urandom(4)))
            assert await read_frame(client.reader) == (opcode, payload)
        client.send(b"".join(frame(1, b"%d" % number, mask=os.urandom(4)) for number in range(50)))
        assert [await read_frame(client.reader) for _ in range(50)] == [(1, b"%d" % n) for n in range(50)]
        client.send(frame(8, b"", mask=os.urandom(4)))      # close
        assert await read_frame(client.reader) == (8, b"")
        assert await client.closed()                        # the endpoint closed: so does the listener
        await until(lambda: not endpoint.open and not forwarder._tasks)
        assert any("/ws -> 101" in line for line in said)


@scenario
async def test_closing_an_upgraded_connection_closes_the_other_side():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/ws", [("Connection", "Upgrade"), ("Upgrade", "websocket"),
                                      ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")])
        assert (await client.answer()).status == 101
        client.send(frame(1, b"hello", mask=os.urandom(4)))
        assert await read_frame(client.reader) == (1, b"hello")
        client.close()                                      # no close frame: the client just goes
        await until(lambda: not endpoint.open and not forwarder._tasks)


@scenario
async def test_a_streamed_answer_is_not_held_back():
    async with forwarding() as (forwarder, _, _said):
        arrived = []
        async with web(forwarder) as client:
            async with client.stream("GET", "/sse") as answer:
                assert answer.headers["transfer-encoding"] == "chunked"
                async for piece in answer.aiter_raw():
                    arrived.append((time.monotonic(), piece))
        assert b"".join(piece for _, piece in arrived) == b"".join(b"data: %d\n\n" % n for n in range(4))
        # Events arrive as they are sent, 0.15 s apart, not together at the end.
        assert arrived[-1][0] - arrived[0][0] > 0.3


@scenario
async def test_a_chunked_upload():
    async with forwarding() as (forwarder, endpoint, _):
        pieces = [os.urandom(50_000) for _ in range(20)]       # 20 pieces, 1,000,000 bytes

        async def body():
            for piece in pieces:
                yield piece

        async with web(forwarder) as client:
            answer = await client.post("/echo", content=body())
        assert answer.json() == {**answer.json(), "length": 1_000_000,
                                 "sha256": hashlib.sha256(b"".join(pieces)).hexdigest()}
        assert endpoint.seen("transfer-encoding") == ["chunked"] and endpoint.seen("content-length") == []


@scenario
async def test_expect_100_continue():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(f"PUT /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: 11\r\n"
                    "Expect: 100-continue\r\n\r\n")
        assert (await client.answer()).status == 100       # the go-ahead comes before any of the body is sent
        client.send(b"hello world")
        answer = await client.answer()
        assert answer.status == 200 and answer.json()["length"] == 11
        assert endpoint.seen("expect") == ["100-continue"]
        client.request("GET", "/echo")                      # and the connection carries on
        assert (await client.answer()).status == 200
        client.close()


@scenario
async def test_a_range_request():
    async with forwarding() as (forwarder, _, _said):
        async with web(forwarder) as client:
            answer = await client.get("/range", headers={"Range": "bytes=100-199"})
        assert answer.status_code == 206 and answer.headers["content-range"] == "bytes 100-199/1024"
        assert answer.content == (bytes(range(256)) * 4)[100:200]


@scenario
async def test_a_head_request():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("HEAD", "/echo")
        answer = await client.answer(head=True)
        assert answer.status == 200 and int(answer.get("content-length")) > 0
        client.request("GET", "/echo")                      # no body followed the HEAD answer
        assert (await client.answer()).json()["method"] == "GET"
        assert [method for method, _ in endpoint.arrivals] == ["HEAD", "GET"] and endpoint.opened == 1
        client.close()


@scenario
async def test_sixty_requests_at_once():
    async with forwarding() as (forwarder, endpoint, _):
        async with web(forwarder, limits=httpx.Limits(max_connections=60)) as client:
            answers = await asyncio.gather(*(client.get(f"/echo?n={n}") for n in range(60)))
        assert [a.status_code for a in answers] == [200] * 60
        assert sorted(a.json()["target"] for a in answers) == sorted(f"/echo?n={n}" for n in range(60))
        assert len(endpoint.requests) == 60


@needs_untouched_traffic
@scenario
async def test_the_certificate_must_be_the_endpoints():
    """The connection goes to the same server, but the forwarder was told another host name: the certificate
    doesn't match, and nothing is sent."""
    async with forwarding(host="ffffffffffffffff.test-cluster.meshive.test") as (forwarder, endpoint, said):
        answer = await ask(forwarder, "GET", "/echo")
        assert answer.status == 502 and b"certificate" in answer.body
        assert endpoint.arrivals == [] and KEY.encode() not in answer.body
        assert sum("certificate" in line for line in said if "->" not in line) == 1
        await ask(forwarder, "GET", "/echo")
        assert sum("certificate" in line for line in said if "->" not in line) == 1     # said once


@needs_untouched_traffic
@scenario
async def test_the_certificate_must_come_from_a_trusted_authority():
    async with forwarding(trust=False) as (forwarder, endpoint, _):     # the system's authorities, not the test's
        answer = await ask(forwarder, "GET", "/echo")
        assert answer.status == 502 and endpoint.arrivals == []


@needs_untouched_traffic
@scenario
async def test_the_default_context_checks_the_name_and_trusts_a_root_added_to_it(monkeypatch):
    """The context the forwarder and the SDK build when given none, against the stand-in endpoint. A root loaded
    from a file is trusted next to the system's own (the certifi bundle is added this way), the name is checked
    all the same, and without that root the endpoint is refused.

    macOS refuses the test certificate whatever signed it (it is valid for a century, and macOS accepts no server
    certificate valid for more than 825 days), so there only the refusal is checked."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    endpoint = Endpoint()
    await endpoint.start()

    async def handshake(context, name):
        _, writer = await asyncio.open_connection("127.0.0.1", endpoint.port, ssl=context, server_hostname=name)
        writer.close()

    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            await handshake(_build_ssl_context(), HOST)
        trusting = _build_ssl_context()
        trusting.load_verify_locations(cafile=str(DATA / "forward_ca.pem"))
        if sys.platform != "darwin":
            await handshake(trusting, HOST)
        with pytest.raises(ssl.SSLCertVerificationError):
            await handshake(trusting, "ffffffffffffffff.test-cluster.meshive.test")
    finally:
        await endpoint.close()


@scenario
async def test_a_rejected_key_is_passed_on_as_401_and_explained_once():
    async with forwarding(key=OTHER_KEY) as (forwarder, endpoint, said):       # expired, revoked, another port's
        async with web(forwarder) as client:
            first, second = await client.get("/echo"), await client.get("/echo")
        assert first.status_code == second.status_code == 401
        assert first.headers["x-meshive-lock-denied"] == "1" and first.json() == {"error": "unauthorized"}
        assert endpoint.keys == [[OTHER_KEY], [OTHER_KEY]] and endpoint.requests == []
        notices = [line for line in said if "rejected" in line]
        assert len(notices) == 1 and KEY_COMMAND in notices[0]
        assert "a key made a few seconds ago may not work yet" in notices[0]     # a new key takes a moment
        assert OTHER_KEY not in "\n".join(said)


@scenario
async def test_redirects_and_cookies_point_back_at_the_listener():
    async with forwarding() as (forwarder, _, _said):
        answer = await ask(forwarder, "GET", "/redirect", host=f"localhost:{forwarder.listen_port}")
        assert answer.status == 302
        # Back to the name the client used, so the browser stays on the origin its cookies are for.
        assert answer.get("location") == f"http://localhost:{forwarder.listen_port}/next?x=1"
        assert answer.all("set-cookie") == ["sid=abc; Path=/; HttpOnly; SameSite=Lax",
                                            "__Host-tok=1; Path=/; Secure; SameSite=None"]
        # An address that only starts like the endpoint's is somebody else's.
        elsewhere = await ask(forwarder, "GET", "/elsewhere")
        assert elsewhere.get("location") == f"https://{HOST}.example.com/x"
        async with web(forwarder, follow_redirects=True) as client:
            followed = await client.get("/redirect")
        assert followed.status_code == 200 and str(followed.url) == f"{forwarder.local_url}/next?x=1"


@scenario
async def test_requests_on_one_connection_share_one_endpoint_connection():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        for number in range(5):
            client.request("POST" if number % 2 else "GET", f"/echo?n={number}", body=b"abc" if number % 2 else b"")
            assert (await client.answer()).json()["target"] == f"/echo?n={number}"
        assert endpoint.opened == 1 and len(endpoint.requests) == 5
        # Two requests sent back to back are answered in order.
        client.request("GET", "/echo?n=first")
        client.request("GET", "/echo?n=second")
        assert [(await client.answer()).json()["target"] for _ in range(2)] == ["/echo?n=first", "/echo?n=second"]
        client.close()


@scenario
async def test_large_bodies_are_streamed_both_ways():
    size = 24 * 1024 * 1024
    async with forwarding() as (forwarder, endpoint, _):
        block = os.urandom(1 << 20)

        async def body():
            for _ in range(size >> 20):
                yield block

        async with web(forwarder) as client:
            up = await client.post("/echo", content=body())
            assert up.json()["length"] == size
            assert up.json()["sha256"] == hashlib.sha256(block * (size >> 20)).hexdigest()
            digest, got = hashlib.sha256(), 0
            async with client.stream("GET", f"/download?n={size}") as down:
                async for piece in down.aiter_raw():
                    digest.update(piece)
                    got += len(piece)
        assert got == size
        assert digest.hexdigest() == hashlib.sha256((bytes(range(256)) * 256) * (size >> 16)).hexdigest()
        endpoint.requests.clear()


# =================================================================================================
# Framing: every way a message can end
# =================================================================================================

@scenario
async def test_an_answer_that_runs_until_the_connection_closes():
    async with forwarding() as (forwarder, _, _said):
        client = await Raw.connect(forwarder)
        client.request("GET", "/until-close")
        answer = await client.answer()
        # Passed on in chunks: the client can tell where it ends, and keeps its connection.
        assert answer.body == b"runs until the connection closes" and answer.chunks == [b"20\r\n"]
        assert answer.get("connection") is None
        client.request("GET", "/echo")
        assert (await client.answer()).status == 200
        client.close()


@scenario
async def test_the_endpoint_closing_its_connection_does_not_close_the_clients():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        for path in ("/says-close", "/old-server", "/echo"):
            client.request("GET", path)
            answer = await client.answer()
            assert answer.status == 200 and answer.get("connection") is None
        assert endpoint.opened == 3
        client.close()


@scenario
@pytest.mark.parametrize("path", ["/says-close?slowly", "/old-server?slowly"])
async def test_a_connection_the_endpoint_will_close_is_not_kept(path):
    """The endpoint says this was the last answer on the connection (or is too old to keep one), but has not
    closed it yet. The listener lets it go at once, so the next request can't run into the closing."""
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", path)
        assert (await client.answer()).status == 200
        await until(lambda: not endpoint.open, 3)
        client.request("POST", "/echo", body=b"abc")
        assert (await client.answer()).json()["length"] == 3 and endpoint.opened == 2
        client.close()


@scenario
async def test_a_client_that_asks_to_close_is_closed():
    async with forwarding() as (forwarder, _, _said):
        client = await Raw.connect(forwarder)
        client.request("GET", "/echo", [("Connection", "close")])
        answer = await client.answer()
        assert answer.status == 200 and answer.get("connection") == "close"
        assert await client.closed()


@needs_untouched_traffic       # an inspecting antivirus was seen to rewrite answers to HTTP/1.0 requests
@scenario
async def test_an_http_1_0_client_gets_a_body_it_can_read():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(f"GET /sse HTTP/1.0\r\nHost: 127.0.0.1:{client.port}\r\n\r\n")
        answer = await client.answer()
        # No chunks for a client that can't read them: the plain data, then the connection closes.
        assert answer.get("transfer-encoding") is None and answer.get("connection") == "close"
        assert answer.body == b"".join(b"data: %d\n\n" % n for n in range(4))
        assert endpoint.requests[-1]["version"] == "HTTP/1.1"
        # The same for an answer the endpoint ends by closing its connection.
        client = await Raw.connect(forwarder)
        client.send(f"GET /until-close HTTP/1.0\r\nHost: 127.0.0.1:{client.port}\r\n\r\n")
        answer = await client.answer()
        assert answer.body == b"runs until the connection closes" and answer.get("transfer-encoding") is None
        assert answer.get("connection") == "close" and await client.closed()


@scenario
async def test_answers_without_a_body_and_early_hints():
    async with forwarding() as (forwarder, _, _said):
        client = await Raw.connect(forwarder)
        client.request("GET", "/no-content")
        answer = await client.answer()
        assert answer.status == 204 and answer.get("content-length") is None
        client.request("GET", "/hints")
        hints = await client.answer()
        assert hints.status == 103 and hints.get("link") == "</style.css>; rel=preload"
        assert (await client.answer()).body == b"after the hints"
        client.close()


@scenario
async def test_chunks_are_written_again_in_the_plain_form():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        # From the endpoint: a chunk extension and a trailer, both dropped on the way to the client.
        client.request("GET", "/trailers")
        answer = await client.answer()
        assert answer.body == b"hello world" and answer.chunks == [b"5\r\n", b"6\r\n"]
        assert answer.get("trailer") is None
        # From the client: the same, dropped on the way to the endpoint.
        client.send(f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nTransfer-Encoding: chunked\r\n\r\n"
                    "5;ext=1\r\nhello\r\n00006\r\n world\r\n0\r\nX-Trailer: 1\r\n\r\n")
        assert (await client.answer()).json()["length"] == 11
        assert endpoint.last_chunk_lines == [b"5\r\n", b"6\r\n", b"0\r\n"]
        client.close()


@scenario
async def test_an_answer_cut_short_is_not_passed_off_as_complete():
    async with forwarding() as (forwarder, _, said):
        client = await Raw.connect(forwarder)
        client.request("GET", "/truncated")
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await client.answer()
        assert any("cut short" in line for line in said)


@scenario
async def test_an_answer_the_forwarder_cannot_read_is_a_502():
    async with forwarding() as (forwarder, _, _said):
        for path in ("/folded", "/two-lengths"):
            answer = await ask(forwarder, "GET", path)
            assert answer.status == 502 and b"could not read" in answer.body


@scenario
async def test_headers_meant_for_one_connection_are_not_passed_on():
    async with forwarding() as (forwarder, endpoint, _):
        answer = await ask(forwarder, "GET", "/hop", [
            ("Connection", "keep-alive, X-Mine"), ("X-Mine", "1"), ("Keep-Alive", "timeout=5"), ("TE", "trailers"),
            ("Proxy-Authorization", "Basic abc"), ("Proxy-Connection", "keep-alive"), ("X-Stays", "1")])
        names = {name.lower() for name, _ in endpoint.requests[-1]["headers"]}
        assert "x-stays" in names
        assert not names & {"x-mine", "keep-alive", "te", "proxy-authorization", "proxy-connection"}
        assert endpoint.seen("connection") == ["keep-alive"]
        assert answer.get("x-kept") == "1"
        assert [answer.get(name) for name in ("x-internal", "keep-alive", "proxy-authenticate", "connection")] \
            == [None] * 4


# =================================================================================================
# The gaps the prototype had
# =================================================================================================

@scenario
async def test_a_header_that_stalls_is_given_up_on():
    async with forwarding(head_timeout=0.3) as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        started = time.monotonic()
        client.send(f"GET /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nX-Slow: ")      # and nothing more
        answer = await client.answer()
        assert answer.status == 408 and await client.closed()
        assert 0.25 < time.monotonic() - started < 3
        assert endpoint.opened == 0
        await until(lambda: not forwarder._tasks)


@scenario
async def test_a_connection_that_never_sends_a_request_is_closed():
    async with forwarding(idle_timeout=0.3) as (forwarder, _, _said):
        silent = await Raw.connect(forwarder)
        used = await Raw.connect(forwarder)
        used.request("GET", "/echo")
        assert (await used.answer()).status == 200
        assert await silent.closed(3) and await used.closed(3)      # no answer: nothing was asked
        await until(lambda: not forwarder._tasks)


@scenario
async def test_only_so_many_connections_at_once():
    async with forwarding(max_connections=3) as (forwarder, endpoint, said):
        held = [await Raw.connect(forwarder) for _ in range(3)]
        for client in held:
            client.request("GET", "/echo")
            assert (await client.answer()).status == 200
        refused = await ask(forwarder, "GET", "/echo")
        assert refused.status == 503 and refused.get("connection") == "close"
        assert len(endpoint.requests) == 3 and sum("3 connections" in line for line in said) == 1
        held[0].close()
        await until(lambda: len(forwarder._tasks) == 2)
        assert (await ask(forwarder, "GET", "/echo")).status == 200     # room again
        for client in held[1:]:
            client.close()


@scenario
async def test_a_safe_request_is_sent_again_when_the_kept_connection_was_closed():
    """The endpoint closes a kept connection at the moment the next request is sent on it (its idle limit). A GET
    goes out again on a new connection, once; the client never notices."""
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/echo?n=1")
        assert (await client.answer()).status == 200
        for method in ("GET", "HEAD", "OPTIONS"):
            endpoint.drop_reused_once = True
            before = len(endpoint.arrivals)
            client.request(method, "/echo?again")
            assert (await client.answer(head=method == "HEAD")).status == 200
            assert endpoint.arrivals[before:] == [(method, "/echo?again")] * 2      # sent twice, answered once
        assert endpoint.opened == 4
        client.close()


@scenario
@pytest.mark.parametrize("method, headers, body", [
    ("POST", (), b""),                      # no body, but not safe to repeat
    ("DELETE", (), b""),
    ("POST", (), b"abc"),                   # a body that has gone out
    ("GET", (), b"abc"),
])
async def test_other_requests_are_not_sent_again(method, headers, body):
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/echo")
        assert (await client.answer()).status == 200
        endpoint.drop_reused_once = True
        client.request(method, "/echo?once", headers, body)
        answer = await client.answer()
        assert answer.status == 502 and b"not sent again" in answer.body
        assert endpoint.arrivals.count((method, "/echo?once")) == 1
        assert await client.closed()


@scenario
async def test_a_new_connection_that_dies_is_not_tried_again():
    async with forwarding() as (forwarder, endpoint, _):
        endpoint.drop_once.add("/echo?fresh")
        answer = await ask(forwarder, "GET", "/echo?fresh")
        assert answer.status == 502 and b"not sent again" not in answer.body
        assert endpoint.arrivals == [("GET", "/echo?fresh")]


@scenario
async def test_a_kept_connection_found_closed_is_replaced_before_use():
    """The usual case of an idle limit: the endpoint closed the connection a while ago. Any request, safe to
    repeat or not, simply goes out on a new one."""
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/echo")
        assert (await client.answer()).status == 200
        endpoint.close_connections()
        await until(lambda: not endpoint.open)
        await asyncio.sleep(0.1)
        client.request("POST", "/echo?after", body=b"abc")
        assert (await client.answer()).json()["length"] == 3
        assert endpoint.opened == 2 and endpoint.arrivals.count(("POST", "/echo?after")) == 1
        client.close()


@scenario
async def test_a_kept_connection_with_leftovers_is_not_used_again():
    """What an endpoint sends beyond its answer must not become the answer to the next request."""
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/says-too-much")
        assert (await client.answer()).body == b"asked for"
        await asyncio.sleep(0.2)                            # the extra answer has arrived by now
        client.request("GET", "/echo?next")
        assert (await client.answer()).json()["target"] == "/echo?next"
        assert endpoint.opened == 2
        client.close()


@scenario
async def test_an_idle_endpoint_connection_is_let_go_before_the_endpoint_closes_it():
    async with forwarding(upstream_idle_timeout=0.2) as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/echo")
        assert (await client.answer()).status == 200 and len(endpoint.open) == 1
        await until(lambda: not endpoint.open, 3)       # the forwarder closed it; the client's stays open
        client.request("POST", "/echo", body=b"abc")
        assert (await client.answer()).status == 200 and endpoint.opened == 2
        client.close()


@scenario
async def test_an_answer_that_comes_before_the_body_was_sent():
    """An app (or the lock) answers without reading the upload. The client gets that answer, whole, and the
    connection is closed instead of being left waiting for a body nobody reads."""
    async with forwarding(linger_time=0.5) as (forwarder, endpoint, _):
        for path, status, key in (("/early", 413, KEY), ("/echo", 401, OTHER_KEY)):
            forwarder._key = key
            client = await Raw.connect(forwarder)
            client.send(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: 50000000\r\n\r\n")
            client.send(b"x" * 100_000)                 # the rest never comes
            answer = await client.answer()
            assert answer.status == status and answer.get("connection") == "close"
            assert await client.closed(5)
        await until(lambda: not forwarder._tasks and not endpoint.open)


@scenario
@pytest.mark.parametrize("extra, status", [("", 413),                              # the app's early answer
                                           ("Origin: http://evil.test\r\n", 403)])   # the listener's own refusal
async def test_a_client_still_sending_after_an_early_answer_is_not_cut_off(extra, status):
    """Closing a connection the client is still writing to resets it, and a reset can take the answer with it.
    So the listener stops sending but keeps reading for a moment."""
    async with forwarding(linger_time=1.5) as (forwarder, _, _said):
        client = await Raw.connect(forwarder)
        client.send(f"POST /early HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: 50000000\r\n{extra}\r\n")

        async def keep_sending():
            for _ in range(40):                         # for 0.4 s, well past the answer
                client.send(b"x" * 20_000)
                await client.writer.drain()
                await asyncio.sleep(0.01)

        sending = asyncio.ensure_future(keep_sending())
        answer = await client.answer()
        assert answer.status == status and answer.get("connection") == "close"
        await sending                                    # no reset while it was still sending
        assert await asyncio.wait_for(client.reader.read(), 5) == b""      # then a proper end
        client.close()


@scenario
async def test_an_early_answer_with_expect_100_continue():
    async with forwarding(key=OTHER_KEY) as (forwarder, _, _said):
        client = await Raw.connect(forwarder)
        client.send(f"PUT /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: 1000\r\n"
                    "Expect: 100-continue\r\n\r\n")
        answer = await client.answer()                  # refused outright: no 100, and no body is sent
        assert answer.status == 401 and answer.get("connection") == "close"
        assert await client.closed(5)


async def _upload(client, target, size):
    """Sends a POST of `size` bytes as fast as the connection takes it, and returns the answer. The sending
    goes on in the background while the answer is read, as a client that is answered early does; it stops
    quietly when the forwarder closes the connection."""
    client.send(f"POST {target} HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: {size}\r\n\r\n")
    block = os.urandom(1 << 20)

    async def send():
        with contextlib.suppress(ConnectionError, OSError):
            for _ in range(size >> 20):
                client.send(block)
                await client.writer.drain()

    sending = asyncio.ensure_future(send())
    try:
        return await client.answer(), block
    finally:
        await asyncio.sleep(0)
        if not sending.done():
            # Still sending what nobody reads: the forwarder lingers, then closes, which ends it.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(sending), 10)
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)


# An antivirus that inspects loopback traffic was seen to hold such uploads for about 20 s and then reset them,
# in every process at once, when several test processes ran side by side.
@needs_untouched_traffic
@scenario
@pytest.mark.parametrize("target, status", [
    ("/echo", 401),                     # the lock refuses the key, reads no more and lingers
    ("/early?then=linger", 413),        # the app answers first, reads no more and lingers
    ("/early?then=close", 413),         # ... closes without reading another byte
    ("/early?then=keep", 413),          # ... keeps the connection open without reading, for a while
    ("/echo", 200),                     # the app reads all of it, then answers
])
async def test_a_large_upload_answered_early_or_late_leaves_the_forwarder_healthy(target, status):
    """A large upload, answered before the endpoint has read the body (which leaves megabytes it will never take
    in the forwarder's buffers) or after it has read all of it. Whatever the endpoint does after an early answer,
    the answer arrives at once, nothing goes wrong inside the event loop, both connections end, and the forwarder
    goes on serving."""
    loop = asyncio.get_running_loop()
    trouble = []
    loop.set_exception_handler(lambda _loop, context: trouble.append(context))
    size = 16 << 20
    async with forwarding() as (forwarder, endpoint, said):
        forwarder._key = OTHER_KEY if status == 401 else KEY
        for _ in range(2):
            client = await Raw.connect(forwarder)
            started = time.monotonic()
            answer, block = await _upload(client, target, size)
            assert answer.status == status
            if status == 200:
                assert answer.json()["length"] == size
                assert answer.json()["sha256"] == hashlib.sha256(block * (size >> 20)).hexdigest()
            else:
                assert answer.get("connection") == "close" and time.monotonic() - started < 10
            client.close()
            # Every connection ends: the client's, and the one to the endpoint.
            await until(lambda: not forwarder._tasks and not endpoint.open, 10)
        forwarder._key = KEY
        assert (await ask(forwarder, "GET", "/echo")).status == 200
        endpoint.requests.clear()
    for _ in range(20):         # whatever the event loop still had scheduled for those connections
        await asyncio.sleep(0.01)
    assert [context.get("message") for context in trouble] == []
    assert not any("Internal error" in line for line in said)


@scenario
async def test_an_endpoint_that_ends_the_session_under_an_upload_and_keeps_reading():
    """The endpoint refuses an upload, ends its TLS session while megabytes of the body are still waiting in the
    forwarder's buffers, and then reads what comes. On the selector event loop of some Python releases (3.12.4
    among them; later ones clear the callback only once the connection is lost) the TLS layer then closes the
    socket transport with data still buffered, which leaves the transport without a write callback; once the
    endpoint has read enough, the data still held above it is written, and the loop runs a callback of None on
    every pass from then on. The forwarder drops such a connection as soon as the answer is through, so none of
    this happens: the client gets the 401, both connections end, the forwarder goes on serving."""
    loop = asyncio.get_running_loop()
    trouble = []
    loop.set_exception_handler(lambda _loop, context: trouble.append(context))
    tls = not tls_is_inspected()
    endpoint = EndingEndpoint(tls=tls)

    class OnASlowLink(Forwarder if tls else PlainForwarder):
        async def _open(self):
            # A small send buffer, as the window of a slow link would be: what the TLS layer holds back can
            # then be written only in part when the endpoint has read a little.
            reader, writer = await super()._open()
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            return reader, writer

    said = []
    forwarder = OnASlowLink(
        HOST, listen_port=0, key=OTHER_KEY, connect_to=("127.0.0.1", endpoint.port),
        ssl_context=_trusting_the_test_ca(), verbose=True, log=said.append, key_command=KEY_COMMAND)
    await forwarder.start()
    try:
        for attempt in range(1, 6):
            client = await Raw.connect(forwarder)
            started = time.monotonic()
            answer, _ = await _upload(client, "/upload", 16 << 20)
            assert answer.status == 401 and time.monotonic() - started < 10
            client.close()
            # Both connections end (the endpoint's may see a reset: what it was still sent is thrown away).
            await until(lambda: len(endpoint.taken) == attempt and not forwarder._tasks, 10)
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert [context.get("message") for context in trouble] == []
        started = time.monotonic()
        assert (await ask(forwarder, "GET", "/")).status == 401 and time.monotonic() - started < 5
    finally:
        await forwarder.close()
        endpoint.close()


@scenario
@pytest.mark.parametrize("request_line, body", [("GET /slow", b""), ("POST /slow", b"abc"),
                                                ("GET /silent-stream", b"")])
async def test_a_client_that_leaves_while_its_request_is_answered(request_line, body):
    """Nobody is left to answer, so the connection to the endpoint is dropped at once instead of being held until
    the app is done (which, for a stream with nothing to say, may be never)."""
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request(*request_line.split(), body=body)
        await until(lambda: endpoint.requests)
        if "stream" in request_line:
            assert (await client.reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        await asyncio.sleep(0.1)
        assert len(endpoint.open) == 1 and len(forwarder._tasks) == 1      # still waiting, as it should
        client.close()
        await until(lambda: not forwarder._tasks and not endpoint.open, 3)


@scenario
async def test_requests_sent_ahead_wait_their_turn():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.request("GET", "/sse")                       # a slow answer, and two more requests behind it
        client.request("POST", "/echo?n=2", body=b"abc")
        client.request("GET", "/echo?n=3")
        first = await client.answer()
        assert first.body == b"".join(b"data: %d\n\n" % n for n in range(4))
        assert (await client.answer()).json()["length"] == 3
        assert (await client.answer()).json()["target"] == "/echo?n=3"
        assert [target for _, target in endpoint.arrivals] == ["/sse", "/echo?n=2", "/echo?n=3"]
        client.close()


@needs_untouched_traffic       # an inspecting antivirus was seen to turn the reset into a proper close
@scenario
async def test_an_answer_that_breaks_off_before_the_connection_closes_properly():
    async with forwarding() as (forwarder, _, said):
        client = await Raw.connect(forwarder)
        client.request("GET", "/until-reset")
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await client.answer()                           # no last chunk: the client knows it is not whole
        assert any("cut short" in line for line in said)


@scenario
async def test_a_client_that_leaves_in_the_middle_of_its_body():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: 1000\r\n\r\nhalf")
        await until(lambda: endpoint.arrivals)
        client.close()
        await until(lambda: not forwarder._tasks and not endpoint.open)     # nothing is left waiting
        assert endpoint.requests == []


# =================================================================================================
# Who may use the listener
# =================================================================================================

@scenario
@pytest.mark.parametrize("host", ["evil.test", "rebind.test:{port}", "127.0.0.1:{other}", "localhost",
                                  "127.0.0.1:{port}.", "[::1]:{port}", f"{HOST}", "127.0.0.1:{port}, evil.test"])
async def test_a_request_for_another_host_name_gets_421(host):
    """A page that points its own domain at 127.0.0.1 reaches the listener, but under its own name."""
    async with forwarding() as (forwarder, endpoint, said):
        host = host.format(port=forwarder.listen_port, other=forwarder.listen_port + 1)
        answer = await ask(forwarder, "GET", "/echo", host=host)
        assert answer.status == 421 and answer.get("connection") == "close"
        assert endpoint.opened == 0 and endpoint.arrivals == []
        assert "evil" not in "\n".join(said) and "rebind" not in "\n".join(said)   # the name asked for is not echoed


@scenario
async def test_both_loopback_names_are_the_listener():
    async with forwarding() as (forwarder, endpoint, _):
        for name in (f"127.0.0.1:{forwarder.listen_port}", f"localhost:{forwarder.listen_port}",
                     f"LOCALHOST:{forwarder.listen_port}"):
            assert (await ask(forwarder, "GET", "/echo", host=name)).status == 200
        assert len(endpoint.requests) == 3


@scenario
async def test_a_request_needs_exactly_one_host():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send("GET /echo HTTP/1.1\r\n\r\n")
        assert (await client.answer()).status == 400
        client = await Raw.connect(forwarder)
        client.send(f"GET /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nHost: 127.0.0.1:{client.port}\r\n\r\n")
        assert (await client.answer()).status == 400
        assert endpoint.arrivals == []


@scenario
@pytest.mark.parametrize("origin", ["http://evil.test", "https://evil.test", "null", "http://127.0.0.1:{other}",
                                    "http://localhost:{other}", "https://127.0.0.1:{port}",
                                    "http://127.0.0.1:{port}.evil.test", f"https://{HOST}", "chrome-extension://abc"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_request_from_a_page_on_another_origin_gets_403(origin, method):
    async with forwarding() as (forwarder, endpoint, said):
        origin = origin.format(port=forwarder.listen_port, other=forwarder.listen_port + 1)
        answer = await ask(forwarder, method, "/echo", [("Origin", origin)], b"a=1" if method == "POST" else b"")
        assert answer.status == 403 and endpoint.opened == 0
        assert sum("--allow-origin" in line for line in said) == 1      # and how to allow it, if it was meant


@scenario
async def test_refusals_do_not_flood_the_terminal():
    """Each origin is named once, and only the first few: a page can make up as many origins as it likes. What
    the person needs to see afterwards still gets through."""
    async with forwarding(key=OTHER_KEY) as (forwarder, _, said):
        forwarder._verbose = False
        for number in range(30):
            for _ in range(2):
                assert (await ask(forwarder, "GET", "/echo", [("Origin", f"http://site-{number}.test")])).status == 403
        assert len(said) == 8 and all("--allow-origin http://site-" in line for line in said)
        assert (await ask(forwarder, "GET", "/echo")).status == 401
        assert len(said) == 9 and "access key was rejected" in said[-1]


@scenario
async def test_two_origin_headers_get_403():
    async with forwarding() as (forwarder, endpoint, _):
        own = forwarder.local_url
        answer = await ask(forwarder, "GET", "/echo", [("Origin", own), ("Origin", "http://evil.test")])
        assert answer.status == 403 and endpoint.opened == 0


@scenario
async def test_the_listeners_own_pages_pass_and_are_shown_to_the_app_as_the_endpoints():
    async with forwarding() as (forwarder, endpoint, _):
        for name in (f"127.0.0.1:{forwarder.listen_port}", f"localhost:{forwarder.listen_port}"):
            answer = await ask(forwarder, "POST", "/echo", [
                ("Origin", f"http://{name}"), ("Referer", f"http://{name}/lab/tree?x=1"),
                ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "cors")], b"a=1", host=name)
            assert answer.status == 200
            assert endpoint.seen("origin") == [f"https://{HOST}"]
            assert endpoint.seen("referer") == [f"https://{HOST}/lab/tree?x=1"]
        # A Referer from somewhere else is left as it is.
        await ask(forwarder, "GET", "/echo", [("Referer", f"http://127.0.0.1:{forwarder.listen_port}0/x")])
        assert endpoint.seen("referer") == [f"http://127.0.0.1:{forwarder.listen_port}0/x"]


@scenario
async def test_the_listeners_two_names_are_one_app():
    """A page opened as http://localhost:PORT that asks for http://127.0.0.1:PORT (or the other way round) is
    another site to the browser, but it is the same app behind the same listener."""
    async with forwarding() as (forwarder, endpoint, _):
        port = forwarder.listen_port
        cross = [("Sec-Fetch-Site", "cross-site")]
        answer = await ask(forwarder, "POST", "/echo", cross + [("Origin", f"http://localhost:{port}"),
                                                                ("Sec-Fetch-Mode", "cors")], b"{}")
        assert answer.status == 200 and endpoint.seen("origin") == [f"https://{HOST}"]
        image = cross + [("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "image")]
        assert (await ask(forwarder, "GET", "/echo", image + [("Referer", f"http://localhost:{port}/")])).status == 200
        link = cross + [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document"),
                        ("Referer", f"http://localhost:{port}/")]
        assert (await ask(forwarder, "GET", "/echo", link)).status == 200      # no page to continue from, either
        # The same requests from a page on another port stay out.
        assert (await ask(forwarder, "GET", "/echo", image + [("Referer", f"http://localhost:{port + 1}/")])).status == 403
        assert len(endpoint.requests) == 3


CROSS_SITE = [
    # what a page on another site can make the browser send without an Origin header
    ("GET", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "image")]),
    ("GET", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "script")]),
    ("GET", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "empty")]),
    ("GET", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "iframe")]),
    ("GET", [("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "image")]),
    ("GET", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "websocket"), ("Connection", "Upgrade"),
             ("Upgrade", "websocket")]),
    ("POST", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]),
    ("GET", [("Sec-Fetch-Site", "something-new")]),
    ("GET", [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")]),
]


@scenario
@pytest.mark.parametrize("method, headers", CROSS_SITE)
async def test_what_another_site_makes_the_browser_send_gets_403(method, headers):
    async with forwarding() as (forwarder, endpoint, _):
        answer = await ask(forwarder, method, "/echo", headers)
        assert answer.status == 403 and endpoint.opened == 0
        assert answer.get("content-type").startswith("text/plain")


@scenario
async def test_a_websocket_from_another_origin_is_not_opened():
    async with forwarding() as (forwarder, endpoint, _):
        answer = await ask(forwarder, "GET", "/ws", [("Connection", "Upgrade"), ("Upgrade", "websocket"),
                                                      ("Origin", "http://evil.test")])
        assert answer.status == 403 and endpoint.opened == 0


@scenario
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_a_link_from_another_site_gets_a_page_to_continue_from(site):
    """Opening the address from another site is not passed on (it could be a request the person never meant to
    make). The page that comes back shows nothing from the request; its link opens the same address from the
    listener itself, which passes."""
    async with forwarding() as (forwarder, endpoint, _):
        marker = "%3Cscript%3Ealert(1)%3C/script%3E"
        navigation = [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document"), ("Sec-Fetch-User", "?1"),
                      ("Accept", "text/html")]
        answer = await ask(forwarder, "GET", f"/do?it={marker}&x=<b>", [("Sec-Fetch-Site", site)] + navigation)
        assert answer.status == 403 and endpoint.opened == 0
        assert answer.get("content-type").startswith("text/html")
        assert b'<a href="">continue</a>' in answer.body
        assert b"script" not in answer.body and b"/do" not in answer.body and b"<b>" not in answer.body
        assert answer.get("content-security-policy") == "default-src 'none'; frame-ancestors 'none'"
        assert answer.get("x-frame-options") == "DENY" and answer.get("cache-control") == "no-store"
        # The link on that page: the same address, opened from the listener's own page.
        again = await ask(forwarder, "GET", f"/do?it={marker}", [("Sec-Fetch-Site", "same-origin")] + navigation)
        assert again.status == 200 and endpoint.arrivals == [("GET", f"/do?it={marker}")]


@scenario
async def test_what_the_person_opens_themselves_passes():
    async with forwarding() as (forwarder, endpoint, _):
        for site in ("none", "same-origin", "NONE"):       # the address bar, a bookmark, the app's own pages
            answer = await ask(forwarder, "GET", "/echo", [("Sec-Fetch-Site", site), ("Sec-Fetch-Mode", "navigate"),
                                                            ("Sec-Fetch-Dest", "document")])
            assert answer.status == 200
        assert len(endpoint.requests) == 3


@scenario
async def test_an_allowed_origin_may_use_the_listener():
    async with forwarding(allow_origins=["http://localhost:3000/", "HTTP://App.Test"]) as (forwarder, endpoint, _):
        page = "http://localhost:3000"
        # A fetch or a WebSocket carries the page's Origin.
        answer = await ask(forwarder, "POST", "/echo", [("Origin", page), ("Sec-Fetch-Site", "same-site"),
                                                         ("Sec-Fetch-Mode", "cors")], b"{}")
        assert answer.status == 200 and endpoint.seen("origin") == [page]      # not rewritten: it is not ours
        assert (await ask(forwarder, "GET", "/echo", [("Origin", "http://app.test")])).status == 200
        # An image or a script on that page carries no Origin: its Referer names the page.
        image = [("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "image")]
        assert (await ask(forwarder, "GET", "/echo", image + [("Referer", page + "/")])).status == 200
        assert (await ask(forwarder, "GET", "/echo", image + [("Referer", "http://localhost:3001/")])).status == 403
        assert (await ask(forwarder, "GET", "/echo", image)).status == 403
        assert (await ask(forwarder, "GET", "/echo", image + [("Referer", "http://[bad")])).status == 403
        # Other pages stay out.
        assert (await ask(forwarder, "GET", "/echo", [("Origin", "http://localhost:3001")])).status == 403
        assert (await ask(forwarder, "GET", "/echo", [("Origin", "http://evil.test"),
                                                       ("Referer", page + "/")])).status == 403
        assert len(endpoint.requests) == 3


# =================================================================================================
# Requests two servers could read differently
# =================================================================================================

HIDDEN = "0\r\n\r\nGET /hidden HTTP/1.1\r\nHost: {host}\r\n\r\n"
AMBIGUOUS = {
    "content-length and transfer-encoding":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n" + HIDDEN,
    "transfer-encoding before content-length":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n" + HIDDEN,
    "two different content-lengths":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length: 5\r\nContent-Length: 50\r\n\r\nhello",
    "a content-length list": "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length: 5, 50\r\n\r\nhello",
    "a signed content-length": "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length: +5\r\n\r\nhello",
    "a negative content-length": "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length: -5\r\n\r\nhello",
    "content-length in other digits": "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length: \xb2\r\n\r\nhi",
    "an empty content-length": "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length:\r\n\r\n",
    "a space before the colon":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nContent-Length : 5\r\n\r\nhello",
    "transfer-encoding with a space before the colon":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding : chunked\r\n\r\n" + HIDDEN,
    "two encodings": "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: gzip, chunked\r\n\r\n" + HIDDEN,
    "chunked twice": "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: chunked, chunked\r\n\r\n" + HIDDEN,
    "two transfer-encoding headers":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: identity\r\n\r\n"
        + HIDDEN,
    "an encoding that only looks like chunked":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: xchunked\r\n\r\n" + HIDDEN,
    "a folded header line":
        "POST /echo HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding:\r\n chunked\r\n\r\n" + HIDDEN,
    "a header continued with a tab": "GET /echo HTTP/1.1\r\nHost: {host}\r\nX-A: 1\r\n\tmore\r\n\r\n",
    "bare line feeds": "GET /echo HTTP/1.1\nHost: {host}\n\r\n\r\n",
    "a line feed inside a header": "GET /echo HTTP/1.1\r\nHost: {host}\r\nX-A: 1\nTransfer-Encoding: chunked\r\n\r\n",
    "a carriage return inside a header": "GET /echo HTTP/1.1\r\nHost: {host}\r\nX-A: 1\rX-B: 2\r\n\r\n",
    "a NUL in a header": "GET /echo HTTP/1.1\r\nHost: {host}\r\nX-A: 1\x002\r\n\r\n",
    "a header without a colon": "GET /echo HTTP/1.1\r\nHost: {host}\r\nnonsense\r\n\r\n",
    "a header name with a space": "GET /echo HTTP/1.1\r\nHost: {host}\r\nX A: 1\r\n\r\n",
    "an empty header name": "GET /echo HTTP/1.1\r\nHost: {host}\r\n: 1\r\n\r\n",
    "a full URL as the target": "GET http://{host}/echo HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "a full URL to another host": "GET http://evil.test/echo HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "CONNECT": "CONNECT evil.test:443 HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "an asterisk for GET": "GET * HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "another HTTP version": "GET /echo HTTP/2.0\r\nHost: {host}\r\n\r\n",
    "no HTTP version": "GET /echo\r\nHost: {host}\r\n\r\n",
    "two spaces in the request line": "GET  /echo HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "a tab in the request line": "GET\t/echo HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "a method that is not a word": "G(ET /echo HTTP/1.1\r\nHost: {host}\r\n\r\n",
    "an upgrade with a body":
        "GET /ws HTTP/1.1\r\nHost: {host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nContent-Length: 3\r\n\r\nabc",
}


@scenario
@pytest.mark.parametrize("name", list(AMBIGUOUS))
async def test_a_request_two_servers_could_read_differently_gets_400(name):
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(AMBIGUOUS[name].format(host=f"127.0.0.1:{client.port}"))
        answer = await client.answer()
        assert answer.status == 400 and answer.get("connection") == "close"
        assert await client.closed()                        # and what followed it is not read as a request
        assert endpoint.opened == 0 and endpoint.arrivals == []


MALFORMED_CHUNKS = {
    "a size that is not a number": "zz\r\nhello\r\n0\r\n\r\n",
    "a signed size": "+5\r\nhello\r\n0\r\n\r\n",
    "a size with a prefix": "0x5\r\nhello\r\n0\r\n\r\n",
    "an empty size line": "\r\nhello\r\n0\r\n\r\n",
    "bare line feeds": "5\nhello\n0\n\n",
    "data longer than its size": "5\r\nhello!\r\n0\r\n\r\n",
    "a size with a space inside": "1 0\r\n" + "x" * 16 + "\r\n0\r\n\r\n",
    "a very long size line": "5;" + "x" * 5000 + "\r\nhello\r\n0\r\n\r\n",
}


@scenario
@pytest.mark.parametrize("name", list(MALFORMED_CHUNKS))
async def test_a_malformed_chunked_body_gets_400(name):
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + MALFORMED_CHUNKS[name] + f"GET /hidden HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\n\r\n")
        answer = await client.answer()
        assert answer.status == 400 and await client.closed()
        await until(lambda: not endpoint.open)
        assert endpoint.requests == [] and ("GET", "/hidden") not in endpoint.arrivals


@scenario
async def test_what_can_be_read_one_way_only_is_passed_on_in_one_form():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nContent-Length: 5\r\n"
                    "content-length: 5\r\nTRANSFER-ENCODING-X: 1\r\n\r\nhello")
        assert (await client.answer()).json()["length"] == 5
        assert endpoint.seen("content-length") == ["5"]         # once
        client.send(f"\r\nPOST /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nTransfer-Encoding: CHUNKED\r\n\r\n"
                    "3\r\nabc\r\n0\r\n\r\n")                     # an empty line before a request is allowed
        assert (await client.answer()).json()["length"] == 3
        assert endpoint.seen("transfer-encoding") == ["chunked"]
        client.close()


@scenario
async def test_a_header_larger_than_the_limit_gets_431():
    async with forwarding() as (forwarder, endpoint, _):
        client = await Raw.connect(forwarder)
        client.send(f"GET /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nX-Big: " + "a" * 300_000)
        assert (await client.answer()).status == 431 and endpoint.opened == 0
        client = await Raw.connect(forwarder)
        client.send(f"GET /echo HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\n" + "X-A: 1\r\n" * 300 + "\r\n")
        assert (await client.answer()).status == 400 and endpoint.opened == 0
        # A large but ordinary header (long cookies) is fine.
        answer = await ask(forwarder, "GET", "/echo", [("Cookie", "c=" + "a" * 60_000)])
        assert answer.status == 200 and len(endpoint.seen("cookie")[0]) == 60_002


# =================================================================================================
# The key
# =================================================================================================

@scenario
async def test_the_forwarders_key_replaces_one_the_client_sent():
    async with forwarding() as (forwarder, endpoint, _):
        await ask(forwarder, "GET", "/echo", [("X-Meshive-Key", OTHER_KEY), ("x-meshive-key", "junk")])
        assert endpoint.keys == [[KEY]]


@scenario
async def test_without_a_key_the_clients_own_headers_go_through():
    async with forwarding(key=None) as (forwarder, endpoint, said):
        answer = await ask(forwarder, "GET", "/echo", [("X-Meshive-Key", KEY)])     # a client that has the key
        assert answer.status == 200 and endpoint.keys == [[KEY]]
        # No key anywhere: the lock's 401 is passed on, and the terminal says what is missing, once.
        for _ in range(2):
            assert (await ask(forwarder, "GET", "/echo")).status == 401
        notices = [line for line in said if "no access key was given" in line]
        assert len(notices) == 1 and KEY_COMMAND in notices[0] and "MESHIVE_ACCESS_KEY" in notices[0]
        assert "Port 8188 of pod p-0 is locked" in notices[0]


@scenario
async def test_on_an_unlocked_port_the_key_is_kept_back_until_the_port_is_locked():
    """An unlocked port has no lock in front of the app to take the key out, so the key is not sent: the app has
    no business seeing it. Once the endpoint says it is locked, the key goes out."""
    async with forwarding(locked=False, send_key=False) as (forwarder, endpoint, said):
        assert (await ask(forwarder, "GET", "/echo")).status == 200
        assert endpoint.keys == [[]] and "mshv_" not in json.dumps(endpoint.requests[-1]["headers"])
        endpoint.locked = True                              # someone locks the port
        assert (await ask(forwarder, "GET", "/echo")).status == 401     # passed on as it is
        assert (await ask(forwarder, "GET", "/echo")).status == 200     # from the next request on, with the key
        assert endpoint.keys == [[], [], [KEY]]
        assert sum("is locked now" in line for line in said) == 1 and KEY not in "\n".join(said)


@scenario
async def test_a_401_of_the_apps_own_says_nothing_about_the_key():
    async with forwarding() as (forwarder, _, said):
        answer = await ask(forwarder, "GET", "/app-401")       # the app's own sign-in, not the lock
        assert answer.status == 401 and answer.get("www-authenticate") == "Basic"
        assert not [line for line in said if "access key" in line]


@scenario
async def test_the_key_is_never_printed():
    async with forwarding() as (forwarder, endpoint, said):
        port = forwarder.listen_port
        await ask(forwarder, "GET", f"/echo?token={KEY}&secret=hunter2")       # a key in a query string, even
        await ask(forwarder, "GET", "/echo", [("X-Meshive-Key", KEY), ("Authorization", f"Bearer {KEY}")])
        await ask(forwarder, "GET", "/echo", [("Origin", "http://evil.test")])
        await ask(forwarder, "GET", "/echo", host="evil.test")
        await ask(forwarder, "GET", "/folded")
        await ask(forwarder, "POST", "/echo", [("Content-Length", "1"), ("Transfer-Encoding", "chunked")])
        endpoint.drop_once.add("/echo?dropped")
        await ask(forwarder, "POST", "/echo?dropped")
        forwarder._key = OTHER_KEY
        await ask(forwarder, "GET", "/echo")
        forwarder._once("made up", f"a message that somehow holds {OTHER_KEY}")
        everything = "\n".join(said)
        assert KEY not in everything and OTHER_KEY not in everything and "mshv_" not in everything
        assert "hunter2" not in everything and "token" not in everything       # no query strings either
        assert "GET /echo -> 200" in everything and "GET /echo -> 403 (Origin is another site)" in everything
        assert "-> 421" in everything and "-> 400" in everything and "-> 502" in everything
        assert "[access key]" in everything
        assert KEY not in repr(forwarder) and OTHER_KEY not in repr(forwarder)
        assert repr(forwarder) == f"Forwarder(http://127.0.0.1:{port} -> https://{HOST})"


@scenario
async def test_quiet_by_default():
    async with forwarding() as (forwarder, _, said):
        forwarder._verbose = False
        await ask(forwarder, "GET", "/echo")
        await ask(forwarder, "GET", "/until-close")
        assert said == []
        await ask(forwarder, "GET", "/echo", [("Origin", "http://evil.test")])     # a refusal is worth a line
        assert len(said) == 1


def test_what_is_not_a_key_or_a_host_is_refused():
    with pytest.raises(ValueError):
        Forwarder(HOST, listen_port=0, key="mshv_short")
    with pytest.raises(ValueError):
        Forwarder(HOST, listen_port=0, key=KEY + "\r\nX-Injected: 1")
    for host in ("evil.test\r\nX-Injected: 1", "a b", "", "host/path", "user@host"):
        with pytest.raises(ValueError):
            Forwarder(host, listen_port=0)


def test_a_silent_endpoint_connection_is_probed():
    with socket.socket() as connection:
        assert not connection.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        forward_cli._keep_alive(connection)
        assert connection.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 60
    forward_cli._keep_alive(None)      # a connection without a socket to set it on


def test_cookies_for_the_listener():
    cookie = forward_cli._local_cookie
    assert cookie("a=1") == "a=1"
    assert cookie("a=1; Domain=x.meshive.ai; Path=/; Secure; HttpOnly") == "a=1; Path=/; HttpOnly"
    assert cookie("a=1; SECURE; SameSite=None; Max-Age=3") == "a=1; SameSite=Lax; Max-Age=3"
    assert cookie("a=1; SameSite=Strict; domain=.x") == "a=1; SameSite=Strict"
    assert cookie("a=secure; Path=/") == "a=secure; Path=/"                    # a value is not an attribute
    assert cookie("__Host-a=1; Path=/; Secure; SameSite=None") == "__Host-a=1; Path=/; Secure; SameSite=None"
    assert cookie("__Secure-a=1; Domain=x; Secure") == "__Secure-a=1; Secure"


# =================================================================================================
# The command
# =================================================================================================

URL = "https://0123456789abcdef.dev.meshive.ai"
POD = {"podName": "p-0", "namespaceName": "ws", "userAlias": "p", "status": "running", "template": {"endpoints": [
    {"userAlias": "ComfyUI", "portType": "connect", "containerPort": 8188, "isExternal": True, "isReady": True,
     "externalUrl": URL, "endpointName": "a3f9", "isLocked": True, "accessKeyCount": 1},
    {"userAlias": "ide", "portType": "http", "containerPort": 8080, "isExternal": True, "isReady": True,
     "externalUrl": "https://fedcba9876543210.dev.meshive.ai", "endpointName": "b2", "isLocked": False},
    {"userAlias": "metrics", "portType": "http", "containerPort": 9100, "isExternal": False, "isReady": True,
     "internalUrl": "http://p-0.ws:9100"},
    {"userAlias": "odd", "portType": "tcp", "containerPort": 2222, "isExternal": True, "isReady": True,
     "externalUrl": "tcp://node.dev.meshive.ai:30022"},
    {"userAlias": "was public", "portType": "http", "containerPort": 9200, "isExternal": False, "isReady": True,
     "externalUrl": "https://aaaaaaaaaaaaaaaa.dev.meshive.ai"}]}}


@pytest.fixture
def command(monkeypatch):
    """`meshive port-forward` with the API answered locally and the listener not started: returns what would have run."""
    started = []

    def handler(request):
        assert request.url.path == "/v1/sdk/pods/p-0" and request.url.params["workspace"] == "ws"
        return httpx.Response(200, json=command.pod)

    def serve(forwarder, announce, listen):
        forwarder.listen_port = listen or 50123
        started.append((forwarder, listen))
        announce()
        return 0

    def command(*args):
        return cli.main(["port-forward", *args])

    command.pod, command.started = POD, started
    monkeypatch.setattr(cli, "Meshive", lambda *a, **kw: sync_client(handler))
    monkeypatch.setattr(forward_cli, "_serve", serve)
    return command


def test_command_forwards_a_locked_port_with_the_key_from_the_environment(command, capsys, monkeypatch):
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", KEY)
    assert command("ws", "p-0", "8188") == 0
    (forwarder, listen), = command.started
    assert listen == 8188                                   # the same number as the pod's port
    assert (forwarder.host, forwarder.port, forwarder._key, forwarder._send_key) == (URL[8:], 443, KEY, True)
    assert forwarder._connect_to == (URL[8:], 443) and forwarder._verbose is False
    captured = capsys.readouterr()
    assert captured.out == f"Forwarding http://127.0.0.1:8188 → {URL}\n"
    assert "Press Ctrl+C to stop." in captured.err and "not locked" not in captured.err
    assert KEY not in captured.out + captured.err


def test_command_options(command, capsys, monkeypatch):
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", KEY)
    assert command("ws", "p-0", "8188", "--listen", "9000", "-v", "--allow-origin", "http://localhost:3000",
                   "--allow-origin", "http://app.test/", "-o", "name") == 0
    (forwarder, listen), = command.started
    assert listen == 9000 and forwarder._verbose is True
    assert forwarder._allow_origins == {"http://localhost:3000", "http://app.test"}
    assert capsys.readouterr().out == "http://127.0.0.1:9000\n"
    assert command("ws", "p-0", "8188", "--listen", "0", "-o", "json") == 0
    assert json.loads(capsys.readouterr().out) == {"localUrl": "http://127.0.0.1:50123", "endpointUrl": URL,
                                                    "port": 8188, "locked": True}


def test_command_refuses_a_locked_port_without_a_key(command, capsys):
    assert command("ws", "p-0", "8188") == 2
    err = capsys.readouterr().err
    assert "port 8188 of pod p-0 is locked, and no access key was given" in err
    assert "meshive port-key-create ws p-0 8188 --name NAME" in err and "MESHIVE_ACCESS_KEY" in err
    assert "--key-file" in err and command.started == []


def test_command_forwards_an_unlocked_port_without_a_key(command, capsys, monkeypatch):
    assert command("ws", "p-0", "8080") == 0
    (forwarder, _), = command.started
    assert forwarder.host == "fedcba9876543210.dev.meshive.ai" and forwarder._key is None
    assert "Port 8080 is not locked, so no access key is needed." in capsys.readouterr().err
    # With a key given anyway, it is kept back (see the forwarder's own tests).
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", KEY)
    assert command("ws", "p-0", "8080") == 0
    forwarder, _ = command.started[-1]
    assert forwarder._key == KEY and forwarder._send_key is False
    assert "sent only if the port is locked later" in capsys.readouterr().err


def test_command_on_a_server_that_reports_no_locks(command, capsys):
    command.pod = {**POD, "template": {"endpoints": [{k: v for k, v in e.items() if k != "isLocked"}
                                                      for e in POD["template"]["endpoints"]]}}
    assert command("ws", "p-0", "8188") == 0               # no locks there: nothing to hold a key for
    assert "not locked" in capsys.readouterr().err


@pytest.mark.parametrize("port, message", [
    ("9100", "port 9100 of pod p-0 is internal"),
    ("9200", "port 9200 of pod p-0 is internal"),          # whatever address it may still carry
    ("2222", "port 2222 of pod p-0 is not reached over HTTPS"),
    ("7777", "pod p-0 has no port 7777 (its ports: 8188, 8080, 9100, 2222, 9200)"),
])
def test_command_refuses_a_port_it_cannot_forward(command, capsys, port, message):
    assert command("ws", "p-0", port) == 1
    assert message in capsys.readouterr().err and command.started == []


def test_command_says_when_the_pod_is_not_running(command, capsys, monkeypatch):
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", KEY)
    command.pod = {**POD, "status": "stopped"}
    assert command("ws", "p-0", "8188") == 0
    assert "Pod p-0 is stopped: requests fail until it runs." in capsys.readouterr().err


def test_command_checks_its_numbers(command, capsys):
    assert command("ws", "p-0", "0") == 2 and command("ws", "p-0", "8188", "--listen", "70000") == 2
    assert command.started == []
    with pytest.raises(SystemExit):
        command("ws", "p-0", "http")


@pytest.mark.parametrize("content", [
    KEY.encode(), KEY.encode() + b"\n", b"\n  " + KEY.encode() + b"\r\n", b"\xef\xbb\xbf" + KEY.encode() + b"\r\n",
    (KEY + "\r\n").encode("utf-16"),                        # what `> key.txt` writes in Windows PowerShell
    b'"' + KEY.encode() + b'"\n',
])
def test_command_reads_the_key_from_a_file(command, tmp_path, monkeypatch, content):
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", OTHER_KEY)    # the file wins
    (tmp_path / "key.txt").write_bytes(content)
    assert command("ws", "p-0", "8188", "--key-file", str(tmp_path / "key.txt")) == 0
    assert command.started[0][0]._key == KEY


@pytest.mark.parametrize("content", [b"", b"mshv_tooshort", b"secret-that-is-not-a-key\n", KEY.encode() + b"x",
                                     b"key: " + KEY.encode(), KEY.encode() + b"\n" + KEY.encode(), b"\xff\xfe\x00"])
def test_command_refuses_a_file_that_holds_no_key(command, tmp_path, capsys, content):
    (tmp_path / "key.txt").write_bytes(content)
    assert command("ws", "p-0", "8188", "--key-file", str(tmp_path / "key.txt")) == 2
    err = capsys.readouterr().err
    assert "does not hold an access key" in err and "mshv_ followed by 43 characters" in err
    assert "secret" not in err and "tooshort" not in err and KEY not in err        # what it holds is not shown
    assert command.started == []


def test_command_refuses_a_key_file_it_cannot_read(command, tmp_path, capsys):
    assert command("ws", "p-0", "8188", "--key-file", str(tmp_path / "missing.txt")) == 2
    assert "--key-file: could not read" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["mshv_short", "'" + KEY, KEY + " " + KEY])
def test_command_refuses_an_environment_variable_that_holds_no_key(command, capsys, monkeypatch, value):
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", value)
    assert command("ws", "p-0", "8188") == 2
    err = capsys.readouterr().err
    assert "MESHIVE_ACCESS_KEY does not hold an access key" in err and value not in err and KEY not in err


def test_command_takes_quotes_off_the_environment_variable(command, monkeypatch):
    monkeypatch.setenv("MESHIVE_ACCESS_KEY", f'"{KEY}" ')       # `set NAME="value"` on Windows keeps the quotes
    assert command("ws", "p-0", "8188") == 0 and command.started[0][0]._key == KEY


def test_command_has_no_option_that_takes_the_key(command, capsys):
    for option in ("--access-key", "-k"):
        with pytest.raises(SystemExit) as stopped:
            command("ws", "p-0", "8188", option, KEY)
        assert stopped.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    # `--key VALUE` is read as short for --key-file: the value is refused as a file name, and not repeated.
    for option in ("--key", "--key-file"):
        assert command("ws", "p-0", "8188", option, KEY) == 2
        err = capsys.readouterr().err
        assert "not the key itself" in err and KEY not in err and "mshv_Z" not in err
    assert command.started == []


def test_command_is_listed(capsys):
    with pytest.raises(SystemExit):
        cli.main(["port-forward", "--help"])
    out = capsys.readouterr().out
    assert "MESHIVE_ACCESS_KEY" in out and "--key-file" in out and "--listen" in out


def test_command_says_when_the_local_port_is_taken(monkeypatch, capsys):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        forwarder = Forwarder(HOST, listen_port=port)
        assert forward_cli._serve(forwarder, lambda: pytest.fail("announced"), port) == 1
    finally:
        taken.close()
    err = capsys.readouterr().err
    assert err.strip() == (f"Error: port {port} on 127.0.0.1 is already in use or not available. Choose another "
                           "local port with --listen PORT (--listen 0 picks a free one).")


@pytest.mark.parametrize("code", sorted({errno.EADDRINUSE, errno.EACCES, 10048, 10013}))
def test_a_port_that_is_taken_or_reserved_is_named_as_such(code):
    """Windows says 10013 (access denied) for a port another program holds exclusively or the system reserves:
    for the person both mean "use another port"."""
    message = forward_cli._cannot_listen(OSError(code, "error while attempting to bind"), 8188)
    assert message.startswith("Error: port 8188 on 127.0.0.1 is already in use or not available.")
    assert "--listen PORT" in message and "bind" not in message


def test_another_failure_to_listen_keeps_its_reason():
    message = forward_cli._cannot_listen(OSError(errno.EADDRNOTAVAIL, "cannot assign requested address"), 0)
    assert message == "Error: could not listen on 127.0.0.1:0: cannot assign requested address."


RUN_IT = """
import importlib, sys
cli = importlib.import_module("meshive.cli.main")
from meshive.models import Pod

class Api:
    def __init__(self, *args, **kwargs): pass
    def get_pod(self, pod, workspace): return Pod.from_dict(%r)
    def close(self): pass

cli.Meshive = Api
raise SystemExit(cli.main(["port-forward", "ws", "p-0", "8188", "--listen", "0", "-o", "name"]))
"""


def test_command_stops_cleanly_when_interrupted():
    """The real command in its own process, with connections open, stopped the way a person stops it: Ctrl+C
    (Ctrl+Break on Windows, the one of the two a test can send to another process)."""
    env = {**os.environ, "MESHIVE_ACCESS_KEY": KEY, "PYTHONPATH": str(Path(__file__).parent.parent),
           "PYTHONUNBUFFERED": "1", "MESHIVE_API_KEY": "meshive_test"}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen([sys.executable, "-c", RUN_IT % (POD,)], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, creationflags=flags)
    try:
        address = process.stdout.readline().strip()
        assert address.startswith("http://127.0.0.1:"), process.stderr.read()
        port = int(address.rsplit(":", 1)[1])
        idle = socket.create_connection(("127.0.0.1", port))
        half = socket.create_connection(("127.0.0.1", port))
        half.sendall(b"GET / HTTP/1.1\r\nHost: ")
        refused = socket.create_connection(("127.0.0.1", port))
        refused.sendall(b"GET / HTTP/1.1\r\nHost: evil.test\r\n\r\n")
        assert refused.recv(100).startswith(b"HTTP/1.1 421")
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        out, err = process.communicate(timeout=20)
        for connection in (idle, half, refused):
            connection.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 130, err
    assert err.splitlines()[-1] == "Stopped.", err
    assert "Traceback" not in err and "Error" not in err and "exception" not in err.lower(), err
    assert KEY not in out + err
