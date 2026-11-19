"""`meshive port-forward <workspace> <pod> <port>`: reach a public pod port through 127.0.0.1.

A locked port lets a program in only when it sends the port's access key in a header. Some apps take nothing but
an address: a ComfyUI client, a browser, a library with no way to set a header. The forwarder listens on
127.0.0.1, passes every request on to the port's public HTTPS address and adds the key as `X-Meshive-Key`, so such
an app is pointed at `http://127.0.0.1:<port>` instead.

The key comes from the MESHIVE_ACCESS_KEY environment variable or from --key-file, never from the command line,
and is never created here (`meshive port-key-create` does that). It is not printed, logged or put in an error.

Who can use the listener. It hands the key's access to whoever can send it a request, so it refuses the requests
a web page on another site could make the browser send:
  - it listens on 127.0.0.1 only, and a request whose Host is not this listener's own loopback name and port gets
    421 (a page that re-points its own domain at 127.0.0.1 can't reach it);
  - a request with an Origin other than the listener's own, or one the browser marks as coming from another site
    (Sec-Fetch-Site), gets 403: no cross-site fetch, form, image, script, frame or WebSocket goes through. A link
    from another site that opens the address as a page gets a 403 page with a "continue" link instead of being
    passed on, so another site can't trigger a request by itself. --allow-origin names a page that may use it.
What is not kept out: a program on this computer that can connect to 127.0.0.1 (it reaches the port as you do),
and a cross-site GET from a browser too old to send Sec-Fetch-Site (it carries no Origin either).

What a request goes through, in order: the head is read within a time limit and parsed strictly (a request that
two servers could read differently, such as one with both Content-Length and Transfer-Encoding, a folded header
line or a bare line feed, gets 400); Host and a loopback Origin or Referer are rewritten to the endpoint's;
`X-Meshive-Key` is added (Authorization is left alone, so an app's own API key keeps working); the body is
streamed, never buffered (Content-Length, chunked, 100-continue); the endpoint's certificate is verified for its
host name. In the answer, a Location that points at the endpoint is pointed back at the listener and cookies lose
`Domain` and `Secure` so that a browser keeps them for 127.0.0.1. An upgrade (WebSocket) turns the connection into
a two-way pipe. Addresses of the endpoint written inside a page's HTML or scripts are not changed.

Connections. One connection to the endpoint per client connection, kept open between requests and let go after a
short idle time. A request without a body that is safe to repeat (GET, HEAD, OPTIONS) is sent once more on a new
connection when the kept one turns out to have been closed; any other request gets 502 instead of being repeated.
A client that closes its connection while its request is being answered ends that request at the endpoint too.
The number of client connections is limited.

An answer the endpoint cuts short is not passed off as a whole one. Cut short means: fewer bytes than its
Content-Length, a chunked body without its last chunk, or, for an answer that runs until the connection closes, a
connection that was reset or that ended without TLS's closing message (anyone on the network path can end a TCP
connection; only the endpoint can send that message). The client is told by the framing where there is one, and
by a reset of its own connection where there is none: an HTTP/1.0 client reading an answer that runs until the
connection closes. That reset can't be relied on on Windows, whose event loop ends a connection in the ordinary
way before closing it, so there such a client may not be able to tell; the forwarder's own log line still says
"cut short".

Only the standard library is used, so the command runs wherever the package does. The one thing taken from the
SDK is which certificate roots are trusted: the same ones as for its API calls.
"""
from __future__ import annotations

import argparse
import asyncio
import codecs
import errno
import json
import os
import re
import signal
import socket
import ssl
import struct
import sys
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit

from . import _format as fmt
from .._client import Meshive, _build_ssl_context

ENV_ACCESS_KEY = "MESHIVE_ACCESS_KEY"

_KEY_SHAPE = re.compile(r"mshv_[A-Za-z0-9_-]{43}\Z")
_HOST_SHAPE = re.compile(r"(?=.{1,253}\Z)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\Z")
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_STATUS_LINE = re.compile(r"HTTP/1\.([01]) ([1-5][0-9][0-9])(?: .*)?\Z")
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
_NOT_IN_A_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

_MAX_HEAD = 256 * 1024          # one request or response head
_MAX_HEADERS = 256
_PIECE = 256 * 1024             # most bytes moved by one read
_MAX_CHUNK_LINE = 4096
_MAX_TRAILERS = 64 * 1024

# Headers that describe one connection, not the message: never passed on. Content-Length and Transfer-Encoding
# are dropped too and written again from what was parsed, so both sides of the forwarder see the same framing.
_PER_CONNECTION = frozenset({"connection", "keep-alive", "proxy-connection", "te", "trailer", "upgrade",
                             "proxy-authorization", "proxy-authenticate", "content-length", "transfer-encoding"})
_SAFE_TO_REPEAT = frozenset({"GET", "HEAD", "OPTIONS"})
# SO_LINGER switched on with a timeout of zero: struct linger is two shorts on Windows, two ints elsewhere.
_RESET_ON_CLOSE = struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0)

_CONTINUE_PAGE = (
    "<!doctype html><meta charset=\"utf-8\"><title>meshive port-forward</title>"
    "<p>This address was opened from another website, so <code>meshive port-forward</code> did not pass the request "
    "on.</p><p>If you meant to open it, <a href=\"\">continue</a>.</p>\n")


class _Reply(Exception):
    """The forwarder answers the request itself, then closes the connection."""

    def __init__(self, status: int, reason: str, text: str, *, page: str | None = None, note: str | None = None):
        super().__init__(reason)
        self.status, self.reason, self.text, self.page, self.note = status, reason, text, page, note


class _CannotListen(Exception):
    """The local port could not be taken."""


class _Hangup(Exception):
    """Nothing can be said on this connection any more (an answer is half sent, or the client is gone)."""


class _Eof(Exception):
    """The side being read from ended before the message did."""


class _SinkClosed(Exception):
    """The side being written to stopped taking data."""


class _Malformed(Exception):
    """A chunked body that doesn't follow the format."""


class _TooLong(Exception):
    """A head or a line longer than its limit."""


