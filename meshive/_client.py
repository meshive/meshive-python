"""Meshive SDK 클라이언트 (동기 Meshive / 비동기 AsyncMeshive).

두 클라이언트는 요청 구성·응답 파싱 로직(_build_headers, _process)을 공유하고
transport(httpx.Client vs httpx.AsyncClient)만 다르다.

인증: Meshive API Key (READ scope). `Authorization: Bearer meshive_...`.
대상 표면: routers/sdk/app.py 의 read allowlist — 계정(me/api-keys/credit/earnings),
워크스페이스(목록/상세/멤버), 파드(목록/단건/메트릭), 스토리지, 머신(목록/단건/메트릭),
GPU 가용량, 템플릿, 서버리스(servings/tasks), 자산(Asset Hub). 전부 GET 이라 재시도가 안전하다.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterable
from datetime import date, datetime
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
from .models import (
    ApiKey,
    Asset,
    AssetPage,
    AssetStorage,
    Credit,
    CreditHistoryEntry,
    Earnings,
    GpuAvailability,
    Machine,
    MachineMetrics,
    Member,
    Pod,
    PodMetrics,
    Serving,
    Storage,
    Task,
    Template,
    WhoAmI,
    Workspace,
    WorkspaceDetail,
)


def _path_segment(value: str, name: str) -> str:
    """URL 경로 세그먼트 인코딩. `/`·`?`·`#` 등이 섞인 값이 경로 구조를 바꾸거나
    (`../me` → 다른 엔드포인트) 쿼리를 주입하지 못하게 percent-encode 한다."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return quote(value, safe="")


def _int_segment(value: int | str, name: str) -> str:
    """정수 ID(template/serving) 경로 세그먼트. bool 은 int 의 서브클래스라 따로 거른다."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if number < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return str(number)


# --- 쿼리 파라미터 구성 (sync/async 클라이언트가 공유) ----------------------------
# 서버 표면은 camelCase 쿼리(startDate/rentalType/appType)를 받는다. 값 검증은 서버 왕복
# 전에 여기서 끝내 "조용히 빈 결과" 대신 ValueError 로 알린다.

_RENTAL_TYPES = ("demand", "spot")


def _iso_date(value: date | datetime | str, name: str) -> str:
    """date/datetime/'YYYY-MM-DD' → 'YYYY-MM-DD'. datetime 은 date 의 서브클래스라 먼저 본다."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            raise ValueError(f"{name} must be a date, datetime, or 'YYYY-MM-DD' string") from None
    raise ValueError(f"{name} must be a date, datetime, or 'YYYY-MM-DD' string")


def _date_range_params(start_date: date | datetime | str | None,
                       end_date: date | datetime | str | None) -> dict[str, str]:
    """startDate/endDate 쿼리. 둘 다 None 이면 빈 dict (서버 기본: 최근 90일)."""
    params: dict[str, str] = {}
    if start_date is not None:
        params["startDate"] = _iso_date(start_date, "start_date")
    if end_date is not None:
        params["endDate"] = _iso_date(end_date, "end_date")
    return params


def _gpus_params(rental_type: str, min_vram: int | None) -> dict[str, Any]:
    rental = (rental_type or "").strip().lower()
    if rental not in _RENTAL_TYPES:
        raise ValueError(f"rental_type must be one of {', '.join(_RENTAL_TYPES)}")
    params: dict[str, Any] = {"rentalType": rental}
    if min_vram is not None:
        if isinstance(min_vram, bool) or not isinstance(min_vram, int) or min_vram < 0:
            raise ValueError("min_vram must be a non-negative integer (GB)")
        params["vram"] = min_vram
    return params


