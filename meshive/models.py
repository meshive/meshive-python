"""SDK 응답 dataclass.

서버 응답은 camelCase(JSON) 이므로 from_dict 에서 camelCase 키를 읽는다.
깊게 중첩된 필드(파드의 machine/template/request 등)는 일일이 타입화하지 않고
원본 dict 를 `.raw` 에 보존한다 → 백엔드가 필드를 추가해도 SDK 가 깨지지 않는다.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def _parse_dt(value: str | None) -> datetime | None:
    """ISO8601 문자열 → datetime. 'Z' 접미사 허용. 파싱 실패 시 None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _as_float(value: object) -> float:
    """숫자/숫자문자열 → float. 변환 불가 시 0.0 (서버가 Numeric 을 문자열로 줘도 안전)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass
class WhoAmI:
    """GET /v1/sdk/me 응답 — 현재 API Key 의 소유자."""

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
    """워크스페이스 내 리소스 개수 요약."""

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
    """GET /v1/sdk/workspaces 항목 (NamespaceMetaData)."""

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
    # 키 주인의 이 워크스페이스 역할 — admin | billing | viewer. viewer 는 쓰기가 403 이다.
    # 표시용이지 권한 판정이 아니다(서버가 쓰기마다 다시 본다). 서버가 모르면 None.
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
    """Pod 의 열린 포트 하나 (template.endpoints[]) — 콘솔 Connect 탭의 URL 줄."""

    name: str                  # userAlias (예: "ComfyUI", "Jupyter")
    port: int                  # containerPort
    port_type: str             # connect | http | tcp | ...
    external_url: str | None
    internal_url: str | None
    is_external: bool
    # 콘솔과 같은 판정: readinessState(preparing|ready|interrupted)가 있으면 그것, 없으면 isReady 로 ready|preparing.
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
    """접속에 필요한 값 하나 — 템플릿이 showOnConnect 로 표시한 env (예: ComfyUI 의 ACCESS_PASSWORD).

    `is_secret` 값은 API 키만 있으면 누구나 읽는 응답에 들어 있다. 화면·로그에 그대로 찍지 말 것 —
    repr 에서 빠지고, CLI 는 `--show-secrets` 없이는 가린다.
    """

    key: str
    value: str = field(repr=False)
    is_secret: bool = False
    is_auto_generated: bool = False   # 템플릿이 Pod 마다 만든 값(사용자가 정하지 않음)


@dataclass
class Pod:
    """GET /v1/sdk/pods[/{name}] 항목 (PodMetaData).

    자주 쓰는 top-level 스칼라와 접속 정보(endpoints·connect_credentials)를 타입화.
    machine/template/request/linkedStorages 등 나머지 중첩 구조는 `.raw` 로 접근한다.
    서버가 주지 않은 값은 None 이다(옛 서버) — False/0 으로 읽지 말 것.
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
    billing_active: bool | None = None          # compute 과금 중인가 (정지 Pod 의 storage 과금은 별개)
    is_downloader: bool | None = None           # 시스템 자산 다운로드 Pod (무료, 사용자가 못 지운다)
    cpu_premium: bool | None = None
    uptime_premium: bool | None = None
    internet_premium: bool | None = None
    # 원래 노드에서 지금 시작할 수 없는 이유(stopped·waiting 에만): spec_mismatch | hardware_changed |
    # node_unavailable | requirement_unmet | capacity. 시작할 수 있거나 판정할 수 없으면 None.
    same_node_unavailable_reason: str | None = None
    can_restart_on_same_node: bool | None = None   # stopped 에서만 계산된다
    can_restart_on_any_node: bool | None = None
    waiting_mode: str | None = None              # waiting 일 때 same_node | any_node
    stop_reason_code: str | None = None          # None 또는 "user" = 사용자 정지, 그 외는 시스템 자동 정지
    stop_reason_detail: str | None = None
    stop_reason_at: datetime | None = None
    container_running_at: datetime | None = None  # connect Pod 의 컨테이너가 Running 이 된 시각

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
            # 콘솔 CredentialLedger 와 같은 선택 — showOnConnect 인 env 만.
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
class Machine:
    """GET /v1/sdk/machines[/{machine_id}] 항목 — host 가 등록한 머신.

    웹 콘솔의 MachineDataInterface 와 같은 스키마(camelCase)지만, SDK read 표면은
    민감 필드(ssh/ipmi credentials, grafana, bootReport 등)를 제외한 trim DTO 를
    받는다. 자주 쓰는 스칼라만 타입화하고 — status/gpu/earning 처럼 중첩에 있던
    표시용 필드는 끌어올린다 — 나머지(specs/state 전체/podUses 등)는 `.raw` 로 접근.
    """

    machine_id: str       # id (조회 키)
    name: str             # 유저 라벨
    machine_type: str     # "gpu" | "cpu" | "storage"
    status: str           # state.name (ONLINE/OFFLINE/MAINTENANCE/...). stageState 는 .raw.
    gpu_model: str         # specs.gpu (cpu/storage 머신은 빈 문자열)
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
# 0.0.7 확장 read 표면 — 워크스페이스 상세 / 스토리지 / 메트릭 / GPU / 계정 / 템플릿 / 서버리스
# =============================================================================

