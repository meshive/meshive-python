"""Meshive SDK clients (sync Meshive / async AsyncMeshive).

Both clients share the request building and response parsing logic (_build_headers, _process);
only the transport differs (httpx.Client vs httpx.AsyncClient).

Authentication: Meshive API key (READ scope). `Authorization: Bearer meshive_...`.
Surface: the SDK read API — account (me/api-keys/credit/earnings),
workspaces (list/detail/members), pods (list/single/metrics), storage, machines (list/single/metrics),
GPU availability, templates, serverless (servings/tasks), assets (Asset Hub). All GET, so retries are safe.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
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
    AssetDownload,
    AssetImported,
    AssetPage,
    AssetStorage,
    DownloadFile,
    TaskOutputs,
    Credit,
    CreditHistoryEntry,
    Earnings,
    GpuAvailability,
    HfToken,
    Logs,
    Machine,
    MachineMetrics,
    Member,
    ModelDetection,
    Pod,
    PodCreated,
    PodEstimate,
    PodMetrics,
    ResourceAction,
    Serving,
    ServingModel,
    SshAccess,
    Storage,
    Transaction,
    WatchedFolders,
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


def _paths_param(paths: str | Iterable[str] | None) -> dict[str, Any] | None:
    """Asset file path globs (fnmatch) — repeated query `path=a&path=b`. Everything if omitted."""
    if not paths:
        return None
    values = [paths] if isinstance(paths, str) else [p for p in paths if p]
    return {"path": values} if values else None


def _not_supported(err: MeshiveAPIError, what: str) -> MeshiveAPIError:
    """Make responses from older servers without the route understandable — FastAPI's default 404 `{"detail": "Not Found"}` (no title), or
    the 405 when the same path only has a different method (POST /assets/import ↔ GET /assets/{id}). A 404 for a missing asset or task
    has a title, so it is left alone."""
    if not (err.status_code == 405 or (err.status_code == 404 and err.title is None)):
        return err
    return NotFoundError(404, f"This Meshive API server does not support {what} yet (it predates this SDK "
                              "release). Use the console for now.", raw=err.raw)


def _download_target(dest: str | os.PathLike[str], rel: str) -> Path:
    """The rel location under dest. Nothing is written if the server-provided path points outside dest (absolute path, `..`)."""
    root = Path(dest).resolve()
    target = (root / rel).resolve()
    if not rel or target == root or not target.is_relative_to(root):
        raise ValueError(f"refusing to write {rel!r} outside {root}")
    return target


def _download_failed(file: DownloadFile, status_code: int) -> MeshiveAPIError:
    return MeshiveAPIError(status_code, f"Downloading {file.path} failed (HTTP {status_code}). Download links "
                                        "expire; request new ones and try again.")


def _incomplete(download: AssetDownload) -> MeshiveError:
    return MeshiveError(f"Only {len(download.files)} of {download.expected_file_count} files of "
                        f"{download.asset_id} could be signed. Try again in a moment.")


def _path_segment(value: str, name: str) -> str:
    """Encode a URL path segment. Percent-encodes so values containing `/`, `?`, `#`, ... can't change the path structure
    (`../me` → a different endpoint) or inject a query."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return quote(value, safe="")


def _query_value(value: str, name: str) -> str:
    """Validate a string for a query parameter. httpx does the encoding, so it isn't done here (avoids double encoding)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _int_segment(value: int | str, name: str) -> str:
    """Integer ID (template/serving) path segment. bool is a subclass of int, so it's filtered out separately."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if number < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return str(number)


# --- Query parameter building (shared by the sync/async clients) -------------------
# The server surface takes camelCase queries (startDate/rentalType/appType). Values are validated here,
# before the round trip, and raise ValueError instead of returning a silent empty result.

_RENTAL_TYPES = ("demand", "spot")


def _iso_date(value: date | datetime | str, name: str) -> str:
    """date/datetime/'YYYY-MM-DD' → 'YYYY-MM-DD'. datetime is a subclass of date, so it's checked first."""
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
    """startDate/endDate query. An empty dict if both are None (server default: last 90 days)."""
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


# Cap on the exception message — even if a proxy/gateway returns a huge HTML page, exception messages
# and logs don't blow up. The full original is available as MeshiveAPIError.raw.
_MAX_ERROR_MESSAGE_LEN = 2000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_ERROR_MESSAGE_LEN:
        return text
    return text[:_MAX_ERROR_MESSAGE_LEN] + "… (truncated)"


def _extract_error(payload: Any) -> tuple[str | None, str]:
    """Extract (title, message) from the server's detail ({"title","message"}). Best-effort for other shapes."""
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
    # "inf"/"nan"/negative values also pass float() — protects callers that sleep on this value.
    return value if math.isfinite(value) and value >= 0 else None


def _raise_for_status(status_code: int, payload: Any, headers: httpx.Headers) -> None:
    """4xx/5xx → the matching MeshiveAPIError subclass."""
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


