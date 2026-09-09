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
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import httpx

from . import _config
from ._version import __version__
from . import _write
from .exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    InsufficientCreditError,
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
    Logs,
    Machine,
    MachineMetrics,
    Member,
    Pod,
    PodCreated,
    PodEstimate,
    PodMetrics,
    ResourceAction,
    Serving,
    Storage,
    StorageCreated,
    StorageEstimate,
    Task,
    TaskEstimate,
    TaskSubmitted,
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


def _query_value(value: str, name: str) -> str:
    """쿼리 파라미터용 문자열 검증. 인코딩은 httpx 가 하므로 여기서 하지 않는다(이중 인코딩 방지)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


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
    if status_code == 402:
        raise InsufficientCreditError(status_code, message, **common)
    if status_code == 409:
        raise ConflictError(status_code, message, **common)
    if status_code == 429:
        raise RateLimitError(status_code, message, retry_after=_retry_after(headers), **common)
    raise MeshiveAPIError(status_code, message, **common)


# --- 재시도 -----------------------------------------------------------------
# 일시적 실패(rate limit / 게이트웨이 오류 / 커넥션 끊김)만 재시도한다. 다른 4xx 는
# 재시도해도 결과가 같으므로 즉시 raise. GET 은 멱등이고, 쓰기 요청은 Idempotency-Key 를
# 같은 값으로 다시 보내므로(서버가 첫 응답을 재생) 이중 생성 없이 재시도할 수 있다.
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
        headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = _config.resolve_api_key(api_key)
        self._base_url = _config.resolve_base_url(base_url)
        self._timeout = timeout
        self._max_retries = max_retries
        # 추가 헤더(예: MCP 서버가 싣는 X-Meshive-Client). 인증/Accept 는 덮어쓸 수 없다.
        self._extra_headers = {str(k): str(v) for k, v in (headers or {}).items()
                               if k.lower() not in ("authorization", "accept")}

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
            "User-Agent": f"meshive-python/{__version__}",
            **self._extra_headers,
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
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
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, headers=headers)
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

    def _send(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              json: dict[str, Any] | None = None, idempotency_key: str | None = None) -> Any:
        """쓰기 요청. Idempotency-Key 를 붙여 보내므로 재시도해도 서버가 첫 응답을 재생한다(이중 생성 없음)."""
        url = self._url(path)
        headers = self._build_headers()
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        attempt = 0
        while True:
            try:
                response = self._client.request(method, url, params=params, json=json, headers=headers)
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

    # --- 쓰기: 파드 (write 스코프) ---------------------------------------------------

    def estimate_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                     gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                     vcpu: int | None = None, ram_gb: int | None = None, disk_gb: int | None = None,
                     volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                     ports: Any = None, command: str | None = None, internet_premium: bool = False,
                     uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                     max_price_per_hour: Any = None) -> PodEstimate:
        """파드 견적 — 아무것도 만들지 않는다(read 스코프로 충분). create_pod 와 인자가 같다."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, disk_gb=disk_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour)
        return PodEstimate.from_dict(self._send("POST", "/pods/estimate",
                                                 params={"workspace": _query_value(workspace, "workspace")}, json=body))

    def create_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                   gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                   vcpu: int | None = None, ram_gb: int | None = None, disk_gb: int | None = None,
                   volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                   ports: Any = None, command: str | None = None, internet_premium: bool = False,
                   uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                   max_price_per_hour: Any = None, idempotency_key: str | None = None) -> PodCreated:
        """파드 생성(202 수락). 시간당 요금이 발생한다 — 먼저 estimate_pod 로 가격을 확인하고,
        max_price_per_hour 를 주면 견적이 그보다 높을 때 서버가 거절한다(ConflictError 'Price Exceeds Cap')."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, disk_gb=disk_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour)
        return PodCreated.from_dict(self._send("POST", "/pods", params={"workspace": _query_value(workspace, "workspace")},
                                                json=body, idempotency_key=idempotency_key))

    def stop_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """파드 정지(replicas=0). 파드 과금은 멈추고 스토리지 과금은 계속된다."""
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/stop",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def start_pod(self, pod_name: str, workspace: str, *, placement: str = "same_node",
                  idempotency_key: str | None = None) -> ResourceAction:
        """정지된 파드 시작. placement: same_node(원래 노드) | any_node(다른 노드로 재배치, 로컬 스토리지는 남음)."""
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/start",
                             params={"workspace": _query_value(workspace, "workspace"), "placement": _write.placement(placement)},
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def restart_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/restart",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def delete_pod(self, pod_name: str, workspace: str, *, delete_local_storages: Iterable[str] | None = None,
                   idempotency_key: str | None = None) -> ResourceAction:
        """파드 삭제. 로컬(hostPath) 스토리지는 delete_local_storages 에 pv_name 을 적은 것만 같이 삭제된다."""
        data = self._send("DELETE", f"/pods/{_path_segment(pod_name, 'pod_name')}",
                             params=_write.delete_pod_params(_query_value(workspace, "workspace"), delete_local_storages),
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    # --- 쓰기: 스토리지 ---------------------------------------------------------------

    def estimate_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                         disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                         max_price_per_hour: Any = None) -> StorageEstimate:
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour)
        return StorageEstimate.from_dict(self._send("POST", "/storages/estimate",
                                                     params={"workspace": _query_value(workspace, "workspace")}, json=body))

    def create_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                       disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                       max_price_per_hour: Any = None, idempotency_key: str | None = None) -> StorageCreated:
        """스토리지(PV) 생성(202). 존재하는 동안 용량 기준으로 시간당 과금된다."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour)
        return StorageCreated.from_dict(self._send("POST", "/storages", params={"workspace": _query_value(workspace, "workspace")},
                                                    json=body, idempotency_key=idempotency_key))

    def delete_storage(self, storage_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """스토리지 삭제. 사용자 파드가 마운트 중이면 ConflictError('Storage In Use', raw.detail.linkedPods)."""
        data = self._send("DELETE", f"/storages/{_path_segment(storage_name, 'storage_name')}",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="storage")

    # --- 쓰기: 서빙 -----------------------------------------------------------------

    def deploy_serving(self, model_registration_id: int, *, workspace: str, price_cap_per_hour: Any,
                       min_replicas: int = 1, max_replicas: int = 3, autoscale: bool = True,
                       max_context_tokens: int | None = None, share_idle_capacity: bool = False,
                       idempotency_key: str | None = None) -> ResourceAction:
        """등록된 모델(registration id) 을 서빙으로 배포(201). 비용 상한 = price_cap_per_hour × max_replicas."""
        body = _write.serving_deploy_body(model_registration_id, price_cap_per_hour=price_cap_per_hour,
                                          min_replicas=min_replicas, max_replicas=max_replicas, autoscale=autoscale,
                                          max_context_tokens=max_context_tokens, share_idle_capacity=share_idle_capacity)
        data = self._send("POST", "/servings", params={"workspace": _query_value(workspace, "workspace")}, json=body,
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    def scale_serving(self, serving_id: int | str, *, min_replicas: int | None = None, max_replicas: int | None = None,
                      autoscale: bool | None = None, price_cap_per_hour: Any = None,
                      idempotency_key: str | None = None) -> ResourceAction:
        body = _write.serving_scale_body(min_replicas=min_replicas, max_replicas=max_replicas, autoscale=autoscale,
                                         price_cap_per_hour=price_cap_per_hour)
        data = self._send("PATCH", f"/servings/{_int_segment(serving_id, 'serving_id')}/scale", json=body,
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    def pause_serving(self, serving_id: int | str, *, paused: bool = True,
                      idempotency_key: str | None = None) -> ResourceAction:
        """paused=True 로 일시정지, False 로 재개."""
        data = self._send("PATCH", f"/servings/{_int_segment(serving_id, 'serving_id')}/pause",
                             json={"paused": bool(paused)}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    def delete_serving(self, serving_id: int | str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = self._send("DELETE", f"/servings/{_int_segment(serving_id, 'serving_id')}", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    # --- 쓰기: 태스크 ----------------------------------------------------------------

    def estimate_task(self, name: str, script: str, *, workspace: str, image: str | None = None,
                      template_id: int | None = None, requirements: str | None = None,
                      env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                      args: Iterable[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                      gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                      webhook_url: str | None = None, input_assets: Any = None,
                      max_price_per_hour: Any = None) -> TaskEstimate:
        body = _write.task_body(name, script, image=image, template_id=template_id, requirements=requirements, env=env,
                                secret_keys=secret_keys, args=args, gpu_model=gpu_model, gpu_count=gpu_count,
                                gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                                webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        return TaskEstimate.from_dict(self._send("POST", "/tasks/estimate",
                                                  params={"workspace": _query_value(workspace, "workspace")}, json=body))

    def submit_task(self, name: str, script: str, *, workspace: str, image: str | None = None,
                    template_id: int | None = None, requirements: str | None = None,
                    env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                    args: Iterable[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                    gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                    webhook_url: str | None = None, input_assets: Any = None, max_price_per_hour: Any = None,
                    idempotency_key: str | None = None) -> TaskSubmitted:
        """단발 태스크 제출(202). 로그를 남기려면 스크립트에서 print(..., flush=True) 를 쓴다(버퍼링 주의)."""
        body = _write.task_body(name, script, image=image, template_id=template_id, requirements=requirements, env=env,
                                secret_keys=secret_keys, args=args, gpu_model=gpu_model, gpu_count=gpu_count,
                                gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                                webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        return TaskSubmitted.from_dict(self._send("POST", "/tasks", params={"workspace": _query_value(workspace, "workspace")},
                                                   json=body, idempotency_key=idempotency_key))

    def stop_task(self, task_id: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = self._send("POST", f"/tasks/{_path_segment(task_id, 'task_id')}/stop", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="task")

    # --- 로그 (read 스코프) -----------------------------------------------------------

    def get_pod_logs(self, pod_name: str, workspace: str, *, tail: int = 200, container: str | None = None,
                     wait: float | None = None) -> Logs:
        """파드 로그 마지막 tail 줄. 버퍼가 비어 있으면 서버가 로그 워처를 깨워 최대 wait 초(기본 8) 기다린다."""
        params = _write.logs_params(tail=tail, wait=wait, container=container)
        params["workspace"] = _query_value(workspace, "workspace")
        return Logs.from_dict(self._get(f"/pods/{_path_segment(pod_name, 'pod_name')}/logs", params))

    def get_task_logs(self, task_id: str, *, tail: int = 200, wait: float | None = None,
                      cursor: int | None = None) -> Logs:
        """태스크 로그 마지막 tail 줄 (내부 태스크). 외부 provider 태스크는 cursor 로 증분 조회."""
        params = _write.logs_params(tail=tail, wait=wait, cursor=cursor)
        return Logs.from_dict(self._get(f"/tasks/{_path_segment(task_id, 'task_id')}/logs", params))

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
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, headers=headers)
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

    async def _send(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              json: dict[str, Any] | None = None, idempotency_key: str | None = None) -> Any:
        """쓰기 요청. Idempotency-Key 를 붙여 보내므로 재시도해도 서버가 첫 응답을 재생한다(이중 생성 없음)."""
        url = self._url(path)
        headers = self._build_headers()
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, url, params=params, json=json, headers=headers)
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

    # --- 쓰기: 파드 (write 스코프) ---------------------------------------------------

    async def estimate_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                     gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                     vcpu: int | None = None, ram_gb: int | None = None, disk_gb: int | None = None,
                     volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                     ports: Any = None, command: str | None = None, internet_premium: bool = False,
                     uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                     max_price_per_hour: Any = None) -> PodEstimate:
        """파드 견적 — 아무것도 만들지 않는다(read 스코프로 충분). create_pod 와 인자가 같다."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, disk_gb=disk_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour)
        return PodEstimate.from_dict(await self._send("POST", "/pods/estimate",
                                                 params={"workspace": _query_value(workspace, "workspace")}, json=body))

    async def create_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                   gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                   vcpu: int | None = None, ram_gb: int | None = None, disk_gb: int | None = None,
                   volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                   ports: Any = None, command: str | None = None, internet_premium: bool = False,
                   uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                   max_price_per_hour: Any = None, idempotency_key: str | None = None) -> PodCreated:
        """파드 생성(202 수락). 시간당 요금이 발생한다 — 먼저 estimate_pod 로 가격을 확인하고,
        max_price_per_hour 를 주면 견적이 그보다 높을 때 서버가 거절한다(ConflictError 'Price Exceeds Cap')."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, disk_gb=disk_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour)
        return PodCreated.from_dict(await self._send("POST", "/pods", params={"workspace": _query_value(workspace, "workspace")},
                                                json=body, idempotency_key=idempotency_key))

    async def stop_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """파드 정지(replicas=0). 파드 과금은 멈추고 스토리지 과금은 계속된다."""
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/stop",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def start_pod(self, pod_name: str, workspace: str, *, placement: str = "same_node",
                  idempotency_key: str | None = None) -> ResourceAction:
        """정지된 파드 시작. placement: same_node(원래 노드) | any_node(다른 노드로 재배치, 로컬 스토리지는 남음)."""
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/start",
                             params={"workspace": _query_value(workspace, "workspace"), "placement": _write.placement(placement)},
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def restart_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/restart",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def delete_pod(self, pod_name: str, workspace: str, *, delete_local_storages: Iterable[str] | None = None,
                   idempotency_key: str | None = None) -> ResourceAction:
        """파드 삭제. 로컬(hostPath) 스토리지는 delete_local_storages 에 pv_name 을 적은 것만 같이 삭제된다."""
        data = await self._send("DELETE", f"/pods/{_path_segment(pod_name, 'pod_name')}",
                             params=_write.delete_pod_params(_query_value(workspace, "workspace"), delete_local_storages),
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    # --- 쓰기: 스토리지 ---------------------------------------------------------------

    async def estimate_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                         disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                         max_price_per_hour: Any = None) -> StorageEstimate:
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour)
        return StorageEstimate.from_dict(await self._send("POST", "/storages/estimate",
                                                     params={"workspace": _query_value(workspace, "workspace")}, json=body))

    async def create_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                       disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                       max_price_per_hour: Any = None, idempotency_key: str | None = None) -> StorageCreated:
        """스토리지(PV) 생성(202). 존재하는 동안 용량 기준으로 시간당 과금된다."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour)
        return StorageCreated.from_dict(await self._send("POST", "/storages", params={"workspace": _query_value(workspace, "workspace")},
                                                    json=body, idempotency_key=idempotency_key))

    async def delete_storage(self, storage_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """스토리지 삭제. 사용자 파드가 마운트 중이면 ConflictError('Storage In Use', raw.detail.linkedPods)."""
        data = await self._send("DELETE", f"/storages/{_path_segment(storage_name, 'storage_name')}",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="storage")

    # --- 쓰기: 서빙 -----------------------------------------------------------------

    async def deploy_serving(self, model_registration_id: int, *, workspace: str, price_cap_per_hour: Any,
                       min_replicas: int = 1, max_replicas: int = 3, autoscale: bool = True,
                       max_context_tokens: int | None = None, share_idle_capacity: bool = False,
                       idempotency_key: str | None = None) -> ResourceAction:
        """등록된 모델(registration id) 을 서빙으로 배포(201). 비용 상한 = price_cap_per_hour × max_replicas."""
        body = _write.serving_deploy_body(model_registration_id, price_cap_per_hour=price_cap_per_hour,
                                          min_replicas=min_replicas, max_replicas=max_replicas, autoscale=autoscale,
                                          max_context_tokens=max_context_tokens, share_idle_capacity=share_idle_capacity)
        data = await self._send("POST", "/servings", params={"workspace": _query_value(workspace, "workspace")}, json=body,
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    async def scale_serving(self, serving_id: int | str, *, min_replicas: int | None = None, max_replicas: int | None = None,
                      autoscale: bool | None = None, price_cap_per_hour: Any = None,
                      idempotency_key: str | None = None) -> ResourceAction:
        body = _write.serving_scale_body(min_replicas=min_replicas, max_replicas=max_replicas, autoscale=autoscale,
                                         price_cap_per_hour=price_cap_per_hour)
        data = await self._send("PATCH", f"/servings/{_int_segment(serving_id, 'serving_id')}/scale", json=body,
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    async def pause_serving(self, serving_id: int | str, *, paused: bool = True,
                      idempotency_key: str | None = None) -> ResourceAction:
        """paused=True 로 일시정지, False 로 재개."""
        data = await self._send("PATCH", f"/servings/{_int_segment(serving_id, 'serving_id')}/pause",
                             json={"paused": bool(paused)}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    async def delete_serving(self, serving_id: int | str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = await self._send("DELETE", f"/servings/{_int_segment(serving_id, 'serving_id')}", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    # --- 쓰기: 태스크 ----------------------------------------------------------------

    async def estimate_task(self, name: str, script: str, *, workspace: str, image: str | None = None,
                      template_id: int | None = None, requirements: str | None = None,
                      env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                      args: Iterable[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                      gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                      webhook_url: str | None = None, input_assets: Any = None,
                      max_price_per_hour: Any = None) -> TaskEstimate:
        body = _write.task_body(name, script, image=image, template_id=template_id, requirements=requirements, env=env,
                                secret_keys=secret_keys, args=args, gpu_model=gpu_model, gpu_count=gpu_count,
                                gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                                webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        return TaskEstimate.from_dict(await self._send("POST", "/tasks/estimate",
                                                  params={"workspace": _query_value(workspace, "workspace")}, json=body))

    async def submit_task(self, name: str, script: str, *, workspace: str, image: str | None = None,
                    template_id: int | None = None, requirements: str | None = None,
                    env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                    args: Iterable[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                    gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                    webhook_url: str | None = None, input_assets: Any = None, max_price_per_hour: Any = None,
                    idempotency_key: str | None = None) -> TaskSubmitted:
        """단발 태스크 제출(202). 로그를 남기려면 스크립트에서 print(..., flush=True) 를 쓴다(버퍼링 주의)."""
        body = _write.task_body(name, script, image=image, template_id=template_id, requirements=requirements, env=env,
                                secret_keys=secret_keys, args=args, gpu_model=gpu_model, gpu_count=gpu_count,
                                gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                                webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        return TaskSubmitted.from_dict(await self._send("POST", "/tasks", params={"workspace": _query_value(workspace, "workspace")},
                                                   json=body, idempotency_key=idempotency_key))

    async def stop_task(self, task_id: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = await self._send("POST", f"/tasks/{_path_segment(task_id, 'task_id')}/stop", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="task")

    # --- 로그 (read 스코프) -----------------------------------------------------------

    async def get_pod_logs(self, pod_name: str, workspace: str, *, tail: int = 200, container: str | None = None,
                     wait: float | None = None) -> Logs:
        """파드 로그 마지막 tail 줄. 버퍼가 비어 있으면 서버가 로그 워처를 깨워 최대 wait 초(기본 8) 기다린다."""
        params = _write.logs_params(tail=tail, wait=wait, container=container)
        params["workspace"] = _query_value(workspace, "workspace")
        return Logs.from_dict(await self._get(f"/pods/{_path_segment(pod_name, 'pod_name')}/logs", params))

    async def get_task_logs(self, task_id: str, *, tail: int = 200, wait: float | None = None,
                      cursor: int | None = None) -> Logs:
        """태스크 로그 마지막 tail 줄 (내부 태스크). 외부 provider 태스크는 cursor 로 증분 조회."""
        params = _write.logs_params(tail=tail, wait=wait, cursor=cursor)
        return Logs.from_dict(await self._get(f"/tasks/{_path_segment(task_id, 'task_id')}/logs", params))

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncMeshive":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
