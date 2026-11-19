"""The default SSL context against a real server: run with `pytest -m network` (left out of the default run).

The other tests never open a connection to the internet, so they can't show that this operating system's own
verifier, which the default context hands the certificate to on Windows and macOS, accepts a real server. This
one does a TLS handshake with the Meshive API and sends nothing over it. What a context refuses is tested without
the internet, in test_forward.py.
"""
import socket
import ssl

import pytest

from meshive import _client, _config

pytestmark = pytest.mark.network

HOST = _config.DEFAULT_BASE_URL.split("://", 1)[1].split("/", 1)[0]


@pytest.fixture
def context(monkeypatch):
    for name in _client._SSL_ENV:
        monkeypatch.delenv(name, raising=False)
    return _client._build_ssl_context()


def _handshake(context: ssl.SSLContext, name: str) -> None:
    with socket.create_connection((HOST, 443), timeout=20) as raw:
        with context.wrap_socket(raw, server_hostname=name):
            pass


def test_the_apis_certificate_is_trusted(context):
    _handshake(context, HOST)
