"""Meshive SDK clients (sync Meshive / async AsyncMeshive).

Both clients share the request building and response parsing logic (_build_headers, _process);
only the transport differs (httpx.Client vs httpx.AsyncClient).

Authentication: Meshive API key, `Authorization: Bearer meshive_...`. Reads, estimates, logs and downloads need the
read scope; creating, changing or deleting pods, storage, servings, tasks, model registrations and asset imports, and
SSH access, need the write scope.
Read surface: account (me/api-keys/credit/earnings), workspaces (list/detail/members), pods (list/single/metrics,
transactions), storage, machines (list/single/metrics), GPU and CPU availability, templates and their model sets,
serverless (servings/tasks), assets (Asset Hub).
Retries are safe for both: GETs are idempotent, and writes resend the same Idempotency-Key, so the
server replays the first response instead of creating twice.
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
    PodCreationFailedError,
    RateLimitError,
    StorageConnectionError,
    WaitTimeoutError,
)
from .models import (
    ApiKey,
    Asset,
    AssetDownload,
    AssetImported,
    AssetPage,
    AssetStorage,
    CpuLine,
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
    ModelSetList,
    Pod,
    PodCreated,
    PodEstimate,
    PodMetrics,
    PodUpdated,
    PodUpdateEstimate,
    ResourceAction,
    Serving,
    ServingModel,
    SshAccess,
    Storage,
    StorageConnection,
    Transaction,
    WatchedFolders,
    StorageCreated,
    StorageEstimate,
    StorageUpdated,
    StorageUpdateEstimate,
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


def _cpus_params(rental_type: str) -> dict[str, Any]:
    rental = (rental_type or "").strip().lower()
    if rental not in _RENTAL_TYPES:
        raise ValueError(f"rental_type must be one of {', '.join(_RENTAL_TYPES)}")
    return {"rentalType": rental}


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

# A long-running write (create_pod with a model set: the server fetches the set's files before it answers) waits up
# to this long for the answer, and is resent automatically only when the server surely never started on it — the
# connection was never made, or the request was refused with 429/503. After a timeout or a gateway error the first
# request may still be running, and a resend with the same Idempotency-Key is refused with 409
# 'Operation Outcome Unknown' until it ends.
LONG_RUNNING_READ_TIMEOUT = 900.0
_NOT_STARTED_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)
_NOT_STARTED_STATUSES = frozenset({429, 503})


def _may_resend(long_running: bool, *, exc: BaseException | None = None,
                response: httpx.Response | None = None) -> bool:
    """Whether a failed attempt may be resent at all (the retry budget and backoff are decided separately)."""
    if not long_running:
        return True
    if exc is not None:
        return isinstance(exc, _NOT_STARTED_EXCEPTIONS)
    return response is not None and response.status_code in _NOT_STARTED_STATUSES


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

    def _long_timeout(self) -> httpx.Timeout:
        """The client's timeout, with a read timeout of at least LONG_RUNNING_READ_TIMEOUT."""
        read = None if self._timeout is None else max(float(self._timeout), LONG_RUNNING_READ_TIMEOUT)
        return httpx.Timeout(self._timeout, read=read)

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
        if pod.creation_failure_reason:
            raise PodCreationFailedError(label, pod.creation_failure_reason)
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


def _new_pod_expired(deadline: float, name: str, timeout: float) -> None:
    if time.monotonic() >= deadline:
        raise WaitTimeoutError(f"Pod {name!r} did not appear within {timeout:g}s.")


# --- Shared connect_storage decisions ------------------------------------------

def _connection_ready(connection: StorageConnection, pv_name: str) -> bool:
    """True once the storage can be reached over SSH. Phase "error" raises; any other phase is still preparing."""
    phase = connection.phase.lower()
    if phase == "error":
        raise StorageConnectionError(pv_name, connection)
    return phase == "ready"


def _connection_expired(deadline: float, pv_name: str, timeout: float, connection: StorageConnection) -> None:
    if time.monotonic() >= deadline:
        raise WaitTimeoutError(
            f"Storage {pv_name} was not ready for SSH access within {timeout:g}s "
            f"(last phase: {connection.phase or '-'}). It keeps getting ready; connect again later."
        )


