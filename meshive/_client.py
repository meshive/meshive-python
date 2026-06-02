"""Meshive SDK 클라이언트 (동기 Meshive / 비동기 AsyncMeshive).

두 클라이언트는 요청 구성·응답 파싱 로직(_build_headers, _process)을 공유하고
transport(httpx.Client vs httpx.AsyncClient)만 다르다.

인증: Meshive API Key (READ scope). `Authorization: Bearer meshive_...`.
대상 표면: routers/sdk/app.py 의 read allowlist 4개.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import _config
from ._version import __version__
from .exceptions import (
    AuthenticationError,
    ConfigurationError,
    MeshiveAPIError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from .models import Pod, WhoAmI, Workspace


def _extract_error(payload: Any) -> tuple[str | None, str]:
    """서버 detail({"title","message"})에서 (title, message) 추출. 형식이 다르면 best-effort."""
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            return detail.get("title"), detail.get("message", str(detail))
        return None, str(detail)
    return None, str(payload) if payload else "Unknown error"


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _raise_for_status(status_code: int, payload: Any, headers: httpx.Headers) -> None:
    """4xx/5xx → 적절한 MeshiveAPIError 하위 예외."""
    if status_code < 400:
        return
    title, message = _extract_error(payload)
    common = {"title": title, "raw": payload}
    if status_code == 401:
        raise AuthenticationError(status_code, message, **common)
    if status_code == 403:
        raise PermissionDeniedError(status_code, message, **common)
    if status_code == 404:
        raise NotFoundError(status_code, message, **common)
    if status_code == 429:
        raise RateLimitError(status_code, message, retry_after=_retry_after(headers), **common)
    raise MeshiveAPIError(status_code, message, **common)


class _BaseClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = _config.resolve_api_key(api_key)
        self._base_url = _config.resolve_base_url(base_url)
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def _url(self, path: str) -> str:
        return f"{self._base_url}{_config.API_PREFIX}{path}"

    def _build_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ConfigurationError(
                "Missing API key. Run `meshive login`, pass api_key=..., "
                f"or set the {_config.ENV_API_KEY} environment variable."
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": f"meshive-python/{__version__}",
        }

    @staticmethod
    def _process(response: httpx.Response) -> Any:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text or None
        _raise_for_status(response.status_code, payload, response.headers)
        return payload


class Meshive(_BaseClient):
    """동기 Meshive SDK 클라이언트.

        from meshive import Meshive

        client = Meshive()                 # MESHIVE_API_KEY / MESHIVE_BASE_URL 사용
        me = client.me()
        for ws in client.list_workspaces():
            print(ws.namespace_name)

    컨텍스트 매니저(`with Meshive() as client:`)로 쓰면 연결을 자동 정리한다.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self._client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(self._url(path), params=params, headers=self._build_headers())
        return self._process(response)

    def me(self) -> WhoAmI:
        """현재 API Key 소유자 정보 (GET /me)."""
        return WhoAmI.from_dict(self._get("/me"))

    def list_workspaces(self) -> list[Workspace]:
        """내 워크스페이스 목록 (GET /workspaces)."""
        return [Workspace.from_dict(d) for d in self._get("/workspaces")]

    def list_pods(self, workspace: str) -> list[Pod]:
        """워크스페이스의 파드 목록 (GET /pods?workspace=)."""
        data = self._get("/pods", params={"workspace": workspace})
        return [Pod.from_dict(d) for d in data.get("pods", [])]

    def get_pod(self, pod_name: str, workspace: str) -> Pod:
        """파드 단건 (GET /pods/{pod_name}?workspace=)."""
        return Pod.from_dict(self._get(f"/pods/{pod_name}", params={"workspace": workspace}))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Meshive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncMeshive(_BaseClient):
    """비동기 Meshive SDK 클라이언트 (httpx.AsyncClient 기반).

        from meshive import AsyncMeshive

        async with AsyncMeshive() as client:
            me = await client.me()
            pods = await client.list_pods("my-workspace")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(
            self._url(path), params=params, headers=self._build_headers()
        )
        return self._process(response)

    async def me(self) -> WhoAmI:
        """현재 API Key 소유자 정보 (GET /me)."""
        return WhoAmI.from_dict(await self._get("/me"))

    async def list_workspaces(self) -> list[Workspace]:
        """내 워크스페이스 목록 (GET /workspaces)."""
        return [Workspace.from_dict(d) for d in await self._get("/workspaces")]

    async def list_pods(self, workspace: str) -> list[Pod]:
        """워크스페이스의 파드 목록 (GET /pods?workspace=)."""
        data = await self._get("/pods", params={"workspace": workspace})
        return [Pod.from_dict(d) for d in data.get("pods", [])]

    async def get_pod(self, pod_name: str, workspace: str) -> Pod:
        """파드 단건 (GET /pods/{pod_name}?workspace=)."""
        data = await self._get(f"/pods/{pod_name}", params={"workspace": workspace})
        return Pod.from_dict(data)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncMeshive":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
