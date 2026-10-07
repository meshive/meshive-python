"""SDK response dataclasses.

Server responses are camelCase (JSON), so from_dict reads camelCase keys.
Deeply nested fields (a pod's machine/template/request, ...) aren't typed one by one;
the original dict is kept in `.raw` → the SDK doesn't break when the backend adds fields.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


def _parse_dt(value: str | None) -> datetime | None:
    """ISO 8601 string → datetime. Accepts a 'Z' suffix. None if parsing fails."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _as_float(value: object) -> float:
    """Number/numeric string → float. 0.0 if it can't be converted (safe when the server sends Numeric as a string)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass
class WhoAmI:
    """GET /v1/sdk/me response — the owner of the current API key."""

    email: str
    username: str | None
    user_role: str
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "WhoAmI":
        return cls(
            email=d.get("email", ""),
            username=d.get("username"),
            user_role=d.get("userRole", ""),
            raw=d,
        )


@dataclass
class WorkspaceResources:
    """Resource count summary for a workspace."""

    pod: int = 0
    storage: int = 0
    serverless: int = 0

    @classmethod
    def from_dict(cls, d: dict | None) -> "WorkspaceResources":
        d = d or {}
        return cls(
            pod=d.get("pod", 0),
            storage=d.get("storage", 0),
            serverless=d.get("serverless", 0),
        )


@dataclass
class Workspace:
    """GET /v1/sdk/workspaces item."""

    namespace_name: str
    workspace_name: str
    description: str
    member_count: int
    status: str
    price_per_hour: str
    resources: WorkspaceResources
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)
    # The key owner's role in this workspace — admin | billing | viewer. Writes are 403 for viewer.
    # For display, not authorization (the server checks again on every write). None if the server doesn't know.
    member_role: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        return cls(
            namespace_name=d.get("namespaceName", ""),
            workspace_name=d.get("workspaceName", ""),
            description=d.get("description", ""),
            member_count=d.get("memberCount", 0),
            status=d.get("status", ""),
            price_per_hour=str(d.get("pricePerHour", "0")),
            resources=WorkspaceResources.from_dict(d.get("resources")),
            created_at=_parse_dt(d.get("createdAt")),
            updated_at=_parse_dt(d.get("updatedAt")),
            member_role=d.get("memberRole"),
            raw=d,
        )


@dataclass
class PodEndpoint:
    """One open port of a Pod (template.endpoints[]) — a URL row in the console's Connect tab."""

    name: str                  # userAlias (e.g. "ComfyUI", "Jupyter")
    port: int                  # containerPort
    port_type: str             # connect | http | tcp | ...
    external_url: str | None
    internal_url: str | None
    is_external: bool
    # Same rule as the console: readinessState (preparing|ready|interrupted) if present, otherwise ready|preparing from isReady.
    readiness: str

    @classmethod
    def from_dict(cls, d: dict) -> "PodEndpoint":
        state = d.get("readinessState")
        if state not in ("preparing", "ready", "interrupted"):
            state = "ready" if d.get("isReady") else "preparing"
        return cls(
            name=str(d.get("userAlias", "") or ""),
            port=_as_int(d.get("containerPort")),
            port_type=str(d.get("portType", "") or ""),
            external_url=d.get("externalUrl") or None,
            internal_url=d.get("internalUrl") or None,
            is_external=bool(d.get("isExternal", False)),
            readiness=state,
        )


@dataclass
class ConnectCredential:
    """One value needed to connect — an env the template marks showOnConnect (e.g. ComfyUI's ACCESS_PASSWORD).

    `is_secret` values are in a response anyone with the API key can read. Don't print them to screens or logs as-is —
    they're left out of repr, and the CLI hides them without `--show-secrets`.
    """

    key: str
    value: str = field(repr=False)
    is_secret: bool = False
    is_auto_generated: bool = False   # a value the template generated per Pod (not chosen by the user)


@dataclass
class Pod:
    """GET /v1/sdk/pods[/{name}] item.

    Types the commonly used top-level scalars and connection info (endpoints, connect_credentials).
    The remaining nested structures (machine/template/request/linkedStorages, ...) are in `.raw`.
    Values the server didn't send are None (older servers) — don't read them as False/0.
    """

    pod_name: str
    namespace_name: str
    user_alias: str
    status: str
    rental_type: str
    price_per_hour: str
    is_maintenance: bool
    created_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)
    has_unpreserved_workspace: bool | None = None
    storage_rate_per_hour: str = "0"
    endpoints: list[PodEndpoint] = field(default_factory=list)
    connect_credentials: list[ConnectCredential] = field(default_factory=list)
    billing_active: bool | None = None          # whether compute is billing (storage billing of a stopped Pod is separate)
    is_downloader: bool | None = None           # system Pod that downloads assets (free, users can't delete it)
    cpu_premium: bool | None = None
    uptime_premium: bool | None = None
    internet_premium: bool | None = None
    # Why it can't start on its original node right now (stopped/waiting only): spec_mismatch | hardware_changed |
    # node_unavailable | requirement_unmet | capacity. None if it can start or this can't be determined.
    same_node_unavailable_reason: str | None = None
    can_restart_on_same_node: bool | None = None   # computed only when stopped
    can_restart_on_any_node: bool | None = None
    waiting_mode: str | None = None              # when waiting: same_node | any_node
    stop_reason_code: str | None = None          # None or "user" = stopped by the user; anything else = stopped automatically by the system
    stop_reason_detail: str | None = None
    stop_reason_at: datetime | None = None
    container_running_at: datetime | None = None  # when the connect Pod's container became Running

    @classmethod
    def from_dict(cls, d: dict) -> "Pod":
        template = d.get("template") or {}
        return cls(
            pod_name=d.get("podName", ""),
            namespace_name=d.get("namespaceName", ""),
            user_alias=d.get("userAlias", ""),
            status=d.get("status", ""),
            rental_type=d.get("rentalType", ""),
            price_per_hour=str(d.get("pricePerHour", "0")),
            is_maintenance=bool(d.get("isMaintenance", False)),
            has_unpreserved_workspace=d.get("hasUnpreservedWorkspace"),
            storage_rate_per_hour=str(d.get("storageRatePerHour", "0")),
            created_at=_parse_dt(d.get("createdAt")),
            endpoints=[PodEndpoint.from_dict(e) for e in template.get("endpoints") or [] if isinstance(e, dict)],
            # Same selection as the console — only envs with showOnConnect.
            connect_credentials=[
                ConnectCredential(key=str(e.get("key", "")), value=str(e.get("value") or ""),
                                  is_secret=bool(e.get("isSecret", False)),
                                  is_auto_generated=bool(e.get("isAutoGenerated", False)))
                for e in template.get("envs") or [] if isinstance(e, dict) and e.get("showOnConnect")
            ],
            billing_active=d.get("billingActive"),
            is_downloader=d.get("isDownloader"),
            cpu_premium=d.get("cpuPremium"),
            uptime_premium=d.get("uptimePremium"),
            internet_premium=d.get("internetPremium"),
            same_node_unavailable_reason=d.get("sameNodeUnavailableReason"),
            can_restart_on_same_node=d.get("canRestartOnSameNode"),
            can_restart_on_any_node=d.get("canRestartOnAnyNode"),
            waiting_mode=d.get("waitingMode"),
            stop_reason_code=d.get("stopReasonCode"),
            stop_reason_detail=d.get("stopReasonDetail"),
            stop_reason_at=_parse_dt(d.get("stopReasonAt")),
            container_running_at=_parse_dt(d.get("containerRunningAt")),
            raw=d,
        )


