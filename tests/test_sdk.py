import asyncio
import json

import httpx
import pytest

from meshive import (
    AsyncMeshive,
    AuthenticationError,
    ConfigurationError,
    Meshive,
    MeshiveAPIError,
    MeshiveError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    WaitTimeoutError,
)
from meshive import _client, _config


# --- fixtures / helpers -----------------------------------------------------

WHOAMI = {"email": "a@b.com", "username": "alice", "userRole": "user"}
WORKSPACE = {
    "namespaceName": "team-ns",
    "workspaceName": "Team",
    "description": "desc",
    "memberCount": 3,
    "status": "active",
    "pricePerHour": "1.50",
    "resources": {"pod": 2, "storage": 1, "serverless": 0},
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-02-01T00:00:00+00:00",
}
POD = {
    "podName": "pod-1",
    "namespaceName": "team-ns",
    "userAlias": "alias",
    "status": "running",
    "rentalType": "on_demand",
    "pricePerHour": "0.90",
    "isMaintenance": False,
    "createdAt": "2026-03-01T12:00:00Z",
    "machine": {"nodeName": "node-a"},  # nested → preserved in .raw only
}
MACHINE = {
    "id": "mac-1",
    "name": "node-a",
    "machineType": "gpu",
    "state": {"name": "ONLINE", "stageState": "COMPLETED"},  # status pulled from state.name
    "specs": {"gpu": "NVIDIA H100", "gpuNumber": 8},          # gpu fields pulled from specs
    "earning": {"hourly": 2.5, "daily": 60.0},
    "uptimeRate": 0.999,
    "hostTier": "gold",
    "sshCredentials": {"password": "secret"},  # nested → preserved in .raw only
}


def sync_client(handler, **kwargs):
    client = Meshive(api_key="meshive_test", base_url="https://api.test", **kwargs)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def async_client(handler, **kwargs):
    client = AsyncMeshive(api_key="meshive_test", base_url="https://api.test", **kwargs)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# --- config -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """재시도/폴링 대기를 실제로 자지 않고 기록만 한다 (테스트는 횟수·간격만 검증)."""
    recorded: list[float] = []

    async def _async_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(_client.time, "sleep", recorded.append)
    monkeypatch.setattr(_client.asyncio, "sleep", _async_sleep)
    return recorded


@pytest.fixture(autouse=True)
def isolate_config_dir(monkeypatch, tmp_path):
    """credentials 파일이 resolve_* 에 끼어들지 않도록 빈 임시 디렉토리로 격리."""
    from meshive import _credentials

    monkeypatch.setenv(_credentials.ENV_CONFIG_DIR, str(tmp_path / "cfg"))


def test_resolve_base_url_default(monkeypatch):
    monkeypatch.delenv(_config.ENV_BASE_URL, raising=False)
    assert _config.resolve_base_url() == _config.DEFAULT_BASE_URL


def test_resolve_base_url_env_and_trailing_slash(monkeypatch):
    monkeypatch.setenv(_config.ENV_BASE_URL, "https://api.dev.meshive.ai/")
    assert _config.resolve_base_url() == "https://api.dev.meshive.ai"


def test_resolve_base_url_explicit_wins(monkeypatch):
    monkeypatch.setenv(_config.ENV_BASE_URL, "https://env")
    assert _config.resolve_base_url("https://explicit") == "https://explicit"


def test_resolve_api_key_env(monkeypatch):
    monkeypatch.setenv(_config.ENV_API_KEY, "meshive_fromenv")
    assert _config.resolve_api_key() == "meshive_fromenv"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv(_config.ENV_API_KEY, raising=False)
    client = Meshive(base_url="https://api.test")
    with pytest.raises(ConfigurationError):
        client.me()


# --- request shape ----------------------------------------------------------

def test_request_url_and_auth_header():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=WHOAMI)

    sync_client(handler).me()
    assert seen["url"] == "https://api.test/v1/sdk/me"
    assert seen["auth"] == "Bearer meshive_test"


def test_list_pods_sends_workspace_query():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"namespaceName": "team-ns", "pods": [POD]})

    sync_client(handler).list_pods("team-ns")
    assert seen["params"] == {"workspace": "team-ns"}


# --- parsing ----------------------------------------------------------------

def test_me_parses():
    me = sync_client(lambda r: httpx.Response(200, json=WHOAMI)).me()
    assert me.email == "a@b.com"
    assert me.username == "alice"
    assert me.user_role == "user"
    assert me.raw == WHOAMI


def test_list_workspaces_parses():
    ws = sync_client(lambda r: httpx.Response(200, json=[WORKSPACE])).list_workspaces()
    assert len(ws) == 1
    assert ws[0].namespace_name == "team-ns"
    assert ws[0].resources.pod == 2
    assert ws[0].created_at.year == 2026


def test_list_pods_unwraps_and_parses():
    pods = sync_client(
        lambda r: httpx.Response(200, json={"namespaceName": "team-ns", "pods": [POD]})
    ).list_pods("team-ns")
    assert len(pods) == 1
    assert pods[0].pod_name == "pod-1"
    assert pods[0].is_maintenance is False
    # nested machine preserved only in raw
    assert pods[0].raw["machine"]["nodeName"] == "node-a"


