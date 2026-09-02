"""SDK 응답 dataclass.

서버 응답은 camelCase(JSON) 이므로 from_dict 에서 camelCase 키를 읽는다.
깊게 중첩된 필드(파드의 machine/template/request 등)는 일일이 타입화하지 않고
원본 dict 를 `.raw` 에 보존한다 → 백엔드가 필드를 추가해도 SDK 가 깨지지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


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
            raw=d,
        )


@dataclass
class Pod:
    """GET /v1/sdk/pods[/{name}] 항목 (PodMetaData).

    자주 쓰는 top-level 스칼라만 타입화. machine/template/request/linkedStorages
    등 중첩 구조는 `.raw` 로 접근한다.
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

    @classmethod
    def from_dict(cls, d: dict) -> "Pod":
        return cls(
            pod_name=d.get("podName", ""),
            namespace_name=d.get("namespaceName", ""),
            user_alias=d.get("userAlias", ""),
            status=d.get("status", ""),
            rental_type=d.get("rentalType", ""),
            price_per_hour=str(d.get("pricePerHour", "0")),
            is_maintenance=bool(d.get("isMaintenance", False)),
            created_at=_parse_dt(d.get("createdAt")),
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
    """GPU 1장의 사용률. rate 는 0.0~1.0, 측정 불가면 None."""

    gpu_number: int
    core_usage_rate: float | None = None
    vram_usage_rate: float | None = None
    vram_size: float = 0.0
    temp: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "GpuUsage":
        return cls(gpu_number=_as_int(d.get("gpuNumber")),
                   core_usage_rate=_as_optional_float(d.get("coreUsageRate")),
                   vram_usage_rate=_as_optional_float(d.get("vramUsageRate")),
                   vram_size=_as_float(d.get("vramSize")),
                   temp=_as_optional_float(d.get("temp")))


@dataclass
class PodMetrics:
    """GET /v1/sdk/pods/{pod_name}/metrics 응답 (ResourceUsage) — 파드 리소스 사용량.

    usage rate 는 0.0~1.0, Prometheus 조회 실패 시 None (요청량 필드는 그대로 채워진다).
    연결 스토리지 사용량은 `.raw["storage"]`.
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
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Serving":
        price = d.get("pricePerHour")
        healthy = d.get("healthyReplicas")
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
            raw=d,
        )


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

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        exit_code = d.get("exitCode")
        return cls(
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
    """자산 버전 1건 (AssetVersionSummary / AssetVersionDetail). 상세의 파일 목록은 `.raw["files"]`."""

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


@dataclass
class Asset:
    """GET /v1/sdk/assets 의 행(AssetListRow) 또는 /assets/{id} 의 상세(AssetDetailResponse).

    목록 행은 최신 READY 버전 요약(latest_version)만, 상세는 버전 스택(versions, 최신순)을
    담는다. size_bytes / file_count 는 최신 READY 버전 기준. 병합 안내·pickle 뱃지·사용 중
    컨텍스트(activeUsageContexts) 등은 `.raw`.
    """

    asset_id: str              # assetExternalId ("asset_…", 조회 키)
    name: str
    asset_type: str            # dataset | model | adapter | checkpoint | output | config | file
    status: str                # active | source_missing | frozen | deleted | purged | merged
    status_reason: str | None
    storage_provider: str      # meshive_r2 (managed) | user_s3 | external, 버전이 없으면 ""
    version_count: int         # READY·미삭제 버전 수
    size_bytes: int
    file_count: int
    in_use: bool
    namespace_name: str = ""
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    latest_version: AssetVersion | None = None
    versions: list[AssetVersion] = field(default_factory=list)   # 상세 응답에서만 채워진다
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict, *, namespace_name: str = "") -> "Asset":
        versions = [AssetVersion.from_dict(v) for v in d.get("versions") or [] if isinstance(v, dict)]
        latest_raw = d.get("latestVersion")
        latest = AssetVersion.from_dict(latest_raw) if isinstance(latest_raw, dict) else None
        ready = [v for v in versions if v.is_ready]
        if latest is None and ready:
            latest = max(ready, key=lambda v: v.version_number)
        version_count = _as_int(d["versionCount"]) if "versionCount" in d else len(ready)
        size_bytes = (_as_int(d["latestTotalSizeBytes"]) if "latestTotalSizeBytes" in d
                      else (latest.total_size_bytes if latest else 0))
        file_count = (_as_int(d["latestFileCount"]) if "latestFileCount" in d
                      else (latest.file_count if latest else 0))
        return cls(
            asset_id=d.get("assetExternalId", ""),
            name=d.get("name", ""),
            asset_type=str(d.get("assetType", "") or ""),
            status=str(d.get("status", "") or ""),
            status_reason=d.get("statusReason"),
            storage_provider=str(d.get("storageProvider") or ""),
            version_count=version_count,
            size_bytes=size_bytes,
            file_count=file_count,
            in_use=bool(d.get("inUse", False)),
            namespace_name=d.get("namespaceName") or namespace_name,
            created_by=d.get("createdBy"),
            created_at=_parse_dt(d.get("createdAt")),
            updated_at=_parse_dt(d.get("updatedAt")),
            latest_version=latest,
            versions=versions,
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
