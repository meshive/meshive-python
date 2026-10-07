"""Meshive SDK settings resolution.

base_url and api_key both resolve as "explicit argument > environment variable > credentials file > default".
The default endpoint is the production API; point elsewhere with MESHIVE_BASE_URL.

    export MESHIVE_API_KEY=meshive_xxxxxxxx
"""
import os
from urllib.parse import urlparse

from . import _credentials
from .exceptions import ConfigurationError

# Production endpoint. Override with MESHIVE_BASE_URL / --base-url.
DEFAULT_BASE_URL = "https://api.meshive.ai"

# Common prefix of the SDK read surface (/sdk + v1).
API_PREFIX = "/v1/sdk"

ENV_BASE_URL = "MESHIVE_BASE_URL"
ENV_API_KEY = "MESHIVE_API_KEY"

# API key format: `meshive_` + token_hex(32). Separate from the serverless inference gateway's `mk-` keys.
API_KEY_PREFIX = "meshive_"


def resolve_base_url(explicit: str | None = None) -> str:
    """Resolve the base URL (explicit > env > credentials file > production default). Strips the trailing slash.

    The env and credentials file can be tampered with outside the trust boundary, so the scheme is validated —
    anything other than http(s), or a value without a host, is rejected before a Bearer key is sent to it.
    """
    url = (
        explicit
        or os.getenv(ENV_BASE_URL)
        or _credentials.load().get("base_url")
        or DEFAULT_BASE_URL
    )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigurationError(
            f"Invalid base URL {url!r}: must be an absolute http(s) URL "
            f"(e.g. {DEFAULT_BASE_URL})."
        )
    return url.rstrip("/")


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Resolve the API key (explicit > env > credentials file). None if missing (ConfigurationError at call time)."""
    return explicit or os.getenv(ENV_API_KEY) or _credentials.load().get("api_key")