def _parse_date(value: str | None) -> date | None:
    """'YYYY-MM-DD' (또는 ISO datetime) → date. 파싱 실패 시 None."""
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
    """숫자/숫자문자열 → int. None/변환 불가 시 0."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_optional_float(value: object) -> float | None:
    """메트릭 usage rate 처럼 '측정 불가(None)' 와 0 을 구분해야 하는 값."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class ResourceCondition:
    """워크스페이스 상세의 리소스 종류별 상태 카운트 (type: pod | storage | serverless)."""

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
    """워크스페이스 일별 비용 (USD). 항목별 합이 total."""

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
    """GET /v1/sdk/workspaces/{namespace} 응답 (NamespaceDetailData).

    비용(현재 시간당/주간 일평균/일별 내역)과 리소스 요약을 타입화한다. 유지보수 일정
    (maintenanceSchedule)과 호스트 메시지(messageFromHost)는 `.raw` 로 접근한다.
    """

    namespace_name: str
    workspace_name: str
    price_per_hour: str        # costData.currentUsage — 지금 과금 중인 시간당 합계 (USD)
    weekly_avg_daily_cost: str  # costData.weeklyAvgUsage — 최근 7일 일평균 (USD)
    gpus: int
    vcpus: int
    ram: int
    total_storage: float
    resources: list[ResourceCondition] = field(default_factory=list)
    costs: list[DailyCost] = field(default_factory=list)   # 최근 일별 비용 (오래된 순)
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


@dataclass
class InitLog:
    """입력 자산 다운로드 컨테이너 로그 발췌 하나."""

    container: str      # source-fetch (외부 원본) | init-fetch (업로드 자산)
    lines: list[str]
    previous: bool      # 재시작 전(실패를 겪은) 인스턴스의 로그인가