@dataclass
class WatchedFolder:
    """One watched folder — new files in it are uploaded as assets."""

    path: str
    origin: str                    # template (folder defined by the template) | user (folder added by the user)
    enabled: bool
    role: str = ""
    include: list[str] = field(default_factory=list)   # the template's include
    include_override: list[str] | None = None          # None = template include, [] = everything, [..] = only these
    since: str | None = None       # user folders: only files created after this time (None = existing files too)
    blocked_reason: str | None = None   # network_storage = on NFS, so not harvested (can't be turned on)


@dataclass
class WatchedFolders:
    """GET /v1/sdk/pods/{pod}/harvest — all of a Pod's watched folders and whether they can be edited."""

    version: str | None            # "3:ab12…" — when changing, send the revision (leading number) as expected_version
    editable: bool
    editable_reason: str | None = None   # no_sidecar | busy | undecidable
    applied: bool | None = None          # whether the sidecar has received this version (None if unknown)
    restart_hint: bool = False           # takes effect only after a restart
    folders: list[WatchedFolder] | None = None   # None = can't be determined
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def revision(self) -> int | None:
        head = (self.version or "").split(":", 1)[0]
        return int(head) if head.isdigit() else None

    @classmethod
    def from_dict(cls, d: dict) -> "WatchedFolders":
        roots = d.get("roots")
        return cls(
            version=d.get("version"), editable=bool(d.get("editable", False)),
            editable_reason=d.get("editableReason"), applied=d.get("applied"),
            restart_hint=bool(d.get("restartHint", False)),
            folders=None if roots is None else [
                WatchedFolder(path=str(r.get("path", "")), origin=str(r.get("origin", "")),
                              enabled=bool(r.get("enabled", True)), role=str(r.get("role", "") or ""),
                              include=list(r.get("include") or []), include_override=r.get("includeOverride"),
                              since=r.get("since"), blocked_reason=r.get("blockedReason"))
                for r in roots if isinstance(r, dict)],
            raw=d)


@dataclass
class SshAccess:
    """POST /v1/sdk/pods/{pod}/ssh response — one-time SSH access (expires after a few minutes). The password is left out of repr."""

    command: str                   # e.g. "ssh -p 2222 gateway@<host>" (the server picks the user name)
    password: str = field(repr=False)
    web_url: str = field(default="", repr=False)   # browser terminal — the password is in the URL
    expires_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "SshAccess":
        expired = d.get("expiredAt")
        return cls(command=str(d.get("sshUrl", "") or ""), password=str(d.get("password", "") or ""),
                   web_url=str(d.get("sshWebUrl", "") or ""),
                   expires_at=None if expired is None else datetime.fromtimestamp(_as_int(expired), tz=timezone.utc),
                   raw=d)


@dataclass
class Machine:
    """GET /v1/sdk/machines[/{machine_id}] item — a machine registered by a host.

    Same schema (camelCase) as the web console's machine data, but the SDK read surface
    gets a trimmed DTO without sensitive fields (ssh/ipmi credentials, grafana, bootReport, ...).
    Only commonly used scalars are typed — display fields that were nested, such as
    status/gpu/earning, are lifted up — and the rest (specs, the whole state, podUses, ...) is in `.raw`.
    """

    machine_id: str       # id (lookup key)
    name: str             # user label
    machine_type: str     # "gpu" | "cpu" | "storage"
    status: str           # state.name (ONLINE/OFFLINE/MAINTENANCE/...). stageState is in .raw.
    gpu_model: str         # specs.gpu (empty string for cpu/storage machines)
    gpu_count: int         # specs.gpuNumber
    earning_hourly: float  # earning.hourly
    uptime_rate: float     # uptimeRate (0.0~1.0)
    host_tier: str         # hostTier
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Machine":
        state = d.get("state") or {}
        specs = d.get("specs") or {}
        earning = d.get("earning") or {}
        return cls(
            machine_id=d.get("id", ""),
            name=d.get("name", ""),
            machine_type=d.get("machineType", ""),
            status=state.get("name", ""),
            gpu_model=specs.get("gpu", "") or "",
            gpu_count=int(specs.get("gpuNumber", 0) or 0),
            earning_hourly=_as_float(earning.get("hourly", 0)),
            uptime_rate=_as_float(d.get("uptimeRate", 0)),
            host_tier=d.get("hostTier", "") or "",
            raw=d,
        )


# =============================================================================
# 0.0.7 read surface extension — workspace details / storage / metrics / GPUs / account / templates / serverless
# =============================================================================

def _parse_date(value: str | None) -> date | None:
    """'YYYY-MM-DD' (or ISO datetime) → date. None if parsing fails."""
    if not value:
        return None
    parsed = _parse_dt(value)
    if parsed is not None:
        return parsed.date()
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _as_int(value: object) -> int:
    """Number/numeric string → int. 0 for None or values that can't be converted."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_optional_float(value: object) -> float | None:
    """For values like metric usage rates where 'not measurable (None)' must be told apart from 0."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class ResourceCondition:
    """Status counts per resource type in workspace details (type: pod | storage | serverless)."""

    type: str
    active: int = 0
    paused: int = 0
    disabled: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceCondition":
        return cls(type=str(d.get("type", "")), active=_as_int(d.get("active")),
                   paused=_as_int(d.get("paused")), disabled=_as_int(d.get("disabled")))


@dataclass
class DailyCost:
    """Daily workspace cost (USD). The per-item amounts add up to total."""

    date: date | None
    pod: float = 0.0
    storage: float = 0.0
    serverless: float = 0.0
    task: float = 0.0
    asset: float = 0.0

    @property
    def total(self) -> float:
        return self.pod + self.storage + self.serverless + self.task + self.asset

    @classmethod
    def from_dict(cls, d: dict) -> "DailyCost":
        return cls(date=_parse_date(d.get("date")), pod=_as_float(d.get("pod")),
                   storage=_as_float(d.get("storage")), serverless=_as_float(d.get("serverless")),
                   task=_as_float(d.get("task")), asset=_as_float(d.get("asset")))


@dataclass
class WorkspaceDetail:
    """GET /v1/sdk/workspaces/{namespace} response.

    Types costs (current hourly / weekly daily average / daily breakdown) and the resource summary. The maintenance
    schedule (maintenanceSchedule) and host message (messageFromHost) are in `.raw`.
    """

    namespace_name: str
    workspace_name: str
    price_per_hour: str        # costData.currentUsage — hourly total currently billing (USD)
    weekly_avg_daily_cost: str  # costData.weeklyAvgUsage — daily average over the last 7 days (USD)
    gpus: int
    vcpus: int
    ram: int
    total_storage: float
    resources: list[ResourceCondition] = field(default_factory=list)
    costs: list[DailyCost] = field(default_factory=list)   # recent daily costs (oldest first)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkspaceDetail":
        cost = d.get("costData") or {}
        detail = d.get("resourceDetail") or {}
        return cls(
            namespace_name=d.get("namespaceName", ""),
            workspace_name=d.get("workspaceName", ""),
            price_per_hour=str(cost.get("currentUsage", "0")),
            weekly_avg_daily_cost=str(cost.get("weeklyAvgUsage", "0")),
            gpus=_as_int(detail.get("gpus")),
            vcpus=_as_int(detail.get("vCpus")),
            ram=_as_int(detail.get("ram")),
            total_storage=_as_float(detail.get("totalStorage")),
            resources=[ResourceCondition.from_dict(r) for r in detail.get("resourceConditions") or []],
            costs=[DailyCost.from_dict(c) for c in cost.get("details") or []],
            raw=d,
        )