def test_get_pod_parses():
    pod = sync_client(lambda r: httpx.Response(200, json=POD)).get_pod("pod-1", "team-ns")
    assert pod.pod_name == "pod-1"
    assert pod.rental_type == "on_demand"


def test_list_machines_parses():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[MACHINE])  # bare list, no workspace query

    machines = sync_client(handler).list_machines()
    assert seen["url"] == "https://api.test/v1/sdk/machines"
    assert len(machines) == 1
    m = machines[0]
    assert m.machine_id == "mac-1"
    assert m.name == "node-a"
    assert m.machine_type == "gpu"
    assert m.status == "ONLINE"          # pulled from state.name
    assert m.gpu_model == "NVIDIA H100"  # pulled from specs.gpu
    assert m.gpu_count == 8
    assert m.earning_hourly == 2.5
    assert m.uptime_rate == 0.999
    assert m.host_tier == "gold"
    # nested / sensitive fields preserved only in raw
    assert m.raw["sshCredentials"]["password"] == "secret"
    assert m.raw["state"]["stageState"] == "COMPLETED"


def test_get_machine_parses():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=MACHINE)

    machine = sync_client(handler).get_machine("mac-1")
    assert seen["url"] == "https://api.test/v1/sdk/machines/mac-1"
    assert machine.machine_id == "mac-1"
    assert machine.status == "ONLINE"


def test_machine_tolerates_missing_nested():
    """trim/부분 응답에도 안 깨진다 (state/specs/earning 없음 → 안전한 기본값)."""
    machine = sync_client(
        lambda r: httpx.Response(200, json={"id": "mac-2", "name": "bare"})
    ).get_machine("mac-2")
    assert machine.machine_id == "mac-2"
    assert machine.status == ""
    assert machine.gpu_count == 0
    assert machine.earning_hourly == 0.0


# --- error mapping ----------------------------------------------------------

@pytest.mark.parametrize(
    "status_code,exc",
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (429, RateLimitError),
        (500, None),
    ],
)
def test_error_status_maps_to_exception(status_code, exc):
    from meshive import MeshiveAPIError

    body = {"detail": {"title": "Oops", "message": "bad"}}
    client = sync_client(lambda r: httpx.Response(status_code, json=body))
    expected = exc or MeshiveAPIError
    with pytest.raises(expected) as info:
        client.me()
    assert info.value.status_code == status_code
    assert info.value.message == "bad"
    assert info.value.title == "Oops"


def test_rate_limit_retry_after():
    client = sync_client(
        lambda r: httpx.Response(429, headers={"Retry-After": "12"},
                                 json={"detail": {"message": "slow down"}})
    )
    with pytest.raises(RateLimitError) as info:
        client.me()
    assert info.value.retry_after == 12.0


def test_non_json_error_body():
    from meshive import MeshiveAPIError

    client = sync_client(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(MeshiveAPIError) as info:
        client.me()
    assert info.value.status_code == 502


# --- async ------------------------------------------------------------------

def test_async_me_parses():
    async def run():
        client = async_client(lambda r: httpx.Response(200, json=WHOAMI))
        async with client:
            return await client.me()

    me = asyncio.run(run())
    assert me.email == "a@b.com"


def test_async_get_pod_query():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=POD)

    async def run():
        async with async_client(handler) as client:
            return await client.get_pod("pod-1", "team-ns")

    pod = asyncio.run(run())
    assert pod.pod_name == "pod-1"
    assert seen["params"] == {"workspace": "team-ns"}


def test_async_list_machines_parses():
    async def run():
        async with async_client(lambda r: httpx.Response(200, json=[MACHINE])) as client:
            return await client.list_machines()

    machines = asyncio.run(run())
    assert len(machines) == 1
    assert machines[0].machine_id == "mac-1"
    assert machines[0].gpu_count == 8


# --- retry ------------------------------------------------------------------

def _counting_handler(responses):
    """호출될 때마다 responses 를 순서대로 반환하는 handler + 호출 횟수 카운터."""
    calls = {"n": 0}

    def handler(request):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[index]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, calls


def test_retries_429_then_succeeds(slept):
    handler, calls = _counting_handler([
        httpx.Response(429, json={"detail": {"message": "slow down"}}, headers={"Retry-After": "2"}),
        httpx.Response(200, json=WHOAMI),
    ])
    me = sync_client(handler).me()
    assert me.email == "a@b.com"
    assert calls["n"] == 2
    assert slept == [2.0]  # Retry-After 를 그대로 존중


def test_retry_uses_backoff_without_retry_after(slept):
    handler, calls = _counting_handler([
        httpx.Response(503, json={"detail": {"message": "unavailable"}}),
        httpx.Response(503, json={"detail": {"message": "unavailable"}}),
        httpx.Response(200, json=WHOAMI),
    ])
    sync_client(handler).me()
    assert calls["n"] == 3
    assert slept == [0.5, 1.0]  # 지수 백오프