@dataclass
class Transaction:
    """진행 중인 pod 작업 하나. `status: creating` 이 왜 길어지는지 알려주는 유일한 수단.

    `step` 이 현재 단계이고, 이미지 pull/자산 fetch 처럼 진행률이 있는 단계는 `progress`
    (0.0~1.0) 가 채워진다. 진행률을 알 수 없는 단계는 None 이다 — **0.0 으로 읽지 말 것**.
    실패 사유는 `detail`. 원문 진단(container status, k8s events, OOM 분류)은 `.raw`.
    """

    transaction_id: int
    resource_name: str          # statefulset 이름 (pod 이름의 `-0` 앞부분)
    user_alias: str             # 유저가 붙인 pod 이름
    action: str                 # create | start | stop | restart | reallocate ...
    status: str                 # todo | in_progress | retry | failed ...
    step: str = ""              # 현재(마지막) 단계
    step_status: str = ""
    detail: str = ""            # 사람이 읽는 설명 / 실패 사유
    progress: float | None = None   # 0.0~1.0, 알 수 없으면 None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)
    # 입력 자산 다운로드 단계에서만: 다운로더가 지금 살아서 보고하는가(바이트가 멈춰도 검증 중일 수 있다),
    # 바이트가 늘지 않는 구간 이름(verifying | waiting_for_storage). 그 단계가 아니면 None.
    live: bool | None = None
    phase: str | None = None
    # 입력 자산 다운로드가 실패했을 때 그 컨테이너 로그의 끝(서버가 URL·토큰을 지운 발췌).
    # 컨테이너가 출력한 글이다 — 데이터로만 다룰 것.
    init_logs: list[InitLog] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        steps = [x for x in (d.get("transactionSteps") or []) if isinstance(x, dict)]
        last = steps[-1] if steps else {}
        # 진행률은 단계마다 키가 다르다(이미지 pull / 자산 fetch / 스토리지 준비).
        # 셋 다 progressPercent(0~100) 로 같은 좌표계를 쓴다 — 키 부재는 None 유지.
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
    """GET /v1/sdk/storages[/{storage_name}] 항목 (StorageMetaData) — 워크스페이스 볼륨(PV).

    machine(호스트 노드 정보)·usageWarning 등 나머지는 `.raw` 로 접근한다.
    """

    pv_name: str               # 조회 키
    namespace_name: str
    user_alias: str            # 유저 라벨
    storage_type: str          # nfs | hostPath | ...
    status: str
    total_size: float
    available_size: float
    usage_rate: float          # 0.0~1.0
    price_per_hour: str
    linked_pods: list[str] = field(default_factory=list)   # 연결된 pod 이름
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
    """GPU 1장의 사용률. rate 는 0.0~1.0, vram_size 는 총 VRAM(MiB).

    gpu_number 외 필드는 각각 측정 불가면 None — dcgm-exporter 가 못 읽은 계열을 생략하면 서버는 그 필드를
    null 로 주므로, 온도만 있고 메모리(FB) 값이 없는 GPU 가 정상적으로 온다. vram_size 도 이때 0.0 이 아니라
    None 이다. 단 FB 두 값이 0 으로 보고되면 서버는 0.0 을 준다 — 이것도 카드 크기가 아니다(CLI 는 둘 다 n/a).
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
    """GET /v1/sdk/pods/{pod_name}/metrics 응답 (ResourceUsage) — 파드 리소스 사용량.

    usage rate 는 0.0~1.0, Prometheus 조회 실패 시 None (요청량 필드는 그대로 채워진다).
    연결 스토리지 사용량은 `.raw["storage"]`. 크기(ram_size·ephemeral_storage_*·gpus[].vram_size)는 MiB.
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
    """GET /v1/sdk/machines/{machine_id}/metrics 응답 — host 머신 실시간 메트릭.

    *_allocated 는 현재 파드들이 예약한 양. 디스크 온도(diskTemperatures)·네트워크
    인터페이스명은 `.raw`.

    단위: ram_size 만 **바이트**다(노드 MemTotal — 서버가 Prometheus 값을 그대로 준다). ram_allocated·
    root/pv_volume_size·gpus[].vram_size 는 MiB, network_receive/transmit 은 바이트/초.
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
    """GET /v1/sdk/gpus 항목 (GpuDistribution) — 지금 대여 가능한 GPU (model, VRAM) 티어.

    combinations(머신별 최대 할당 가능량)는 available_gpus / max_gpus_per_pod 로 요약하고
    원본은 `.raw["combinations"]`.
    """

    gpu_model: str
    vram: int                  # GB
    rental_type: str           # demand | spot
    price_per_hour: str        # GPU 1장 시간당 (USD)
    vcpu_recommended: int
    ram_recommended: int
    available_gpus: int        # 전 머신 합계
    max_gpus_per_pod: int      # 한 머신에서 한 파드에 줄 수 있는 최대 장수
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
    """GET /v1/sdk/api-keys 항목 — 내 Meshive API Key (활성 키만).

    평문은 발급 시 한 번만 보이고 서버에는 해시만 남으므로 여기서는 표시용 prefix 만 온다.
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
    """GET /v1/sdk/credit 응답 — 크레딧 잔액 (USD).

    balance = paid_balance + bonus_balance. bonus 는 serverless 추론에만 쓸 수 있고,
    GPU 파드/워크스페이스 실행에는 paid_balance 가 필요하다.
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
    """GET /v1/sdk/credit/history 항목 — 충전/환불 원장 1건 (환불은 음수).

    Stripe 영수증/인보이스 링크는 SDK 표면에 실리지 않는다 (콘솔에서 확인).
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
    """host 일별 수익 (USD)."""

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
    """GET /v1/sdk/earnings 응답 — host 수익 요약 (USD) + 일별 내역 (최신순)."""

    current_hourly: float          # 지금 과금 중인 파드/볼륨의 시간당 수익
    daily: float                   # 오늘 누적
    accumulated_until_payout: float  # 다음 정산까지 누적
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
    """GET /v1/sdk/members 항목 — 워크스페이스 멤버 (role: admin | billing | viewer)."""

    user: str                  # email (조회 키)
    role: str
    joined_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Member":
        return cls(user=d.get("user", ""), role=str(d.get("role", "") or ""),
                   joined_at=_parse_dt(d.get("createdAt")), raw=d)