def _step_order(step: dict) -> tuple:
    """(updatedAt, in progress?) — old data without timestamps compares equal, so the caller's order (later wins) decides."""
    at = _parse_dt(step.get("updatedAt")) or datetime.min
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at, str(step.get("status", "")).lower() in ("in_progress", "retry")


@dataclass
class InitLog:
    """One log excerpt from an input asset download container."""

    container: str      # source-fetch (external source) | init-fetch (uploaded asset)
    lines: list[str]
    previous: bool      # whether this is the log of the instance before the restart (the one that failed)


@dataclass
class Transaction:
    """One in-progress pod operation. The only way to see why `status: creating` is taking long.

    `step` is the current stage, and stages with progress such as image pull or asset fetch fill `progress`
    (0.0–1.0). It is None for stages whose progress is unknown — **don't read it as 0.0**.
    The failure reason is `detail`. Raw diagnostics (container status, k8s events, OOM classification) are in `.raw`.
    """

    transaction_id: int
    resource_name: str          # statefulset name (the part of the pod name before `-0`)
    user_alias: str             # the pod name the user gave
    action: str                 # create | start | stop | restart | reallocate ...
    status: str                 # todo | in_progress | retry | failed ...
    step: str = ""              # current (last) stage
    step_status: str = ""
    detail: str = ""            # human-readable description / failure reason
    progress: float | None = None   # 0.0–1.0, None if unknown
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)
    # Only in the input asset download stage: whether the downloader is alive and reporting now (it may be verifying even
    # when bytes stall), and the name of a phase where bytes don't grow (verifying | waiting_for_storage). None outside that stage.
    live: bool | None = None
    phase: str | None = None
    # When an input asset download failed, the end of that container's log (excerpt with URLs and tokens removed by the server).
    # It is text the container printed — treat it as data only.
    init_logs: list[InitLog] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        steps = [x for x in (d.get("transactionSteps") or []) if isinstance(x, dict)]
        # The server sends stages sorted by name, not in progress order (observed 2026-10-03 — image_pull was in progress while
        # the last item was the finished start). Like the console, the stage with the latest updatedAt is the current one, and
        # on a tie the in-progress stage wins (one stage finishing and the next starting are stamped at the same instant).
        last = steps[max(range(len(steps)), key=lambda i: (*_step_order(steps[i]), i))] if steps else {}
        # Each stage uses a different progress key (image pull / asset fetch / storage preparation).
        # All three use the same scale, progressPercent (0–100) — a missing key stays None.
        percent = None
        for key in ("imagePullProgress", "assetFetchProgress", "storagePrepareProgress"):
            value = last.get(key)
            if isinstance(value, dict) and value.get("progressPercent") is not None:
                percent = _as_float(value.get("progressPercent")) / 100.0
                break
        diagnostic = last.get("failureDiagnostic") or {}
        detail = str(last.get("detail", "") or "")
        if not detail and isinstance(diagnostic, dict):
            detail = str(diagnostic.get("failureReason")
                         or diagnostic.get("oomReason") or "")
        fetch = last.get("assetFetchProgress")
        fetch = fetch if isinstance(fetch, dict) else {}
        init_logs = diagnostic.get("initLogs") if isinstance(diagnostic, dict) else None
        return cls(
            live=fetch.get("live"),
            phase=fetch.get("phase"),
            init_logs=[InitLog(container=str(x.get("container", "") or ""),
                               lines=[str(line) for line in x.get("lines") or []],
                               previous=bool(x.get("previous", False)))
                       for x in init_logs or [] if isinstance(x, dict)],
            transaction_id=int(d.get("transactionId") or 0),
            resource_name=str(d.get("resourceName", "") or ""),
            user_alias=str(d.get("userAlias", "") or ""),
            action=str(d.get("action", "") or ""),
            status=str(d.get("status", "") or ""),
            step=str(last.get("step", "") or ""),
            step_status=str(last.get("status", "") or ""),
            detail=detail,
            progress=percent,
            created_at=_parse_dt(d.get("createdAt")),
            updated_at=_parse_dt(d.get("updatedAt")),
            raw=d,
        )


@dataclass
class Storage:
    """GET /v1/sdk/storages[/{storage_name}] item — a workspace volume (PV).

    The rest, such as machine (host node info) and usageWarning, is in `.raw`.
    """

    pv_name: str               # lookup key
    namespace_name: str
    user_alias: str            # user label
    storage_type: str          # nfs | hostPath | ...
    status: str
    total_size: float
    available_size: float
    usage_rate: float          # 0.0~1.0
    price_per_hour: str
    linked_pods: list[str] = field(default_factory=list)   # names of attached pods
    is_maintenance: bool = False
    encrypted: bool = False
    created_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Storage":
        return cls(
            pv_name=d.get("pvName", ""),
            namespace_name=d.get("namespaceName", ""),
            user_alias=d.get("userAlias", "") or "",
            storage_type=str(d.get("storageType", "") or ""),
            status=str(d.get("status", "") or ""),
            total_size=_as_float(d.get("totalSize")),
            available_size=_as_float(d.get("availableSize")),
            usage_rate=_as_float(d.get("usageRate")),
            price_per_hour=str(d.get("pricePerHour", "0")),
            linked_pods=[p.get("podName", "") for p in d.get("linkedPod") or [] if isinstance(p, dict)],
            is_maintenance=bool(d.get("isMaintenance", False)),
            encrypted=bool(d.get("encrypted", False)),
            created_at=_parse_dt(d.get("createdAt")),
            raw=d,
        )


@dataclass
class GpuUsage:
    """Utilization of one GPU. rate is 0.0–1.0, vram_size is total VRAM (MiB).

    Each field other than gpu_number is None when not measurable — when dcgm-exporter omits a metric family it can't
    read, the server sends that field as null, so a GPU with a temperature but no memory (FB) values is normal. vram_size
    is then None too, not 0.0. But when both FB values are reported as 0 the server sends 0.0 — that isn't the card size either (the CLI shows n/a for both).
    """

    gpu_number: int
    core_usage_rate: float | None = None
    vram_usage_rate: float | None = None
    vram_size: float | None = None
    temp: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "GpuUsage":
        return cls(gpu_number=_as_int(d.get("gpuNumber")),
                   core_usage_rate=_as_optional_float(d.get("coreUsageRate")),
                   vram_usage_rate=_as_optional_float(d.get("vramUsageRate")),
                   vram_size=_as_optional_float(d.get("vramSize")),
                   temp=_as_optional_float(d.get("temp")))