def test_retries_are_capped_then_raise(slept):
    handler, calls = _counting_handler([httpx.Response(429, json={"detail": {"message": "nope"}})])
    with pytest.raises(RateLimitError):
        sync_client(handler).me()
    assert calls["n"] == 3  # 최초 1회 + max_retries(2)


def test_long_retry_after_is_not_waited_out(slept):
    handler, calls = _counting_handler([
        httpx.Response(429, json={"detail": {"message": "nope"}}, headers={"Retry-After": "3600"}),
    ])
    with pytest.raises(RateLimitError) as info:
        sync_client(handler).me()
    assert calls["n"] == 1  # 한 시간 기다리느니 바로 올린다
    assert info.value.retry_after == 3600.0
    assert slept == []


def test_client_errors_are_not_retried():
    handler, calls = _counting_handler([httpx.Response(404, json={"detail": {"message": "gone"}})])
    with pytest.raises(NotFoundError):
        sync_client(handler).get_pod("pod-1", "team-ns")
    assert calls["n"] == 1


def test_retries_can_be_disabled():
    handler, calls = _counting_handler([httpx.Response(500, json={"detail": {"message": "boom"}})])
    with pytest.raises(MeshiveAPIError):
        sync_client(handler, max_retries=0).me()
    assert calls["n"] == 1


def test_connect_errors_are_retried(slept):
    handler, calls = _counting_handler([
        httpx.ConnectError("refused"),
        httpx.Response(200, json=WHOAMI),
    ])
    assert sync_client(handler).me().email == "a@b.com"
    assert calls["n"] == 2


def test_connect_errors_propagate_after_retries():
    handler, calls = _counting_handler([httpx.ConnectError("refused")])
    with pytest.raises(httpx.ConnectError):
        sync_client(handler).me()
    assert calls["n"] == 3


def test_async_retries(slept):
    handler, calls = _counting_handler([
        httpx.Response(502, json={"detail": {"message": "bad gateway"}}),
        httpx.Response(200, json=WHOAMI),
    ])

    async def run():
        async with async_client(handler) as client:
            return await client.me()

    assert asyncio.run(run()).email == "a@b.com"
    assert calls["n"] == 2
    assert slept == [0.5]


# --- wait_for_pod -----------------------------------------------------------

def _pod_with(status):
    return httpx.Response(200, json={**POD, "status": status})


def test_wait_for_pod_polls_until_target(slept):
    handler, calls = _counting_handler([
        _pod_with("pending"), _pod_with("creating"), _pod_with("running"),
    ])
    pod = sync_client(handler).wait_for_pod("pod-1", "team-ns", interval=5.0)
    assert pod.status == "running"
    assert calls["n"] == 3
    assert slept == [5.0, 5.0]


def test_wait_for_pod_returns_immediately_when_already_there():
    handler, calls = _counting_handler([_pod_with("running")])
    assert sync_client(handler).wait_for_pod("pod-1", "team-ns").status == "running"
    assert calls["n"] == 1


def test_wait_for_pod_accepts_multiple_targets():
    handler, calls = _counting_handler([_pod_with("stopped")])
    pod = sync_client(handler).wait_for_pod("pod-1", "team-ns", until=("running", "stopped"))
    assert pod.status == "stopped"
    assert calls["n"] == 1


def test_wait_for_pod_gives_up_on_terminal_status():
    handler, calls = _counting_handler([_pod_with("error")])
    with pytest.raises(MeshiveError, match="terminal status"):
        sync_client(handler).wait_for_pod("pod-1", "team-ns")
    assert calls["n"] == 1  # timeout 을 채우지 않는다


def test_wait_for_pod_can_target_a_terminal_status():
    """error 를 기다리라고 했으면 error 는 실패가 아니라 목표다."""
    handler, calls = _counting_handler([_pod_with("error")])
    assert sync_client(handler).wait_for_pod("pod-1", "team-ns", until="error").status == "error"
    assert calls["n"] == 1


def test_wait_for_pod_times_out():
    handler, calls = _counting_handler([_pod_with("pending")])
    with pytest.raises(WaitTimeoutError, match="pending"):
        sync_client(handler).wait_for_pod("pod-1", "team-ns", timeout=0.0)
    assert calls["n"] == 1


def test_wait_timeout_is_also_a_builtin_timeout_error():
    handler, _ = _counting_handler([_pod_with("pending")])
    with pytest.raises(TimeoutError):
        sync_client(handler).wait_for_pod("pod-1", "team-ns", timeout=0.0)


def test_wait_for_pod_rejects_empty_target():
    with pytest.raises(ValueError):
        sync_client(lambda r: _pod_with("running")).wait_for_pod("pod-1", "team-ns", until=[])


def test_async_wait_for_pod(slept):
    handler, calls = _counting_handler([_pod_with("pending"), _pod_with("running")])

    async def run():
        async with async_client(handler) as client:
            return await client.wait_for_pod("pod-1", "team-ns", interval=3.0)

    assert asyncio.run(run()).status == "running"
    assert calls["n"] == 2
    assert slept == [3.0]