@dataclass
class Template:
    """GET /v1/sdk/templates[/{template_id}] 항목 (TemplateResponse).

    envs/endpoints/volumeMounts/semanticPaths 등 배포 명세는 `.raw`.
    """

    template_id: int           # 조회 키
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
    """GET /v1/sdk/servings[/{serving_id}] 항목 (ServingGroupResponse) — serverless serving 배포.

    replicas(개별 replica 상태/GPU/과금)와 라이브 메트릭(throughput/latency 등)은 `.raw`.
    """

    serving_id: int            # 조회 키 (group id)
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
    price_per_hour: str        # 과금 중 replica 단가 합 (USD), 없으면 "0"
    billing_active: bool
    autoscale: bool = False                 # auto_scale_enabled
    price_cap_per_hour: str | None = None   # replica 당 시간당 상한 (USD), None = 무제한
    raw: dict = field(default_factory=dict, repr=False)

    def scale_raises_cost(self, *, min_replicas: int | None = None, max_replicas: int | None = None,
                          autoscale: bool | None = None, price_cap_per_hour: object = None) -> bool:
        """scale_serving 인자가 시간당 비용을 **늘릴 수 있는지** — CLI/MCP 가 확인을 요구하는 기준.
        replica 범위 확대, autoscale 켜기(꺼져 있던 경우), replica 당 상한 인상(무제한은 이미 최대)."""
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
                return True     # 해석 불가한 값은 안전하게 "확인 필요"
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
    """task 에 붙은 입력 자산 하나 (상세 응답의 inputAssets[])."""

    asset_id: str
    name: str
    target_dir: str
    reference_mode: str | None = None   # None = managed / "external" = 실행마다 원본에서 다시 받는다
    ingest_source: str | None = None    # hf_import | civitai_import | web_upload | ...