@dataclass
class PodMetrics:
    """GET /v1/sdk/pods/{pod_name}/metrics response — pod resource usage.

    Usage rates are 0.0–1.0, None if the Prometheus query failed (requested amounts are still filled in).
    Attached storage usage is in `.raw["storage"]`. Sizes (ram_size, ephemeral_storage_*, gpus[].vram_size) are MiB.
    """

    pod_name: str
    cpu_cores: float
    cpu_usage_rate: float | None
    ram_size: float
    ram_usage_rate: float | None
    gpus: list[GpuUsage] = field(default_factory=list)
    ephemeral_storage_request: int = 0
    ephemeral_storage_usage: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "PodMetrics":
        cpu = d.get("cpu") or {}
        ram = d.get("ram") or {}
        ephemeral = d.get("ephemeralStorage") or {}
        return cls(
            pod_name=d.get("podName", ""),
            cpu_cores=_as_float(cpu.get("core")),
            cpu_usage_rate=_as_optional_float(cpu.get("usageRate")),
            ram_size=_as_float(ram.get("size")),
            ram_usage_rate=_as_optional_float(ram.get("usageRate")),
            gpus=[GpuUsage.from_dict(g) for g in d.get("gpu") or []],
            ephemeral_storage_request=_as_int(ephemeral.get("request")),
            ephemeral_storage_usage=_as_int(ephemeral.get("usage")),
            raw=d,
        )


@dataclass
class MachineMetrics:
    """GET /v1/sdk/machines/{machine_id}/metrics response — live metrics of a host machine.

    *_allocated is what the current pods have reserved. Disk temperatures (diskTemperatures) and network
    interface names are in `.raw`.

    Units: only ram_size is **bytes** (node MemTotal — the server passes the Prometheus value through). ram_allocated,
    root/pv_volume_size and gpus[].vram_size are MiB; network_receive/transmit are bytes/sec.
    """

    machine_id: str
    cpu_cores: float
    cpu_usage_rate: float
    cpu_allocated: float
    ram_size: float
    ram_usage_rate: float
    ram_allocated: float
    gpus: list[GpuUsage] = field(default_factory=list)
    root_volume_size: float = 0.0
    root_volume_usage_rate: float = 0.0
    pv_volume_size: float = 0.0
    pv_volume_usage_rate: float = 0.0
    network_receive: float = 0.0
    network_transmit: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "MachineMetrics":
        cpu = d.get("cpu") or {}
        ram = d.get("ram") or {}
        root = d.get("rootVolume") or {}
        pv = d.get("pvVolume") or {}
        net = d.get("networkIo") or {}
        return cls(
            machine_id=d.get("machineId", ""),
            cpu_cores=_as_float(cpu.get("core")),
            cpu_usage_rate=_as_float(cpu.get("usageRate")),
            cpu_allocated=_as_float(cpu.get("allocationCore")),
            ram_size=_as_float(ram.get("size")),
            ram_usage_rate=_as_float(ram.get("usageRate")),
            ram_allocated=_as_float(ram.get("allocationSize")),
            gpus=[GpuUsage.from_dict(g) for g in d.get("gpu") or []],
            root_volume_size=_as_float(root.get("size")),
            root_volume_usage_rate=_as_float(root.get("usageRate")),
            pv_volume_size=_as_float(pv.get("size")),
            pv_volume_usage_rate=_as_float(pv.get("usageRate")),
            network_receive=_as_float(net.get("receive")),
            network_transmit=_as_float(net.get("transmit")),
            raw=d,
        )


@dataclass
class GpuAvailability:
    """GET /v1/sdk/gpus item — a (model, VRAM) GPU tier that can be rented now.

    combinations (max allocatable per machine) is summarized as available_gpus / max_gpus_per_pod;
    the original is in `.raw["combinations"]`.
    """

    gpu_model: str
    vram: int                  # GB
    rental_type: str           # demand | spot
    price_per_hour: str        # per GPU per hour (USD)
    vcpu_recommended: int
    ram_recommended: int
    available_gpus: int        # total across all machines
    max_gpus_per_pod: int      # max number one pod can get on a single machine
    machine_count: int
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "GpuAvailability":
        combos = [c for c in d.get("combinations") or [] if isinstance(c, dict)]
        per_machine = [_as_int(c.get("maxGpu")) for c in combos]
        return cls(
            gpu_model=d.get("gpuModel", ""),
            vram=_as_int(d.get("vram")),
            rental_type=str(d.get("rentalType", "") or ""),
            price_per_hour=str(d.get("gpuPrice", "0")),
            vcpu_recommended=_as_int(d.get("vcpuRecommended")),
            ram_recommended=_as_int(d.get("ramRecommended")),
            available_gpus=sum(per_machine),
            max_gpus_per_pod=max(per_machine, default=0),
            machine_count=len(combos),
            raw=d,
        )


@dataclass
class ApiKey:
    """GET /v1/sdk/api-keys item — one of my Meshive API keys (active keys only).

    The plaintext is shown only once at issuance and the server keeps only a hash, so only the display prefix comes back here.
    """

    key_id: int
    name: str | None
    prefix: str                # "meshive_a1b2c3d4"
    scopes: list[str]
    status: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "ApiKey":
        return cls(
            key_id=_as_int(d.get("id")),
            name=d.get("keyName"),
            prefix=d.get("keyPrefix", ""),
            scopes=[str(s) for s in d.get("scopes") or []],
            status=str(d.get("status", "") or ""),
            created_at=_parse_dt(d.get("createdAt")),
            last_used_at=_parse_dt(d.get("lastUsedAt")),
            expires_at=_parse_dt(d.get("expiresAt")),
            raw=d,
        )


@dataclass
class Credit:
    """GET /v1/sdk/credit response — credit balance (USD).

    There is one kind of balance — pods, tasks and serverless all draw from it. paid_balance and bonus_balance are
    leftovers of the paid/bonus split from before free credits were retired (2026-10); the server sends
    paid_balance = balance and bonus_balance = 0 (the fields stay for backward compatibility).
    """

    balance: float
    paid_balance: float
    bonus_balance: float
    auto_recharge: bool
    auto_recharge_threshold: int
    auto_recharge_amount: int
    has_default_payment_method: bool
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Credit":
        return cls(
            balance=_as_float(d.get("creditBalance")),
            paid_balance=_as_float(d.get("paidBalance")),
            bonus_balance=_as_float(d.get("bonusBalance")),
            auto_recharge=bool(d.get("autoRecharge", False)),
            auto_recharge_threshold=_as_int(d.get("autoRechargeThreshold")),
            auto_recharge_amount=_as_int(d.get("autoRechargeAmount")),
            has_default_payment_method=bool(d.get("hasDefaultPaymentMethod", False)),
            raw=d,
        )


@dataclass
class CreditHistoryEntry:
    """GET /v1/sdk/credit/history item — one top-up/refund ledger entry (refunds are negative).

    Stripe receipt/invoice links aren't on the SDK surface (see the console).
    """

    entry_id: int
    amount: float
    is_paid: bool
    payment_method: str
    created_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "CreditHistoryEntry":
        return cls(
            entry_id=_as_int(d.get("id")),
            amount=_as_float(d.get("amount")),
            is_paid=bool(d.get("isPaid", False)),
            payment_method=d.get("paymentMethod", "") or "",
            created_at=_parse_dt(d.get("createdAt")),
            raw=d,
        )


@dataclass
class DailyEarning:
    """Host daily earnings (USD)."""

    date: date | None
    cpu: float = 0.0
    gpu: float = 0.0
    storage: float = 0.0
    total: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "DailyEarning":
        return cls(date=_parse_date(d.get("date")), cpu=_as_float(d.get("cpu")),
                   gpu=_as_float(d.get("gpu")), storage=_as_float(d.get("storage")),
                   total=_as_float(d.get("total")))


