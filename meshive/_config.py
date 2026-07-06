"""Meshive SDK 설정 해석.

base_url / api_key 는 모두 "명시 인자 > 환경변수 > 기본값" 순으로 해석된다.
기본 엔드포인트는 prod(real). dev 로 붙으려면 MESHIVE_BASE_URL 을 export 한다.

    export MESHIVE_BASE_URL=https://api.dev.meshive.ai
    export MESHIVE_API_KEY=meshive_xxxxxxxx
"""
import os
from urllib.parse import urlparse

from . import _credentials
from .exceptions import ConfigurationError

# prod(real) 엔드포인트. dev 는 MESHIVE_BASE_URL 로 오버라이드.
DEFAULT_BASE_URL = "https://api.meshive.ai"

# SDK 전용 read 표면의 공통 prefix (routers/sdk/app.py 의 /sdk + v1 마운트).
API_PREFIX = "/v1/sdk"

ENV_BASE_URL = "MESHIVE_BASE_URL"
ENV_API_KEY = "MESHIVE_API_KEY"

# API Key 포맷: `meshive_` + token_hex(32). 서버리스 추론 게이트웨이의 `mk-` 키와는 별개 체계.
API_KEY_PREFIX = "meshive_"


def resolve_base_url(explicit: str | None = None) -> str:
    """base URL 해석 (명시 > env > credentials 파일 > 기본 prod). 후행 슬래시 제거.

    env/credentials 파일은 신뢰 경계 밖에서 조작될 수 있으므로 스킴을 검증한다 —
    http(s) 외 스킴이나 host 없는 값이면 Bearer 키를 실어 보내기 전에 거부.
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
    """API Key 해석 (명시 > env > credentials 파일). 없으면 None (호출 시점에 ConfigurationError)."""
    return explicit or os.getenv(ENV_API_KEY) or _credentials.load().get("api_key")