@dataclass
class Task:
    """GET /v1/sdk/tasks[/{task_id}] 항목 (TaskResponse / TaskDetailResponse) — serverless task.

    단건 응답의 script/requirements/env(secret 값은 마스킹)/비용 분해는 `.raw`.
    """

    task_id: str               # 조회 키 (externalId, "task_...")
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
    # 입력 자산 다운로드에서 실패했으면 그 자산 id.
    failed_asset_id: str | None = None
    # 입력 자산 — 상세(`get_task`)에만 온다. 목록에서는 빈 리스트.
    input_assets: list[TaskInputAsset] = field(default_factory=list)
    # 출력 업로드는 task 상태와 별개 축이다: 끝난 task 도 출력은 아직 올라가는 중일 수 있다.
    # state: pending | uploading | completed | failed | skipped | discarded. None = 옛 행(skipped 와 같게 읽는다).
    output_upload_state: str | None = None
    output_files_declared: int | None = None
    output_files_completed: int | None = None
    output_bytes_declared: int | None = None
    output_bytes_completed: int | None = None
    output_upload_last_error: str | None = None   # 마지막 업로드 실패 사유 (자격증명 오류 등)
    outputs_purged_at: datetime | None = None      # 출력이 지워진 시각

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
    """[Deprecated — 0.2 에서 제거] 자산 버전 1건. 서버에 자산 버전은 더 이상 없다.

    서버가 옛 SDK 를 위해 자산 요약을 versionNumber=1 인 단일 버전처럼 되비춘 값이다.
    `Asset` 의 자산 레벨 필드(size_bytes·file_count·upload_status·files …)를 쓸 것.
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
    """자산의 파일 하나 (상세 응답의 files[])."""

    path: str                  # relativePath — 자산 안의 상대 경로
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
    """자산을 지금 쓰는 곳 하나 (상세 응답의 activeUsageContexts[]) — 삭제·변경을 막는 이유."""

    kind: str                  # pod | task | serving ...
    identifier: str
    name: str
    status: str
    role: str | None = None


@dataclass
class Asset:
    """GET /v1/sdk/assets 의 행(AssetListRow) 또는 /assets/{id} 의 상세(AssetDetailResponse).

    자산에는 버전이 없다 — 크기·파일 수·업로드 상태는 자산 자체의 값이다. 파일 목록(files)과
    사용 중인 곳(active_usage_contexts)은 상세에만 온다. pickle 뱃지 등 나머지는 `.raw`.
    `version_count`·`latest_version`·`versions` 는 deprecated(0.2 에서 제거).
    """

    asset_id: str              # assetExternalId ("asset_…", 조회 키)
    name: str
    asset_type: str            # dataset | model | adapter | checkpoint | output | config | file
    status: str                # active | source_missing | frozen | deleted | purged | merged
    status_reason: str | None
    storage_provider: str      # meshive_r2 (managed) | user_s3 | external, 모르면 ""
    # 외부 원본을 아직 재지 않은 링크 자산은 크기·파일 수를 모른다 → None (0 이 아니다).
    size_bytes: int | None
    file_count: int | None
    in_use: bool
    namespace_name: str = ""
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)
    upload_status: str = ""    # uploading | ready | failed
    stale: bool = False        # uploading 인데 끝낼 Pod 이 이미 없다 (콘솔 "Interrupted")
    ingest_source: str = ""    # web_upload | hf_import | civitai_import | harvest | task_output | ...
    import_failure_reason: str | None = None
    kind: str | None = None
    semantic_type: str | None = None   # 목록 행에만 온다
    files: list[AssetFile] = field(default_factory=list)                   # 상세에만
    active_usage_contexts: list[AssetUsage] = field(default_factory=list)  # 상세에만

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

    # --- deprecated: 서버가 옛 SDK 용으로 되비추는 버전 필드(Phase C 에서 사라진다) -----------------

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
    """다운로드할 파일 하나 — presigned URL 은 짧게 산다(`expires_in`). URL 에 Meshive 키를 붙이지 말 것."""

    path: str                  # 자산 안 상대 경로 / task 결과물 파일 이름
    url: str
    size_bytes: int | None = None
    content_hash: str | None = None


@dataclass
class AssetDownload:
    """GET /v1/sdk/assets/{id}/download-urls 응답 — 자산 파일별 presigned URL."""

    asset_id: str
    name: str
    files: list[DownloadFile]
    expires_in: int            # URL 유효 시간(초)
    # 서버가 발급했어야 할 파일 수. files 가 이보다 적으면 일부 서명이 실패한 것이다(다시 요청).
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
    """GET /v1/sdk/tasks/{id}/outputs 응답 — task 결과물 파일과 presigned URL(수 시간 유효)."""

    task_id: str
    files: list[DownloadFile]
    expired: bool              # 결과물이 지워졌다(자산 삭제 등) — files 가 비어 있다
    storage_provider: str      # meshive_r2 (managed) | user_s3
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict, *, task_id: str = "") -> "TaskOutputs":
        destination = d.get("destination") or {}
        return cls(
            task_id=task_id,
            # downloadUrl 은 첨부(attachment)로 서명된 것 — 없으면 미리보기 url 로도 받을 수 있다.
            files=[DownloadFile(path=str(f.get("filename", "")), url=str(f.get("downloadUrl") or f.get("url") or ""),
                                size_bytes=f.get("sizeBytes"))
                   for f in d.get("files") or [] if isinstance(f, dict)],
            expired=bool(d.get("expired", False)),
            storage_provider=str(destination.get("provider", "") or ""),
            raw=d,
        )


@dataclass
class AssetPage:
    """GET /v1/sdk/assets 응답 한 페이지. `for asset in page` 로 항목을 순회한다."""

    items: list[Asset]
    total: int                 # 필터 조건에 맞는 전체 자산 수
    page: int
    page_size: int
    raw: dict = field(default_factory=dict, repr=False)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def pages(self) -> int:
        """전체 페이지 수 (최소 1)."""
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
    """GET /v1/sdk/assets/storage-summary 응답 — managed 저장량/단가/월 예상 비용 + 크레딧 차단 상태.

    credit_state 가 grace 면 blocks_at 에 차단, blocked 면 purge_deadline_at 에 managed
    저장분이 삭제된다. 유저 S3 자산은 Meshive 과금 대상이 아니라 managed_bytes 에 안 들어간다.
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
# 쓰기 표면 (0.1.0) — 견적·수락 응답·로그
# =============================================================================

