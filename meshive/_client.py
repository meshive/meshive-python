"""Meshive SDK 클라이언트 (동기 Meshive / 비동기 AsyncMeshive).

두 클라이언트는 요청 구성·응답 파싱 로직(_build_headers, _process)을 공유하고
transport(httpx.Client vs httpx.AsyncClient)만 다르다.

인증: Meshive API Key (READ scope). `Authorization: Bearer meshive_...`.
대상 표면: routers/sdk/app.py 의 read allowlist 4개.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import httpx

from . import _config
from ._version import __version__
from .exceptions import (
    AuthenticationError,
    ConfigurationError,
    MeshiveAPIError,
    MeshiveError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    WaitTimeoutError,
)
from .models import Machine, Pod, WhoAmI, Workspace


def _path_segment(value: str, name: str) -> str:
    """URL 경로 세그먼트 인코딩. `/`·`?`·`#` 등이 섞인 값이 경로 구조를 바꾸거나
    (`../me` → 다른 엔드포인트) 쿼리를 주입하지 못하게 percent-encode 한다."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return quote(value, safe="")


# 예외 message 상한 — 프록시/게이트웨이가 거대한 HTML 등을 돌려줘도 예외 메시지와
# 로그가 폭주하지 않게 자른다. 원본 전체는 MeshiveAPIError.raw 로 접근 가능.
_MAX_ERROR_MESSAGE_LEN = 2000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_ERROR_MESSAGE_LEN:
        return text
    return text[:_MAX_ERROR_MESSAGE_LEN] + "… (truncated)"


def _extract_error(payload: Any) -> tuple[str | None, str]:
    """서버 detail({"title","message"})에서 (title, message) 추출. 형식이 다르면 best-effort."""
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            return detail.get("title"), _truncate(str(detail.get("message", detail)))
        return None, _truncate(str(detail))
    return None, _truncate(str(payload)) if payload else "Unknown error"


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # "inf"/"nan"/음수도 float() 을 통과한다 — 이 값으로 sleep 하는 호출자를 보호.
    return value if math.isfinite(value) and value >= 0 else None


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


# --- 재시도 -----------------------------------------------------------------
# 일시적 실패(rate limit / 게이트웨이 오류 / 커넥션 끊김)만 재시도한다. 다른 4xx 는
# 재시도해도 결과가 같으므로 즉시 raise. read 표면은 전부 GET(멱등)이라 안전하다 —
# 쓰기 엔드포인트가 생기면 이 가정을 다시 따져야 한다.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException)
_RETRY_BACKOFF = 0.5  # 0.5s → 1s → 2s ...
# Retry-After 가 이보다 길면 기다리지 않고 RateLimitError 를 그대로 올린다 —
# 스크립트가 영문도 모르고 몇 분씩 멈춰 있는 편이 에러보다 나쁘다.
_MAX_RETRY_AFTER = 60.0


class _BaseClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = _config.resolve_api_key(api_key)
        self._base_url = _config.resolve_base_url(base_url)
        self._timeout = timeout
        self._max_retries = max_retries

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

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float | None:
        """재시도 대기 시간(초). 재시도하지 않을 상황이면 None.

        response=None 은 네트워크 예외를 뜻한다 (응답 자체가 없음).
        """
        if attempt >= self._max_retries:
            return None
        if response is None:
            return _RETRY_BACKOFF * 2 ** attempt
        if response.status_code not in _RETRY_STATUSES:
            return None
        after = _retry_after(response.headers)
        if after is None:
            return _RETRY_BACKOFF * 2 ** attempt
        return after if after <= _MAX_RETRY_AFTER else None


# --- wait_for_pod 공통 판정 ---------------------------------------------------
# 여기 도달하면 목표 상태로 갈 가능성이 없다 — timeout 을 채우지 않고 바로 실패시킨다.
# (목표 상태로 지정된 값은 아래 _wait_targets 에서 제외한다.)
_POD_TERMINAL_STATUSES = frozenset({"error", "terminated"})


def _wait_targets(until: str | Iterable[str]) -> tuple[set[str], set[str]]:
    """until → (목표 상태 set, 즉시 실패로 볼 상태 set). 비교는 소문자 기준."""
    values = [until] if isinstance(until, str) else list(until)
    targets = {s.lower() for s in values if s and s.strip()}
    if not targets:
        raise ValueError("until must name at least one status")
    return targets, _POD_TERMINAL_STATUSES - targets