class _CreationWatch:
    """wait_for_new_pod(transaction_id=...) — follows the pod's creation transaction alongside the pod itself, so a
    creation that failed (or was cancelled by deleting the pod) ends the wait at once with the server's reason.

    It stops following when the transaction is done (the pod's own status takes over), when the transaction can't be
    read (404: an older server or an id outside the workspace — the wait falls back to watching the pod only), and when
    `until` names a terminal status (then a failed creation is what was waited for, not an error)."""

    def __init__(self, name: str, transaction_id: int | str | None, targets: set[str]) -> None:
        self.name = name
        self.transaction_id = None if transaction_id is None else int(_int_segment(transaction_id, "transaction_id"))
        self.active = self.transaction_id is not None and not targets & _POD_TERMINAL_STATUSES

    def unreadable(self) -> None:
        self.active = False

    def check(self, txn: Transaction) -> None:
        status = txn.status.lower()
        if status == "failed":
            raise PodCreationFailedError(self.name, f"creation {txn.failure_reason}")
        if status == "superseded":
            raise PodCreationFailedError(self.name, "the creation was cancelled because the pod was deleted")
        if status == "done":
            self.active = False


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
              json: dict[str, Any] | None = None, idempotency_key: str | None = None,
              long_running: bool = False) -> Any:
        """Write request. Sent with an Idempotency-Key, so on retry the server replays the first response (no double creation).
        long_running: a request the server may work on for minutes — a long read timeout, and resent only when the
        server surely never started on it (see LONG_RUNNING_READ_TIMEOUT)."""
        url = self._url(path)
        headers = self._build_headers()
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        timeout = self._long_timeout() if long_running else httpx.USE_CLIENT_DEFAULT
        attempt = 0
        while True:
            try:
                response = self._client.request(method, url, params=params, json=json, headers=headers,
                                                timeout=timeout)
            except _RETRY_EXCEPTIONS as exc:
                delay = self._retry_delay(attempt) if _may_resend(long_running, exc=exc) else None
                if delay is None:
                    exc.idempotency_key = headers["Idempotency-Key"]
                    exc.operation_method = method
                    exc.operation_path = path
                    raise
            else:
                delay = self._retry_delay(attempt, response) if _may_resend(long_running, response=response) else None
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

        On error/terminated it raises MeshiveError without waiting out the timeout (PodCreationFailedError,
        with the reason, when the pod was cleaned up because creating it failed).
        A timeout raises WaitTimeoutError (both a MeshiveError and a built-in TimeoutError).
        """
        return self._wait_for_pod(pod_name, workspace, until=until, timeout=timeout, interval=interval)

    def _wait_for_pod(self, pod_name: str, workspace: str, *, until: str | Iterable[str], timeout: float,
                      interval: float, watch: _CreationWatch | None = None) -> Pod:
        targets, terminal = _wait_targets(until)
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            self._check_creation(watch, workspace)
            pod = self.get_pod(pod_name, workspace)
            if _wait_reached(pod, targets, terminal, pod_name):
                return pod
            last = pod.status
            _wait_expired(deadline, pod_name, targets, last)
            time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    def _check_creation(self, watch: _CreationWatch | None, workspace: str) -> None:
        if watch is None or not watch.active:
            return
        try:
            txn = self.get_transaction(watch.transaction_id, workspace)
        except NotFoundError:
            watch.unreadable()
            return
        watch.check(txn)

    def wait_for_new_pod(
        self,
        name: str,
        workspace: str,
        *,
        until: str | Iterable[str] = "running",
        timeout: float = 600.0,
        interval: float = 5.0,
        transaction_id: int | str | None = None,
    ) -> Pod:
        """Wait for a pod just created with `create_pod(name, ...)` to appear and reach `until`, and return it.

            created = client.create_pod("my-pod", template_id, workspace="my-workspace", gpu_model="RTX 3060")
            pod = client.wait_for_new_pod("my-pod", "my-workspace", transaction_id=created.transaction_id)

        create_pod returns before the pod has an ID, so this finds it by name (user_alias), then waits like
        wait_for_pod. With `transaction_id` (create_pod's `transaction_id`) it also follows the creation on every
        poll: if the server records it as failed, or as cancelled because the pod was deleted, PodCreationFailedError
        is raised right away with the reason instead of waiting for the pod. Without it, or on a server that can't
        look the transaction up, only the pod is watched. Raises WaitTimeoutError if the pod doesn't appear or reach
        `until` within `timeout` seconds.
        """
        if not name:
            raise ValueError("name is required")  # an empty name would match system pods, which have no label
        watch = _CreationWatch(name, transaction_id, _wait_targets(until)[0])
        deadline = time.monotonic() + timeout
        while True:
            pod = next((p for p in self.list_pods(workspace) if p.user_alias == name), None)
            if pod is not None:
                return self._wait_for_pod(pod.pod_name, workspace, until=until,
                                          timeout=max(deadline - time.monotonic(), 0.0), interval=interval,
                                          watch=watch)
            self._check_creation(watch, workspace)
            _new_pod_expired(deadline, name, timeout)
            time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    # --- Read surface: workspaces, storage, metrics, account, templates, serverless, assets ---

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

    def get_transaction(self, transaction_id: int | str, workspace: str) -> Transaction:
        """One pod operation by id (GET /transactions/{transaction_id}?workspace=), such as create_pod's
        `transaction_id`. Unlike list_transactions there is no time window, so a finished or failed operation can be
        read later; `failure_reason` says why a failed one failed. NotFoundError for an id outside the workspace."""
        segment = _int_segment(transaction_id, "transaction_id")
        try:
            data = self._get(f"/transactions/{segment}", params={"workspace": _query_value(workspace, "workspace")})
        except MeshiveAPIError as err:
            raise _not_supported(err, "transaction lookup") from None
        return Transaction.from_dict(data)

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

    def list_cpus(self, *, rental_type: str = "demand") -> list[CpuLine]:
        """CPU lines available to rent now, with prices (GET /cpus?rentalType=). Pass a line's `cpu_line` to
        create_pod / estimate_pod to put a CPU pod on it."""
        try:
            data = self._get("/cpus", params=_cpus_params(rental_type))
        except MeshiveAPIError as err:
            raise _not_supported(err, "CPU lines") from None
        return [CpuLine.from_dict(d) for d in data]

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

    def list_model_sets(self, workspace: str, template_id: int | str) -> ModelSetList:
        """The model sets a template can start with — the console's model-set tiles (GET /model-sets?workspace=&templateId=).
        Pass a set's slug to create_pod(model_set=...); a pod takes one set. `selectable` is False, with `reason`, when
        the template adds its own set automatically or has no model folders."""
        params = {"workspace": _query_value(workspace, "workspace"), "templateId": _int_segment(template_id, "template_id")}
        try:
            data = self._get("/model-sets", params=params)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model sets") from None
        return ModelSetList.from_dict(data)

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
                     harvest_destination: Any = None, local_volumes: Any = None,
                     cpu_line: str | None = None, template_ports: Any = None,
                     model_set: str | None = None) -> PodEstimate:
        """Pod estimate — creates nothing (read scope is enough). Same arguments as create_pod; the price includes any
        new local_volumes (breakdown "local_storage"), and `model_set` describes the model set when one is given."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination, local_volumes=local_volumes,
                               cpu_line=cpu_line, template_ports=template_ports, model_set=model_set)
        return PodEstimate.from_dict(self._send("POST", "/pods/estimate",
                                                 params={"workspace": _query_value(workspace, "workspace")}, json=body))

    def create_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                   gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                   vcpu: int | None = None, ram_gb: int | None = None,
                   volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                   ports: Any = None, command: str | None = None, internet_premium: bool = False,
                   uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                   max_price_per_hour: Any = None, input_assets: Any = None, watched_folders: Any = None,
                   harvest_destination: Any = None, idempotency_key: str | None = None, local_volumes: Any = None,
                   cpu_line: str | None = None, template_ports: Any = None,
                   model_set: str | None = None) -> PodCreated:
        """Create a pod (202 accepted). It is billed hourly — check the price with estimate_pod first;
        max_price_per_hour caps the final hourly compute price. Placement above it can fail asynchronously.
        Storage (including the automatic PV) and asset storage charges are separate and not covered by the cap.
        volumes attaches existing network storage ((pv_name, mount_path) or {"storage", "mount_path"}); local_volumes
        creates new local storage on the pod's machine together with the pod ({"name", "mount_path", "size_gb", and
        optionally "usage_warning", "usage_warning_threshold", "encrypted"}). cpu_line puts a CPU pod on a line from
        list_cpus (default: the cheapest line that fits). region is deprecated and ignored.
        ports opens new ports; template_ports changes the ports the template already opens, as the console's deploy
        screen does: [{"port": 8080, "name": "ide", "new_port": 8443, "external": False}] — port is the template's
        port, and each other key, when given, renames it, renumbers it or exposes/hides it (template ports can't be
        removed). A port the template doesn't open is refused ('Port Not In Template'), and so is a clash with
        another port or name ('Port In Use', 'Port Name In Use').
        input_assets attaches Asset Hub assets ("asset_id" or {"asset", "target_dir", "role", "paths"}),
        watched_folders are folders whose new files are uploaded as assets ("path" or {"path", "include", "include_existing"}).
        Pass the result's transaction_id to wait_for_new_pod to learn right away if the creation fails.
        model_set starts the pod with one of the template's model sets (a slug from list_model_sets; one per pod). The
        server fetches the set's files before it answers, which can take minutes, so this request waits up to
        LONG_RUNNING_READ_TIMEOUT seconds for the answer and is not resent after a timeout: the first request may
        still be running, and a resend with the same idempotency_key is refused with ConflictError
        'Operation Outcome Unknown' until it ends. After a timeout the pod may still be created — check list_pods,
        or get_operation(err.idempotency_key, method="POST", path="/pods"), before trying again. If a file can't be
        fetched the pod isn't created (400 'Model Set Unavailable', raw["detail"]["failed"]); files already fetched
        stay in the workspace and are reused next time."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination, local_volumes=local_volumes,
                               cpu_line=cpu_line, template_ports=template_ports, model_set=model_set)
        return PodCreated.from_dict(self._send("POST", "/pods", params={"workspace": _query_value(workspace, "workspace")},
                                                json=body, idempotency_key=idempotency_key,
                                                long_running="modelSet" in body))

    def stop_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Stop a pod (replicas=0). Pod billing stops; storage billing continues."""
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/stop",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    def start_pod(self, pod_name: str, workspace: str, *, placement: str | None = None,
                  allow_data_loss: bool = False, idempotency_key: str | None = None) -> ResourceAction:
        """Start a stopped pod. placement: same_node (its original node) | any_node (any node with capacity) |
        wait_same_node / wait_any_node (the pod waits until capacity frees up, then starts). The server decides which
        placements exist and refuses an unknown one. Without a placement (the default) the server picks what the web
        console would: wait_any_node for a pod already waiting for any node; any_node, or wait_any_node if no node has
        room now, when its original node can no longer run its configuration; otherwise same_node, or wait_same_node
        if that node has no room now. The result's `placement` says which one was used.
        Moving to another node (any_node, wait_any_node) permanently deletes work files that aren't preserved;
        allow_data_loss=True is a separate data-loss consent for this pod/move request. Without it such a start is
        refused with ConflictError 'Data Loss Consent Required', whose raw["detail"]["placement"] names the placement."""
        data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/start",
                             params=_write.start_pod_params(_query_value(workspace, "workspace"), placement, allow_data_loss),
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

    def estimate_pod_update(self, pod_name: str, workspace: str, *, name: str | None = None,
                            template_id: int | None = None, rental_type: str | None = None, gpu_model: str | None = None,
                            gpu_vram_gb: int | None = None, gpu_count: int | None = None, vcpu: int | None = None,
                            ram_gb: int | None = None, cpu_line: str | None = None, env: dict[str, str] | None = None,
                            secret_keys: Iterable[str] | None = None, remove_env: Iterable[str] | None = None,
                            ports: Any = None, template_ports: Any = None, remove_ports: Iterable[int] | None = None,
                            command: str | None = None, volumes: Any = None, local_volumes: Any = None,
                            detach_storages: Iterable[str] | None = None, allow_node_migration: bool | None = None,
                            allow_data_loss: bool | None = None, drop_watched_folders: Iterable[str] | None = None,
                            max_price_per_hour: Any = None) -> PodUpdateEstimate:
        """What update_pod would change, whether a running pod restarts, and the new hourly price — changes nothing
        (read scope is enough). Same arguments as update_pod."""
        body = _write.pod_update_body(
            name=name, template_id=template_id, rental_type=rental_type, gpu_model=gpu_model, gpu_vram_gb=gpu_vram_gb,
            gpu_count=gpu_count, vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, env=env, secret_keys=secret_keys,
            remove_env=remove_env, ports=ports, template_ports=template_ports, remove_ports=remove_ports,
            command=command, volumes=volumes, local_volumes=local_volumes, detach_storages=detach_storages,
            allow_node_migration=allow_node_migration, allow_data_loss=allow_data_loss,
            drop_watched_folders=drop_watched_folders, max_price_per_hour=max_price_per_hour)
        try:
            data = self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/estimate",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body)
        except MeshiveAPIError as err:
            raise _not_supported(err, "pod editing") from None
        return PodUpdateEstimate.from_dict(data)

    def update_pod(self, pod_name: str, workspace: str, *, name: str | None = None,
                   template_id: int | None = None, rental_type: str | None = None, gpu_model: str | None = None,
                   gpu_vram_gb: int | None = None, gpu_count: int | None = None, vcpu: int | None = None,
                   ram_gb: int | None = None, cpu_line: str | None = None, env: dict[str, str] | None = None,
                   secret_keys: Iterable[str] | None = None, remove_env: Iterable[str] | None = None,
                   ports: Any = None, template_ports: Any = None, remove_ports: Iterable[int] | None = None,
                   command: str | None = None, volumes: Any = None, local_volumes: Any = None,
                   detach_storages: Iterable[str] | None = None, allow_node_migration: bool | None = None,
                   allow_data_loss: bool | None = None, drop_watched_folders: Iterable[str] | None = None,
                   max_price_per_hour: Any = None,
                   idempotency_key: str | None = None) -> PodUpdated:
        """Change a pod as the console's edit screen does (202 accepted). Only what you pass changes.

        name renames it; template_id moves it to another template (variables, ports and command then start from
        it); rental_type, gpu_model, gpu_vram_gb, gpu_count, vcpu, ram_gb and cpu_line change its hardware. env sets or adds environment variables (secret_keys marks some
        of them secret) and remove_env removes variables you added (not the template's). ports opens new ports,
        template_ports changes the template's ports (as in create_pod) and remove_ports closes ports you added.
        command replaces the command ("" goes back to the template's). volumes attaches existing storage — network
        storage, or local storage on the pod's current machine — local_volumes creates new local storage there, and
        detach_storages detaches storage by pv_name (its data stays). max_price_per_hour caps the new hourly price.

        Most changes restart a running pod; estimate_pod_update says so before you commit. A rename alone is applied
        in place without a restart (renamed_only, no transaction_id). Follow the change with
        get_transaction(transaction_id) (action "update").

        Some changes are refused until consent is given — ask the user, then call again with it:
        ConflictError 'Node Migration Required' (the pod's machine can't fit the change; allow_node_migration=True
        lets it move to another machine), ConflictError 'Data Loss Consent Required' (the move deletes the pod's
        unpreserved workspace files; allow_data_loss=True as well), and MeshiveAPIError 400 whose
        raw["detail"]["code"] is "harvest_roots_orphaned" (the change removes the watched folders in
        raw["detail"]["paths"]; pass them as drop_watched_folders). ConflictError 'Resource values need
        confirmation' (an older pod) asks for ram_gb to be passed explicitly. Other refusals ('Nothing To Change',
        'Pod Type Locked', 'Incompatible Hardware', 'Port In Use', 'Cannot Change Resources', ...) carry the server's
        explanation in the message."""
        body = _write.pod_update_body(
            name=name, template_id=template_id, rental_type=rental_type, gpu_model=gpu_model, gpu_vram_gb=gpu_vram_gb,
            gpu_count=gpu_count, vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, env=env, secret_keys=secret_keys,
            remove_env=remove_env, ports=ports, template_ports=template_ports, remove_ports=remove_ports,
            command=command, volumes=volumes, local_volumes=local_volumes, detach_storages=detach_storages,
            allow_node_migration=allow_node_migration, allow_data_loss=allow_data_loss,
            drop_watched_folders=drop_watched_folders, max_price_per_hour=max_price_per_hour)
        try:
            data = self._send("PATCH", f"/pods/{_path_segment(pod_name, 'pod_name')}",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body,
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "pod editing") from None
        return PodUpdated.from_dict(data)

    # --- Writes: storage ---------------------------------------------------------------

    def estimate_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                         disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                         max_price_per_hour: Any = None, usage_warning: bool | None = None,
                         usage_warning_threshold: float | None = None) -> StorageEstimate:
        """Storage estimate — creates nothing. Same arguments as create_storage."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour, usage_warning=usage_warning,
                                   usage_warning_threshold=usage_warning_threshold)
        return StorageEstimate.from_dict(self._send("POST", "/storages/estimate",
                                                     params={"workspace": _query_value(workspace, "workspace")}, json=body))

    def create_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                       disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                       max_price_per_hour: Any = None, idempotency_key: str | None = None,
                       usage_warning: bool | None = None,
                       usage_warning_threshold: float | None = None) -> StorageCreated:
        """Create network storage (PV) (202). Billed hourly by capacity while it exists; the server's minimum size is
        10 GiB. Local storage can't be created on its own (the server refuses storage_type "hostPath"); create it with
        a pod: create_pod(local_volumes=...). usage_warning=True emails you when the storage fills past
        usage_warning_threshold (a fraction in (0, 1], default 0.9), as the console's usage warning does; both are
        sent only when given. disk_type and region are deprecated and ignored (storage is always NVMe)."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour, usage_warning=usage_warning,
                                   usage_warning_threshold=usage_warning_threshold)
        return StorageCreated.from_dict(self._send("POST", "/storages", params={"workspace": _query_value(workspace, "workspace")},
                                                    json=body, idempotency_key=idempotency_key))

    def delete_storage(self, storage_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Delete storage. If a user pod has it mounted: ConflictError('Storage In Use', raw.detail.linkedPods)."""
        data = self._send("DELETE", f"/storages/{_path_segment(storage_name, 'storage_name')}",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="storage")

    def estimate_storage_update(self, pv_name: str, workspace: str, *, name: str | None = None,
                                size_gb: int | None = None, usage_warning: bool | None = None,
                                usage_warning_threshold: float | None = None) -> StorageUpdateEstimate:
        """What update_storage would do — the new size, price and usage warning next to the current ones. Changes
        nothing (read scope is enough). Same arguments as update_storage."""
        body = _write.storage_update_body(name=name, size_gb=size_gb, usage_warning=usage_warning,
                                          usage_warning_threshold=usage_warning_threshold)
        try:
            data = self._send("POST", f"/storages/{_path_segment(pv_name, 'pv_name')}/estimate",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body)
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage editing") from None
        return StorageUpdateEstimate.from_dict(data)

    def update_storage(self, pv_name: str, workspace: str, *, name: str | None = None,
                       size_gb: int | None = None, usage_warning: bool | None = None,
                       usage_warning_threshold: float | None = None,
                       idempotency_key: str | None = None) -> StorageUpdated:
        """Change a storage as the console does (202 accepted). Only what you pass changes: name renames it, size_gb
        grows it (GiB; storage can't shrink — 400 'Storage Cannot Shrink', raw["detail"]["currentSizeGb"]), and
        usage_warning / usage_warning_threshold turn its usage warning on or off as in create_storage. Growing is
        billed at the new size; check with estimate_storage_update first. A rename or a warning change alone is
        applied at once, and transaction_id is then None (or the id of an earlier operation)."""
        body = _write.storage_update_body(name=name, size_gb=size_gb, usage_warning=usage_warning,
                                          usage_warning_threshold=usage_warning_threshold)
        try:
            data = self._send("PATCH", f"/storages/{_path_segment(pv_name, 'pv_name')}",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body,
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage editing") from None
        return StorageUpdated.from_dict(data)

    def get_storage_connection(self, pv_name: str, workspace: str) -> StorageConnection:
        """Where a storage's SSH access stands (GET /storages/{pv}/connect): phase preparing | ready | error, the
        access pod and, for encrypted storage, when it locks again. `ssh` is not filled here — see connect_storage."""
        try:
            data = self._get(f"/storages/{_path_segment(pv_name, 'pv_name')}/connect",
                             params={"workspace": _query_value(workspace, "workspace")})
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage SSH access") from None
        return StorageConnection.from_dict(data)

    def connect_storage(self, pv_name: str, workspace: str, *, wait: bool = True, timeout: float = 300.0,
                        interval: float = 3.0, idempotency_key: str | None = None) -> StorageConnection:
        """Open a storage for SSH and SFTP, as the console's Connect does (write scope; safe to call again).

        Plain storage is reached through its always-on access pod. Encrypted storage is unlocked and gets an access
        pod, and it locks again automatically after `auto_lock_minutes`. With wait=True (the default) this polls every
        `interval` seconds until the storage is ready and then fills `ssh` with a one-time SSH command and password
        for the access pod (as ssh_access does — don't leave the password in logs). With wait=False it returns the
        status as it is, with `ssh` filled only if the storage is ready already. Raises StorageConnectionError when
        the server reports phase "error", and WaitTimeoutError when it isn't ready within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        try:
            data = self._send("POST", f"/storages/{_path_segment(pv_name, 'pv_name')}/connect",
                              params={"workspace": _query_value(workspace, "workspace")},
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage SSH access") from None
        connection = StorageConnection.from_dict(data)
        while not _connection_ready(connection, pv_name) and wait:
            _connection_expired(deadline, pv_name, timeout, connection)
            time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))
            connection = self.get_storage_connection(pv_name, workspace)
        if connection.ready and connection.pod_name:
            connection.ssh = self.ssh_access(connection.pod_name, workspace)
        return connection

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
              json: dict[str, Any] | None = None, idempotency_key: str | None = None,
              long_running: bool = False) -> Any:
        """Write request. Sent with an Idempotency-Key, so on retry the server replays the first response (no double creation).
        long_running: a request the server may work on for minutes — a long read timeout, and resent only when the
        server surely never started on it (see LONG_RUNNING_READ_TIMEOUT)."""
        url = self._url(path)
        headers = self._build_headers()
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        timeout = self._long_timeout() if long_running else httpx.USE_CLIENT_DEFAULT
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, url, params=params, json=json, headers=headers,
                                                timeout=timeout)
            except _RETRY_EXCEPTIONS as exc:
                delay = self._retry_delay(attempt) if _may_resend(long_running, exc=exc) else None
                if delay is None:
                    exc.idempotency_key = headers["Idempotency-Key"]
                    exc.operation_method = method
                    exc.operation_path = path
                    raise
            else:
                delay = self._retry_delay(attempt, response) if _may_resend(long_running, response=response) else None
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
        return await self._wait_for_pod(pod_name, workspace, until=until, timeout=timeout, interval=interval)

    async def _wait_for_pod(self, pod_name: str, workspace: str, *, until: str | Iterable[str], timeout: float,
                            interval: float, watch: _CreationWatch | None = None) -> Pod:
        targets, terminal = _wait_targets(until)
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            await self._check_creation(watch, workspace)
            pod = await self.get_pod(pod_name, workspace)
            if _wait_reached(pod, targets, terminal, pod_name):
                return pod
            last = pod.status
            _wait_expired(deadline, pod_name, targets, last)
            await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    async def _check_creation(self, watch: _CreationWatch | None, workspace: str) -> None:
        if watch is None or not watch.active:
            return
        try:
            txn = await self.get_transaction(watch.transaction_id, workspace)
        except NotFoundError:
            watch.unreadable()
            return
        watch.check(txn)

    async def wait_for_new_pod(
        self,
        name: str,
        workspace: str,
        *,
        until: str | Iterable[str] = "running",
        timeout: float = 600.0,
        interval: float = 5.0,
        transaction_id: int | str | None = None,
    ) -> Pod:
        """Wait for a pod just created with `create_pod(name, ...)` to appear and reach `until` (same rules as the
        sync wait_for_new_pod, including `transaction_id` to report a failed creation right away)."""
        if not name:
            raise ValueError("name is required")  # an empty name would match system pods, which have no label
        watch = _CreationWatch(name, transaction_id, _wait_targets(until)[0])
        deadline = time.monotonic() + timeout
        while True:
            pod = next((p for p in await self.list_pods(workspace) if p.user_alias == name), None)
            if pod is not None:
                return await self._wait_for_pod(pod.pod_name, workspace, until=until,
                                                timeout=max(deadline - time.monotonic(), 0.0), interval=interval,
                                                watch=watch)
            await self._check_creation(watch, workspace)
            _new_pod_expired(deadline, name, timeout)
            await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    # --- Read surface (same rules as the sync client) ---------------------------

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

    async def get_transaction(self, transaction_id: int | str, workspace: str) -> Transaction:
        """One pod operation by id (GET /transactions/{transaction_id}?workspace=). Same contract as the sync client."""
        segment = _int_segment(transaction_id, "transaction_id")
        try:
            data = await self._get(f"/transactions/{segment}",
                                   params={"workspace": _query_value(workspace, "workspace")})
        except MeshiveAPIError as err:
            raise _not_supported(err, "transaction lookup") from None
        return Transaction.from_dict(data)

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

    async def list_cpus(self, *, rental_type: str = "demand") -> list[CpuLine]:
        """CPU lines available to rent now, with prices (GET /cpus?rentalType=). Pass a line's `cpu_line` to create_pod."""
        try:
            data = await self._get("/cpus", params=_cpus_params(rental_type))
        except MeshiveAPIError as err:
            raise _not_supported(err, "CPU lines") from None
        return [CpuLine.from_dict(d) for d in data]

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

    async def list_model_sets(self, workspace: str, template_id: int | str) -> ModelSetList:
        """The model sets a template can start with (GET /model-sets?workspace=&templateId=). Same contract as the sync client."""
        params = {"workspace": _query_value(workspace, "workspace"), "templateId": _int_segment(template_id, "template_id")}
        try:
            data = await self._get("/model-sets", params=params)
        except MeshiveAPIError as err:
            raise _not_supported(err, "model sets") from None
        return ModelSetList.from_dict(data)

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
                     harvest_destination: Any = None, local_volumes: Any = None,
                     cpu_line: str | None = None, template_ports: Any = None,
                     model_set: str | None = None) -> PodEstimate:
        """Pod estimate — creates nothing (read scope is enough). Same arguments as create_pod; the price includes any
        new local_volumes (breakdown "local_storage"), and `model_set` describes the model set when one is given."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination, local_volumes=local_volumes,
                               cpu_line=cpu_line, template_ports=template_ports, model_set=model_set)
        return PodEstimate.from_dict(await self._send("POST", "/pods/estimate",
                                                 params={"workspace": _query_value(workspace, "workspace")}, json=body))

    async def create_pod(self, name: str, template_id: int, *, workspace: str, gpu_model: str | None = None,
                   gpu_count: int = 1, gpu_vram_gb: int | None = None, rental_type: str = "demand",
                   vcpu: int | None = None, ram_gb: int | None = None,
                   volumes: Any = None, env: dict[str, str] | None = None, secret_keys: Iterable[str] | None = None,
                   ports: Any = None, command: str | None = None, internet_premium: bool = False,
                   uptime_premium: bool = False, cpu_premium: bool = False, region: str | None = None,
                   max_price_per_hour: Any = None, input_assets: Any = None, watched_folders: Any = None,
                   harvest_destination: Any = None, idempotency_key: str | None = None, local_volumes: Any = None,
                   cpu_line: str | None = None, template_ports: Any = None,
                   model_set: str | None = None) -> PodCreated:
        """Create a pod (202 accepted). It is billed hourly — check the price with estimate_pod first;
        max_price_per_hour caps the final hourly compute price. Placement above it can fail asynchronously.
        Storage (including the automatic PV) and asset storage charges are separate and not covered by the cap.
        volumes attaches existing network storage ((pv_name, mount_path) or {"storage", "mount_path"}); local_volumes
        creates new local storage on the pod's machine together with the pod ({"name", "mount_path", "size_gb", and
        optionally "usage_warning", "usage_warning_threshold", "encrypted"}). cpu_line puts a CPU pod on a line from
        list_cpus (default: the cheapest line that fits). region is deprecated and ignored.
        ports opens new ports; template_ports changes the ports the template already opens, as the console's deploy
        screen does: [{"port": 8080, "name": "ide", "new_port": 8443, "external": False}] — port is the template's
        port, and each other key, when given, renames it, renumbers it or exposes/hides it (template ports can't be
        removed). A port the template doesn't open is refused ('Port Not In Template'), and so is a clash with
        another port or name ('Port In Use', 'Port Name In Use').
        input_assets attaches Asset Hub assets ("asset_id" or {"asset", "target_dir", "role", "paths"}),
        watched_folders are folders whose new files are uploaded as assets ("path" or {"path", "include", "include_existing"}).
        Pass the result's transaction_id to wait_for_new_pod to learn right away if the creation fails.
        model_set starts the pod with one of the template's model sets (a slug from list_model_sets; one per pod). The
        server fetches the set's files before it answers, which can take minutes, so this request waits up to
        LONG_RUNNING_READ_TIMEOUT seconds for the answer and is not resent after a timeout: the first request may
        still be running, and a resend with the same idempotency_key is refused with ConflictError
        'Operation Outcome Unknown' until it ends. After a timeout the pod may still be created — check list_pods,
        or get_operation(err.idempotency_key, method="POST", path="/pods"), before trying again. If a file can't be
        fetched the pod isn't created (400 'Model Set Unavailable', raw["detail"]["failed"]); files already fetched
        stay in the workspace and are reused next time."""
        body = _write.pod_body(name, template_id, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb,
                               rental_type=rental_type, vcpu=vcpu, ram_gb=ram_gb, volumes=volumes,
                               env=env, secret_keys=secret_keys, ports=ports, command=command,
                               internet_premium=internet_premium, uptime_premium=uptime_premium,
                               cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour,
                               input_assets=input_assets, watched_folders=watched_folders,
                               harvest_destination=harvest_destination, local_volumes=local_volumes,
                               cpu_line=cpu_line, template_ports=template_ports, model_set=model_set)
        return PodCreated.from_dict(await self._send("POST", "/pods", params={"workspace": _query_value(workspace, "workspace")},
                                                json=body, idempotency_key=idempotency_key,
                                                long_running="modelSet" in body))

    async def stop_pod(self, pod_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Stop a pod (replicas=0). Pod billing stops; storage billing continues."""
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/stop",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="pod")

    async def start_pod(self, pod_name: str, workspace: str, *, placement: str | None = None,
                  allow_data_loss: bool = False, idempotency_key: str | None = None) -> ResourceAction:
        """Start a stopped pod. placement: same_node (its original node) | any_node (any node with capacity) |
        wait_same_node / wait_any_node (the pod waits until capacity frees up, then starts). The server decides which
        placements exist and refuses an unknown one. Without a placement (the default) the server picks what the web
        console would: wait_any_node for a pod already waiting for any node; any_node, or wait_any_node if no node has
        room now, when its original node can no longer run its configuration; otherwise same_node, or wait_same_node
        if that node has no room now. The result's `placement` says which one was used.
        Moving to another node (any_node, wait_any_node) permanently deletes work files that aren't preserved;
        allow_data_loss=True is a separate data-loss consent for this pod/move request. Without it such a start is
        refused with ConflictError 'Data Loss Consent Required', whose raw["detail"]["placement"] names the placement."""
        data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/start",
                             params=_write.start_pod_params(_query_value(workspace, "workspace"), placement, allow_data_loss),
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

    async def estimate_pod_update(self, pod_name: str, workspace: str, *, name: str | None = None,
                                  template_id: int | None = None, rental_type: str | None = None, gpu_model: str | None = None,
                                  gpu_vram_gb: int | None = None, gpu_count: int | None = None, vcpu: int | None = None,
                                  ram_gb: int | None = None, cpu_line: str | None = None, env: dict[str, str] | None = None,
                                  secret_keys: Iterable[str] | None = None, remove_env: Iterable[str] | None = None,
                                  ports: Any = None, template_ports: Any = None, remove_ports: Iterable[int] | None = None,
                                  command: str | None = None, volumes: Any = None, local_volumes: Any = None,
                                  detach_storages: Iterable[str] | None = None, allow_node_migration: bool | None = None,
                                  allow_data_loss: bool | None = None, drop_watched_folders: Iterable[str] | None = None,
                                  max_price_per_hour: Any = None) -> PodUpdateEstimate:
        """What update_pod would change and cost (POST /pods/{pod}/estimate). Same contract as the sync client."""
        body = _write.pod_update_body(
            name=name, template_id=template_id, rental_type=rental_type, gpu_model=gpu_model, gpu_vram_gb=gpu_vram_gb,
            gpu_count=gpu_count, vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, env=env, secret_keys=secret_keys,
            remove_env=remove_env, ports=ports, template_ports=template_ports, remove_ports=remove_ports,
            command=command, volumes=volumes, local_volumes=local_volumes, detach_storages=detach_storages,
            allow_node_migration=allow_node_migration, allow_data_loss=allow_data_loss,
            drop_watched_folders=drop_watched_folders, max_price_per_hour=max_price_per_hour)
        try:
            data = await self._send("POST", f"/pods/{_path_segment(pod_name, 'pod_name')}/estimate",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body)
        except MeshiveAPIError as err:
            raise _not_supported(err, "pod editing") from None
        return PodUpdateEstimate.from_dict(data)

    async def update_pod(self, pod_name: str, workspace: str, *, name: str | None = None,
                         template_id: int | None = None, rental_type: str | None = None, gpu_model: str | None = None,
                         gpu_vram_gb: int | None = None, gpu_count: int | None = None, vcpu: int | None = None,
                         ram_gb: int | None = None, cpu_line: str | None = None, env: dict[str, str] | None = None,
                         secret_keys: Iterable[str] | None = None, remove_env: Iterable[str] | None = None,
                         ports: Any = None, template_ports: Any = None, remove_ports: Iterable[int] | None = None,
                         command: str | None = None, volumes: Any = None, local_volumes: Any = None,
                         detach_storages: Iterable[str] | None = None, allow_node_migration: bool | None = None,
                         allow_data_loss: bool | None = None, drop_watched_folders: Iterable[str] | None = None,
                         max_price_per_hour: Any = None,
                         idempotency_key: str | None = None) -> PodUpdated:
        """Change a pod (PATCH /pods/{pod}, 202). Same contract as the sync client."""
        body = _write.pod_update_body(
            name=name, template_id=template_id, rental_type=rental_type, gpu_model=gpu_model, gpu_vram_gb=gpu_vram_gb,
            gpu_count=gpu_count, vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, env=env, secret_keys=secret_keys,
            remove_env=remove_env, ports=ports, template_ports=template_ports, remove_ports=remove_ports,
            command=command, volumes=volumes, local_volumes=local_volumes, detach_storages=detach_storages,
            allow_node_migration=allow_node_migration, allow_data_loss=allow_data_loss,
            drop_watched_folders=drop_watched_folders, max_price_per_hour=max_price_per_hour)
        try:
            data = await self._send("PATCH", f"/pods/{_path_segment(pod_name, 'pod_name')}",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body,
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "pod editing") from None
        return PodUpdated.from_dict(data)

    # --- Writes: storage ---------------------------------------------------------------

    async def estimate_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                         disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                         max_price_per_hour: Any = None, usage_warning: bool | None = None,
                         usage_warning_threshold: float | None = None) -> StorageEstimate:
        """Storage estimate — creates nothing. Same arguments as create_storage."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour, usage_warning=usage_warning,
                                   usage_warning_threshold=usage_warning_threshold)
        return StorageEstimate.from_dict(await self._send("POST", "/storages/estimate",
                                                     params={"workspace": _query_value(workspace, "workspace")}, json=body))

    async def create_storage(self, name: str, size_gb: int, *, workspace: str, storage_type: str = "nfs",
                       disk_type: str = "NVMe", encrypted: bool = False, region: str | None = None,
                       max_price_per_hour: Any = None, idempotency_key: str | None = None,
                       usage_warning: bool | None = None,
                       usage_warning_threshold: float | None = None) -> StorageCreated:
        """Create network storage (PV) (202). Billed hourly by capacity while it exists; the server's minimum size is
        10 GiB. Local storage can't be created on its own (the server refuses storage_type "hostPath"); create it with
        a pod: create_pod(local_volumes=...). usage_warning=True emails you when the storage fills past
        usage_warning_threshold (a fraction in (0, 1], default 0.9), as the console's usage warning does; both are
        sent only when given. disk_type and region are deprecated and ignored (storage is always NVMe)."""
        body = _write.storage_body(name, size_gb, storage_type=storage_type, disk_type=disk_type, encrypted=encrypted,
                                   region=region, max_price_per_hour=max_price_per_hour, usage_warning=usage_warning,
                                   usage_warning_threshold=usage_warning_threshold)
        return StorageCreated.from_dict(await self._send("POST", "/storages", params={"workspace": _query_value(workspace, "workspace")},
                                                    json=body, idempotency_key=idempotency_key))

    async def delete_storage(self, storage_name: str, workspace: str, *, idempotency_key: str | None = None) -> ResourceAction:
        """Delete storage. If a user pod has it mounted: ConflictError('Storage In Use', raw.detail.linkedPods)."""
        data = await self._send("DELETE", f"/storages/{_path_segment(storage_name, 'storage_name')}",
                             params={"workspace": _query_value(workspace, "workspace")}, idempotency_key=idempotency_key)
        return ResourceAction.from_dict(data, resource="storage")

    async def estimate_storage_update(self, pv_name: str, workspace: str, *, name: str | None = None,
                                      size_gb: int | None = None, usage_warning: bool | None = None,
                                      usage_warning_threshold: float | None = None) -> StorageUpdateEstimate:
        """What update_storage would change and cost (POST /storages/{pv}/estimate). Same contract as the sync client."""
        body = _write.storage_update_body(name=name, size_gb=size_gb, usage_warning=usage_warning,
                                          usage_warning_threshold=usage_warning_threshold)
        try:
            data = await self._send("POST", f"/storages/{_path_segment(pv_name, 'pv_name')}/estimate",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body)
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage editing") from None
        return StorageUpdateEstimate.from_dict(data)

    async def update_storage(self, pv_name: str, workspace: str, *, name: str | None = None,
                             size_gb: int | None = None, usage_warning: bool | None = None,
                             usage_warning_threshold: float | None = None,
                             idempotency_key: str | None = None) -> StorageUpdated:
        """Change a storage (PATCH /storages/{pv}, 202). Same contract as the sync client."""
        body = _write.storage_update_body(name=name, size_gb=size_gb, usage_warning=usage_warning,
                                          usage_warning_threshold=usage_warning_threshold)
        try:
            data = await self._send("PATCH", f"/storages/{_path_segment(pv_name, 'pv_name')}",
                              params={"workspace": _query_value(workspace, "workspace")}, json=body,
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage editing") from None
        return StorageUpdated.from_dict(data)

    async def get_storage_connection(self, pv_name: str, workspace: str) -> StorageConnection:
        """A storage's SSH access status (GET /storages/{pv}/connect). Same contract as the sync client."""
        try:
            data = await self._get(f"/storages/{_path_segment(pv_name, 'pv_name')}/connect",
                             params={"workspace": _query_value(workspace, "workspace")})
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage SSH access") from None
        return StorageConnection.from_dict(data)

    async def connect_storage(self, pv_name: str, workspace: str, *, wait: bool = True, timeout: float = 300.0,
                        interval: float = 3.0, idempotency_key: str | None = None) -> StorageConnection:
        """Open a storage for SSH and SFTP and wait until it is ready. Same contract as the sync client."""
        deadline = time.monotonic() + timeout
        try:
            data = await self._send("POST", f"/storages/{_path_segment(pv_name, 'pv_name')}/connect",
                              params={"workspace": _query_value(workspace, "workspace")},
                              idempotency_key=idempotency_key)
        except MeshiveAPIError as err:
            raise _not_supported(err, "storage SSH access") from None
        connection = StorageConnection.from_dict(data)
        while not _connection_ready(connection, pv_name) and wait:
            _connection_expired(deadline, pv_name, timeout, connection)
            await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))
            connection = await self.get_storage_connection(pv_name, workspace)
        if connection.ready and connection.pod_name:
            connection.ssh = await self.ssh_access(connection.pod_name, workspace)
        return connection

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