class _Stream:
    """Buffered reads over one connection: heads, lines, and bodies in pieces. A read error counts as its end,
    and is remembered in `failed` (an answer that runs until the connection closes ended early if it failed)."""

    __slots__ = ("_reader", "buffer", "failed")

    def __init__(self, reader: asyncio.StreamReader):
        self._reader = reader
        self.buffer = bytearray()
        self.failed = False

    def unusable(self) -> bool:
        """True for an idle connection that can't carry another request: it ended, failed, or holds bytes nobody
        asked for."""
        return (bool(self.buffer) or self.failed or self._reader.at_eof()
                or self._reader.exception() is not None)

    async def _read(self, limit: int) -> bytes:
        try:
            return await self._reader.read(limit)
        except OSError:      # reset, TLS failure: the connection is over either way
            self.failed = True
            return b""

    async def more(self) -> bool:
        """Waits for more data and keeps it in `buffer`. False when the connection ended instead."""
        data = await self._read(_PIECE)
        self.buffer += data
        return bool(data)

    async def until(self, end: bytes, limit: int) -> bytes | None:
        """Everything up to and including `end`. None when the connection ends first (what arrived stays in
        `buffer`); _TooLong when `end` is not within `limit` bytes."""
        searched = 0
        while True:
            at = self.buffer.find(end, searched)
            if at >= 0:
                if at + len(end) > limit:
                    raise _TooLong()
                out = bytes(self.buffer[:at + len(end)])
                del self.buffer[:at + len(end)]
                return out
            if len(self.buffer) >= limit:
                raise _TooLong()
            searched = max(0, len(self.buffer) - len(end) + 1)
            data = await self._read(_PIECE)
            if not data:
                return None
            self.buffer += data

    async def some(self, limit: int) -> bytes:
        """Up to `limit` bytes; b"" when the connection ended."""
        if self.buffer:
            out = bytes(self.buffer[:limit])
            del self.buffer[:limit]
            return out
        return await self._read(limit)