@dataclass
class Earnings:
    """GET /v1/sdk/earnings response — host earnings summary (USD) + daily breakdown (newest first)."""

    current_hourly: float          # hourly earnings of pods/volumes currently billing
    daily: float                   # accumulated today
    accumulated_until_payout: float  # accumulated until the next payout
    history: list[DailyEarning] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Earnings":
        return cls(
            current_hourly=_as_float(d.get("currentEarning")),
            daily=_as_float(d.get("dailyEarning")),
            accumulated_until_payout=_as_float(d.get("accumulatedEarningUntilPayout")),
            history=[DailyEarning.from_dict(h) for h in d.get("earningHistory") or []],
            raw=d,
        )


@dataclass
class Member:
    """GET /v1/sdk/members item — a workspace member (role: admin | billing | viewer)."""

    user: str                  # email (lookup key)
    role: str
    joined_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Member":
        return cls(user=d.get("user", ""), role=str(d.get("role", "") or ""),
                   joined_at=_parse_dt(d.get("createdAt")), raw=d)


@dataclass
class Template:
    """GET /v1/sdk/templates[/{template_id}] item.

    The deployment spec (envs/endpoints/volumeMounts/semanticPaths, ...) is in `.raw`.
    """

    template_id: int           # lookup key
    name: str
    description: str
    is_official: bool
    deploy_type: str           # pod | serverless
    app_type: str              # ide | framework | ... | custom
    app_sub_type: str
    image: str
    hardware_type: str         # gpu | cpu | any
    cuda_version: str = ""
    framework: str = ""
    framework_version: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Template":
        return cls(
            template_id=_as_int(d.get("id")),
            name=d.get("name", ""),
            description=d.get("description", "") or "",
            is_official=bool(d.get("isOfficial", False)),
            deploy_type=str(d.get("templateDeployType", "") or ""),
            app_type=str(d.get("appType", "") or ""),
            app_sub_type=d.get("appSubType", "") or "",
            image=d.get("image", ""),
            hardware_type=str(d.get("hardwareType", "") or ""),
            cuda_version=d.get("cudaVersion", "") or "",
            framework=d.get("framework", "") or "",
            framework_version=d.get("frameworkVersion", "") or "",
            raw=d,
        )


@dataclass
class Serving:
    """GET /v1/sdk/servings[/{serving_id}] item — a serverless serving deployment.

    replicas (per-replica status/GPU/billing) and live metrics (throughput/latency, ...) are in `.raw`.
    """

    serving_id: int            # lookup key (group id)
    namespace_name: str
    model_name: str | None
    api_model_id: str | None
    framework: str
    status: str                # provisioning | active | scaling | error | ...
    paused: bool
    min_replicas: int
    max_replicas: int
    current_replicas: int
    healthy_replicas: int | None
    endpoint_url: str | None
    price_per_hour: str        # sum of billing replica prices (USD), "0" if none
    billing_active: bool
    autoscale: bool = False                 # auto_scale_enabled
    price_cap_per_hour: str | None = None   # hourly cap per replica (USD), None = unlimited
    raw: dict = field(default_factory=dict, repr=False)

    def scale_raises_cost(self, *, min_replicas: int | None = None, max_replicas: int | None = None,
                          autoscale: bool | None = None, price_cap_per_hour: object = None) -> bool:
        """Whether scale_serving arguments **can raise** the hourly cost — the rule the CLI/MCP use to require confirmation.
        A wider replica range, turning on autoscale (when it was off), a higher per-replica cap (unlimited is already the max)."""
        if min_replicas is not None and min_replicas > self.min_replicas:
            return True
        if max_replicas is not None and max_replicas > self.max_replicas:
            return True
        if autoscale and not self.autoscale:
            return True
        if price_cap_per_hour is not None and self.price_cap_per_hour is not None:
            try:
                return Decimal(str(price_cap_per_hour)) > Decimal(self.price_cap_per_hour)
            except (InvalidOperation, ValueError):
                return True     # a value that can't be parsed safely counts as "needs confirmation"
        return False

    @classmethod
    def from_dict(cls, d: dict) -> "Serving":
        price = d.get("pricePerHour")
        healthy = d.get("healthyReplicas")
        cap = d.get("priceCapPerHour")
        return cls(
            serving_id=_as_int(d.get("id")),
            namespace_name=d.get("namespaceName", ""),
            model_name=d.get("modelName"),
            api_model_id=d.get("apiModelId"),
            framework=d.get("framework", "") or "",
            status=str(d.get("status", "") or ""),
            paused=bool(d.get("paused", False)),
            min_replicas=_as_int(d.get("minReplicas")),
            max_replicas=_as_int(d.get("maxReplicas")),
            current_replicas=_as_int(d.get("currentReplicas")),
            healthy_replicas=None if healthy is None else _as_int(healthy),
            endpoint_url=d.get("endpointUrl"),
            price_per_hour=str(price) if price is not None else "0",
            billing_active=bool(d.get("billingActive", False)),
            autoscale=bool(d.get("autoScaleEnabled", False)),
            price_cap_per_hour=None if cap is None else str(cap),
            raw=d,
        )


@dataclass
class TaskInputAsset:
    """One input asset attached to a task (inputAssets[] of the detail response)."""

    asset_id: str
    name: str
    target_dir: str
    reference_mode: str | None = None   # None = managed / "external" = fetched from the source again on every run
    ingest_source: str | None = None    # hf_import | civitai_import | web_upload | ...


@dataclass
class ModelDetection:
    """POST /v1/sdk/models/detect response — whether an HF repo can be served (the detection in the console's registration dialog)."""

    status: str                    # ok | unsupported | failed (failed = couldn't read HF or couldn't decide)
    output: str | None = None      # text | image | video
    takes_image: bool = False      # whether it accepts image input (VLMs, ...)
    capability: str | None = None  # text_generation | embedding | text_to_image ...
    framework: str | None = None   # vllm | sglang
    detail: str | None = None      # reason for unsupported/failed
    architecture: str | None = None
    file_size_bytes: int | None = None
    context_length: int | None = None   # detected max context — None if unknown
    suggested_repo: str | None = None   # candidate original repo for an unsupported GGUF
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def from_dict(cls, d: dict) -> "ModelDetection":
        return cls(status=str(d.get("status", "") or ""), output=d.get("output"), takes_image=bool(d.get("takesImage")),
                   capability=d.get("cap"), framework=d.get("framework"), detail=d.get("detail"),
                   architecture=d.get("arch"), file_size_bytes=d.get("fileSizeBytes"),
                   context_length=d.get("contextLength"), suggested_repo=d.get("suggestedRepo"), raw=d)


@dataclass
class ServingModel:
    """GET /v1/sdk/models item — a serving model registered by the workspace. Pass `registration_id` to deploy_serving."""

    registration_id: int
    name: str
    huggingface_repo: str | None
    api_model_id: str | None       # the model value in inference requests
    framework: str
    model_type: str
    context_length: int | None = None
    quantization: str | None = None
    parameter_count_b: float | None = None
    min_vram_gb: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "ServingModel":
        return cls(registration_id=_as_int(d.get("id")), name=str(d.get("displayName") or d.get("modelName") or ""),
                   huggingface_repo=d.get("huggingfaceRepo"), api_model_id=d.get("apiModelId"),
                   framework=str(d.get("framework", "") or ""), model_type=str(d.get("modelType", "") or ""),
                   context_length=d.get("contextLength"), quantization=d.get("quantization"),
                   parameter_count_b=d.get("parameterCountB"), min_vram_gb=d.get("minVramGb"), raw=d)