# --- Retries -----------------------------------------------------------------
# Only transient failures (rate limit / gateway errors / dropped connections) are retried. Other 4xx
# give the same result on retry, so they raise immediately. GET is idempotent, and write requests resend the same
# Idempotency-Key (the server replays the first response), so they can be retried without double creation.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException)
_RETRY_BACKOFF = 0.5  # 0.5s → 1s → 2s ...
# If Retry-After is longer than this, don't wait — raise RateLimitError as-is.
# A script silently stuck for minutes is worse than an error.
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
        # Extra headers (e.g. X-Meshive-Client sent by the MCP server). Can't override auth/Accept.
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
        """Retry wait time (seconds). None when it shouldn't retry.

        response=None means a network exception (no response at all).
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


# --- Shared wait_for_pod decisions --------------------------------------------
# Reaching these means the target state can't happen — fail right away instead of waiting out the timeout.
# (Values given as the target state are excluded in _wait_targets below.)
_POD_TERMINAL_STATUSES = frozenset({"error", "terminated"})


def _wait_targets(until: str | Iterable[str]) -> tuple[set[str], set[str]]:
    """until → (set of target states, set of states that fail immediately). Compared in lowercase."""
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
    """Synchronous Meshive SDK client.

        from meshive import Meshive

        client = Meshive()                 # uses MESHIVE_API_KEY / MESHIVE_BASE_URL
        me = client.me()
        for ws in client.list_workspaces():
            print(ws.namespace_name)

    Used as a context manager (`with Meshive() as client:`), connections are cleaned up automatically.
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
        """Write request. Sent with an Idempotency-Key, so on retry the server replays the first response (no double creation)."""
        url = self._url(path)
        headers = self._build_headers()
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        attempt = 0
        while True:
            try:
                response = self._client.request(method, url, params=params, json=json, headers=headers)
            except _RETRY_EXCEPTIONS as exc:
                delay = self._retry_delay(attempt)
                if delay is None:
                    exc.idempotency_key = headers["Idempotency-Key"]
                    exc.operation_method = method
                    exc.operation_path = path
                    raise
            else:
                delay = self._retry_delay(attempt, response)
                if delay is None:
                    try:
                        data = self._process(response)
                    except Exception as exc:
                        exc.idempotency_key = headers["Idempotency-Key"]
                        exc.operation_method = method
                        exc.operation_path = path
                        raise
                    if isinstance(data, dict):
                        data = {**data, "idempotencyKey": headers["Idempotency-Key"],
                                "operationMethod": method, "operationPath": path}
                    return data
            time.sleep(delay)
            attempt += 1

    def get_operation(self, operation_id: str, *, method: str, path: str) -> dict:
        """Lookup an earlier write by its key and SDK-relative path; this never resubmits work."""
        return self._get(f"/operations/{_path_segment(operation_id, 'operation_id')}",
                         params={"method": method, "path": path})

    def me(self) -> WhoAmI:
        """Info about the owner of the current API key (GET /me)."""
        return WhoAmI.from_dict(self._get("/me"))

    def list_workspaces(self) -> list[Workspace]:
        """My workspaces (GET /workspaces)."""
        return [Workspace.from_dict(d) for d in self._get("/workspaces")]

    def list_pods(self, workspace: str) -> list[Pod]:
        """Pods in a workspace (GET /pods?workspace=)."""
        data = self._get("/pods", params={"workspace": workspace})
        return [Pod.from_dict(d) for d in data.get("pods", [])]

    def get_pod(self, pod_name: str, workspace: str) -> Pod:
        """A single pod (GET /pods/{pod_name}?workspace=)."""
        segment = _path_segment(pod_name, "pod_name")
        return Pod.from_dict(self._get(f"/pods/{segment}", params={"workspace": workspace}))

    def list_machines(self) -> list[Machine]:
        """Machines registered as a host (GET /machines). No workspace needed (the host owns them directly)."""
        return [Machine.from_dict(d) for d in self._get("/machines")]

    def get_machine(self, machine_id: str) -> Machine:
        """A single machine (GET /machines/{machine_id})."""
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
        """Poll until the pod reaches the `until` state and return the Pod at that point.

            pod = client.wait_for_pod("pod-1", "my-workspace", until="running")

        On error/terminated it raises MeshiveError without waiting out the timeout.
        A timeout raises WaitTimeoutError (both a MeshiveError and a built-in TimeoutError).
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

    # --- 0.0.7 read surface extension ------------------------------------------------

    def get_workspace(self, workspace: str) -> WorkspaceDetail:
        """Workspace details — cost/resource summary (GET /workspaces/{namespace})."""
        return WorkspaceDetail.from_dict(
            self._get(f"/workspaces/{_path_segment(workspace, 'workspace')}"))

    def list_members(self, workspace: str) -> list[Member]:
        """Workspace members (GET /members?workspace=)."""
        data = self._get("/members", params={"workspace": workspace})
        return [Member.from_dict(d) for d in data.get("members", [])]

    def list_storages(self, workspace: str) -> list[Storage]:
        """Storage (volumes) in a workspace (GET /storages?workspace=)."""
        data = self._get("/storages", params={"workspace": workspace})
        return [Storage.from_dict(d) for d in data.get("storages", [])]

    def list_transactions(self, workspace: str) -> list[Transaction]:
        """In-progress pod operations (GET /transactions?workspace=).

        When a pod stays in `creating` for long, see why here — image pull progress,
        asset fetch, failure diagnostics. Finished operations drop out of the list, so **an empty list means "nothing in progress"**,
        not "no failures"."""
        return [Transaction.from_dict(d)
                for d in self._get("/transactions", params={"workspace": workspace})]

    def get_storage(self, storage_name: str, workspace: str) -> Storage:
        """A single storage (GET /storages/{storage_name}?workspace=). Same argument order as get_pod."""
        segment = _path_segment(storage_name, "storage_name")
        return Storage.from_dict(self._get(f"/storages/{segment}", params={"workspace": workspace}))

    def get_pod_metrics(self, pod_name: str, workspace: str) -> PodMetrics:
        """Pod resource usage (GET /pods/{pod_name}/metrics?workspace=)."""
        segment = _path_segment(pod_name, "pod_name")
        return PodMetrics.from_dict(
            self._get(f"/pods/{segment}/metrics", params={"workspace": workspace}))

    def get_machine_metrics(self, machine_id: str) -> MachineMetrics:
        """Live metrics of a host machine (GET /machines/{machine_id}/metrics)."""
        segment = _path_segment(machine_id, "machine_id")
        return MachineMetrics.from_dict(self._get(f"/machines/{segment}/metrics"))

    def list_gpus(self, *, rental_type: str = "demand",
                  min_vram: int | None = None) -> list[GpuAvailability]:
        """GPU tiers available to rent now, with prices (GET /gpus?rentalType=&vram=)."""
        return [GpuAvailability.from_dict(d)
                for d in self._get("/gpus", params=_gpus_params(rental_type, min_vram))]

    def list_api_keys(self) -> list[ApiKey]:
        """My active API keys — prefixes only, no plaintext (GET /api-keys)."""
        return [ApiKey.from_dict(d) for d in self._get("/api-keys")]

    def get_credit(self) -> Credit:
        """Credit balance + auto top-up settings (GET /credit)."""
        return Credit.from_dict(self._get("/credit"))

    def list_credit_history(self, *, start_date: date | datetime | str | None = None,
                            end_date: date | datetime | str | None = None) -> list[CreditHistoryEntry]:
        """Credit top-up/refund history (GET /credit/history). Defaults to the last 90 days."""
        data = self._get("/credit/history", params=_date_range_params(start_date, end_date))
        return [CreditHistoryEntry.from_dict(d) for d in data]

    def get_earnings(self, *, start_date: date | datetime | str | None = None,
                     end_date: date | datetime | str | None = None) -> Earnings:
        """Host earnings summary + daily breakdown (GET /earnings). Defaults to the last 90 days."""
        return Earnings.from_dict(self._get("/earnings", params=_date_range_params(start_date, end_date)))

    def list_templates(self, workspace: str | None = None, *,
                       app_type: str | None = None) -> list[Template]:
        """Official templates (+ that workspace's custom templates when workspace is given) (GET /templates)."""
        data = self._get("/templates", params=_templates_params(workspace, app_type))
        return [Template.from_dict(d) for d in data]

    def get_template(self, template_id: int | str, workspace: str | None = None) -> Template:
        """A single template (GET /templates/{template_id}). Pass workspace along for custom templates."""
        params = {"workspace": workspace} if workspace else None
        segment = _int_segment(template_id, "template_id")
        return Template.from_dict(self._get(f"/templates/{segment}", params=params))

    def list_servings(self, workspace: str) -> list[Serving]:
        """Serverless serving deployments in a workspace (GET /servings?workspace=)."""
        return [Serving.from_dict(d) for d in self._get("/servings", params={"workspace": workspace})]

    def get_serving(self, serving_id: int | str) -> Serving:
        """A single serving deployment (GET /servings/{serving_id})."""
        return Serving.from_dict(self._get(f"/servings/{_int_segment(serving_id, 'serving_id')}"))

    def list_tasks(self, workspace: str, *, status: str | Iterable[str] | None = None,
                   limit: int = 50, offset: int = 0) -> list[Task]:
        """Serverless tasks in a workspace, newest first (GET /tasks?workspace=&status=&limit=&offset=)."""
        data = self._get("/tasks", params=_tasks_params(workspace, status, limit, offset))
        return [Task.from_dict(d) for d in data]

    def get_task(self, task_id: str) -> Task:
        """A single task — script/settings/cost breakdown are in `.raw` (GET /tasks/{task_id})."""
        return Task.from_dict(self._get(f"/tasks/{_path_segment(task_id, 'task_id')}"))

    def list_assets(self, workspace: str, *, asset_type: str | None = None, status: str | None = None,
                    page: int = 1, page_size: int = 20) -> AssetPage:
        """One page of workspace assets (GET /assets?workspace=&assetType=&status=&page=&pageSize=).
        Without status, deleted/purged/merged are excluded."""
        data = self._get("/assets", params=_assets_params(workspace, asset_type, status, page, page_size))
        return AssetPage.from_dict(data, namespace_name=workspace)

    def get_asset(self, asset_id: str) -> Asset:
        """Asset details — including the file list and where it's in use (GET /assets/{asset_id})."""
        return Asset.from_dict(self._get(f"/assets/{_path_segment(asset_id, 'asset_id')}"))

    def get_asset_storage(self, workspace: str) -> AssetStorage:
        """Managed asset storage amount / estimated monthly cost / credit block state (GET /assets/storage-summary?workspace=)."""
        return AssetStorage.from_dict(self._get("/assets/storage-summary", params={"workspace": workspace}))

    # --- Downloads (read scope) ------------------------------------------------------

    def asset_download_urls(self, asset_id: str, *, paths: str | Iterable[str] | None = None) -> AssetDownload:
        """Presigned GET URLs per asset file (GET /assets/{asset_id}/download-urls?path=). `paths` is a list of file path
        globs (fnmatch) — there's a per-request file limit, so large assets are fetched in parts. Linked external assets can't be downloaded."""
        try:
            data = self._get(f"/assets/{_path_segment(asset_id, 'asset_id')}/download-urls", params=_paths_param(paths))
        except MeshiveAPIError as err:
            raise _not_supported(err, "asset downloads") from None
        return AssetDownload.from_dict(data, asset_id=asset_id)

    def download_asset(self, asset_id: str, dest: str | os.PathLike[str], *,
                       paths: str | Iterable[str] | None = None) -> list[Path]:
        """Download asset files under `dest`, keeping their relative paths inside the asset, and return the written paths (existing files are overwritten)."""
        download = self.asset_download_urls(asset_id, paths=paths)
        if not download.complete:
            raise _incomplete(download)
        return [self._save(f, dest) for f in download.files]

    def task_outputs(self, task_id: str) -> TaskOutputs:
        """Task output files and presigned URLs (GET /tasks/{task_id}/outputs)."""
        try:
            data = self._get(f"/tasks/{_path_segment(task_id, 'task_id')}/outputs")
        except MeshiveAPIError as err:
            raise _not_supported(err, "task outputs") from None
        return TaskOutputs.from_dict(data, task_id=task_id)

    def download_task_outputs(self, task_id: str, dest: str | os.PathLike[str]) -> list[Path]:
        """Download task outputs under `dest` and return the written paths."""
        return [self._save(f, dest) for f in self.task_outputs(task_id).files]

    def _save(self, file: DownloadFile, dest: str | os.PathLike[str]) -> Path:
        # Presigned URLs are sent without the auth header — the Meshive key never goes to storage.
        target = _download_target(dest, file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            with self._client.stream("GET", file.url) as response:
                if response.status_code >= 400:
                    raise _download_failed(file, response.status_code)
                with open(part, "wb") as fh:
                    for chunk in response.iter_bytes():
                        fh.write(chunk)
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return target

    # --- Writes: pods (write scope) ---------------------------------------------------

    def estimate_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                     gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                     vcpu: int | None = None, ram_gb: int | None = None,
                     volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                     ports: Any = None, command: str | None = None, internet_premium: bool = False,
                     uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                     max_price_per_hour: Any = None, input_assets: Any = None, watched_folders: Any = None,
                     harvest_destination: Any = None) -> PodEstimate:
        """Pod estimate — creates nothing (read scope is enough). Same arguments as create_pod."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination)
        return PodEstimate.from_dict(self._send("POST", "/pods/estimate",
                                                 params={"workspace": _query_value(workspace, "workspace")}, json=body))

    def create_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                   gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                   vcpu: int | None = None, ram_gb: int | None = None,
                   volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                   ports: Any = None, command: str | None = None, internet_premium: bool = False,
                   uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                   max_price_per_hour: Any = None, input_assets: Any = None, watched_folders: Any = None,
                   harvest_destination: Any = None, idempotency_key: str | None = None) -> PodCreated:
        """Create a pod (202 accepted). It is billed hourly — check the price with estimate_pod first;
        max_price_per_hour caps the final hourly compute price. Placement above it can fail asynchronously.
        Storage (including the automatic PV) and asset storage charges are separate and not covered by the cap.
        input_assets attaches Asset Hub assets ("asset_id" or {"asset", "target_dir", "role", "paths"}),
        watched_folders are folders whose new files are uploaded as assets ("path" or {"path", "include", "include_existing"})."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination)
        return PodCreated.from_dict(self._send("POST", "/pods", params={"workspace": _query_value(workspace, "workspace")},
                                                json=body, idempotency_key=idempotency_key))

    def stop_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Stop a pod (replicas=0). Pod billing stops; storage billing continues."""
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/stop",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def start_pod(self, pod_name: str, workspace: str, *, placement: str = "same_node",
                  allow_data_loss: bool = False, idempotency_key: str | None = None) -> ResourceAction:
        """Start a stopped pod. placement: same_node (original node) | any_node (moving nodes permanently deletes work files that aren't preserved).
        allow_data_loss=True is a separate data-loss consent for this pod/move request."""
        if not isinstance(allow_data_loss, bool):
            raise ValueError("allow_data_loss must be an explicit boolean")
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/start",
                             params={"workspace": _query_value(workspace, "workspace"), "placement": _write.placement(placement),
                                     "allow_data_loss": bool(allow_data_loss)},
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def restart_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/restart",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def delete_pod(self, pod_name: str, workspace: str, *, delete_local_storages: Iterable[str] | None = None,
                   idempotency_key: str | None = None) -> ResourceAction:
        """Delete a pod. Local (hostPath) storage is deleted along with it only if its pv_name is listed in delete_local_storages."""
        data = self._send("DELETE", f"/pods/{_path_segment(pod_name, 'pod_name')}",
                             params=_write.delete_pod_params(_query_value(workspace, "workspace"), delete_local_storages),
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    # --- Writes: storage ---------------------------------------------------------------

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
        """Create storage (PV) (202). Billed hourly by capacity while it exists."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour)
        return StorageCreated.from_dict(self._send("POST", "/storages", params={"workspace": _query_value(workspace, "workspace")},
                                                    json=body, idempotency_key=idempotency_key))

    def delete_storage(self, storage_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Delete storage. If a user pod has it mounted: ConflictError('Storage In Use', raw.detail.linkedPods)."""
        data = self._send("DELETE", f"/storages/{_path_segment(storage_name, 'storage_name')}",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="storage")

    # --- Writes: serving -----------------------------------------------------------------

    def deploy_serving(self, model_registration_id: int, *, workspace: str, price_cap_per_hour: Any,
                       min_replicas: int = 1, max_replicas: int = 3, autoscale: bool = True,
                       max_context_tokens: int | None = None, share_idle_capacity: bool = False,
                       idempotency_key: str | None = None) -> ResourceAction:
        """Deploy a registered model (registration id — list_models / register_model) as a serving (201).
        Cost cap = price_cap_per_hour × max_replicas."""
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
        """paused=True pauses, False resumes."""
        data = self._send("PATCH", f"/servings/{_int_segment(serving_id, 'serving_id')}/pause",
                             json={"paused": bool(paused)}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    def delete_serving(self, serving_id: int | str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = self._send("DELETE", f"/servings/{_int_segment(serving_id, 'serving_id')}", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    def ssh_access(self, pod_name: str, workspace: str) -> SshAccess:
        """One-time SSH access (write scope, expires after a few minutes) — connect with `command` and enter `password`.
        Each call returns a new password. Don't leave the password in logs or files."""
        try:
            data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/ssh",
                              params={"workspace": _query_value(workspace, "workspace")})
        except MeshiveAPIError as err:
            raise _not_supported(err, "SSH access") from None
        return SshAccess.from_dict(data)

    # --- Watched folders (harvest folders of a running Pod) ------------------------------------

    def get_watched_folders(self, pod_name: str, workspace: str) -> WatchedFolders:
        """A Pod's watched folders (GET /pods/{pod}/harvest?workspace=) — to change them, pass revision as expected_version."""
        try:
            data = self._get(f"/pods/{_path_segment(pod_name, 'pod_name')}/harvest", params={"workspace": workspace})
        except MeshiveAPIError as err:
            raise _not_supported(err, "watched folders") from None
        return WatchedFolders.from_dict(data)

    def set_watched_folders(self, pod_name: str, workspace: str, *, expected_version: int,
                            template: dict[str, Any] | None = None, user: Any = None,
                            idempotency_key: str | None = None) -> WatchedFolders:
        """**Replace all** watched folders (applies without a restart). template = {template folder path: {"enabled", "include"}},
        user = list of user folders ("path" or {"path", "include", "include_existing"}). 409 on a version mismatch — read again."""
        body = _write.watched_folders_body(expected_version, template, user)
        try:
            data = self._send("PUT", f"/pods/{_path_segment(pod_name, 'pod_name')}/harvest",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body,
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "watched folders") from None
        return WatchedFolders.from_dict(data)

    # --- Asset import ---------------------------------------------------------------

    def import_asset(self, target: str, *, workspace: str, name: str | None = None, asset_type: str | None = None,
                     revision: str | None = None, paths: str | Iterable[str] | None = None,
                     hf_token_id: int | None = None, civitai_key_id: int | None = None,
                     idempotency_key: str | None = None) -> AssetImported:
        """Register an HF repo (`owner/name` or URL), CivitAI URL or direct link as a linked asset (201, ready immediately). Bytes aren't copied, so
        there's no storage charge — Pods and tasks fetch from the source when they use it. For private/gated sources, pass the id of a token/key saved in the console."""
        body = _write.asset_import_body(target, name=name, asset_type=asset_type, revision=revision, paths=paths,
                                        hf_token_id=hf_token_id, civitai_key_id=civitai_key_id)
        try:
            data = self._send("POST", "/assets/import", params={"workspace": _query_value(workspace, "workspace")},
                              json=body, idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "asset import") from None
        return AssetImported.from_dict(data)

    def list_civitai_keys(self, workspace: str) -> list[HfToken]:
        """The workspace's CivitAI key ids and names (GET /civitai-keys?workspace=). Keys are added in the console."""
        try:
            return [HfToken.from_dict(d) for d in self._get("/civitai-keys", params={"workspace": workspace})]
        except MeshiveAPIError as err:
            raise _not_supported(err, "asset import") from None

    # --- Serving model registration ---------------------------------------------------------------

    def list_models(self, workspace: str) -> list[ServingModel]:
        """Serving models registered by the workspace (GET /models?workspace=) — the registration id for deploy_serving."""
        try:
            return [ServingModel.from_dict(d) for d in self._get("/models", params={"workspace": workspace})]
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None

    def list_hf_tokens(self, workspace: str) -> list[HfToken]:
        """The workspace's Hugging Face token ids and names (GET /hf-tokens?workspace=). Tokens are added in the console."""
        try:
            return [HfToken.from_dict(d) for d in self._get("/hf-tokens", params={"workspace": workspace})]
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None

    def detect_model(self, huggingface_repo: str, *, workspace: str, hf_token_id: int | None = None) -> ModelDetection:
        """Detect whether an HF repo can be served (POST /models/detect). Creates nothing (read scope)."""
        body = _write.model_body(huggingface_repo, hf_token_id=hf_token_id)
        try:
            data = self._send("POST", "/models/detect", params={"workspace": _query_value(workspace, "workspace")},
                              json=body)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None
        return ModelDetection.from_dict(data)

    def register_model(self, huggingface_repo: str, *, workspace: str, name: str | None = None,
                       framework: str | None = None, hf_token_id: int | None = None,
                       context_length: int | None = None, idempotency_key: str | None = None) -> ResourceAction:
        """Register a serving model (201) — `id` is the registration id. No cost (the download happens at deploy). The same repo returns the existing registration."""
        body = _write.model_body(huggingface_repo, hf_token_id=hf_token_id, name=name, framework=framework,
                                 context_length=context_length)
        try:
            data = self._send("POST", "/models", params={"workspace": _query_value(workspace, "workspace")}, json=body,
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None
        return ResourceAction.from_dict(data, resource="model")

    def delete_model(self, registration_id: int | str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Delete a registration — 409 if a deployment of that model is still alive."""
        try:
            data = self._send("DELETE", f"/models/{_int_segment(registration_id, 'registration_id')}",
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None
        return ResourceAction.from_dict(data, resource="model")

    # --- Writes: tasks ----------------------------------------------------------------

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
        """Submit a one-shot task (202). To get logs, use print(..., flush=True) in the script (mind buffering)."""
        body = _write.task_body(name, script, image=image, template_id=template_id, requirements=requirements, env=env,
                                secret_keys=secret_keys, args=args, gpu_model=gpu_model, gpu_count=gpu_count,
                                gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                                webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        return TaskSubmitted.from_dict(self._send("POST", "/tasks", params={"workspace": _query_value(workspace, "workspace")},
                                                   json=body, idempotency_key=idempotency_key))

    def stop_task(self, task_id: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = self._send("POST", f"/tasks/{_path_segment(task_id, 'task_id')}/stop", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="task")

    # --- Logs (read scope) -----------------------------------------------------------

    def get_pod_logs(self, pod_name: str, workspace: str, *, tail: int = 200, container: str | None = None,
                     wait: float | None = None) -> Logs:
        """The last tail lines of a pod's log. If the buffer is empty, the server wakes the log watcher and waits up to wait seconds (default 8)."""
        params = _write.logs_params(tail=tail, wait=wait, container=container)
        params["workspace"] = _query_value(workspace, "workspace")
        return Logs.from_dict(self._get(f"/pods/{_path_segment(pod_name, 'pod_name')}/logs", params))

    def get_task_logs(self, task_id: str, *, tail: int = 200, wait: float | None = None,
                      cursor: int | None = None) -> Logs:
        """The last tail lines of a task's log. For external-provider tasks, cursor=None/0 returns the last tail lines, and passing the response's
        next_cursor as cursor returns only lines added since (incremental). Internal tasks always return the last tail lines (no next_cursor)."""
        params = _write.logs_params(tail=tail, wait=wait, cursor=cursor)
        return Logs.from_dict(self._get(f"/tasks/{_path_segment(task_id, 'task_id')}/logs", params))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Meshive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncMeshive(_BaseClient):
    """Asynchronous Meshive SDK client (based on httpx.AsyncClient).

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
        """Write request. Sent with an Idempotency-Key, so on retry the server replays the first response (no double creation)."""
        url = self._url(path)
        headers = self._build_headers()
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, url, params=params, json=json, headers=headers)
            except _RETRY_EXCEPTIONS as exc:
                delay = self._retry_delay(attempt)
                if delay is None:
                    exc.idempotency_key = headers["Idempotency-Key"]
                    exc.operation_method = method
                    exc.operation_path = path
                    raise
            else:
                delay = self._retry_delay(attempt, response)
                if delay is None:
                    try:
                        data = self._process(response)
                    except Exception as exc:
                        exc.idempotency_key = headers["Idempotency-Key"]
                        exc.operation_method = method
                        exc.operation_path = path
                        raise
                    if isinstance(data, dict):
                        data = {**data, "idempotencyKey": headers["Idempotency-Key"],
                                "operationMethod": method, "operationPath": path}
                    return data
            await asyncio.sleep(delay)
            attempt += 1

    async def get_operation(self, operation_id: str, *, method: str, path: str) -> dict:
        """Lookup an earlier write by its key and SDK-relative path; this never resubmits work."""
        return await self._get(f"/operations/{_path_segment(operation_id, 'operation_id')}",
                               params={"method": method, "path": path})

    async def me(self) -> WhoAmI:
        """Info about the owner of the current API key (GET /me)."""
        return WhoAmI.from_dict(await self._get("/me"))

    async def list_workspaces(self) -> list[Workspace]:
        """My workspaces (GET /workspaces)."""
        return [Workspace.from_dict(d) for d in await self._get("/workspaces")]

    async def list_pods(self, workspace: str) -> list[Pod]:
        """Pods in a workspace (GET /pods?workspace=)."""
        data = await self._get("/pods", params={"workspace": workspace})
        return [Pod.from_dict(d) for d in data.get("pods", [])]

    async def get_pod(self, pod_name: str, workspace: str) -> Pod:
        """A single pod (GET /pods/{pod_name}?workspace=)."""
        segment = _path_segment(pod_name, "pod_name")
        data = await self._get(f"/pods/{segment}", params={"workspace": workspace})
        return Pod.from_dict(data)

    async def list_machines(self) -> list[Machine]:
        """Machines registered as a host (GET /machines). No workspace needed (the host owns them directly)."""
        return [Machine.from_dict(d) for d in await self._get("/machines")]

    async def get_machine(self, machine_id: str) -> Machine:
        """A single machine (GET /machines/{machine_id})."""
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
        """Poll until the pod reaches the `until` state (same rules as the sync wait_for_pod)."""
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

    # --- 0.0.7 read surface extension (same rules as the sync client) ---------------------------

    async def get_workspace(self, workspace: str) -> WorkspaceDetail:
        """Workspace details (GET /workspaces/{namespace})."""
        return WorkspaceDetail.from_dict(
            await self._get(f"/workspaces/{_path_segment(workspace, 'workspace')}"))

    async def list_members(self, workspace: str) -> list[Member]:
        """Workspace members (GET /members?workspace=)."""
        data = await self._get("/members", params={"workspace": workspace})
        return [Member.from_dict(d) for d in data.get("members", [])]

    async def list_storages(self, workspace: str) -> list[Storage]:
        """Storage (volumes) in a workspace (GET /storages?workspace=)."""
        data = await self._get("/storages", params={"workspace": workspace})
        return [Storage.from_dict(d) for d in data.get("storages", [])]

    async def list_transactions(self, workspace: str) -> list[Transaction]:
        """In-progress pod operations (GET /transactions?workspace=). Same contract as the sync client."""
        data = await self._get("/transactions", params={"workspace": workspace})
        return [Transaction.from_dict(d) for d in data]

    async def get_storage(self, storage_name: str, workspace: str) -> Storage:
        """A single storage (GET /storages/{storage_name}?workspace=)."""
        segment = _path_segment(storage_name, "storage_name")
        data = await self._get(f"/storages/{segment}", params={"workspace": workspace})
        return Storage.from_dict(data)

    async def get_pod_metrics(self, pod_name: str, workspace: str) -> PodMetrics:
        """Pod resource usage (GET /pods/{pod_name}/metrics?workspace=)."""
        segment = _path_segment(pod_name, "pod_name")
        data = await self._get(f"/pods/{segment}/metrics", params={"workspace": workspace})
        return PodMetrics.from_dict(data)

    async def get_machine_metrics(self, machine_id: str) -> MachineMetrics:
        """Live metrics of a host machine (GET /machines/{machine_id}/metrics)."""
        segment = _path_segment(machine_id, "machine_id")
        return MachineMetrics.from_dict(await self._get(f"/machines/{segment}/metrics"))

    async def list_gpus(self, *, rental_type: str = "demand",
                        min_vram: int | None = None) -> list[GpuAvailability]:
        """GPU tiers available to rent now, with prices (GET /gpus?rentalType=&vram=)."""
        data = await self._get("/gpus", params=_gpus_params(rental_type, min_vram))
        return [GpuAvailability.from_dict(d) for d in data]

    async def list_api_keys(self) -> list[ApiKey]:
        """My active API keys — prefixes only, no plaintext (GET /api-keys)."""
        return [ApiKey.from_dict(d) for d in await self._get("/api-keys")]

    async def get_credit(self) -> Credit:
        """Credit balance + auto top-up settings (GET /credit)."""
        return Credit.from_dict(await self._get("/credit"))

    async def list_credit_history(self, *, start_date: date | datetime | str | None = None,
                                  end_date: date | datetime | str | None = None) -> list[CreditHistoryEntry]:
        """Credit top-up/refund history (GET /credit/history). Defaults to the last 90 days."""
        data = await self._get("/credit/history", params=_date_range_params(start_date, end_date))
        return [CreditHistoryEntry.from_dict(d) for d in data]

    async def get_earnings(self, *, start_date: date | datetime | str | None = None,
                           end_date: date | datetime | str | None = None) -> Earnings:
        """Host earnings summary + daily breakdown (GET /earnings). Defaults to the last 90 days."""
        data = await self._get("/earnings", params=_date_range_params(start_date, end_date))
        return Earnings.from_dict(data)

    async def list_templates(self, workspace: str | None = None, *,
                             app_type: str | None = None) -> list[Template]:
        """Official templates (+ custom templates when workspace is given) (GET /templates)."""
        data = await self._get("/templates", params=_templates_params(workspace, app_type))
        return [Template.from_dict(d) for d in data]

    async def get_template(self, template_id: int | str, workspace: str | None = None) -> Template:
        """A single template (GET /templates/{template_id}). Pass workspace along for custom templates."""
        params = {"workspace": workspace} if workspace else None
        segment = _int_segment(template_id, "template_id")
        return Template.from_dict(await self._get(f"/templates/{segment}", params=params))

    async def list_servings(self, workspace: str) -> list[Serving]:
        """Serverless serving deployments in a workspace (GET /servings?workspace=)."""
        data = await self._get("/servings", params={"workspace": workspace})
        return [Serving.from_dict(d) for d in data]

    async def get_serving(self, serving_id: int | str) -> Serving:
        """A single serving deployment (GET /servings/{serving_id})."""
        segment = _int_segment(serving_id, "serving_id")
        return Serving.from_dict(await self._get(f"/servings/{segment}"))

    async def list_tasks(self, workspace: str, *, status: str | Iterable[str] | None = None,
                         limit: int = 50, offset: int = 0) -> list[Task]:
        """Serverless tasks in a workspace, newest first (GET /tasks?...)."""
        data = await self._get("/tasks", params=_tasks_params(workspace, status, limit, offset))
        return [Task.from_dict(d) for d in data]

    async def get_task(self, task_id: str) -> Task:
        """A single task — script/settings/cost breakdown are in `.raw` (GET /tasks/{task_id})."""
        return Task.from_dict(await self._get(f"/tasks/{_path_segment(task_id, 'task_id')}"))

    async def list_assets(self, workspace: str, *, asset_type: str | None = None,
                          status: str | None = None, page: int = 1, page_size: int = 20) -> AssetPage:
        """One page of workspace assets (GET /assets?...). Without status, deleted/purged/merged are excluded."""
        data = await self._get("/assets", params=_assets_params(workspace, asset_type, status, page, page_size))
        return AssetPage.from_dict(data, namespace_name=workspace)

    async def get_asset(self, asset_id: str) -> Asset:
        """Asset details — including the file list and where it's in use (GET /assets/{asset_id})."""
        return Asset.from_dict(await self._get(f"/assets/{_path_segment(asset_id, 'asset_id')}"))

    async def get_asset_storage(self, workspace: str) -> AssetStorage:
        """Managed asset storage amount / estimated monthly cost / credit block state (GET /assets/storage-summary?workspace=)."""
        data = await self._get("/assets/storage-summary", params={"workspace": workspace})
        return AssetStorage.from_dict(data)

    # --- Downloads (read scope) ------------------------------------------------------

    async def asset_download_urls(self, asset_id: str, *,
                                  paths: str | Iterable[str] | None = None) -> AssetDownload:
        """Presigned GET URLs per asset file (GET /assets/{asset_id}/download-urls?path=)."""
        try:
            data = await self._get(f"/assets/{_path_segment(asset_id, 'asset_id')}/download-urls",
                                   params=_paths_param(paths))
        except MeshiveAPIError as err:
            raise _not_supported(err, "asset downloads") from None
        return AssetDownload.from_dict(data, asset_id=asset_id)

    async def download_asset(self, asset_id: str, dest: str | os.PathLike[str], *,
                             paths: str | Iterable[str] | None = None) -> list[Path]:
        """Download asset files under `dest`, keeping their relative paths, and return the written paths."""
        download = await self.asset_download_urls(asset_id, paths=paths)
        if not download.complete:
            raise _incomplete(download)
        return [await self._save(f, dest) for f in download.files]

    async def task_outputs(self, task_id: str) -> TaskOutputs:
        """Task output files and presigned URLs (GET /tasks/{task_id}/outputs)."""
        try:
            data = await self._get(f"/tasks/{_path_segment(task_id, 'task_id')}/outputs")
        except MeshiveAPIError as err:
            raise _not_supported(err, "task outputs") from None
        return TaskOutputs.from_dict(data, task_id=task_id)

    async def download_task_outputs(self, task_id: str, dest: str | os.PathLike[str]) -> list[Path]:
        """Download task outputs under `dest` and return the written paths."""
        return [await self._save(f, dest) for f in (await self.task_outputs(task_id)).files]

    async def _save(self, file: DownloadFile, dest: str | os.PathLike[str]) -> Path:
        # Presigned URLs are sent without the auth header — the Meshive key never goes to storage.
        target = _download_target(dest, file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            async with self._client.stream("GET", file.url) as response:
                if response.status_code >= 400:
                    raise _download_failed(file, response.status_code)
                with open(part, "wb") as fh:
                    async for chunk in response.aiter_bytes():
                        fh.write(chunk)
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return target

    # --- Writes: pods (write scope) ---------------------------------------------------

    async def estimate_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                     gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                     vcpu: int | None = None, ram_gb: int | None = None,
                     volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                     ports: Any = None, command: str | None = None, internet_premium: bool = False,
                     uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                     max_price_per_hour: Any = None, input_assets: Any = None, watched_folders: Any = None,
                     harvest_destination: Any = None) -> PodEstimate:
        """Pod estimate — creates nothing (read scope is enough). Same arguments as create_pod."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination)
        return PodEstimate.from_dict(await self._send("POST", "/pods/estimate",
                                                 params={"workspace": _query_value(workspace, "workspace")}, json=body))

    async def create_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                   gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                   vcpu: int | None = None, ram_gb: int | None = None,
                   volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                   ports: Any = None, command: str | None = None, internet_premium: bool = False,
                   uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                   max_price_per_hour: Any = None, input_assets: Any = None, watched_folders: Any = None,
                   harvest_destination: Any = None, idempotency_key: str | None = None) -> PodCreated:
        """Create a pod (202 accepted). It is billed hourly — check the price with estimate_pod first;
        max_price_per_hour caps the final hourly compute price. Placement above it can fail asynchronously.
        Storage (including the automatic PV) and asset storage charges are separate and not covered by the cap.
        input_assets attaches Asset Hub assets ("asset_id" or {"asset", "target_dir", "role", "paths"}),
        watched_folders are folders whose new files are uploaded as assets ("path" or {"path", "include", "include_existing"})."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination)
        return PodCreated.from_dict(await self._send("POST", "/pods", params={"workspace": _query_value(workspace, "workspace")},
                                                json=body, idempotency_key=idempotency_key))

    async def stop_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Stop a pod (replicas=0). Pod billing stops; storage billing continues."""
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/stop",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def start_pod(self, pod_name: str, workspace: str, *, placement: str = "same_node",
                  allow_data_loss: bool = False, idempotency_key: str | None = None) -> ResourceAction:
        """Start a stopped pod. placement: same_node (original node) | any_node (moving nodes permanently deletes work files that aren't preserved).
        allow_data_loss=True is a separate data-loss consent for this pod/move request."""
        if not isinstance(allow_data_loss, bool):
            raise ValueError("allow_data_loss must be an explicit boolean")
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/start",
                             params={"workspace": _query_value(workspace, "workspace"), "placement": _write.placement(placement),
                                     "allow_data_loss": bool(allow_data_loss)},
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def restart_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/restart",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def delete_pod(self, pod_name: str, workspace: str, *, delete_local_storages: Iterable[str] | None = None,
                   idempotency_key: str | None = None) -> ResourceAction:
        """Delete a pod. Local (hostPath) storage is deleted along with it only if its pv_name is listed in delete_local_storages."""
        data = await self._send("DELETE", f"/pods/{_path_segment(pod_name, 'pod_name')}",
                             params=_write.delete_pod_params(_query_value(workspace, "workspace"), delete_local_storages),
                             idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    # --- Writes: storage ---------------------------------------------------------------

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
        """Create storage (PV) (202). Billed hourly by capacity while it exists."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour)
        return StorageCreated.from_dict(await self._send("POST", "/storages", params={"workspace": _query_value(workspace, "workspace")},
                                                    json=body, idempotency_key=idempotency_key))

    async def delete_storage(self, storage_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Delete storage. If a user pod has it mounted: ConflictError('Storage In Use', raw.detail.linkedPods)."""
        data = await self._send("DELETE", f"/storages/{_path_segment(storage_name, 'storage_name')}",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="storage")

    # --- Writes: serving -----------------------------------------------------------------

    async def deploy_serving(self, model_registration_id: int, *, workspace: str, price_cap_per_hour: Any,
                       min_replicas: int = 1, max_replicas: int = 3, autoscale: bool = True,
                       max_context_tokens: int | None = None, share_idle_capacity: bool = False,
                       idempotency_key: str | None = None) -> ResourceAction:
        """Deploy a registered model (registration id — list_models / register_model) as a serving (201).
        Cost cap = price_cap_per_hour × max_replicas."""
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
        """paused=True pauses, False resumes."""
        data = await self._send("PATCH", f"/servings/{_int_segment(serving_id, 'serving_id')}/pause",
                             json={"paused": bool(paused)}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    async def delete_serving(self, serving_id: int | str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = await self._send("DELETE", f"/servings/{_int_segment(serving_id, 'serving_id')}", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="serving")

    async def ssh_access(self, pod_name: str, workspace: str) -> SshAccess:
        """One-time SSH access (write scope, expires after a few minutes)."""
        try:
            data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/ssh",
                                    params={"workspace": _query_value(workspace, "workspace")})
        except MeshiveAPIError as err:
            raise _not_supported(err, "SSH access") from None
        return SshAccess.from_dict(data)

    # --- Watched folders (harvest folders of a running Pod) ------------------------------------

    async def get_watched_folders(self, pod_name: str, workspace: str) -> WatchedFolders:
        """A Pod's watched folders (GET /pods/{pod}/harvest?workspace=)."""
        try:
            data = await self._get(f"/pods/{_path_segment(pod_name, 'pod_name')}/harvest",
                                   params={"workspace": workspace})
        except MeshiveAPIError as err:
            raise _not_supported(err, "watched folders") from None
        return WatchedFolders.from_dict(data)

    async def set_watched_folders(self, pod_name: str, workspace: str, *, expected_version: int,
                                  template: dict[str, Any] | None = None, user: Any = None,
                                  idempotency_key: str | None = None) -> WatchedFolders:
        """Replace all watched folders (applies without a restart). 409 on a version mismatch."""
        body = _write.watched_folders_body(expected_version, template, user)
        try:
            data = await self._send("PUT", f"/pods/{_path_segment(pod_name, 'pod_name')}/harvest",
                                    params={"workspace": _query_value(workspace, "workspace")}, json=body,
                                    idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "watched folders") from None
        return WatchedFolders.from_dict(data)

    # --- Asset import ---------------------------------------------------------------

    async def import_asset(self, target: str, *, workspace: str, name: str | None = None,
                           asset_type: str | None = None, revision: str | None = None,
                           paths: str | Iterable[str] | None = None, hf_token_id: int | None = None,
                           civitai_key_id: int | None = None, idempotency_key: str | None = None) -> AssetImported:
        """Register an HF repo, CivitAI URL or direct link as a linked asset (201, ready immediately)."""
        body = _write.asset_import_body(target, name=name, asset_type=asset_type, revision=revision, paths=paths,
                                        hf_token_id=hf_token_id, civitai_key_id=civitai_key_id)
        try:
            data = await self._send("POST", "/assets/import",
                                    params={"workspace": _query_value(workspace, "workspace")}, json=body,
                                    idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "asset import") from None
        return AssetImported.from_dict(data)

    async def list_civitai_keys(self, workspace: str) -> list[HfToken]:
        """The workspace's CivitAI key ids and names (GET /civitai-keys?workspace=)."""
        try:
            return [HfToken.from_dict(d) for d in await self._get("/civitai-keys", params={"workspace": workspace})]
        except MeshiveAPIError as err:
            raise _not_supported(err, "asset import") from None

    # --- Serving model registration ---------------------------------------------------------------

    async def list_models(self, workspace: str) -> list[ServingModel]:
        """Serving models registered by the workspace (GET /models?workspace=)."""
        try:
            return [ServingModel.from_dict(d) for d in await self._get("/models", params={"workspace": workspace})]
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None

    async def list_hf_tokens(self, workspace: str) -> list[HfToken]:
        """The workspace's Hugging Face token ids and names (GET /hf-tokens?workspace=)."""
        try:
            return [HfToken.from_dict(d) for d in await self._get("/hf-tokens", params={"workspace": workspace})]
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None

    async def detect_model(self, huggingface_repo: str, *, workspace: str,
                           hf_token_id: int | None = None) -> ModelDetection:
        """Detect whether an HF repo can be served (POST /models/detect)."""
        body = _write.model_body(huggingface_repo, hf_token_id=hf_token_id)
        try:
            data = await self._send("POST", "/models/detect",
                                    params={"workspace": _query_value(workspace, "workspace")}, json=body)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None
        return ModelDetection.from_dict(data)

    async def register_model(self, huggingface_repo: str, *, workspace: str, name: str | None = None,
                             framework: str | None = None, hf_token_id: int | None = None,
                             context_length: int | None = None, idempotency_key: str | None = None) -> ResourceAction:
        """Register a serving model (201) — `id` is the registration id."""
        body = _write.model_body(huggingface_repo, hf_token_id=hf_token_id, name=name, framework=framework,
                                 context_length=context_length)
        try:
            data = await self._send("POST", "/models", params={"workspace": _query_value(workspace, "workspace")},
                                    json=body, idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None
        return ResourceAction.from_dict(data, resource="model")

    async def delete_model(self, registration_id: int | str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Delete a registration — 409 if a deployment of that model is still alive."""
        try:
            data = await self._send("DELETE", f"/models/{_int_segment(registration_id, 'registration_id')}",
                                    idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model registration") from None
        return ResourceAction.from_dict(data, resource="model")

    # --- Writes: tasks ----------------------------------------------------------------

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
        """Submit a one-shot task (202). To get logs, use print(..., flush=True) in the script (mind buffering)."""
        body = _write.task_body(name, script, image=image, template_id=template_id, requirements=requirements, env=env,
                                secret_keys=secret_keys, args=args, gpu_model=gpu_model, gpu_count=gpu_count,
                                gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                                webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        return TaskSubmitted.from_dict(await self._send("POST", "/tasks", params={"workspace": _query_value(workspace, "workspace")},
                                                   json=body, idempotency_key=idempotency_key))

    async def stop_task(self, task_id: str, *, idempotency_key: str | None = None) -> ResourceAction:
        data = await self._send("POST", f"/tasks/{_path_segment(task_id, 'task_id')}/stop", idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="task")

    # --- Logs (read scope) -----------------------------------------------------------

    async def get_pod_logs(self, pod_name: str, workspace: str, *, tail: int = 200, container: str | None = None,
                     wait: float | None = None) -> Logs:
        """The last tail lines of a pod's log. If the buffer is empty, the server wakes the log watcher and waits up to wait seconds (default 8)."""
        params = _write.logs_params(tail=tail, wait=wait, container=container)
        params["workspace"] = _query_value(workspace, "workspace")
        return Logs.from_dict(await self._get(f"/pods/{_path_segment(pod_name, 'pod_name')}/logs", params))

    async def get_task_logs(self, task_id: str, *, tail: int = 200, wait: float | None = None,
                      cursor: int | None = None) -> Logs:
        """The last tail lines of a task's log. For external-provider tasks, cursor=None/0 returns the last tail lines, and passing the response's
        next_cursor as cursor returns only lines added since (incremental). Internal tasks always return the last tail lines (no next_cursor)."""
        params = _write.logs_params(tail=tail, wait=wait, cursor=cursor)
        return Logs.from_dict(await self._get(f"/tasks/{_path_segment(task_id, 'task_id')}/logs", params))

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncMeshive":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