# =============================================================================
# 0.0.7 확장 read 표면 — URL/쿼리 형태와 파싱
# =============================================================================

def _capture(payload, seen):
    """요청 URL/쿼리를 seen 에 기록하고 payload 를 돌려주는 handler."""
    def handler(request):
        seen["path"] = request.url.path      # 디코드된 경로 (httpx 가 %2F 를 풀어서 준다)
        seen["url"] = str(request.url)       # 실제 전송된 URL (인코딩 검증용)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=payload)
    return handler


WORKSPACE_DETAIL = {
    "workspaceName": "Team", "namespaceName": "team-ns",
    "costData": {"currentUsage": "2.10", "weeklyAvgUsage": "40.5",
                 "details": [{"date": "2026-08-30", "pod": "1.0", "storage": "0.5",
                              "serverless": "0", "task": "0.25", "asset": "0"}]},
    "resourceDetail": {"resourceConditions": [{"type": "pod", "active": 2, "paused": 1, "disabled": 0},
                                              {"type": "storage", "active": 1, "paused": 0, "disabled": 0}],
                       "gpus": 4, "vCpus": 32, "ram": 128, "totalStorage": 500.0},
    "maintenanceSchedule": [], "messageFromHost": [{"id": 1, "title": "hi"}],
}
STORAGE = {
    "pvName": "pv-1", "namespaceName": "team-ns", "userAlias": "datasets",
    "storageType": "nfs", "status": "running", "totalSize": 100.0, "availableSize": 40.0,
    "usageRate": 0.6, "pricePerHour": "0.01000000", "linkedPod": [{"podName": "pod-1", "mountPath": "/data"}],
    "isMaintenance": False, "encrypted": True, "createdAt": "2026-03-01T12:00:00Z",
    "machine": {"machineId": "mac-1"},
}
POD_METRICS = {
    "podName": "pod-1", "cpu": {"core": 8, "usageRate": 0.25}, "ram": {"size": 32, "usageRate": None},
    "gpu": [{"gpuNumber": 0, "coreUsageRate": 0.9, "vramUsageRate": 0.5, "vramSize": 24, "temp": 61}],
    "storage": [], "ephemeralStorage": {"request": 50, "usage": 12},
}
MACHINE_METRICS = {
    "machineId": "mac-1", "cpu": {"core": 64, "usageRate": 0.1, "allocationCore": 16},
    "ram": {"size": 256, "usageRate": 0.5, "allocationSize": 64},
    "gpu": [{"gpuNumber": 0, "coreUsageRate": 0.0, "vramUsageRate": 0.1, "vramSize": 80, "temp": 40}],
    "rootVolume": {"size": 500, "usageRate": 0.3}, "pvVolume": {"size": 4000, "usageRate": 0.7},
    "networkIo": {"ip": "1.2.3.4", "interface": "eth0", "receive": 10.5, "transmit": 3.5},
    "diskTemperatures": {"nvme0": 35.0},
}
GPU_TIER = {
    "rentalType": "demand", "gpuModel": "NVIDIA H100", "vram": 80, "vcpuRecommended": 16,
    "ramRecommended": 128, "gpuPrice": "2.50", "cpuPricePerCore": "0.01", "ramPricePerGb": "0.001",
    "baseEphemeralStorage": 15, "ephemeralStoragePerGpu": 10,
    "combinations": [{"machineId": "m1", "maxCpu": 64, "maxRam": 512, "maxGpu": 8, "maxStorage": 1000},
                     {"machineId": "m2", "maxCpu": 32, "maxRam": 256, "maxGpu": 2, "maxStorage": 500}],
}
API_KEY = {"id": 7, "keyName": "laptop", "keyPrefix": "meshive_a1b2c3d4", "scopes": ["read"],
           "status": "active", "createdAt": "2026-06-01T00:00:00Z", "lastUsedAt": None, "expiresAt": None}
CREDIT = {"creditBalance": 110.0, "autoRecharge": True, "autoRechargeThreshold": 10,
          "autoRechargeAmount": 50, "hasDefaultPaymentMethod": True, "bonusBalance": 10.0, "paidBalance": 100.0}
CREDIT_ENTRY = {"id": 3, "amount": -12.5, "isPaid": True, "paymentMethod": "refund",
                "createdAt": "2026-07-01T00:00:00Z"}
EARNINGS = {"currentEarning": "2.5", "dailyEarning": "12", "accumulatedEarningUntilPayout": "340.25",
            "earningHistory": [{"date": "2026-08-30", "cpu": "1", "gpu": "10", "storage": "0.5", "total": "11.5"}]}
MEMBERS = {"namespaceName": "team-ns", "workspaceName": "Team",
           "members": [{"user": "a@b.com", "role": "admin", "createdAt": "2026-01-01T00:00:00Z"}]}
TEMPLATE = {"id": 12, "name": "PyTorch", "description": "d", "isOfficial": True, "templateDeployType": "pod",
            "appType": "framework", "appSubType": "", "image": "meshive/pytorch:2.4", "command": [], "args": [],
            "hardwareType": "gpu", "cudaVersion": "12.4", "framework": "pytorch", "frameworkVersion": "2.4",
            "envs": [{"key": "A", "value": "1"}], "endpoints": []}
