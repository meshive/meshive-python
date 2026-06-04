"""SDK 응답 dataclass.

서버 응답은 camelCase(JSON) 이므로 from_dict 에서 camelCase 키를 읽는다.
깊게 중첩된 필드(파드의 machine/template/request 등)는 일일이 타입화하지 않고
원본 dict 를 `.raw` 에 보존한다 → 백엔드가 필드를 추가해도 SDK 가 깨지지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