@dataclass
class HfToken:
    """A Hugging Face token (or CivitAI key) saved in the workspace — id and name only (the API never returns the value)."""

    token_id: int
    label: str
    created_at: datetime | None = None
    used_by_asset_count: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "HfToken":
        return cls(token_id=_as_int(d.get("id")), label=str(d.get("label", "") or ""),
                   created_at=_parse_dt(d.get("createdAt")), used_by_asset_count=_as_int(d.get("usedByAssetCount")))


@dataclass
class Task:
    """GET /v1/sdk/tasks[/{task_id}] item — a serverless task.

    The single-item response's script/requirements/env (secret values masked)/cost breakdown are in `.raw`.
    """

    task_id: str               # lookup key (externalId, "task_...")
    name: str
    namespace_name: str
    status: str                # queued | scheduling | pulling | fetching | running | succeeded | failed | timed_out | stopped
    pod_name: str
    image: str
    gpu_model: str | None
    gpu_count: int
    cpu_cores: int
    ram_gb: int
    price_per_hour: str
    cost_so_far: str
    total_cost: str
    created_at: datetime | None = None
    container_running_at: datetime | None = None
    finished_at: datetime | None = None
    failure_reason: str | None = None
    exit_code: int | None = None
    raw: dict = field(default_factory=dict, repr=False)
    # The asset id, if the input asset download failed.
    failed_asset_id: str | None = None
    # Input assets — only in the detail response (`get_task`). An empty list in listings.
    input_assets: list[TaskInputAsset] = field(default_factory=list)
    # Output upload is a separate axis from task status: a finished task may still be uploading outputs.
    # state: pending | uploading | completed | failed | skipped | discarded. None = old row (read it like skipped).
    output_upload_state: str | None = None
    output_files_declared: int | None = None
    output_files_completed: int | None = None
    output_bytes_declared: int | None = None
    output_bytes_completed: int | None = None
    output_upload_last_error: str | None = None   # reason of the last upload failure (credential error, ...)
    outputs_purged_at: datetime | None = None      # when the outputs were deleted

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        exit_code = d.get("exitCode")
        return cls(
            failed_asset_id=d.get("failedAssetExternalId"),
            input_assets=[TaskInputAsset(asset_id=str(a.get("assetExternalId", "") or ""),
                                         name=str(a.get("name", "") or ""),
                                         target_dir=str(a.get("targetDir", "") or ""),
                                         reference_mode=a.get("referenceMode"),
                                         ingest_source=a.get("ingestSource"))
                          for a in d.get("inputAssets") or [] if isinstance(a, dict)],
            output_upload_state=d.get("outputUploadState"),
            output_files_declared=d.get("outputFilesDeclared"),
            output_files_completed=d.get("outputFilesCompleted"),
            output_bytes_declared=d.get("outputBytesDeclared"),
            output_bytes_completed=d.get("outputBytesCompleted"),
            output_upload_last_error=d.get("outputUploadLastError"),
            outputs_purged_at=_parse_dt(d.get("outputsPurgedAt")),
            task_id=d.get("externalId", ""),
            name=d.get("name", ""),
            namespace_name=d.get("namespaceName", ""),
            status=str(d.get("status", "") or ""),
            pod_name=d.get("podName", "") or "",
            image=d.get("image", "") or "",
            gpu_model=d.get("gpuModel"),
            gpu_count=_as_int(d.get("gpuCount")),
            cpu_cores=_as_int(d.get("cpuCores")),
            ram_gb=_as_int(d.get("ramGb")),
            price_per_hour=str(d.get("pricePerHour", "0")),
            cost_so_far=str(d.get("costSoFar", "0")),
            total_cost=str(d.get("totalCost", "0")),
            created_at=_parse_dt(d.get("createdAt")),
            container_running_at=_parse_dt(d.get("containerRunningAt")),
            finished_at=_parse_dt(d.get("finishedAt")),
            failure_reason=d.get("failureReason"),
            exit_code=None if exit_code is None else _as_int(exit_code),
            raw=d,
        )


# --- assets (Asset Hub) -------------------------------------------------------

_READY = "ready"


@dataclass
class AssetVersion:
    """[Deprecated — removed in 0.2] One asset version. Assets no longer have versions on the server.

    It is the asset summary the server echoes back as a single version with versionNumber=1, for older SDKs.
    Use the asset-level fields of `Asset` (size_bytes, file_count, upload_status, files …).
    """

    version_number: int
    status: str                # uploading | ready | ...
    total_size_bytes: int
    file_count: int
    ingest_source: str         # web_upload | hf_import | civitai_import | harvest | ...
    storage_provider: str      # meshive_r2 (managed) | user_s3 | external
    created_at: datetime | None = None
    deleted: bool = False
    import_failure_reason: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "AssetVersion":
        return cls(
            version_number=_as_int(d.get("versionNumber")),
            status=str(d.get("status", "") or ""),
            total_size_bytes=_as_int(d.get("totalSizeBytes")),
            file_count=_as_int(d.get("fileCount")),
            ingest_source=str(d.get("ingestSource", "") or ""),
            storage_provider=str(d.get("storageProvider", "") or ""),
            created_at=_parse_dt(d.get("createdAt")),
            deleted=bool(d.get("deleted", False)),
            import_failure_reason=d.get("importFailureReason"),
            raw=d,
        )

    @property
    def is_ready(self) -> bool:
        return not self.deleted and self.status.lower() == _READY


def _warn_versions(name: str) -> None:
    warnings.warn(f"Asset.{name} is deprecated and will be removed in meshive 0.2: assets no longer have "
                  "versions. Use Asset.size_bytes, file_count, upload_status and files.",
                  DeprecationWarning, stacklevel=3)


@dataclass
class AssetFile:
    """One file of an asset (files[] of the detail response)."""

    path: str                  # relativePath — relative path inside the asset
    size_bytes: int
    status: str
    file_id: str | None = None  # fileExternalId
    content_hash: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "AssetFile":
        return cls(path=str(d.get("relativePath", "") or ""), size_bytes=_as_int(d.get("sizeBytes")),
                   status=str(d.get("status", "") or ""), file_id=d.get("fileExternalId"),
                   content_hash=d.get("contentHash"), created_at=_parse_dt(d.get("createdAt")))


@dataclass
class AssetUsage:
    """One place currently using the asset (activeUsageContexts[] of the detail response) — why it can't be deleted or changed."""

    kind: str                  # pod | task | serving ...
    identifier: str
    name: str
    status: str
    role: str | None = None