@dataclass
class PodEstimate:
    """POST /v1/sdk/pods/estimate 응답 — 파드 시간당 견적(추정). 청구가는 착지 노드에서 확정된다."""

    price_per_hour: str            # USD/h (문자열, 정규화)
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
    """POST /v1/sdk/pods 응답 (202). pod_name 은 K8sCS 가 확정하므로 생성 직후에는 None —
    `list_pods()` 에서 user_alias == name 인 파드로 찾거나 `wait_for_pod_by_name()` 을 쓴다."""

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
    """정지/시작/재시작/삭제/스케일 등 수락 응답. 상태 변화는 비동기 — 조회 메서드로 폴링한다."""

    resource: str                  # pod | storage | serving | task
    id: str                        # pod_name / pv_name / serving id / task id
    action: str
    workspace: str | None = None
    accepted: bool = True
    result: object = None          # 위임된 웹 핸들러의 반환값(트랜잭션 id 등)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict, *, resource: str | None = None) -> "ResourceAction":
        return cls(resource=d.get("resource") or resource or "", id=str(d.get("id") or d.get("podName") or ""),
                   action=d.get("action", ""), workspace=d.get("workspace"), accepted=bool(d.get("accepted", True)),
                   result=d.get("result"), raw=d)


@dataclass
class StorageEstimate:
    """POST /v1/sdk/storages/estimate 응답."""

    price_per_hour: str
    price_per_gb_month: str
    size_gb: int
    storage_type: str
    max_size_gb: int
    disk_type: str = "NVMe"        # 가격이 (storage_type, disk_type) 조합으로 정해진다
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
    """POST /v1/sdk/storages 응답 (202). pv_name 은 생성 후 `list_storages()` 에서 user_alias == name 으로 찾는다."""

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
    """POST /v1/sdk/tasks/estimate 응답. CPU 프리셋 태스크는 가격이 착지 노드에 따라 달라 None."""

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
    """POST /v1/sdk/tasks 응답 (202) — 제출된 태스크 + 견적."""

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
    """GET /v1/sdk/pods/{pod}/logs · /tasks/{id}/logs 응답 — 마지막 N줄."""

    pod_name: str
    workspace: str
    source: str                    # live | archive | external | none
    lines: list[LogLine]
    count: int
    truncated: bool = False
    note: str | None = None
    container: str | None = None
    task_id: str | None = None
    finished: bool | None = None   # 태스크 로그에서만
    next_cursor: int | None = None # 외부 provider 태스크에서만 — get_task_logs(cursor=next_cursor) 로 그 뒤 증분 조회
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