def _templates_params(workspace: str | None, app_type: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if workspace:
        params["workspace"] = workspace
    if app_type:
        params["appType"] = app_type.strip().lower()
    return params


def _assets_params(workspace: str, asset_type: str | None, status: str | None,
                   page: int, page_size: int) -> dict[str, Any]:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ValueError("page_size must be an integer between 1 and 100")
    params: dict[str, Any] = {"workspace": workspace, "page": page, "pageSize": page_size}
    if asset_type:
        params["assetType"] = asset_type.strip().lower()
    if status:
        params["status"] = status.strip().lower()
    return params


def _tasks_params(workspace: str, status: str | Iterable[str] | None,
                  limit: int, offset: int) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    params: dict[str, Any] = {"workspace": workspace, "limit": limit, "offset": offset}
    if status:
        values = [status] if isinstance(status, str) else list(status)
        joined = ",".join(s.strip().lower() for s in values if s and s.strip())
        if joined:
            params["status"] = joined
    return params


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

    # --- 0.0.7 확장 read 표면 ------------------------------------------------

    def get_workspace(self, workspace: str) -> WorkspaceDetail:
        """워크스페이스 상세 — 비용/리소스 요약 (GET /workspaces/{namespace})."""
        return WorkspaceDetail.from_dict(
            self._get(f"/workspaces/{_path_segment(workspace, 'workspace')}"))

    def list_members(self, workspace: str) -> list[Member]:
        """워크스페이스 멤버 목록 (GET /members?workspace=)."""
        data = self._get("/members", params={"workspace": workspace})
        return [Member.from_dict(d) for d in data.get("members", [])]

    def list_storages(self, workspace: str) -> list[Storage]:
        """워크스페이스의 스토리지(볼륨) 목록 (GET /storages?workspace=)."""
        data = self._get("/storages", params={"workspace": workspace})
        return [Storage.from_dict(d) for d in data.get("storages", [])]

    def get_storage(self, storage_name: str, workspace: str) -> Storage:
        """스토리지 단건 (GET /storages/{storage_name}?workspace=). 인자 순서는 get_pod 와 동일."""
        segment = _path_segment(storage_name, "storage_name")
        return Storage.from_dict(self._get(f"/storages/{segment}", params={"workspace": workspace}))

    def get_pod_metrics(self, pod_name: str, workspace: str) -> PodMetrics:
        """파드 리소스 사용량 (GET /pods/{pod_name}/metrics?workspace=)."""
        segment = _path_segment(pod_name, "pod_name")
        return PodMetrics.from_dict(
            self._get(f"/pods/{segment}/metrics", params={"workspace": workspace}))

    def get_machine_metrics(self, machine_id: str) -> MachineMetrics:
        """host 머신 실시간 메트릭 (GET /machines/{machine_id}/metrics)."""
        segment = _path_segment(machine_id, "machine_id")
        return MachineMetrics.from_dict(self._get(f"/machines/{segment}/metrics"))

    def list_gpus(self, *, rental_type: str = "demand",
                  min_vram: int | None = None) -> list[GpuAvailability]:
        """지금 대여 가능한 GPU 티어와 가격 (GET /gpus?rentalType=&vram=)."""
        return [GpuAvailability.from_dict(d)
                for d in self._get("/gpus", params=_gpus_params(rental_type, min_vram))]

    def list_api_keys(self) -> list[ApiKey]:
        """내 활성 API Key 목록 — prefix 만, 평문 없음 (GET /api-keys)."""
        return [ApiKey.from_dict(d) for d in self._get("/api-keys")]

    def get_credit(self) -> Credit:
        """크레딧 잔액 + 자동충전 설정 (GET /credit)."""
        return Credit.from_dict(self._get("/credit"))

    def list_credit_history(self, *, start_date: date | datetime | str | None = None,
                            end_date: date | datetime | str | None = None) -> list[CreditHistoryEntry]:
        """크레딧 충전/환불 내역 (GET /credit/history). 기본 최근 90일."""
        data = self._get("/credit/history", params=_date_range_params(start_date, end_date))
        return [CreditHistoryEntry.from_dict(d) for d in data]

    def get_earnings(self, *, start_date: date | datetime | str | None = None,
                     end_date: date | datetime | str | None = None) -> Earnings:
        """host 수익 요약 + 일별 내역 (GET /earnings). 기본 최근 90일."""
        return Earnings.from_dict(self._get("/earnings", params=_date_range_params(start_date, end_date)))

    def list_templates(self, workspace: str | None = None, *,
                       app_type: str | None = None) -> list[Template]:
        """official 템플릿 (+ workspace 지정 시 그 워크스페이스의 custom 템플릿) (GET /templates)."""
        data = self._get("/templates", params=_templates_params(workspace, app_type))
        return [Template.from_dict(d) for d in data]

    def get_template(self, template_id: int | str, workspace: str | None = None) -> Template:
        """템플릿 단건 (GET /templates/{template_id}). custom 템플릿은 workspace 를 함께 넘긴다."""
        params = {"workspace": workspace} if workspace else None
        segment = _int_segment(template_id, "template_id")
        return Template.from_dict(self._get(f"/templates/{segment}", params=params))

    def list_servings(self, workspace: str) -> list[Serving]:
        """워크스페이스의 serverless serving 배포 목록 (GET /servings?workspace=)."""
        return [Serving.from_dict(d) for d in self._get("/servings", params={"workspace": workspace})]

    def get_serving(self, serving_id: int | str) -> Serving:
        """serving 배포 단건 (GET /servings/{serving_id})."""
        return Serving.from_dict(self._get(f"/servings/{_int_segment(serving_id, 'serving_id')}"))

    def list_tasks(self, workspace: str, *, status: str | Iterable[str] | None = None,
                   limit: int = 50, offset: int = 0) -> list[Task]:
        """워크스페이스의 serverless task 목록, 최신순 (GET /tasks?workspace=&status=&limit=&offset=)."""
        data = self._get("/tasks", params=_tasks_params(workspace, status, limit, offset))
        return [Task.from_dict(d) for d in data]

    def get_task(self, task_id: str) -> Task:
        """task 단건 — 스크립트/설정/비용 분해는 `.raw` (GET /tasks/{task_id})."""
        return Task.from_dict(self._get(f"/tasks/{_path_segment(task_id, 'task_id')}"))

    def list_assets(self, workspace: str, *, asset_type: str | None = None, status: str | None = None,
                    page: int = 1, page_size: int = 20) -> AssetPage:
        """워크스페이스 자산 목록 한 페이지 (GET /assets?workspace=&assetType=&status=&page=&pageSize=).
        status 미지정 시 deleted/purged/merged 는 제외된다."""
        data = self._get("/assets", params=_assets_params(workspace, asset_type, status, page, page_size))
        return AssetPage.from_dict(data, namespace_name=workspace)

    def get_asset(self, asset_id: str) -> Asset:
        """자산 상세 — 버전 스택과 파일 목록 포함 (GET /assets/{asset_id})."""
        return Asset.from_dict(self._get(f"/assets/{_path_segment(asset_id, 'asset_id')}"))

    def get_asset_storage(self, workspace: str) -> AssetStorage:
        """managed 자산 저장량/월 예상 비용/크레딧 차단 상태 (GET /assets/storage-summary?workspace=)."""
        return AssetStorage.from_dict(self._get("/assets/storage-summary", params={"workspace": workspace}))

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

    # --- 0.0.7 확장 read 표면 (동기판과 동일 규칙) ---------------------------

    async def get_workspace(self, workspace: str) -> WorkspaceDetail:
        """워크스페이스 상세 (GET /workspaces/{namespace})."""
        return WorkspaceDetail.from_dict(
            await self._get(f"/workspaces/{_path_segment(workspace, 'workspace')}"))

    async def list_members(self, workspace: str) -> list[Member]:
        """워크스페이스 멤버 목록 (GET /members?workspace=)."""
        data = await self._get("/members", params={"workspace": workspace})
        return [Member.from_dict(d) for d in data.get("members", [])]

    async def list_storages(self, workspace: str) -> list[Storage]:
        """워크스페이스의 스토리지(볼륨) 목록 (GET /storages?workspace=)."""
        data = await self._get("/storages", params={"workspace": workspace})
        return [Storage.from_dict(d) for d in data.get("storages", [])]

    async def get_storage(self, storage_name: str, workspace: str) -> Storage:
        """스토리지 단건 (GET /storages/{storage_name}?workspace=)."""
        segment = _path_segment(storage_name, "storage_name")
        data = await self._get(f"/storages/{segment}", params={"workspace": workspace})
        return Storage.from_dict(data)

    async def get_pod_metrics(self, pod_name: str, workspace: str) -> PodMetrics:
        """파드 리소스 사용량 (GET /pods/{pod_name}/metrics?workspace=)."""
        segment = _path_segment(pod_name, "pod_name")
        data = await self._get(f"/pods/{segment}/metrics", params={"workspace": workspace})
        return PodMetrics.from_dict(data)

    async def get_machine_metrics(self, machine_id: str) -> MachineMetrics:
        """host 머신 실시간 메트릭 (GET /machines/{machine_id}/metrics)."""
        segment = _path_segment(machine_id, "machine_id")
        return MachineMetrics.from_dict(await self._get(f"/machines/{segment}/metrics"))

    async def list_gpus(self, *, rental_type: str = "demand",
                        min_vram: int | None = None) -> list[GpuAvailability]:
        """지금 대여 가능한 GPU 티어와 가격 (GET /gpus?rentalType=&vram=)."""
        data = await self._get("/gpus", params=_gpus_params(rental_type, min_vram))
        return [GpuAvailability.from_dict(d) for d in data]

    async def list_api_keys(self) -> list[ApiKey]:
        """내 활성 API Key 목록 — prefix 만, 평문 없음 (GET /api-keys)."""
        return [ApiKey.from_dict(d) for d in await self._get("/api-keys")]

    async def get_credit(self) -> Credit:
        """크레딧 잔액 + 자동충전 설정 (GET /credit)."""
        return Credit.from_dict(await self._get("/credit"))

    async def list_credit_history(self, *, start_date: date | datetime | str | None = None,
                                  end_date: date | datetime | str | None = None) -> list[CreditHistoryEntry]:
        """크레딧 충전/환불 내역 (GET /credit/history). 기본 최근 90일."""
        data = await self._get("/credit/history", params=_date_range_params(start_date, end_date))
        return [CreditHistoryEntry.from_dict(d) for d in data]

    async def get_earnings(self, *, start_date: date | datetime | str | None = None,
                           end_date: date | datetime | str | None = None) -> Earnings:
        """host 수익 요약 + 일별 내역 (GET /earnings). 기본 최근 90일."""
        data = await self._get("/earnings", params=_date_range_params(start_date, end_date))
        return Earnings.from_dict(data)

    async def list_templates(self, workspace: str | None = None, *,
                             app_type: str | None = None) -> list[Template]:
        """official 템플릿 (+ workspace 지정 시 custom 템플릿) (GET /templates)."""
        data = await self._get("/templates", params=_templates_params(workspace, app_type))
        return [Template.from_dict(d) for d in data]

    async def get_template(self, template_id: int | str, workspace: str | None = None) -> Template:
        """템플릿 단건 (GET /templates/{template_id}). custom 템플릿은 workspace 를 함께 넘긴다."""
        params = {"workspace": workspace} if workspace else None
        segment = _int_segment(template_id, "template_id")
        return Template.from_dict(await self._get(f"/templates/{segment}", params=params))

    async def list_servings(self, workspace: str) -> list[Serving]:
        """워크스페이스의 serverless serving 배포 목록 (GET /servings?workspace=)."""
        data = await self._get("/servings", params={"workspace": workspace})
        return [Serving.from_dict(d) for d in data]

    async def get_serving(self, serving_id: int | str) -> Serving:
        """serving 배포 단건 (GET /servings/{serving_id})."""
        segment = _int_segment(serving_id, "serving_id")
        return Serving.from_dict(await self._get(f"/servings/{segment}"))

    async def list_tasks(self, workspace: str, *, status: str | Iterable[str] | None = None,
                         limit: int = 50, offset: int = 0) -> list[Task]:
        """워크스페이스의 serverless task 목록, 최신순 (GET /tasks?...)."""
        data = await self._get("/tasks", params=_tasks_params(workspace, status, limit, offset))
        return [Task.from_dict(d) for d in data]

    async def get_task(self, task_id: str) -> Task:
        """task 단건 — 스크립트/설정/비용 분해는 `.raw` (GET /tasks/{task_id})."""
        return Task.from_dict(await self._get(f"/tasks/{_path_segment(task_id, 'task_id')}"))

    async def list_assets(self, workspace: str, *, asset_type: str | None = None,
                          status: str | None = None, page: int = 1, page_size: int = 20) -> AssetPage:
        """워크스페이스 자산 목록 한 페이지 (GET /assets?...). status 미지정 시 deleted/purged/merged 제외."""
        data = await self._get("/assets", params=_assets_params(workspace, asset_type, status, page, page_size))
        return AssetPage.from_dict(data, namespace_name=workspace)

    async def get_asset(self, asset_id: str) -> Asset:
        """자산 상세 — 버전 스택과 파일 목록 포함 (GET /assets/{asset_id})."""
        return Asset.from_dict(await self._get(f"/assets/{_path_segment(asset_id, 'asset_id')}"))

    async def get_asset_storage(self, workspace: str) -> AssetStorage:
        """managed 자산 저장량/월 예상 비용/크레딧 차단 상태 (GET /assets/storage-summary?workspace=)."""
        data = await self._get("/assets/storage-summary", params={"workspace": workspace})
        return AssetStorage.from_dict(data)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncMeshive":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