def _wait_reached(pod: Pod, targets: set[str], terminal: set[str], label: str) -> bool:
    status = pod.status.lower()
    if status in targets:
        return True
    if status in terminal:
        raise MeshiveError(
            f"Pod {label} reached terminal status {pod.status!r} "
            f"while waiting for {'/'.join(sorted(targets))}."
        )
    return False


def _wait_expired(deadline: float, label: str, targets: set[str], last: str) -> None:
    if time.monotonic() < deadline:
        return
    raise WaitTimeoutError(
        f"Timed out waiting for pod {label} to reach {'/'.join(sorted(targets))} "
        f"(last status: {last or '-'})."
    )


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
        max_retries: int = 2,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._url(path)
        headers = self._build_headers()
        attempt = 0
        while True:
            try:
                response = self._client.get(url, params=params, headers=headers)
            except _RETRY_EXCEPTIONS:
                delay = self._retry_delay(attempt)
                if delay is None:
                    raise
            else:
                delay = self._retry_delay(attempt, response)
                if delay is None:
                    return self._process(response)
            time.sleep(delay)
            attempt += 1

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
        segment = _path_segment(pod_name, "pod_name")
        return Pod.from_dict(self._get(f"/pods/{segment}", params={"workspace": workspace}))

    def list_machines(self) -> list[Machine]:
        """host 로 등록한 머신 목록 (GET /machines). workspace 불필요 (host 가 직접 소유)."""
        return [Machine.from_dict(d) for d in self._get("/machines")]

    def get_machine(self, machine_id: str) -> Machine:
        """머신 단건 (GET /machines/{machine_id})."""
        return Machine.from_dict(self._get(f"/machines/{_path_segment(machine_id, 'machine_id')}"))

    def wait_for_pod(
        self,
        pod_name: str,
        workspace: str,
        *,
        until: str | Iterable[str] = "running",
        timeout: float = 600.0,
        interval: float = 5.0,
    ) -> Pod:
        """파드가 `until` 상태가 될 때까지 폴링하고 그 시점의 Pod 를 반환.

            pod = client.wait_for_pod("pod-1", "my-workspace", until="running")

        error/terminated 에 도달하면 timeout 을 채우지 않고 MeshiveError 를 올린다.
        시간 초과는 WaitTimeoutError (MeshiveError 이자 내장 TimeoutError).
        """
        targets, terminal = _wait_targets(until)
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            pod = self.get_pod(pod_name, workspace)
            if _wait_reached(pod, targets, terminal, pod_name):
                return pod
            last = pod.status
            _wait_expired(deadline, pod_name, targets, last)
            time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

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
        max_retries: int = 2,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._url(path)
        headers = self._build_headers()
        attempt = 0
        while True:
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except _RETRY_EXCEPTIONS:
                delay = self._retry_delay(attempt)
                if delay is None:
                    raise
            else:
                delay = self._retry_delay(attempt, response)
                if delay is None:
                    return self._process(response)
            await asyncio.sleep(delay)
            attempt += 1

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
        segment = _path_segment(pod_name, "pod_name")
        data = await self._get(f"/pods/{segment}", params={"workspace": workspace})
        return Pod.from_dict(data)

    async def list_machines(self) -> list[Machine]:
        """host 로 등록한 머신 목록 (GET /machines). workspace 불필요 (host 가 직접 소유)."""
        return [Machine.from_dict(d) for d in await self._get("/machines")]

    async def get_machine(self, machine_id: str) -> Machine:
        """머신 단건 (GET /machines/{machine_id})."""
        return Machine.from_dict(await self._get(f"/machines/{_path_segment(machine_id, 'machine_id')}"))

    async def wait_for_pod(
        self,
        pod_name: str,
        workspace: str,
        *,
        until: str | Iterable[str] = "running",
        timeout: float = 600.0,
        interval: float = 5.0,
    ) -> Pod:
        """파드가 `until` 상태가 될 때까지 폴링 (동기판 wait_for_pod 와 동일 규칙)."""
        targets, terminal = _wait_targets(until)
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            pod = await self.get_pod(pod_name, workspace)
            if _wait_reached(pod, targets, terminal, pod_name):
                return pod
            last = pod.status
            _wait_expired(deadline, pod_name, targets, last)
            await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncMeshive":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