@dataclass
class Asset:
    """A row of GET /v1/sdk/assets or the detail of /assets/{id}.

    Assets have no versions — size, file count and upload status are values of the asset itself. The file list (files) and
    where it's in use (active_usage_contexts) come only with the detail. The rest, such as the pickle badge, is in `.raw`.
    `version_count`, `latest_version` and `versions` are deprecated (removed in 0.2).
    """

    asset_id: str              # assetExternalId ("asset_…", lookup key)
    name: str
    asset_type: str            # dataset | model | adapter | checkpoint | output | config | file
    status: str                # active | source_missing | frozen | deleted | purged | merged
    status_reason: str | None
    storage_provider: str      # meshive_r2 (managed) | user_s3 | external, "" if unknown
    # A link asset whose external source hasn't been measured yet has unknown size and file count → None (not 0).
    size_bytes: int | None
    file_count: int | None
    in_use: bool
    namespace_name: str = ""
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)
    upload_status: str = ""    # uploading | ready | failed
    stale: bool = False        # uploading, but the Pod that would finish it is gone (console "Interrupted")
    ingest_source: str = ""    # web_upload | hf_import | civitai_import | harvest | task_output | ...
    import_failure_reason: str | None = None
    kind: str | None = None
    semantic_type: str | None = None   # only in list rows
    files: list[AssetFile] = field(default_factory=list)                   # detail only
    active_usage_contexts: list[AssetUsage] = field(default_factory=list)  # detail only

    @classmethod
    def from_dict(cls, d: dict, *, namespace_name: str = "") -> "Asset":
        measured = d.get("sizeMeasured") is not False
        return cls(
            asset_id=d.get("assetExternalId", ""),
            name=d.get("name", ""),
            asset_type=str(d.get("assetType", "") or ""),
            status=str(d.get("status", "") or ""),
            status_reason=d.get("statusReason"),
            storage_provider=str(d.get("storageProvider") or ""),
            size_bytes=_as_int(d.get("totalSizeBytes")) if measured else None,
            file_count=_as_int(d.get("fileCount")) if measured else None,
            in_use=bool(d.get("inUse", False)),
            namespace_name=d.get("namespaceName") or namespace_name,
            created_by=d.get("createdBy"),
            created_at=_parse_dt(d.get("createdAt")),
            updated_at=_parse_dt(d.get("updatedAt")),
            upload_status=str(d.get("uploadStatus", "") or ""),
            stale=bool(d.get("stale", False)),
            ingest_source=str(d.get("ingestSource", "") or ""),
            import_failure_reason=d.get("importFailureReason"),
            kind=d.get("kind"),
            semantic_type=d.get("semanticType"),
            files=[AssetFile.from_dict(f) for f in d.get("files") or [] if isinstance(f, dict)],
            active_usage_contexts=[
                AssetUsage(kind=str(c.get("kind", "") or ""), identifier=str(c.get("identifier", "") or ""),
                           name=str(c.get("name", "") or ""), status=str(c.get("status", "") or ""),
                           role=c.get("role"))
                for c in d.get("activeUsageContexts") or [] if isinstance(c, dict)],
            raw=d,
        )

    # --- deprecated: version fields the server echoes back for older SDKs (removed in 0.2) -----------------

    def _raw_versions(self) -> list[AssetVersion]:
        return [AssetVersion.from_dict(v) for v in self.raw.get("versions") or [] if isinstance(v, dict)]

    @property
    def versions(self) -> list[AssetVersion]:
        _warn_versions("versions")
        return self._raw_versions()

    @property
    def latest_version(self) -> AssetVersion | None:
        _warn_versions("latest_version")
        latest = self.raw.get("latestVersion")
        if isinstance(latest, dict):
            return AssetVersion.from_dict(latest)
        ready = [v for v in self._raw_versions() if v.is_ready]
        return max(ready, key=lambda v: v.version_number) if ready else None

    @property
    def version_count(self) -> int:
        _warn_versions("version_count")
        if "versionCount" in self.raw:
            return _as_int(self.raw["versionCount"])
        return len([v for v in self._raw_versions() if v.is_ready])


@dataclass
class DownloadFile:
    """One file to download — presigned URLs are short-lived (`expires_in`). Don't attach your Meshive key to the URL."""

    path: str                  # relative path inside the asset / task output file name
    url: str
    size_bytes: int | None = None
    content_hash: str | None = None


@dataclass
class AssetDownload:
    """GET /v1/sdk/assets/{id}/download-urls response — presigned URLs per asset file."""

    asset_id: str
    name: str
    files: list[DownloadFile]
    expires_in: int            # URL lifetime (seconds)
    # Number of files the server should have issued. If files has fewer, some signatures failed (request again).
    expected_file_count: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def complete(self) -> bool:
        return self.expected_file_count is None or len(self.files) >= self.expected_file_count

    @classmethod
    def from_dict(cls, d: dict, *, asset_id: str = "") -> "AssetDownload":
        items = [i for i in d.get("items") or [] if isinstance(i, dict)]
        item = items[0] if items else {}
        expected = item.get("expectedFileCount")
        return cls(
            asset_id=item.get("assetExternalId") or asset_id,
            name=str(item.get("name", "") or ""),
            files=[DownloadFile(path=str(f.get("relativePath", "")), url=str(f.get("url", "")),
                                size_bytes=f.get("sizeBytes"), content_hash=f.get("contentHash"))
                   for f in item.get("files") or [] if isinstance(f, dict)],
            expires_in=_as_int(d.get("expiresIn")),
            expected_file_count=None if expected is None else _as_int(expected),
            raw=d,
        )


@dataclass
class TaskOutputs:
    """GET /v1/sdk/tasks/{id}/outputs response — task output files and presigned URLs (valid for a few hours)."""

    task_id: str
    files: list[DownloadFile]
    expired: bool              # outputs were deleted (asset deleted, ...) — files is empty
    storage_provider: str      # meshive_r2 (managed) | user_s3
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict, *, task_id: str = "") -> "TaskOutputs":
        destination = d.get("destination") or {}
        return cls(
            task_id=task_id,
            # downloadUrl is signed as an attachment — without it the preview url works for downloading too.
            files=[DownloadFile(path=str(f.get("filename", "")), url=str(f.get("downloadUrl") or f.get("url") or ""),
                                size_bytes=f.get("sizeBytes"))
                   for f in d.get("files") or [] if isinstance(f, dict)],
            expired=bool(d.get("expired", False)),
            storage_provider=str(destination.get("provider", "") or ""),
            raw=d,
        )


@dataclass
class AssetImported:
    """POST /v1/sdk/assets/import response — an asset registered by link (bytes aren't copied; ready immediately)."""

    asset_id: str
    name: str
    status: str
    ingest_source: str            # hf_import | civitai_import | url_import
    file_count: int
    total_bytes: int
    is_gated: bool = False        # the source can't be read without a token/key — a saved token is needed at startup too
    resolved_commit: str | None = None   # HF: the pinned commit (doesn't change after registration)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "AssetImported":
        return cls(asset_id=str(d.get("assetExternalId", "") or ""), name=str(d.get("name", "") or ""),
                   status=str(d.get("status", "") or ""), ingest_source=str(d.get("ingestSource", "") or ""),
                   file_count=_as_int(d.get("fileCount")), total_bytes=_as_int(d.get("totalBytes")),
                   is_gated=bool(d.get("isGated", False)), resolved_commit=d.get("resolvedCommit"), raw=d)


