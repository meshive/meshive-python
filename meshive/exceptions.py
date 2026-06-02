"""Meshive SDK 예외 계층.

서버 에러 포맷: {"detail": {"title": ..., "message": ...}}.
HTTP 상태코드 → 예외 매핑은 _client._raise_for_status 에서 수행한다.
"""
from __future__ import annotations


class MeshiveError(Exception):
    """모든 Meshive SDK 예외의 베이스."""


class ConfigurationError(MeshiveError):
    """API Key 누락 등 클라이언트 설정 문제 (요청을 보내기 전)."""


class MeshiveAPIError(MeshiveError):
    """서버가 4xx/5xx 를 반환했을 때.

    status_code: HTTP 상태코드
    title/message: 서버의 detail.title / detail.message (있으면)
    raw: 파싱된 응답 바디 전체 (dict | str | None)
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        title: str | None = None,
        raw: object = None,
    ) -> None:
        self.status_code = status_code
        self.title = title
        self.message = message
        self.raw = raw
        prefix = f"[{status_code}]"
        if title:
            prefix += f" {title}"
        super().__init__(f"{prefix}: {message}")


class AuthenticationError(MeshiveAPIError):
    """401 — API Key 누락/무효/만료, 또는 비활성 계정."""


class PermissionDeniedError(MeshiveAPIError):
    """403 — 키에 필요한 scope 가 없음."""


class NotFoundError(MeshiveAPIError):
    """404 — 리소스(파드/유저 등) 없음."""


class RateLimitError(MeshiveAPIError):
    """429 — 유저 단위 rate limit 초과. retry_after(초) 가 있으면 노출."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        title: str | None = None,
        raw: object = None,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(status_code, message, title=title, raw=raw)