SERVING = {"id": 5, "registrationId": 1, "modelName": "Llama 3 70B", "apiModelId": "llama-3-70b",
           "customHfRepo": None, "namespaceName": "team-ns", "framework": "vllm", "deploymentType": "serving",
           "minReplicas": 1, "maxReplicas": 3, "currentReplicas": 2, "autoScaleEnabled": True,
           "endpointUrl": "https://api.meshive.ai/v1/serving/x", "status": "active", "paused": False,
           "pricePerHour": "5.0", "billingActive": True, "healthyReplicas": 2,
           "replicas": [{"replicaIndex": 0, "status": "running"}]}
TASK = {"externalId": "task_abc", "name": "train", "namespaceName": "team-ns", "status": "running",
        "faultClass": None, "failureReason": None, "exitCode": None, "podName": "task-abc",
        "image": "python:3.12", "templateId": None, "gpuModel": "NVIDIA RTX 4090", "gpuCount": 1,
        "gpuVramGb": 24, "cpuPreset": None, "cpuCores": 6, "ramGb": 24, "maxDurationSeconds": 3600,
        "provider": "internal", "pricePerHour": "1.0", "totalCost": "0", "costSoFar": "0.5",
        "refundedAmount": "0", "webhookUrl": None, "createdAt": "2026-08-30T00:00:00Z",
        "containerRunningAt": "2026-08-30T00:01:00Z", "finishedAt": None, "billingSettledAt": None,
        "outputsPurgedAt": None, "weInitiatedDelete": False}


def test_get_workspace_parses():
    seen = {}
    ws = sync_client(_capture(WORKSPACE_DETAIL, seen)).get_workspace("team-ns")
    assert seen["path"] == "/v1/sdk/workspaces/team-ns"
    assert ws.namespace_name == "team-ns" and ws.workspace_name == "Team"
    assert ws.price_per_hour == "2.10" and ws.weekly_avg_daily_cost == "40.5"
    assert ws.gpus == 4 and ws.vcpus == 32 and ws.ram == 128 and ws.total_storage == 500.0
    assert [(r.type, r.active, r.paused) for r in ws.resources] == [("pod", 2, 1), ("storage", 1, 0)]
    assert ws.costs[0].date.isoformat() == "2026-08-30" and ws.costs[0].total == 1.75
    assert ws.raw["messageFromHost"][0]["title"] == "hi"   # nested → raw only


def test_get_workspace_encodes_path():
    seen = {}
    sync_client(_capture(WORKSPACE_DETAIL, seen)).get_workspace("a/b")
    assert seen["url"] == "https://api.test/v1/sdk/workspaces/a%2Fb"   # 경로 구조를 못 바꾼다


def test_list_members_parses():
    seen = {}
    members = sync_client(_capture(MEMBERS, seen)).list_members("team-ns")
    assert seen["path"] == "/v1/sdk/members" and seen["params"] == {"workspace": "team-ns"}
    assert members[0].user == "a@b.com" and members[0].role == "admin"
    assert members[0].joined_at.year == 2026


def test_list_and_get_storage_parse():
    seen = {}
    storages = sync_client(_capture({"namespaceName": "team-ns", "storages": [STORAGE]}, seen)).list_storages("team-ns")
    assert seen["path"] == "/v1/sdk/storages" and seen["params"] == {"workspace": "team-ns"}
    s = storages[0]
    assert s.pv_name == "pv-1" and s.storage_type == "nfs" and s.status == "running"
    assert s.total_size == 100.0 and s.usage_rate == 0.6 and s.encrypted is True
    assert s.linked_pods == ["pod-1"]
    assert s.raw["machine"]["machineId"] == "mac-1"

    storage = sync_client(_capture(STORAGE, seen)).get_storage("pv-1", "team-ns")
    assert seen["path"] == "/v1/sdk/storages/pv-1" and seen["params"] == {"workspace": "team-ns"}
    assert storage.pv_name == "pv-1"


def test_pod_metrics_parses_with_missing_rates():
    seen = {}
    m = sync_client(_capture(POD_METRICS, seen)).get_pod_metrics("pod-1", "team-ns")
    assert seen["path"] == "/v1/sdk/pods/pod-1/metrics" and seen["params"] == {"workspace": "team-ns"}
    assert m.cpu_cores == 8 and m.cpu_usage_rate == 0.25
    assert m.ram_size == 32 and m.ram_usage_rate is None    # 측정 불가 → None (0 과 구분)
    assert m.gpus[0].vram_size == 24 and m.gpus[0].temp == 61
    assert m.ephemeral_storage_request == 50 and m.ephemeral_storage_usage == 12


def test_machine_metrics_parses():
    seen = {}
    m = sync_client(_capture(MACHINE_METRICS, seen)).get_machine_metrics("mac-1")
    assert seen["path"] == "/v1/sdk/machines/mac-1/metrics"
    assert m.cpu_cores == 64 and m.cpu_allocated == 16 and m.ram_allocated == 64
    assert m.pv_volume_size == 4000 and m.pv_volume_usage_rate == 0.7
    assert m.network_receive == 10.5 and m.gpus[0].vram_size == 80
    assert m.raw["diskTemperatures"] == {"nvme0": 35.0}