@dataclass
class AssetPage:
    """One page of the GET /v1/sdk/assets response. Iterate items with `for asset in page`."""

    items: list[Asset]
    total: int                 # total number of assets matching the filters
    page: int
    page_size: int
    raw: dict = field(default_factory=dict, repr=False)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def pages(self) -> int:
        """Total number of pages (at least 1)."""
        return max(1, -(-self.total // self.page_size)) if self.page_size > 0 else 1

    @classmethod
    def from_dict(cls, d: dict, *, namespace_name: str = "") -> "AssetPage":
        items = [Asset.from_dict(a, namespace_name=namespace_name)
                 for a in d.get("items") or [] if isinstance(a, dict)]
        return cls(
            items=items,
            total=_as_int(d.get("total")),
            page=_as_int(d.get("page")) or 1,
            page_size=_as_int(d.get("pageSize")) or (len(items) or 1),
            raw=d,
        )


@dataclass
class AssetStorage:
    """GET /v1/sdk/assets/storage-summary response — managed storage amount/unit price/estimated monthly cost + credit block state.

    If credit_state is grace, it gets blocked at blocks_at; if blocked, the managed data is deleted at
    purge_deadline_at. User S3 assets aren't billed by Meshive and aren't counted in managed_bytes.
    """

    managed_bytes: int
    price_per_gb_month: float
    estimated_monthly_cost: float
    credit_state: str          # normal | grace | blocked
    credit_blocked: bool
    credit_depleted_at: datetime | None = None
    blocks_at: datetime | None = None
    purge_deadline_at: datetime | None = None
    paid_balance_available: bool = True
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "AssetStorage":
        return cls(
            managed_bytes=_as_int(d.get("managedBytes")),
            price_per_gb_month=_as_float(d.get("pricePerGbMonth")),
            estimated_monthly_cost=_as_float(d.get("estimatedMonthlyCost")),
            credit_state=str(d.get("creditState", "") or ""),
            credit_blocked=bool(d.get("creditBlocked", False)),
            credit_depleted_at=_parse_dt(d.get("creditDepletedAt")),
            blocks_at=_parse_dt(d.get("blocksAt")),
            purge_deadline_at=_parse_dt(d.get("purgeDeadlineAt")),
            paid_balance_available=bool(d.get("paidBalanceAvailable", True)),
            raw=d,
        )


# =============================================================================
# Write surface (0.1.0) — estimates, accepted responses, logs
# =============================================================================

@dataclass
class PodEstimate:
    """POST /v1/sdk/pods/estimate response — hourly pod estimate. The billed price is fixed on the node it lands on."""

    price_per_hour: str            # USD/h (string, normalized)
    breakdown: dict                # {"gpu": "...", "cpu_extra": "...", "ram_extra": "...", ...}
    resources: dict                # gpu_model/vram_gb/gpu_count/vcpu/ram_gb/disk_gb/rental_type ...
    availability: dict             # available_gpus/max_gpus_per_pod/machine_count
    template: dict                 # id/name/image/hardware_type
    volumes: list = field(default_factory=list)
    note: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "PodEstimate":
        return cls(price_per_hour=str(d.get("pricePerHourUsd", "0")), breakdown=d.get("breakdown") or {},
                   resources=d.get("resources") or {}, availability=d.get("availability") or {},
                   template=d.get("template") or {}, volumes=list(d.get("volumes") or []),
                   note=d.get("note") or "", raw=d)


@dataclass
class PodCreated:
    """POST /v1/sdk/pods response (202). pod_name is assigned asynchronously, so it is None right after creation —
    find the pod with user_alias == name in `list_pods()` or use `wait_for_pod_by_name()`."""

    name: str
    workspace: str
    transaction_id: int | str | None
    estimate: PodEstimate
    pod_name: str | None = None
    accepted: bool = True
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "PodCreated":
        return cls(name=d.get("name", ""), workspace=d.get("workspace", ""), transaction_id=d.get("transactionId"),
                   estimate=PodEstimate.from_dict(d.get("estimate") or {}), pod_name=d.get("podName"),
                   accepted=bool(d.get("accepted", True)), raw=d)


@dataclass
class ResourceAction:
    """Accepted response for stop/start/restart/delete/scale, etc. State changes are asynchronous — poll with the read methods."""

    resource: str                  # pod | storage | serving | task
    id: str                        # pod_name / pv_name / serving id / task id
    action: str
    workspace: str | None = None
    accepted: bool = True
    result: object = None          # return value of the underlying operation (transaction id, ...)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict, *, resource: str | None = None) -> "ResourceAction":
        return cls(resource=d.get("resource") or resource or "", id=str(d.get("id") or d.get("podName") or ""),
                   action=d.get("action", ""), workspace=d.get("workspace"), accepted=bool(d.get("accepted", True)),
                   result=d.get("result"), raw=d)


@dataclass
class StorageEstimate:
    """POST /v1/sdk/storages/estimate response."""

    price_per_hour: str
    price_per_gb_month: str
    size_gb: int
    storage_type: str
    max_size_gb: int
    disk_type: str = "NVMe"        # the price is set by the (storage_type, disk_type) combination
    note: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "StorageEstimate":
        return cls(price_per_hour=str(d.get("pricePerHourUsd", "0")), price_per_gb_month=str(d.get("pricePerGbMonthUsd", "0")),
                   size_gb=_as_int(d.get("sizeGb")), storage_type=str(d.get("storageType", "")),
                   max_size_gb=_as_int(d.get("maxSizeGb")), disk_type=str(d.get("diskType") or "NVMe"),
                   note=d.get("note") or "", raw=d)


@dataclass
class StorageCreated:
    """POST /v1/sdk/storages response (202). After creation, find pv_name in `list_storages()` by user_alias == name."""

    name: str
    workspace: str
    transaction_id: int | str | None
    estimate: StorageEstimate
    pv_name: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "StorageCreated":
        return cls(name=d.get("name", ""), workspace=d.get("workspace", ""), transaction_id=d.get("transactionId"),
                   estimate=StorageEstimate.from_dict(d.get("estimate") or {}), pv_name=d.get("pvName"), raw=d)


@dataclass
class TaskEstimate:
    """POST /v1/sdk/tasks/estimate response. None for CPU-preset tasks, whose price depends on the node they land on."""

    price_per_hour: str | None
    max_cost: str | None           # total bill ceiling, None when storage/fetch costs cannot be bounded
    max_duration: int
    resources: dict
    note: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskEstimate":
        price = d.get("pricePerHourUsd")
        cost = d.get("maxCostUsd")
        return cls(price_per_hour=None if price is None else str(price), max_cost=None if cost is None else str(cost),
                   max_duration=_as_int(d.get("maxDurationS")), resources=d.get("resources") or {},
                   note=d.get("note") or "", raw=d)


@dataclass
class TaskSubmitted:
    """POST /v1/sdk/tasks response (202) — the submitted task + estimate."""

    task: Task
    estimate: TaskEstimate
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskSubmitted":
        return cls(task=Task.from_dict(d.get("task") or {}), estimate=TaskEstimate.from_dict(d.get("estimate") or {}),
                   raw=d)


@dataclass
class LogLine:
    line: str
    ts: str | None = None


@dataclass
class Logs:
    """GET /v1/sdk/pods/{pod}/logs · /tasks/{id}/logs response — the last N lines."""

    pod_name: str
    workspace: str
    source: str                    # live | archive | external | none
    lines: list[LogLine]
    count: int
    truncated: bool = False
    note: str | None = None
    container: str | None = None
    task_id: str | None = None
    finished: bool | None = None   # task logs only
    next_cursor: int | None = None # external-provider tasks only — get_task_logs(cursor=next_cursor) fetches the increment after it
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def text(self) -> str:
        return "\n".join(item.line for item in self.lines)

    @classmethod
    def from_dict(cls, d: dict) -> "Logs":
        lines = [LogLine(line=str(item.get("line", "")), ts=item.get("ts")) for item in d.get("lines") or []
                 if isinstance(item, dict)]
        return cls(pod_name=d.get("podName", ""), workspace=d.get("workspace", ""), source=str(d.get("source", "")),
                   lines=lines, count=_as_int(d.get("count")) or len(lines), truncated=bool(d.get("truncated", False)),
                   note=d.get("note"), container=d.get("container"), task_id=d.get("taskId"),
                   finished=d.get("finished"), next_cursor=d.get("nextCursor"), raw=d)