def _parse_head(raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    """A head (ending in an empty line) as its first line and its headers. ValueError for anything two parsers
    could read differently: a bare CR or LF, a folded line, a space before the colon, a control character."""
    text = raw[:-4].decode("latin-1")
    rest = text.replace("\r\n", "")
    if "\r" in rest or "\n" in rest or "\x00" in rest:
        raise ValueError("line endings")
    lines = text.split("\r\n")
    if len(lines) - 1 > _MAX_HEADERS:
        raise ValueError("too many headers")
    headers = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t"):
            raise ValueError("folded header line")
        name, colon, value = line.partition(":")
        if not colon or not _TOKEN.match(name):
            raise ValueError("header name")
        value = value.strip(" \t")
        if _NOT_IN_A_VALUE.search(value):
            raise ValueError("header value")
        headers.append((name, value))
    return lines[0], headers


def _values(headers: list[tuple[str, str]], name: str) -> list[str]:
    return [value for key, value in headers if key.lower() == name]


def _tokens(headers: list[tuple[str, str]], name: str) -> set[str]:
    return {item.strip().lower() for value in _values(headers, name) for item in value.split(",") if item.strip()}


def _serialize(first: str, headers: list[tuple[str, str]]) -> bytes:
    return (first + "\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers) + "\r\n").encode("latin-1")


def _content_length(headers: list[tuple[str, str]]) -> int | None:
    """The one Content-Length the headers agree on, or None without any. ValueError when they disagree or it
    isn't a plain number."""
    found = {item.strip() for value in _values(headers, "content-length") for item in value.split(",")}
    if not found:
        return None
    text = found.pop()
    if found or not (text.isascii() and text.isdigit()) or len(text) > 18:
        raise ValueError("Content-Length")
    return int(text)


def _request_framing(headers: list[tuple[str, str]]) -> tuple[str, int]:
    """How the request's body is delimited: ("none", 0), ("length", n) or ("chunked", 0). ValueError for a request
    whose end two servers could place differently."""
    encodings = _values(headers, "transfer-encoding")
    length = _content_length(headers)
    if encodings:
        if length is not None or len(encodings) != 1 or encodings[0].lower() != "chunked":
            raise ValueError("Transfer-Encoding")
        return "chunked", 0
    return ("none", 0) if length is None else ("length", length)


def _response_framing(method: str, status: int, headers: list[tuple[str, str]]) -> tuple[str, int | None]:
    """How the answer's body is delimited: "none", "length", "chunked" or "close" (it runs until the connection
    closes), with the Content-Length the answer states, if any."""
    encodings = _values(headers, "transfer-encoding")
    length = _content_length(headers)
    if method == "HEAD" or status in (204, 304):
        return "none", (None if status == 204 or encodings else length)
    if encodings:
        if len(encodings) != 1 or encodings[0].lower() != "chunked":
            raise ValueError("Transfer-Encoding")
        return "chunked", None
    return ("close", None) if length is None else ("length", length)


async def _push(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)
    try:
        await writer.drain()
    except OSError as exc:
        raise _SinkClosed() from exc


async def _copy_exact(source: _Stream, writer: asyncio.StreamWriter, count: int) -> None:
    while count > 0:
        data = await source.some(min(count, _PIECE))
        if not data:
            raise _Eof()
        count -= len(data)
        await _push(writer, data)


async def _copy_all(source: _Stream, writer: asyncio.StreamWriter, *, as_chunks: bool = False) -> None:
    """Moves data until the source ends. With as_chunks each piece is written as a chunk (the last, empty chunk
    is the caller's to write, once it knows the source ended properly)."""
    while True:
        data = await source.some(_PIECE)
        if not data:
            return
        if as_chunks:
            writer.write(b"%x\r\n" % len(data))
            writer.write(data)
            data = b"\r\n"
        await _push(writer, data)


async def _line(source: _Stream, limit: int) -> bytes:
    try:
        line = await source.until(b"\r\n", limit)
    except _TooLong:
        raise _Malformed() from None
    if line is None:
        raise _Eof()
    return line


async def _copy_chunked(source: _Stream, writer: asyncio.StreamWriter, *, framed: bool = True) -> None:
    """Moves a chunked body. Every chunk is parsed and written again in the plain form (size, data, CRLF), so chunk
    extensions and trailers are dropped and nothing the two sides could read differently is passed on. With
    framed=False only the data is written (for a client that can't read chunks)."""
    while True:
        size_text = (await _line(source, _MAX_CHUNK_LINE))[:-2].split(b";", 1)[0].strip(b" \t")
        if not size_text or len(size_text) > 15 or not _HEX_DIGITS.issuperset(size_text):
            raise _Malformed()
        size = int(size_text, 16)
        if size == 0:
            seen = 0
            while (line := await _line(source, _MAX_CHUNK_LINE)) != b"\r\n":
                seen += len(line)
                if seen > _MAX_TRAILERS:
                    raise _Malformed()
            await _push(writer, b"0\r\n\r\n" if framed else b"")
            return
        if framed:
            writer.write(b"%x\r\n" % size)
        await _copy_exact(source, writer, size)
        if await _line(source, 2) != b"\r\n":
            raise _Malformed()
        await _push(writer, b"\r\n" if framed else b"")


def _said_goodbye(writer: asyncio.StreamWriter | None) -> bool:
    """Whether a TLS connection that has reached its end was ended by the other side's closing message
    (close_notify), and not just by the TCP connection closing under it. asyncio reports both as the end of the
    stream. They differ in what the TLS object answers when asked for more: "the other side closed" in the first
    case, "nothing has arrived yet" in the second. A connection without TLS has no such message, and counts as
    ended properly."""
    tls = writer.get_extra_info("ssl_object") if writer is not None else None
    if tls is None:
        return True
    try:
        return tls.read(1) == b""
    except ssl.SSLZeroReturnError:
        return True
    except Exception:  # noqa: BLE001 - SSLWantReadError (nothing more came), or a connection in no state to say
        return False


def _close(writer: asyncio.StreamWriter | None, *, abort: bool = False, reset: bool = False) -> None:
    """Closes a connection: after what was written has gone out, or at once (abort). With reset, the other side
    is to read an error instead of the ordinary end of the stream, as far as this system allows: a zero linger
    time makes closing the socket send a reset. (The Windows event loop shuts a connection down before closing
    it, so the other side usually sees an ordinary end there all the same.)"""
    if writer is None:
        return
    if reset:
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _RESET_ON_CLOSE)
        except OSError:      # the socket is gone already; it is still closed below
            pass
    try:
        if abort:
            writer.transport.abort()
        else:
            writer.close()
    except Exception:  # noqa: BLE001 - closing a connection that is already gone
        pass


def _why(exc: BaseException) -> str:
    """A connection failure in a few words. Never the exception's own text."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "host name not found"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ssl.SSLError):
        return "TLS failure"
    return type(exc).__name__


def _keep_alive(sock: socket.socket | None) -> None:
    """Asks the system to probe an idle connection to the endpoint about once a minute, so a connection that died
    without a word (a router that forgot it, a network that went away) is noticed instead of waited on forever."""
    if sock is None:
        return
    # The option that sets the idle time has a different name on each system; where none exists, the system's
    # own (long) default applies.
    for level, name, value in ((socket.SOL_SOCKET, "SO_KEEPALIVE", 1), (socket.IPPROTO_TCP, "TCP_KEEPIDLE", 60),
                               (socket.IPPROTO_TCP, "TCP_KEEPALIVE", 60), (socket.IPPROTO_TCP, "TCP_KEEPINTVL", 10)):
        option = getattr(socket, name, None)
        if option is None or (name == "TCP_KEEPALIVE" and hasattr(socket, "TCP_KEEPIDLE")):
            continue
        try:
            sock.setsockopt(level, option, value)
        except (OSError, ValueError):
            pass


class _Connection:
    """One client connection and, while it has one, its connection to the endpoint."""

    __slots__ = ("client", "writer", "upstream", "upstream_writer", "client_task", "body_sent", "gone", "linger",
                 "reset")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.client = _Stream(reader)
        self.writer = writer
        self.upstream: _Stream | None = None
        self.upstream_writer: asyncio.StreamWriter | None = None
        self.client_task: "asyncio.Future[None] | None" = None    # reads the client while a request is answered
        self.body_sent = True   # the request's body has all gone out
        self.gone = False       # the client closed its connection while its request was being answered
        self.linger = False     # the client may still be sending: read what it sends for a moment before closing
        self.reset = False      # an answer broke off where only a reset of the client's connection can say so


class Forwarder:
    """Listens on 127.0.0.1 and passes requests on to `https://<host>`; see the module docstring.

    `key` is the port's access key (None without one). With `send_key=False` it is held back until the endpoint
    says it is locked: an unlocked port's requests reach the app as they are, headers included, and the key is
    none of the app's business. `connect_to` and `ssl_context` exist for tests, which stand in for the endpoint on
    a local port; certificate and host name are checked in every case.
    """

    head_timeout = 15.0             # from a request's first byte to the end of its head
    idle_timeout = 300.0            # a client connection with no request on it
    upstream_idle_timeout = 30.0    # a kept endpoint connection with no request on it (below what servers allow)
    connect_timeout = 20.0
    linger_time = 2.0
    max_connections = 100           # client connections at once (each may hold one more, to the endpoint)

    def __init__(self, host: str, *, listen_port: int, port: int = 443, key: str | None = None, send_key: bool = True,
                 allow_origins: Iterable[str] = (), connect_to: tuple[str, int] | None = None,
                 ssl_context: ssl.SSLContext | None = None, verbose: bool = False,
                 log: Callable[[str], None] | None = None, key_command: str = "meshive port-key-create",
                 port_label: str = "The port"):
        host = host.lower()
        if not _HOST_SHAPE.match(host):
            raise ValueError("not a host name")
        if key is not None and not _KEY_SHAPE.match(key):
            raise ValueError("not an access key")
        self.host = host
        self.port = port
        self.listen_port = listen_port
        self._authority = host if port == 443 else f"{host}:{port}"
        self._origin = f"https://{self._authority}"
        self._key = key
        self._send_key = key is not None and send_key
        self._allow_origins = frozenset(_normal_origin(origin) for origin in allow_origins)
        self._connect_to = connect_to or (host, port)
        if ssl_context is None:
            ssl_context = _build_ssl_context()      # the roots the SDK trusts for the API, on a context of its own
        try:
            ssl_context.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:
            pass
        self._ssl = ssl_context
        self._verbose = verbose
        self._print = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._key_command = key_command
        self._port_label = port_label
        self._said: set[object] = set()
        self._tasks: "set[asyncio.Task[object]]" = set()
        self._server: asyncio.AbstractServer | None = None
        self._local_names: frozenset[str] = frozenset()
        self._local_origins: frozenset[str] = frozenset()

    def __repr__(self) -> str:     # never the key
        return f"Forwarder(http://127.0.0.1:{self.listen_port} -> {self._origin})"

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.listen_port}"

    @property
    def endpoint_url(self) -> str:
        return self._origin

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Starts listening on 127.0.0.1 (OSError if the port can't be taken). `listen_port` 0 picks a free port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port, limit=_PIECE)
        self.listen_port = self._server.sockets[0].getsockname()[1]
        names = [f"127.0.0.1:{self.listen_port}", f"localhost:{self.listen_port}"]
        if self.listen_port == 80:      # a client leaves the default port out of Host
            names += ["127.0.0.1", "localhost"]
        self._local_names = frozenset(names)
        self._local_origins = frozenset(f"http://{name}" for name in names)

    async def close(self) -> None:
        """Stops listening and drops every connection at once."""
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(server.wait_closed(), 2)
        except (asyncio.TimeoutError, OSError):
            pass

    # ------------------------------------------------------------------ messages

    def _say(self, text: str) -> None:
        if self._key:
            text = text.replace(self._key, "[access key]")
        self._print(fmt.clean(text))

    def _once(self, kind: object, text: str) -> None:
        """Says something the first time only. Kinds a stranger can multiply (one per origin) stop after a few."""
        if kind in self._said or (isinstance(kind, tuple) and sum(isinstance(k, tuple) for k in self._said) >= 8):
            return
        self._said.add(kind)
        self._say(text)

    def _log(self, method: str, target: str, outcome: object) -> None:
        """One line per request with -v: method, path and result. The query string is left out (it can hold
        tokens), and so is every header."""
        if self._verbose:
            path = "".join(ch if "!" <= ch <= "~" else "?" for ch in target.split("?", 1)[0][:200])
            self._say(f"{time.strftime('%H:%M:%S')} {method} {path} -> {outcome}")

    def _lock_answer(self, status: int, headers: list[tuple[str, str]]) -> None:
        """The endpoint's lock turned the request away (401, or 302 to the console for a page): say why, once."""
        if status not in (401, 302) or "1" not in _values(headers, "x-meshive-lock-denied"):
            return
        if self._key is None:
            self._once("locked", f"{self._port_label} is locked and no access key was given, so requests get 401. "
                                 f"Create a key with `{self._key_command}`, put it in {ENV_ACCESS_KEY} (or pass "
                                 "--key-file) and start `meshive port-forward` again.")
        elif not self._send_key:
            self._send_key = True
            self._once("locked now", f"{self._port_label} is locked now: the access key is sent from the next "
                                     "request on.")
        else:
            self._once("rejected", "The access key was rejected (401): it may be expired, revoked or made for "
                                   "another port (a key made a few seconds ago may not work yet: try again "
                                   f"shortly). Create a new one with `{self._key_command}` and start "
                                   "`meshive port-forward` again with it.")

    # ------------------------------------------------------------------ who may use the listener

    def _guard(self, method: str, headers: list[tuple[str, str]]) -> str:
        """Refuses what a web page on another site could make a browser send (see the module docstring). Returns
        the name the client used for the listener ("127.0.0.1:8188"), for addresses written into the answer."""
        hosts = _values(headers, "host")
        if len(hosts) != 1:
            raise _Reply(400, "Bad Request", "The request needs exactly one Host header.")
        local = hosts[0].lower()
        if local not in self._local_names:
            self._once("host", "Refused a request addressed to another host name: the forwarder answers only at "
                               f"{self.local_url} and http://localhost:{self.listen_port}.")
            raise _Reply(421, "Misdirected Request",
                         f"meshive port-forward answers only at {self.local_url} and "
                         f"http://localhost:{self.listen_port}.",
                         note="Host is not this listener")
        origins = _values(headers, "origin")
        origin = origins[0].lower() if origins else None
        # Pages served through this listener, under either of its names, and pages named with --allow-origin.
        trusted = self._local_origins | self._allow_origins
        if len(origins) > 1 or (origin is not None and origin not in trusted):
            shown = (origins[0] if len(origins) == 1 else "several origins")[:120]
            self._once(("origin", shown), f"Refused a request made by a page on {shown}. If that page should use "
                                          f"this forwarder, start it with --allow-origin {shown}.")
            raise _Reply(403, "Forbidden",
                         "meshive port-forward refused a request made by a page on another site. To let that page use "
                         "it, start it with --allow-origin <the page's origin>.", note="Origin is another site")
        site = _values(headers, "sec-fetch-site")
        if origin is None and site and not all(value.lower() in ("same-origin", "none") for value in site):
            # The browser says another site made the request, and there is no Origin to say which (an image, a
            # script, a link). The Referer names the page: a page can leave it out, but can't make one up.
            try:
                parts = urlsplit((_values(headers, "referer") or [""])[0])
            except ValueError:
                parts = urlsplit("")
            if parts.netloc and _normal_origin(f"{parts.scheme}://{parts.netloc}") in trusted:
                return local
            modes, kinds = _values(headers, "sec-fetch-mode"), _values(headers, "sec-fetch-dest")
            if (method == "GET" and [m.lower() for m in modes] == ["navigate"]
                    and [k.lower() for k in kinds] in ([], ["document"])):
                # A link or redirect from another site opening this address as a page. It is not passed on: the
                # person gets a page whose link opens the same address from here, which a site can't click.
                raise _Reply(403, "Forbidden", "", page=_CONTINUE_PAGE, note="opened from another site")
            self._once("site", "Refused a request made by a page on another site. If that page should use this "
                               "forwarder, start it with --allow-origin <the page's origin>.")
            raise _Reply(403, "Forbidden",
                         "meshive port-forward refused a request made by a page on another site. To let that page use "
                         "it, start it with --allow-origin <the page's origin>.", note="request from another site")
        return local

    # ------------------------------------------------------------------ rewriting

    def _local_to_endpoint(self, value: str) -> str:
        """A URL on the listener as the same URL on the endpoint (Origin, Referer); anything else unchanged."""
        lowered = value.lower()
        for origin in self._local_origins:
            if lowered.startswith(origin) and (len(value) == len(origin) or value[len(origin)] in "/?#"):
                return self._origin + value[len(origin):]
        return value

    def _request_head(self, method: str, target: str, headers: list[tuple[str, str]], framing: str, length: int,
                      upgrade: str | None) -> bytes:
        named = _tokens(headers, "connection")     # headers the client says are for this connection only
        out = [("Host", self._authority)]
        for name, value in headers:
            low = name.lower()
            if low == "host" or low in _PER_CONNECTION or low in named:
                continue
            if low == "x-meshive-key" and self._send_key:
                continue
            if low in ("origin", "referer"):
                value = self._local_to_endpoint(value)
            out.append((name, value))
        if framing == "chunked":
            out.append(("Transfer-Encoding", "chunked"))
        elif framing == "length":
            out.append(("Content-Length", str(length)))
        if self._send_key and self._key is not None:
            out.append(("X-Meshive-Key", self._key))
        out += [("Connection", "Upgrade"), ("Upgrade", upgrade)] if upgrade else [("Connection", "keep-alive")]
        return _serialize(f"{method} {target} HTTP/1.1", out)

    def _response_headers(self, headers: list[tuple[str, str]], local: str) -> list[tuple[str, str]]:
        named = _tokens(headers, "connection")
        prefixes = (self._origin, f"http://{self._authority}")
        out = []
        for name, value in headers:
            low = name.lower()
            if low in _PER_CONNECTION or low in named:
                continue
            if low in ("location", "content-location"):
                for prefix in prefixes:
                    if value.lower().startswith(prefix) and (len(value) == len(prefix) or value[len(prefix)] in "/?#"):
                        value = f"http://{local}" + value[len(prefix):]
                        break
            elif low == "set-cookie":
                value = _local_cookie(value)
            out.append((name, value))
        return out

    # ------------------------------------------------------------------ the endpoint connection

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A TLS connection to the endpoint, with its certificate verified for the endpoint's host name."""
        host, port = self._connect_to
        return await asyncio.open_connection(host, port, ssl=self._ssl, server_hostname=self.host, limit=_PIECE,
                                             ssl_handshake_timeout=self.connect_timeout)

    async def _connect(self, conn: _Connection) -> None:
        try:
            reader, writer = await asyncio.wait_for(self._open(), self.connect_timeout)
        except ssl.SSLCertVerificationError:
            self._once("certificate", f"Could not verify the certificate of {self._origin}; nothing was sent.")
            raise _Reply(502, "Bad Gateway",
                         f"meshive port-forward could not verify the certificate of {self._origin}.",
                         note="certificate not verified") from None
        except (OSError, asyncio.TimeoutError) as exc:
            reason = _why(exc)
            self._once(("connect", reason), f"Could not connect to {self._origin} ({reason}).")
            raise _Reply(502, "Bad Gateway", f"meshive port-forward could not connect to {self._origin} ({reason}).",
                         note=f"connect failed: {reason}") from None
        _keep_alive(writer.get_extra_info("socket"))
        conn.upstream, conn.upstream_writer = _Stream(reader), writer

    def _drop_upstream(self, conn: _Connection, *, abort: bool = False) -> None:
        _close(conn.upstream_writer, abort=abort)
        conn.upstream = conn.upstream_writer = None

    # ------------------------------------------------------------------ one client connection

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        conn = _Connection(reader, writer)
        if task is None or len(self._tasks) >= self.max_connections:
            self._once("full", f"{self.max_connections} connections are open at once; more are refused until some "
                               "close.")
            self._write_reply(conn, _Reply(503, "Service Unavailable",
                                           "meshive port-forward has too many open connections."), None)
            try:
                await writer.drain()
            except OSError:
                pass
            _close(writer)
            return
        self._tasks.add(task)
        aborted = True
        try:
            while await self._exchange(conn):
                pass
            await self._finish(conn)
            aborted = False
        except _Hangup:
            pass
        except Exception as exc:  # noqa: BLE001 - a bug must not leave the socket open or flood the terminal
            self._once(("bug", type(exc).__name__), f"Internal error while forwarding ({type(exc).__name__}); the "
                                                    "connection was closed.")
        finally:
            self._tasks.discard(task)
            if conn.client_task is not None:
                conn.client_task.cancel()
            # Dropped at once when something went wrong; otherwise what is still buffered goes out first.
            self._drop_upstream(conn, abort=aborted)
            _close(writer, abort=aborted, reset=aborted and conn.reset)

    def _write_reply(self, conn: _Connection, reply: _Reply, method: str | None) -> None:
        """Writes the forwarder's own answer. The connection is closed after it."""
        body = (reply.page if reply.page is not None else reply.text + "\n").encode("utf-8")
        headers = [("Content-Type", "text/html; charset=utf-8" if reply.page is not None
                    else "text/plain; charset=utf-8"),
                   ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                   ("X-Content-Type-Options", "nosniff"), ("Connection", "close")]
        if reply.page is not None:
            # The page shows nothing from the request and may load nothing; it can't be framed by another site.
            headers += [("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
                        ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer")]
        conn.writer.write(_serialize(f"HTTP/1.1 {reply.status} {reply.reason}", headers)
                          + (b"" if method == "HEAD" else body))

    async def _stop_client_side(self, conn: _Connection) -> None:
        """Stops the task that reads from the client, and waits until it has stopped: only one reader at a time
        may wait on a connection."""
        task, conn.client_task = conn.client_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _finish(self, conn: _Connection) -> None:
        """Flushes what was written. When the client may still be sending (it was answered before its body was
        read), its data is read and dropped for a moment first: closing on unread data would reset the connection
        and could take the answer with it."""
        writer = conn.writer
        try:
            await writer.drain()
            if conn.linger and writer.can_write_eof():
                writer.write_eof()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.linger_time
                while (left := deadline - loop.time()) > 0:
                    if not await asyncio.wait_for(conn.client.some(_PIECE), left):
                        break
        except asyncio.TimeoutError:
            pass
        except OSError:
            raise _Hangup() from None

    async def _next_head(self, conn: _Connection) -> bytes | None:
        """The next request's head, or None when the client is done. An idle client is waited on for
        `idle_timeout`; the endpoint connection is let go earlier, before the endpoint's own idle limit closes it."""
        client = conn.client
        loop = asyncio.get_running_loop()
        give_up = loop.time() + self.idle_timeout
        let_go = loop.time() + self.upstream_idle_timeout
        while True:
            while client.buffer[:2] == b"\r\n":      # an empty line before a request is allowed
                del client.buffer[:2]
            if client.buffer:
                break
            now = loop.time()
            if now >= give_up:
                return None
            if conn.upstream is not None and now >= let_go:
                self._drop_upstream(conn)
            wake = give_up if conn.upstream is None else min(give_up, let_go)
            try:
                if not await asyncio.wait_for(client.more(), wake - now):
                    return None
            except asyncio.TimeoutError:
                pass
        try:
            raw = await asyncio.wait_for(client.until(b"\r\n\r\n", _MAX_HEAD), self.head_timeout)
        except asyncio.TimeoutError:
            raise _Reply(408, "Request Timeout", "The request's header did not arrive in time.",
                         note="header not sent in time") from None
        except _TooLong:
            raise _Reply(431, "Request Header Fields Too Large", "The request's header is too large.",
                         note="header too large") from None
        if raw is None:
            raise _Hangup()     # the client left in the middle of a header
        return raw

    async def _client_side(self, conn: _Connection, framing: str, length: int) -> None:
        """What the client does while its request is answered. First the request's body, passed on as it comes.
        Then nothing: but if the client closes its connection, nobody is left to answer, and the exchange is
        given up (_Eof) instead of holding a connection to the endpoint until the app is done. Requests the
        client sends ahead are kept for later."""
        assert conn.upstream_writer is not None
        if framing == "length":
            await _copy_exact(conn.client, conn.upstream_writer, length)
        elif framing == "chunked":
            await _copy_chunked(conn.client, conn.upstream_writer)
        conn.body_sent = True
        while len(conn.client.buffer) < _MAX_HEAD:
            if not await conn.client.more():
                raise _Eof()
        await asyncio.get_running_loop().create_future()     # too much sent ahead to keep reading: wait it out

    def _start_client_side(self, conn: _Connection, framing: str, length: int) -> None:
        conn.body_sent = False
        task = conn.client_task = asyncio.ensure_future(self._client_side(conn, framing, length))

        def ended(done: "asyncio.Future[None]") -> None:
            if not done.cancelled() and isinstance(done.exception(), _Eof) and conn.client_task is done:
                # The client left. Whatever is waiting on the endpoint is woken by dropping that connection.
                conn.gone = True
                _close(conn.upstream_writer, abort=True)

        task.add_done_callback(ended)

    async def _response_head(self, conn: _Connection) -> bytes | None:
        """The next head the endpoint sends; None when it closes the connection instead. A failure on the
        client's side ends the wait: nothing would come back, or nobody would be there to take it."""
        assert conn.upstream is not None
        client = conn.client_task
        read = asyncio.ensure_future(conn.upstream.until(b"\r\n\r\n", _MAX_HEAD))
        try:
            if client is not None and not client.done():
                await asyncio.wait({read, client}, return_when=asyncio.FIRST_COMPLETED)
            if client is not None and client.done() and not read.done():
                failure = client.exception()
                if isinstance(failure, _Malformed):
                    raise _Reply(400, "Bad Request", "The request's chunked body is malformed.",
                                 note="malformed chunked body")
                if not isinstance(failure, _SinkClosed):
                    raise _Hangup()     # the client left
            raw = await read
        except _TooLong:
            raise _Reply(502, "Bad Gateway", "The endpoint's response header is too large.",
                         note="response header too large") from None
        finally:
            read.cancel()
        if conn.gone:
            raise _Hangup()
        return raw

    async def _exchange(self, conn: _Connection) -> bool:
        """One request and its answer. True when the client connection can carry another request."""
        method: str | None = None
        target = ""
        try:
            raw = await self._next_head(conn)
            if raw is None:
                return False
            try:
                first, headers = _parse_head(raw)
                parts = first.split(" ")
                if len(parts) != 3:
                    raise ValueError("request line")
                version = parts[2]
                if not _TOKEN.match(parts[0]) or version not in ("HTTP/1.1", "HTTP/1.0"):
                    raise ValueError("request line")
                method = parts[0]
                if any(ch <= " " or ch == "\x7f" for ch in parts[1]):
                    raise ValueError("request target")
                if not (parts[1].startswith("/") or (parts[1] == "*" and method == "OPTIONS")):
                    raise ValueError("request target")     # a full URL or CONNECT: this is not a proxy
                target = parts[1]
                framing, length = _request_framing(headers)
            except ValueError as exc:
                raise _Reply(400, "Bad Request", f"meshive port-forward could not read the request ({exc}).",
                             note=f"malformed request: {exc}") from None
            local = self._guard(method, headers)
            connection = _tokens(headers, "connection")
            upgrade = (_values(headers, "upgrade") or [None])[0] if "upgrade" in connection else None
            if upgrade and (framing == "chunked" or length > 0):
                raise _Reply(400, "Bad Request", "An upgrade request can't have a body.",
                             note="upgrade request with a body")
            try:
                return await self._forward(conn, method, target, version, headers, framing, length, upgrade, local,
                                           wants_close="close" in connection or version == "HTTP/1.0")
            finally:
                await self._stop_client_side(conn)
        except _Reply as reply:
            # The answer is the forwarder's own, and the last thing on this connection. The client may not have
            # sent all of its request yet.
            self._log(method or "-", target, f"{reply.status} ({reply.note or reply.reason})")
            self._drop_upstream(conn, abort=True)
            self._write_reply(conn, reply, method)
            conn.linger = True
            return False

    async def _forward(self, conn: _Connection, method: str, target: str, version: str,
                       headers: list[tuple[str, str]], framing: str, length: int, upgrade: str | None, local: str,
                       *, wants_close: bool) -> bool:
        client_writer = conn.writer
        has_body = framing == "chunked" or length > 0
        # A kept connection the endpoint has closed in the meantime is replaced before it is used.
        if conn.upstream is not None and (conn.upstream.unusable() or conn.upstream_writer is None
                                          or conn.upstream_writer.is_closing()):
            self._drop_upstream(conn)
        reused = conn.upstream is not None
        if not reused:
            await self._connect(conn)
        conn.gone = False
        while True:
            assert conn.upstream is not None and conn.upstream_writer is not None
            conn.upstream_writer.write(self._request_head(method, target, headers, framing, length, upgrade))
            try:
                await conn.upstream_writer.drain()
            except OSError:
                pass        # shows as a closed connection below
            if conn.client_task is None:
                self._start_client_side(conn, framing, length)
            raw = await self._response_head(conn)
            if raw is not None:
                break
            # The endpoint closed the connection without a byte of an answer. On a kept connection that is the
            # endpoint's idle limit: a request that is safe to repeat goes out once more on a new connection.
            nothing_arrived = not conn.upstream.buffer
            await self._stop_client_side(conn)
            self._drop_upstream(conn)
            if reused and nothing_arrived and not has_body and method in _SAFE_TO_REPEAT:
                reused = False
                await self._connect(conn)
                continue
            raise _Reply(502, "Bad Gateway", "The endpoint closed the connection before answering."
                         + (" The request was not sent again: it may not be safe to repeat."
                            if reused and nothing_arrived else ""), note="endpoint closed the connection")

        while True:
            try:
                status_line, answer = _parse_head(raw)
                matched = _STATUS_LINE.match(status_line)
                if matched is None:
                    raise ValueError("status line")
                status, current = int(matched.group(2)), matched.group(1) == "1"
                if status == 101 and not upgrade:
                    raise ValueError("unexpected upgrade")
                framed, stated = _response_framing(method, status, answer) if status >= 200 else ("none", None)
            except ValueError as exc:
                raise _Reply(502, "Bad Gateway", f"The endpoint sent a response meshive port-forward could not read "
                             f"({exc}).", note=f"malformed response: {exc}") from None
            if status == 101 or status >= 200:
                break
            # 100 Continue, 103 Early Hints: passed on, the answer itself follows.
            try:
                await _push(client_writer, _serialize(status_line, self._response_headers(answer, local)))
            except _SinkClosed:
                raise _Hangup() from None
            raw = await self._response_head(conn)
            if raw is None:
                raise _Reply(502, "Bad Gateway", "The endpoint closed the connection before answering.",
                             note="endpoint closed the connection")

        out = self._response_headers(answer, local)
        if status == 101:
            out += [("Connection", "Upgrade"), ("Upgrade", (_values(answer, "upgrade") or [upgrade or ""])[0])]
            await self._stop_client_side(conn)      # from here on the pipe reads the client
            try:
                await _push(client_writer, _serialize(status_line, out))
            except _SinkClosed:
                raise _Hangup() from None
            self._log(method, target, "101 (upgraded)")
            await self._pipe(conn)
            return False

        self._lock_answer(status, answer)
        # Answered before the request's body was all sent (a refusal, usually): the client may still be sending,
        # so neither connection can carry another request.
        early = not conn.body_sent
        close = wants_close or early
        # (After an early answer the client's connection is closed, and the endpoint's goes with it.)
        reusable = framed != "close" and current and "close" not in _tokens(answer, "connection")
        # A client that reads chunks gets them, also for an answer the endpoint ends by closing its connection:
        # the client's connection stays usable, and an answer that broke off can't be taken for a whole one.
        chunks = framed in ("chunked", "close") and version == "HTTP/1.1"
        if chunks:
            out.append(("Transfer-Encoding", "chunked"))
        elif stated is not None:
            out.append(("Content-Length", str(stated)))
        if close:
            out.append(("Connection", "close"))
        try:
            client_writer.write(_serialize(status_line, out))
            if framed == "length":
                await _copy_exact(conn.upstream, client_writer, stated or 0)
            elif framed == "chunked":
                await _copy_chunked(conn.upstream, client_writer, framed=chunks)
            elif framed == "close":
                # Whether this answer ended or broke off, the client can tell only from its last chunk; a client
                # that doesn't read chunks (HTTP/1.0) only from its connection being reset.
                conn.reset = not chunks
                await _copy_all(conn.upstream, client_writer, as_chunks=chunks)
                if conn.upstream.failed or conn.gone or not _said_goodbye(conn.upstream_writer):
                    raise _Eof()
                conn.reset = False
                await _push(client_writer, b"0\r\n\r\n" if chunks else b"")
            else:
                await _push(client_writer, b"")
        except (_Eof, _Malformed, _SinkClosed):
            # The answer's head is out already, so the only way to say "incomplete" is to break the connection.
            self._log(method, target, f"{status} (cut short)")
            raise _Hangup() from None
        self._log(method, target, status)

        if early:
            # The endpoint answered without taking the whole body, and won't take the rest: what is still
            # buffered for it is dropped with the connection, at once, instead of being flushed to a peer that
            # stopped reading. Waiting would also run into a flaw of the selector event loop in some Python
            # releases (3.12.4 among them): when the endpoint ends its TLS session while body data is still
            # buffered, the TLS layer closes the socket transport with that data in it, which clears the
            # transport's write callback; once the endpoint reads again, the data the TLS layer still holds is
            # written and the loop registers a callback of None, then runs it on every pass for as long as the
            # socket lives. Aborting the connection discards the buffered data and unregisters the socket.
            if conn.client_task is not None:
                conn.client_task.cancel()       # it writes the body to this connection: stop it first
            self._drop_upstream(conn, abort=True)
        await self._stop_client_side(conn)
        if not conn.body_sent:          # the client was still sending a body nobody will read (see _finish)
            conn.linger = True
        if not reusable or conn.gone:
            self._drop_upstream(conn)
        return not close

    async def _pipe(self, conn: _Connection) -> None:
        """After an upgrade: bytes both ways until either side closes."""
        assert conn.upstream is not None and conn.upstream_writer is not None

        async def copy(source: _Stream, writer: asyncio.StreamWriter) -> bool:
            """True when the source ended, False when the writer stopped taking data."""
            try:
                await _copy_all(source, writer)
            except _SinkClosed:
                return False
            return True

        directions = [asyncio.ensure_future(copy(conn.client, conn.upstream_writer)),
                      asyncio.ensure_future(copy(conn.upstream, conn.writer))]
        try:
            await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            from_endpoint = directions[1]
            if from_endpoint.done() and from_endpoint.exception() is None and from_endpoint.result():
                # The endpoint ended the connection: nothing sent to it now is read, so what is still buffered
                # for it is dropped with the connection at once (see the early answer in _forward).
                directions[0].cancel()
                self._drop_upstream(conn, abort=True)
        finally:
            for direction in directions:
                direction.cancel()
            await asyncio.gather(*directions, return_exceptions=True)


def _normal_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def _local_cookie(value: str) -> str:
    """A Set-Cookie the browser will keep for http://127.0.0.1: without Domain (the endpoint's), without Secure,
    and SameSite=None (which needs Secure) as Lax. A `__Host-` or `__Secure-` cookie keeps Secure, which its name
    requires: browsers that treat 127.0.0.1 as secure keep it, the others drop it either way."""
    parts = [part.strip() for part in value.split(";")]
    prefixed = parts[0].lower().startswith(("__host-", "__secure-"))
    kept = [parts[0]]
    for part in parts[1:]:
        low = part.lower()
        if not part or low.startswith("domain=") or (low == "secure" and not prefixed):
            continue
        if low == "samesite=none" and not prefixed:
            part = "SameSite=Lax"
        kept.append(part)
    return "; ".join(kept)


# =============================================================================
# The command
# =============================================================================

def _read_key(path: str | None) -> str | None:
    """The access key from --key-file, else from MESHIVE_ACCESS_KEY; None when neither is given. The value is
    never part of an error message."""
    if path is not None:
        if path.strip().startswith("mshv_"):
            # The key itself where a file name belongs (`--key` is read as short for --key-file). It is not
            # repeated in the message.
            raise ValueError(f"--key-file takes the name of a file that holds the access key, not the key itself. "
                             f"Put the key in a file, or in the {ENV_ACCESS_KEY} environment variable.")
        source = f"The file given with --key-file ({path})"
        try:
            with open(path, "rb") as handle:
                raw = handle.read(4097)
        except OSError as exc:
            raise ValueError(f"--key-file: could not read {path}: {exc.strerror or type(exc).__name__}") from None
        # Windows PowerShell's `>` writes UTF-16 with a byte order mark; `-o name > key.txt` is how a key gets
        # into a file.
        if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            text = raw.decode("utf-16", errors="replace")
        else:
            text = raw.decode("utf-8-sig", errors="replace")
    else:
        source = f"{ENV_ACCESS_KEY}"
        text = os.environ.get(ENV_ACCESS_KEY, "")
        if not text.strip():
            return None
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":     # `set NAME="value"` keeps the quotes
        text = text[1:-1].strip()
    if not _KEY_SHAPE.match(text):
        raise ValueError(f"{source} does not hold an access key. A key is mshv_ followed by 43 characters, as "
                         "`meshive port-key-create` prints it once.")
    return text


def _endpoint_address(url: str) -> tuple[str, int]:
    """Host and port of a port's public URL. ValueError unless it is a plain https://host[:port] address."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not _HOST_SHAPE.match(host) or parts.path not in ("", "/") or parts.query
            or parts.fragment or parts.username is not None or parts.password is not None):
        raise ValueError("not an https address")
    return host, parts.port or 443


def _arrow(stream: object) -> str:
    try:
        "→".encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeError, LookupError):
        return "->"
    return "→"


def cmd_forward(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if not 1 <= args.port <= 65535:
        raise ValueError(f"port must be between 1 and 65535, got {args.port}")
    listen = args.port if args.listen is None else args.listen
    if not 0 <= listen <= 65535:
        raise ValueError(f"--listen must be between 0 and 65535, got {listen}")
    key = _read_key(args.key_file)
    key_command = f"meshive port-key-create {args.workspace} {args.pod_name} {args.port} --name NAME"

    pod = client.get_pod(args.pod_name, args.workspace)
    client.close()      # the API is not needed again, and the forwarder runs for hours
    endpoint = next((e for e in pod.endpoints if e.port == args.port), None)
    if endpoint is None:
        ports = ", ".join(str(e.port) for e in pod.endpoints) or "none"
        print(fmt.clean(f"Error: pod {args.pod_name} has no port {args.port} (its ports: {ports}). See `meshive pod "
                        f"{args.workspace} {args.pod_name}`."), file=sys.stderr)
        return 1
    if not endpoint.is_external or not endpoint.external_url:
        print(fmt.clean(f"Error: port {args.port} of pod {args.pod_name} is internal: it has no public address to "
                        "forward to. Expose it over HTTPS first (`meshive pod-update`)."), file=sys.stderr)
        return 1
    try:
        host, port = _endpoint_address(endpoint.external_url)
    except ValueError:
        print(fmt.clean(f"Error: port {args.port} of pod {args.pod_name} is not reached over HTTPS "
                        f"({endpoint.external_url}), so it can't be forwarded."), file=sys.stderr)
        return 1
    if endpoint.locked and key is None:
        print(fmt.clean(f"Error: port {args.port} of pod {args.pod_name} is locked, and no access key was given. "
                        f"Create one with `{key_command}`, then put its value in {ENV_ACCESS_KEY} or in a file "
                        "passed with --key-file. (`-o name` prints only the value.)"), file=sys.stderr)
        return 2

    forwarder = Forwarder(host, port=port, listen_port=listen, key=key, send_key=bool(endpoint.locked),
                          allow_origins=args.allow_origin or (), verbose=args.verbose, key_command=key_command,
                          port_label=f"Port {args.port} of pod {args.pod_name}")

    def announce() -> None:
        if output == "json":
            print(json.dumps({"localUrl": forwarder.local_url, "endpointUrl": forwarder.endpoint_url,
                              "port": args.port, "locked": endpoint.locked}, indent=2), flush=True)
        elif output == "name":
            print(forwarder.local_url, flush=True)
        else:
            print(f"Forwarding {forwarder.local_url} {_arrow(sys.stdout)} {forwarder.endpoint_url}", flush=True)
        notes = []
        if not endpoint.locked:
            notes.append(f"Port {args.port} is not locked, so no access key is needed"
                         + (" (the one given is sent only if the port is locked later)." if key else "."))
        if pod.status and pod.status.lower() != "running":
            notes.append(f"Pod {args.pod_name} is {pod.status}: requests fail until it runs.")
        notes.append("Programs on this computer reach the port through this address without a key. "
                     "Press Ctrl+C to stop.")
        for note in notes:
            print(fmt.paint(fmt.clean(note), "dim", fmt.color_enabled(sys.stderr)), file=sys.stderr, flush=True)

    return _serve(forwarder, announce, listen)


def _serve(forwarder: Forwarder, announce: Callable[[], None], listen: int) -> int:
    """Runs the forwarder until Ctrl+C, and then returns 130 (128 + SIGINT, as the CLI does for an interrupt)."""
    interrupted = False

    async def run() -> None:
        nonlocal interrupted
        loop = asyncio.get_running_loop()
        stop: "asyncio.Future[None]" = loop.create_future()

        def on_interrupt() -> None:
            nonlocal interrupted
            interrupted = True
            if not stop.done():
                stop.set_result(None)

        # Where the loop can take SIGINT itself (not on Windows), Ctrl+C ends the wait below instead of being raised
        # inside whichever connection happens to be running, which would leave a traceback behind on Python 3.10.
        try:
            loop.add_signal_handler(signal.SIGINT, on_interrupt)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

        def quiet(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
            # A connection interrupted by Ctrl+C is not an error worth a traceback.
            if isinstance(context.get("exception"), KeyboardInterrupt):
                return
            _loop.default_exception_handler(context)

        loop.set_exception_handler(quiet)
        try:
            await forwarder.start()
        except OSError as exc:
            raise _CannotListen(exc) from None
        try:
            announce()
            await stop                                            # until Ctrl+C
        finally:
            await forwarder.close()

    # Ctrl+Break on Windows stops it the same way as Ctrl+C.
    restore = None
    if hasattr(signal, "SIGBREAK"):
        try:
            restore = signal.signal(signal.SIGBREAK, signal.default_int_handler)
        except ValueError:      # not the main thread
            pass
    try:
        asyncio.run(run())
    except KeyboardInterrupt:       # where the loop can't take SIGINT itself, it is raised out of the loop
        interrupted = True
    except _CannotListen as exc:
        print(fmt.clean(_cannot_listen(exc.args[0], listen)), file=sys.stderr)
        return 1
    finally:
        if restore is not None:
            signal.signal(signal.SIGBREAK, restore)
    if interrupted:
        print(file=sys.stderr)      # past the ^C the terminal shows
        print("Stopped.", file=sys.stderr)
        return 130
    return 0


# Taken by another program, or (on Windows) reserved or held exclusively by one: either way, another port is the
# way out. 10048 and 10013 are WSAEADDRINUSE and WSAEACCES.
_PORT_TAKEN = frozenset({errno.EADDRINUSE, errno.EACCES, 10048, 10013})


def _cannot_listen(exc: OSError, listen: int) -> str:
    """Why the local port can't be used, in plain words (the system's own text is no help with this)."""
    if listen != 0 and (exc.errno in _PORT_TAKEN or getattr(exc, "winerror", None) in _PORT_TAKEN):
        return (f"Error: port {listen} on 127.0.0.1 is already in use or not available. Choose another local "
                "port with --listen PORT (--listen 0 picks a free one).")
    reason = exc.strerror or type(exc).__name__
    hint = "" if listen == 0 else " Choose another local port with --listen PORT (--listen 0 picks a free one)."
    return f"Error: could not listen on 127.0.0.1:{listen}: {reason}.{hint}"


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", common: argparse.ArgumentParser) -> None:
    p = sub.add_parser(
        "port-forward", parents=[common],
        help="Reach a public port of a pod at http://127.0.0.1:<port>, with the port's access key added to "
             "every request (for apps that take only an address). Runs until Ctrl+C.",
        description="Listens on 127.0.0.1 and passes every request on to the port's public HTTPS address, adding "
                    f"the port's access key. The key is read from the {ENV_ACCESS_KEY} environment variable or "
                    "from --key-file, never from the command line; create one with `meshive port-key-create`. A "
                    "port that isn't locked needs no key. Requests made by web pages on other sites are refused; "
                    "programs on this computer that can connect to 127.0.0.1 are not.")
    p.add_argument("workspace", help="Workspace ID (namespace name).")
    p.add_argument("pod_name", help="Pod ID (pod name).")
    p.add_argument("port", type=int, help="Port number, as `meshive pod` lists it.")
    p.add_argument("--listen", type=int, default=None, metavar="PORT",
                   help="Local port to listen on (default: the same number as the pod's port; 0 picks a free one).")
    p.add_argument("--key-file", default=None, metavar="PATH",
                   help=f"File holding the access key (default: the {ENV_ACCESS_KEY} environment variable).")
    p.add_argument("--allow-origin", action="append", metavar="ORIGIN",
                   help="Let pages on this origin (such as http://localhost:3000) send requests through the "
                        "forwarder (repeatable). Pages on other sites are always refused.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print one line per request: method, path and status (never the query string or the key).")