def test_list_gpus_params_and_summary():
    seen = {}
    gpus = sync_client(_capture([GPU_TIER], seen)).list_gpus(rental_type="spot", min_vram=40)
    assert seen["path"] == "/v1/sdk/gpus" and seen["params"] == {"rentalType": "spot", "vram": "40"}
    g = gpus[0]
    assert g.gpu_model == "NVIDIA H100" and g.vram == 80 and g.price_per_hour == "2.50"
    assert g.available_gpus == 10 and g.max_gpus_per_pod == 8 and g.machine_count == 2


def test_list_gpus_defaults_and_validation():
    seen = {}
    sync_client(_capture([], seen)).list_gpus()
    assert seen["params"] == {"rentalType": "demand"}   # vram 미지정 → 서버 기본(제한 없음)
    with pytest.raises(ValueError):
        sync_client(_capture([], seen)).list_gpus(rental_type="reserved")
    with pytest.raises(ValueError):
        sync_client(_capture([], seen)).list_gpus(min_vram=-1)


def test_list_api_keys_parses():
    keys = sync_client(lambda r: httpx.Response(200, json=[API_KEY])).list_api_keys()
    assert keys[0].key_id == 7 and keys[0].name == "laptop"
    assert keys[0].prefix == "meshive_a1b2c3d4" and keys[0].scopes == ["read"]
    assert keys[0].last_used_at is None and keys[0].expires_at is None


def test_get_credit_parses():
    credit = sync_client(lambda r: httpx.Response(200, json=CREDIT)).get_credit()
    assert credit.balance == 110.0 and credit.paid_balance == 100.0 and credit.bonus_balance == 10.0
    assert credit.auto_recharge is True and credit.auto_recharge_threshold == 10
    assert credit.has_default_payment_method is True


def test_credit_history_dates_and_parse():
    import datetime as dt

    seen = {}
    client = sync_client(_capture([CREDIT_ENTRY], seen))
    entries = client.list_credit_history(start_date=dt.date(2026, 7, 1), end_date="2026-07-31")
    assert seen["path"] == "/v1/sdk/credit/history"
    assert seen["params"] == {"startDate": "2026-07-01", "endDate": "2026-07-31"}
    assert entries[0].entry_id == 3 and entries[0].amount == -12.5 and entries[0].payment_method == "refund"
    # Stripe 영수증/인보이스 링크는 SDK 표면에 없다 (서버가 보내지도, 모델이 받지도 않는다).
    assert not hasattr(entries[0], "receipt_url") and not hasattr(entries[0], "invoice_url")

    client.list_credit_history()
    assert seen["params"] == {}   # 미지정 → 서버 기본(최근 90일)
    client.list_credit_history(start_date=dt.datetime(2026, 1, 2, 3, 4))
    assert seen["params"] == {"startDate": "2026-01-02"}
    with pytest.raises(ValueError):
        client.list_credit_history(start_date="July 1st")


def test_get_earnings_parses():
    seen = {}
    e = sync_client(_capture(EARNINGS, seen)).get_earnings(end_date="2026-08-31")
    assert seen["path"] == "/v1/sdk/earnings" and seen["params"] == {"endDate": "2026-08-31"}
    assert e.current_hourly == 2.5 and e.daily == 12.0 and e.accumulated_until_payout == 340.25
    assert e.history[0].date.isoformat() == "2026-08-30" and e.history[0].gpu == 10.0


def test_list_templates_params_and_parse():
    seen = {}
    client = sync_client(_capture([TEMPLATE], seen))
    templates = client.list_templates()
    assert seen["path"] == "/v1/sdk/templates" and seen["params"] == {}
    t = templates[0]
    assert t.template_id == 12 and t.is_official is True and t.app_type == "framework"
    assert t.hardware_type == "gpu" and t.image == "meshive/pytorch:2.4"
    assert t.raw["envs"][0]["key"] == "A"   # 배포 명세는 raw

    client.list_templates("team-ns", app_type="IDE")
    assert seen["params"] == {"workspace": "team-ns", "appType": "ide"}


def test_get_template_int_id_and_workspace():
    seen = {}
    client = sync_client(_capture(TEMPLATE, seen))
    client.get_template(12)
    assert seen["path"] == "/v1/sdk/templates/12" and seen["params"] == {}
    client.get_template("12", workspace="team-ns")
    assert seen["params"] == {"workspace": "team-ns"}
    with pytest.raises(ValueError):
        client.get_template("twelve")
    with pytest.raises(ValueError):
        client.get_template(True)


def test_list_and_get_serving_parse():
    seen = {}
    servings = sync_client(_capture([SERVING], seen)).list_servings("team-ns")
    assert seen["path"] == "/v1/sdk/servings" and seen["params"] == {"workspace": "team-ns"}
    s = servings[0]
    assert s.serving_id == 5 and s.model_name == "Llama 3 70B" and s.status == "active"
    assert (s.min_replicas, s.max_replicas, s.current_replicas, s.healthy_replicas) == (1, 3, 2, 2)
    assert s.price_per_hour == "5.0" and s.billing_active is True
    assert s.raw["replicas"][0]["status"] == "running"

    serving = sync_client(_capture(SERVING, seen)).get_serving(5)
    assert seen["path"] == "/v1/sdk/servings/5" and serving.serving_id == 5


def test_serving_without_price_defaults_to_zero():
    s = sync_client(lambda r: httpx.Response(200, json={**SERVING, "pricePerHour": None,
                                                         "healthyReplicas": None})).get_serving(5)
    assert s.price_per_hour == "0" and s.healthy_replicas is None


def test_list_tasks_params_and_parse():
    seen = {}
    client = sync_client(_capture([TASK], seen))
    tasks = client.list_tasks("team-ns")
    assert seen["path"] == "/v1/sdk/tasks"
    assert seen["params"] == {"workspace": "team-ns", "limit": "50", "offset": "0"}
    t = tasks[0]
    assert t.task_id == "task_abc" and t.status == "running" and t.gpu_model == "NVIDIA RTX 4090"
    assert t.cost_so_far == "0.5" and t.container_running_at.minute == 1 and t.finished_at is None

    client.list_tasks("team-ns", status=["Running", "failed"], limit=10, offset=20)
    assert seen["params"] == {"workspace": "team-ns", "status": "running,failed",
                              "limit": "10", "offset": "20"}
    client.list_tasks("team-ns", status="succeeded")
    assert seen["params"]["status"] == "succeeded"
    with pytest.raises(ValueError):
        client.list_tasks("team-ns", limit=0)
    with pytest.raises(ValueError):
        client.list_tasks("team-ns", offset=-1)


def test_get_task_parses_detail_into_raw():
    seen = {}
    detail = {**TASK, "scriptContent": "print(1)", "env": {"HF_TOKEN": "***"}, "exitCode": 0}
    t = sync_client(_capture(detail, seen)).get_task("task_abc")
    assert seen["path"] == "/v1/sdk/tasks/task_abc"
    assert t.exit_code == 0 and t.raw["scriptContent"] == "print(1)" and t.raw["env"] == {"HF_TOKEN": "***"}


def test_new_methods_reject_empty_ids():
    client = sync_client(lambda r: httpx.Response(200, json={}))
    for call in (lambda: client.get_workspace(""), lambda: client.get_storage(" ", "ns"),
                 lambda: client.get_pod_metrics("", "ns"), lambda: client.get_machine_metrics(""),
                 lambda: client.get_task("")):
        with pytest.raises(ValueError):
            call()


def test_async_new_methods_mirror_sync():
    seen = {}

    async def run():
        async with async_client(_capture({"namespaceName": "team-ns", "storages": [STORAGE]}, seen)) as client:
            storages = await client.list_storages("team-ns")
        async with async_client(_capture(CREDIT, seen)) as client:
            credit = await client.get_credit()
        async with async_client(_capture([TASK], seen)) as client:
            tasks = await client.list_tasks("team-ns", status="running", limit=5)
        return storages, credit, tasks

    storages, credit, tasks = asyncio.run(run())
    assert storages[0].pv_name == "pv-1"
    assert credit.paid_balance == 100.0
    assert tasks[0].task_id == "task_abc"
    assert seen["params"] == {"workspace": "team-ns", "status": "running", "limit": "5", "offset": "0"}


def test_async_get_workspace_and_gpus():
    seen = {}

    async def run():
        async with async_client(_capture(WORKSPACE_DETAIL, seen)) as client:
            ws = await client.get_workspace("team-ns")
        async with async_client(_capture([GPU_TIER], seen)) as client:
            gpus = await client.list_gpus(min_vram=80)
        return ws, gpus

    ws, gpus = asyncio.run(run())
    assert ws.gpus == 4 and gpus[0].available_gpus == 10
    assert seen["params"] == {"rentalType": "demand", "vram": "80"}


# --- assets -----------------------------------------------------------------

ASSET_ROW = {
    "assetExternalId": "asset_abc", "name": "imagenet-mini", "assetType": "dataset", "kind": "collection",
    "semanticType": "dataset", "latestFileCount": 3, "latestTotalSizeBytes": 2_000_000_000,
    "status": "active", "statusReason": None, "createdBy": "a@b.com",
    "createdAt": "2026-08-01T00:00:00Z", "updatedAt": "2026-08-02T00:00:00Z", "versionCount": 2,
    "latestVersion": {"versionNumber": 2, "status": "ready", "totalSizeBytes": 2_000_000_000, "fileCount": 3,
                      "ingestSource": "web_upload", "storageProvider": "meshive_r2",
                      "createdAt": "2026-08-02T00:00:00Z", "deleted": False},
    "storageProvider": "meshive_r2", "inUse": True,
}
ASSET_PAGE = {"total": 57, "page": 2, "pageSize": 20, "items": [ASSET_ROW]}
ASSET_DETAIL = {
    "assetExternalId": "asset_abc", "namespaceName": "team-ns", "name": "imagenet-mini",
    "assetType": "dataset", "status": "active", "statusReason": None, "createdBy": "a@b.com",
    "createdAt": "2026-08-01T00:00:00Z", "updatedAt": "2026-08-02T00:00:00Z", "inUse": False,
    "activeUsageContexts": [], "storageProvider": "meshive_r2",
    "versions": [
        {"versionNumber": 2, "status": "uploading", "totalSizeBytes": 0, "fileCount": 0,
         "ingestSource": "hf_import", "storageProvider": "meshive_r2", "deleted": False,
         "importFailureReason": None, "files": []},
        {"versionNumber": 1, "status": "ready", "totalSizeBytes": 1_500_000, "fileCount": 2,
         "ingestSource": "web_upload", "storageProvider": "meshive_r2", "deleted": False,
         "files": [{"relativePath": "a.bin", "sizeBytes": 1_000_000}, {"relativePath": "b.bin", "sizeBytes": 500_000}]},
    ],
}
ASSET_STORAGE = {"managedBytes": 2_000_000_000, "pricePerGbMonth": 0.015, "estimatedMonthlyCost": 0.03,
                 "creditState": "grace", "creditBlocked": False, "creditDepletedAt": "2026-08-30T00:00:00Z",
                 "blocksAt": "2026-09-06T00:00:00Z", "purgeDeadlineAt": None, "paidBalanceAvailable": False}


def test_list_assets_params_and_page():
    seen = {}
    page = sync_client(_capture(ASSET_PAGE, seen)).list_assets(
        "team-ns", asset_type="Dataset", status="active", page=2, page_size=20)
    assert seen["path"] == "/v1/sdk/assets"
    assert seen["params"] == {"workspace": "team-ns", "assetType": "dataset", "status": "active",
                              "page": "2", "pageSize": "20"}
    assert (page.total, page.page, page.page_size, page.pages) == (57, 2, 20, 3)
    assert len(page) == 1 and [a.asset_id for a in page] == ["asset_abc"]   # 순회 가능
    a = page.items[0]
    assert a.namespace_name == "team-ns"        # 목록 행에는 없어 호출 인자로 채운다
    assert a.asset_type == "dataset" and a.version_count == 2
    assert a.size_bytes == 2_000_000_000 and a.file_count == 3
    assert a.storage_provider == "meshive_r2" and a.in_use is True
    assert a.latest_version.version_number == 2 and a.latest_version.is_ready
    assert a.versions == []
    assert a.raw["kind"] == "collection"


def test_list_assets_defaults_and_validation():
    seen = {}
    client = sync_client(_capture({"total": 0, "page": 1, "pageSize": 20, "items": []}, seen))
    page = client.list_assets("team-ns")
    assert seen["params"] == {"workspace": "team-ns", "page": "1", "pageSize": "20"}
    assert page.total == 0 and page.pages == 1 and list(page) == []
    with pytest.raises(ValueError):
        client.list_assets("team-ns", page=0)
    with pytest.raises(ValueError):
        client.list_assets("team-ns", page_size=101)


def test_get_asset_detail_derives_latest_ready_version():
    seen = {}
    a = sync_client(_capture(ASSET_DETAIL, seen)).get_asset("asset_abc")
    assert seen["path"] == "/v1/sdk/assets/asset_abc"
    assert a.namespace_name == "team-ns" and a.created_by == "a@b.com"
    assert [v.version_number for v in a.versions] == [2, 1]
    assert a.latest_version.version_number == 1          # v2 는 uploading → 최신 READY 는 v1
    assert a.version_count == 1 and a.size_bytes == 1_500_000 and a.file_count == 2
    assert a.versions[1].raw["files"][0]["relativePath"] == "a.bin"   # 파일 목록은 raw


def test_get_asset_storage_parses():
    seen = {}
    s = sync_client(_capture(ASSET_STORAGE, seen)).get_asset_storage("team-ns")
    assert seen["path"] == "/v1/sdk/assets/storage-summary" and seen["params"] == {"workspace": "team-ns"}
    assert s.managed_bytes == 2_000_000_000 and s.price_per_gb_month == 0.015
    assert s.estimated_monthly_cost == 0.03
    assert s.credit_state == "grace" and s.credit_blocked is False and s.blocks_at.day == 6
    assert s.purge_deadline_at is None and s.paid_balance_available is False


def test_async_assets_mirror_sync():
    seen = {}

    async def run():
        async with async_client(_capture(ASSET_PAGE, seen)) as client:
            page = await client.list_assets("team-ns", page_size=5)
        async with async_client(_capture(ASSET_DETAIL, seen)) as client:
            asset = await client.get_asset("asset_abc")
        async with async_client(_capture(ASSET_STORAGE, seen)) as client:
            storage = await client.get_asset_storage("team-ns")
        return page, asset, storage

    page, asset, storage = asyncio.run(run())
    assert page.total == 57 and asset.latest_version.version_number == 1
    assert storage.credit_state == "grace"
    assert seen["params"] == {"workspace": "team-ns"}
