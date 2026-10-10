import asyncio
import json

import httpx
import pytest

import dataclasses

from meshive import (
    Asset,
    Pod,
    Task,
    Transaction,
    Workspace,
    AsyncMeshive,
    AuthenticationError,
    ConfigurationError,
    Meshive,
    MeshiveAPIError,
    MeshiveError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    PodCreationFailedError,
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
    """Records retry/poll waits instead of actually sleeping (tests check only counts and intervals)."""
    recorded: list[float] = []

    async def _async_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(_client.time, "sleep", recorded.append)
    monkeypatch.setattr(_client.asyncio, "sleep", _async_sleep)
    return recorded


@pytest.fixture(autouse=True)
def isolate_config_dir(monkeypatch, tmp_path):
    """Isolate in an empty temp directory so the credentials file doesn't interfere with resolve_*."""
    from meshive import _credentials

    monkeypatch.setenv(_credentials.ENV_CONFIG_DIR, str(tmp_path / "cfg"))


def test_resolve_base_url_default(monkeypatch):
    monkeypatch.delenv(_config.ENV_BASE_URL, raising=False)
    assert _config.resolve_base_url() == _config.DEFAULT_BASE_URL


def test_resolve_base_url_env_and_trailing_slash(monkeypatch):
    monkeypatch.setenv(_config.ENV_BASE_URL, "https://api.example.com/")
    assert _config.resolve_base_url() == "https://api.example.com"


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
    # Status fields the server didn't send are None — not disguised as False.
    assert pod.billing_active is None and pod.can_restart_on_same_node is None and pod.endpoints == []


def test_pod_connect_info_and_state_fields():
    """Same selection as the console's Connect tab: every endpoint, and only envs with showOnConnect."""
    pod = Pod.from_dict({
        **POD, "status": "stopped", "billingActive": False, "isDownloader": False, "uptimePremium": True,
        "sameNodeUnavailableReason": "capacity", "canRestartOnSameNode": False, "canRestartOnAnyNode": True,
        "stopReasonCode": "credit_exhausted", "stopReasonDetail": "balance 0", "stopReasonAt": "2026-10-03T00:00:00Z",
        "template": {
            "endpoints": [
                {"userAlias": "ComfyUI", "containerPort": 8188, "portType": "connect", "isExternal": True,
                 "externalUrl": "https://x.meshive.ai", "internalUrl": "", "isReady": False,
                 "readinessState": "interrupted"},
                {"userAlias": "Jupyter", "containerPort": 8888, "portType": "http", "externalUrl": "https://j",
                 "isReady": True},
            ],
            "envs": [
                {"key": "ACCESS_PASSWORD", "value": "s3cret", "isSecret": True, "isAutoGenerated": True,
                 "showOnConnect": True},
                {"key": "USERNAME", "value": "admin", "isSecret": False, "showOnConnect": True},
                {"key": "HF_TOKEN", "value": "hf_x", "isSecret": True, "showOnConnect": False},
            ],
        },
    })
    assert [(e.name, e.port, e.external_url, e.internal_url, e.readiness) for e in pod.endpoints] == [
        ("ComfyUI", 8188, "https://x.meshive.ai", None, "interrupted"), ("Jupyter", 8888, "https://j", None, "ready")]
    assert [(c.key, c.value, c.is_secret, c.is_auto_generated) for c in pod.connect_credentials] == [
        ("ACCESS_PASSWORD", "s3cret", True, True), ("USERNAME", "admin", False, False)]
    assert "s3cret" not in repr(pod)                       # secrets don't leak through repr
    assert pod.billing_active is False and pod.uptime_premium is True and pod.cpu_premium is None
    assert pod.same_node_unavailable_reason == "capacity" and pod.can_restart_on_same_node is False
    assert pod.stop_reason_code == "credit_exhausted" and pod.stop_reason_at.year == 2026


def test_workspace_member_role():
    assert Workspace.from_dict({"namespaceName": "ns", "memberRole": "viewer"}).member_role == "viewer"
    assert Workspace.from_dict({"namespaceName": "ns"}).member_role is None


def test_task_detail_fields():
    t = Task.from_dict({
        "externalId": "task_1", "failedAssetExternalId": "asset_b", "outputUploadState": "uploading",
        "outputFilesDeclared": 4, "outputFilesCompleted": 1, "outputBytesDeclared": 4096, "outputBytesCompleted": 1024,
        "outputUploadLastError": "AccessDenied", "outputsPurgedAt": None,
        "inputAssets": [{"assetExternalId": "asset_b", "name": "weights", "targetDir": "/inputs/w",
                         "versionNumber": 1, "referenceMode": "external", "ingestSource": "hf_import"}],
    })
    assert t.failed_asset_id == "asset_b" and t.output_upload_state == "uploading"
    assert (t.output_files_completed, t.output_files_declared, t.output_bytes_completed) == (1, 4, 1024)
    assert t.output_upload_last_error == "AccessDenied" and t.outputs_purged_at is None
    assert [(a.asset_id, a.target_dir, a.reference_mode) for a in t.input_assets] == [
        ("asset_b", "/inputs/w", "external")]
    # List responses (no such fields) are None/empty list — not 0
    bare = Task.from_dict({"externalId": "task_2"})
    assert bare.output_files_declared is None and bare.input_assets == []


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
    """Doesn't break on trimmed/partial responses (no state/specs/earning → safe defaults)."""
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
    """A handler that returns responses in order on each call + a call counter."""
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
    assert slept == [2.0]  # honors Retry-After as-is


def test_retry_uses_backoff_without_retry_after(slept):
    handler, calls = _counting_handler([
        httpx.Response(503, json={"detail": {"message": "unavailable"}}),
        httpx.Response(503, json={"detail": {"message": "unavailable"}}),
        httpx.Response(200, json=WHOAMI),
    ])
    sync_client(handler).me()
    assert calls["n"] == 3
    assert slept == [0.5, 1.0]  # exponential backoff


def test_retries_are_capped_then_raise(slept):
    handler, calls = _counting_handler([httpx.Response(429, json={"detail": {"message": "nope"}})])
    with pytest.raises(RateLimitError):
        sync_client(handler).me()
    assert calls["n"] == 3  # first attempt + max_retries (2)


def test_long_retry_after_is_not_waited_out(slept):
    handler, calls = _counting_handler([
        httpx.Response(429, json={"detail": {"message": "nope"}}, headers={"Retry-After": "3600"}),
    ])
    with pytest.raises(RateLimitError) as info:
        sync_client(handler).me()
    assert calls["n"] == 1  # raise right away rather than wait an hour
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
    assert calls["n"] == 1  # doesn't wait out the timeout


def test_wait_for_pod_reports_why_creation_failed():
    failed = httpx.Response(200, json={**POD, "status": "terminated",
                                       "creationFailure": {"reason": "CrashLoopBackOff", "failedAt": None}})
    handler, _ = _counting_handler([failed])
    with pytest.raises(PodCreationFailedError, match="CrashLoopBackOff") as exc:
        sync_client(handler).wait_for_pod("pod-1", "team-ns")
    assert exc.value.reason == "CrashLoopBackOff" and isinstance(exc.value, MeshiveError)


def test_wait_for_pod_can_target_a_terminal_status():
    """If asked to wait for error, error is the target, not a failure."""
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
# 0.0.7 read surface extension — URL/query shape and parsing
# =============================================================================

def _capture(payload, seen):
    """A handler that records the request URL/query in seen and returns payload."""
    def handler(request):
        seen["path"] = request.url.path      # decoded path (httpx unescapes %2F)
        seen["url"] = str(request.url)       # the URL actually sent (to check encoding)
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
    assert seen["url"] == "https://api.test/v1/sdk/workspaces/a%2Fb"   # can't change the path structure


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
    assert m.ram_size == 32 and m.ram_usage_rate is None    # not measurable → None (distinct from 0)
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


def test_gpu_vram_size_unreadable_is_none_not_zero():
    # When DCGM can't read the FB (memory) metric family, the server sends vramSize as null (GPUs with only temperature and
    # utilization are normal). Filling in 0.0 would make a 'card with 0 VRAM' — per the documented contract, fields other than gpu_number are None when not measurable.
    unreadable = {"gpuNumber": 0, "coreUsageRate": 0.5, "vramUsageRate": None, "vramSize": None, "temp": 40.0}
    seen = {}
    pod = sync_client(_capture({**POD_METRICS, "gpu": [unreadable]}, seen)).get_pod_metrics("pod-1", "team-ns")
    machine = sync_client(_capture({**MACHINE_METRICS, "gpu": [unreadable]}, seen)).get_machine_metrics("mac-1")
    for g in (pod.gpus[0], machine.gpus[0]):
        assert g.vram_size is None and g.vram_usage_rate is None
        assert g.core_usage_rate == 0.5 and g.temp == 40.0      # values that were read stay as-is

    from meshive.models import GpuUsage

    assert GpuUsage.from_dict({"gpuNumber": 0}).vram_size is None                 # even when the key is missing entirely
    assert GpuUsage.from_dict({"gpuNumber": 0, "vramSize": 0}).vram_size == 0.0   # a 0 sent by the server stays 0
    assert GpuUsage(0).vram_size is None


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
    assert seen["params"] == {"rentalType": "demand"}   # vram not given → server default (no limit)
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
    # Stripe receipt/invoice links aren't on the SDK surface (the server doesn't send them and the model doesn't take them).
    assert not hasattr(entries[0], "receipt_url") and not hasattr(entries[0], "invoice_url")

    client.list_credit_history()
    assert seen["params"] == {}   # not given → server default (last 90 days)
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
    assert t.raw["envs"][0]["key"] == "A"   # the deployment spec is in raw

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

# Server response shape: asset-level fields + version compatibility fields for older SDKs (removed in 0.2).
ASSET_ROW = {
    "assetExternalId": "asset_abc", "name": "imagenet-mini", "assetType": "dataset", "kind": "collection",
    "semanticType": "dataset", "status": "active", "statusReason": None, "createdBy": "a@b.com",
    "createdAt": "2026-08-01T00:00:00Z", "updatedAt": "2026-08-02T00:00:00Z",
    "uploadStatus": "ready", "stale": False, "totalSizeBytes": 2_000_000_000, "fileCount": 3,
    "ingestSource": "web_upload", "storageProvider": "meshive_r2", "sizeMeasured": True, "inUse": True,
    "versionCount": 1, "latestFileCount": 3, "latestTotalSizeBytes": 2_000_000_000,
    "latestVersion": {"versionNumber": 1, "status": "ready", "totalSizeBytes": 2_000_000_000, "fileCount": 3,
                      "ingestSource": "web_upload", "storageProvider": "meshive_r2", "deleted": False},
}
ASSET_PAGE = {"total": 57, "page": 2, "pageSize": 20, "items": [ASSET_ROW]}
ASSET_DETAIL = {
    "assetExternalId": "asset_abc", "namespaceName": "team-ns", "name": "imagenet-mini",
    "assetType": "dataset", "kind": "collection", "status": "active", "statusReason": None, "createdBy": "a@b.com",
    "createdAt": "2026-08-01T00:00:00Z", "updatedAt": "2026-08-02T00:00:00Z", "inUse": True,
    "uploadStatus": "ready", "totalSizeBytes": 1_500_000, "fileCount": 2, "ingestSource": "hf_import",
    "storageProvider": "meshive_r2", "importFailureReason": None,
    "activeUsageContexts": [{"kind": "pod", "identifier": "sts-1", "name": "train", "status": "running",
                             "role": "input"}],
    "files": [{"fileId": 1, "fileExternalId": "file_a", "relativePath": "a.bin", "sizeBytes": 1_000_000,
               "ext": "bin", "status": "ready"},
              {"fileId": 2, "fileExternalId": "file_b", "relativePath": "b.bin", "sizeBytes": 500_000,
               "ext": "bin", "status": "ready"}],
    "versionCount": 1,
    "versions": [{"versionNumber": 1, "status": "ready", "totalSizeBytes": 1_500_000, "fileCount": 2,
                  "ingestSource": "hf_import", "storageProvider": "meshive_r2", "deleted": False, "files": []}],
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
    assert len(page) == 1 and [a.asset_id for a in page] == ["asset_abc"]   # iterable
    a = page.items[0]
    assert a.namespace_name == "team-ns"        # not in list rows, so filled from the call argument
    assert a.asset_type == "dataset" and a.kind == "collection" and a.semantic_type == "dataset"
    assert a.size_bytes == 2_000_000_000 and a.file_count == 3 and a.upload_status == "ready"
    assert a.ingest_source == "web_upload" and a.stale is False
    assert a.storage_provider == "meshive_r2" and a.in_use is True
    assert a.files == [] and a.active_usage_contexts == []   # detail only


def test_asset_version_fields_are_deprecated_but_still_read():
    a = sync_client(_capture(ASSET_PAGE, {})).list_assets("team-ns").items[0]
    with pytest.warns(DeprecationWarning, match="no longer have versions"):
        assert a.version_count == 1
    with pytest.warns(DeprecationWarning):
        assert a.latest_version.version_number == 1 and a.latest_version.is_ready
    with pytest.warns(DeprecationWarning):
        assert a.versions == []
    assert "version_count" not in {f.name for f in dataclasses.fields(a)}   # left out of MCP serialization


def test_unmeasured_linked_asset_size_is_none_not_zero():
    a = Asset.from_dict({**ASSET_ROW, "sizeMeasured": False, "totalSizeBytes": 0, "fileCount": 0})
    assert a.size_bytes is None and a.file_count is None


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


def test_get_asset_detail_reads_files_and_usage():
    seen = {}
    a = sync_client(_capture(ASSET_DETAIL, seen)).get_asset("asset_abc")
    assert seen["path"] == "/v1/sdk/assets/asset_abc"
    assert a.namespace_name == "team-ns" and a.created_by == "a@b.com"
    assert a.size_bytes == 1_500_000 and a.file_count == 2 and a.ingest_source == "hf_import"
    assert [(f.path, f.size_bytes, f.file_id, f.status) for f in a.files] == [
        ("a.bin", 1_000_000, "file_a", "ready"), ("b.bin", 500_000, "file_b", "ready")]
    assert [(u.kind, u.name, u.role) for u in a.active_usage_contexts] == [("pod", "train", "input")]
    with pytest.warns(DeprecationWarning):
        assert [v.version_number for v in a.versions] == [1]


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
    assert page.total == 57 and [f.path for f in asset.files] == ["a.bin", "b.bin"]
    assert storage.credit_state == "grace"
    assert seen["params"] == {"workspace": "team-ns"}


def test_format_gib_uses_mib_below_one_gibibyte():
    from meshive.cli import _format as fmt

    # MiB values /1024 — the numbers were always 1024-based; only the label was GB. Written as GiB/MiB like the console.
    assert fmt.gib(0) == "0 GiB"
    assert fmt.gib(512) == "512 MiB"
    assert fmt.gib(1024) == "1 GiB"
    assert fmt.gib(1536) == "1.5 GiB"
    assert fmt.gib(131072) == "128 GiB"
    assert fmt.gib(None) == "-"


def test_format_vram_keeps_the_gb_label():
    from meshive.cli import _format as fmt

    # DCGM framebuffer MiB → "GB" like the card name. A 4060 Ti 16GB reports 15946 MiB (measured).
    assert fmt.vram(24576) == "24 GB"
    assert fmt.vram(15946) == "16 GB"
    assert fmt.vram(None) == "-"


def test_format_mbps_is_decimal_bits_per_second():
    from meshive.cli import _format as fmt

    # Bytes/sec × 8 ÷ 10^6. Dividing by 1024² makes 1 Gbps (125,000,000 B/s) come out as 953.7 Mbps.
    assert fmt.mbps(125_000_000) == "1000.0 Mbps"
    assert fmt.mbps(12_500_000) == "100.0 Mbps"
    assert fmt.mbps(0) == "0.0 Mbps"
    assert fmt.mbps(None) == "-"
    assert fmt.mbps("abc") == "-"


def test_format_bytes_human_uses_binary_labels():
    from meshive.cli import _format as fmt

    assert fmt.bytes_human(512) == "512 B"
    assert fmt.bytes_human(150_000) == "146.5 KiB"
    assert fmt.bytes_human(1_500_000) == "1.4 MiB"
    assert fmt.bytes_human(2 * 1024 ** 3) == "2.00 GiB"
    assert fmt.bytes_human(3 * 1024 ** 4) == "3.00 TiB"
    # A 20.69 GB (decimal) file is 19.27 GiB — numbers divided by 1024 get 1024-based labels.
    assert fmt.bytes_human(20_690_000_000) == "19.27 GiB"


# --- Display helpers: same amounts as the web console -------------------------------------------
# The SDK returns server numbers as-is ("0.06770833") — these two are public for showing amounts to people.
# The rules match the console (hourly 3 decimals / everything else 2), rounding is halfExpand like Intl.
def test_formatting_helpers_are_public_and_match_the_console():
    import meshive

    assert meshive.format_hourly("0.06770833") == "$0.068"     # workspace hourly total
    assert meshive.format_hourly("0.00097222") == "$0.001"     # with 2 decimals it would be "$0.00"
    assert meshive.format_usd("12.5") == "$12.50"
    assert meshive.format_usd("0.015") == "$0.02"              # halfExpand — same direction as the console
    assert meshive.format_hourly(None) == "-" and meshive.format_usd("") == "-"
    assert {"format_hourly", "format_usd"} <= set(meshive.__all__)


def test_cli_money_helpers_delegate_to_the_public_formatter():
    """Whether the CLI and SDK use the same function — two copies drift apart eventually."""
    import meshive
    from meshive.cli import _format as fmt

    for value in ("0.06770833", "2.1", "0.015", None, ""):
        assert fmt.money_hourly(value) == meshive.format_hourly(value)
        assert fmt.money(value) == meshive.format_usd(value)


TRANSACTION = {
    "transactionId": 73213,
    "namespace": "team-ns",
    "nodeName": "node-1",
    "userAlias": "sdk-real-0910-w05pod",
    "resource": "statefulset",
    "resourceName": "9eb176aae9309fb8",
    "action": "create",
    "status": "in_progress",
    "createdAt": "2026-09-10T07:55:07.081000Z",
    "updatedAt": "2026-09-10T07:58:00.000000Z",
    "doneAt": None,
    "transactionSteps": [
        {"step": "prepare_storage", "status": "done", "detail": "",
         "updatedAt": "2026-09-10T07:55:20.000000Z"},
        {"step": "pull_image", "status": "in_progress", "detail": "",
         "updatedAt": "2026-09-10T07:58:00.000000Z",
         "imagePullProgress": {"totalBytes": 100, "downloadedBytes": 42,
                               "progressPercent": 42.0,
                               "completedLayers": 3, "totalLayers": 9}},
    ],
}


def test_list_transactions_parses_in_flight_step():
    """Why `creating` doesn't finish — the last step and progress must show."""
    seen = {}
    txns = sync_client(_capture([TRANSACTION], seen)).list_transactions("team-ns")
    assert seen["path"] == "/v1/sdk/transactions" and seen["params"] == {"workspace": "team-ns"}
    t = txns[0]
    assert t.transaction_id == 73213 and t.action == "create" and t.status == "in_progress"
    assert t.step == "pull_image" and t.step_status == "in_progress"
    assert t.progress == pytest.approx(0.42)
    assert t.user_alias == "sdk-real-0910-w05pod"
    assert t.raw["transactionSteps"][1]["imagePullProgress"]["totalLayers"] == 9


def test_transaction_progress_is_none_when_unknown():
    """Reading a step without progress as 0% makes it look 'stuck' — it must be None."""
    payload = dict(TRANSACTION, transactionSteps=[
        {"step": "create_statefulset", "status": "in_progress", "detail": "",
         "updatedAt": "2026-09-10T07:55:20.000000Z"}])
    t = sync_client(_capture([payload], {})).list_transactions("team-ns")[0]
    assert t.progress is None and t.step == "create_statefulset"


def test_transaction_failure_reason_surfaces_as_detail():
    """Even with an empty detail, the diagnostic failure_reason must come through — for stalls it's the only clue."""
    payload = dict(TRANSACTION, status="failed", transactionSteps=[
        {"step": "pull_image", "status": "failed", "detail": "",
         "updatedAt": "2026-09-10T08:00:00.000000Z",
         "failureDiagnostic": {"failureReason": "image pull stalled for 600s"}}])
    t = sync_client(_capture([payload], {})).list_transactions("team-ns")[0]
    assert t.status == "failed" and t.detail == "image pull stalled for 600s"


def test_transaction_fetch_liveness_and_init_logs():
    t = Transaction.from_dict({"transactionId": 5, "status": "failed", "transactionSteps": [
        {"step": "fetch_assets", "status": "failed", "detail": "",
         "assetFetchProgress": {"progressPercent": 100.0, "live": True, "phase": "verifying"},
         "failureDiagnostic": {"failureReason": "checksum mismatch", "initLogs": [
             {"container": "source-fetch", "lines": ["GET model.safetensors", "error: 403"], "previous": True}]}}]})
    assert t.live is True and t.phase == "verifying" and t.detail == "checksum mismatch"
    assert [(x.container, x.lines, x.previous) for x in t.init_logs] == [
        ("source-fetch", ["GET model.safetensors", "error: 403"], True)]
    pulling = Transaction.from_dict({"transactionId": 6, "transactionSteps": [{"step": "pull_image"}]})
    assert pulling.live is None and pulling.phase is None and pulling.init_logs == []


def test_transaction_current_step_is_the_latest_not_the_last_listed():
    """The server sends stages sorted by name — the current stage is the in-progress one with the latest updatedAt, not the end of the list (start, finished).
    Observed 2026-10-03: fetch_assets finishing and image_pull starting had the same timestamp."""
    t = Transaction.from_dict({"transactionId": 15640, "status": "in_progress", "transactionSteps": [
        {"step": "create_statefulset", "status": "done", "updatedAt": "2026-10-03T10:53:07Z"},
        {"step": "fetch_assets", "status": "done", "updatedAt": "2026-10-03T10:53:22Z",
         "assetFetchProgress": {"progressPercent": 100.0, "live": False}},
        {"step": "image_pull", "status": "in_progress", "updatedAt": "2026-10-03T10:53:22Z",
         "imagePullProgress": {"progressPercent": 40.0}},
        {"step": "start", "status": "done", "updatedAt": "2026-10-03T10:53:02Z", "detail": "Starting Pod creation process"},
    ]})
    assert (t.step, t.step_status, t.progress, t.detail) == ("image_pull", "in_progress", 0.4, "")
    assert t.live is None    # the current stage isn't the asset download
    legacy = Transaction.from_dict({"transactionId": 1, "transactionSteps": [{"step": "a"}, {"step": "b"}]})
    assert legacy.step == "b"                # without timestamps, the later list item as before


def test_async_list_transactions_mirrors_sync():
    async def run():
        async with async_client(_capture([TRANSACTION], {})) as client:
            return await client.list_transactions("team-ns")
    txns = asyncio.run(run())
    assert txns[0].step == "pull_image" and txns[0].progress == pytest.approx(0.42)


# --- downloads (G05) ----------------------------------------------------------

DOWNLOAD_URLS = {"expiresIn": 3600, "items": [{
    "assetExternalId": "asset_abc", "name": "weights", "expectedFileCount": 2, "files": [
        {"fileId": 1, "relativePath": "model.safetensors", "sizeBytes": 5, "contentHash": None,
         "url": "https://r2.test/k/model.safetensors?sig=1"},
        {"fileId": 2, "relativePath": "config/config.json", "sizeBytes": 2, "contentHash": "h",
         "url": "https://r2.test/k/config/config.json?sig=2"}]}]}


def _download_handler(urls_payload, seen, bodies=None):
    bodies = bodies or {"/k/model.safetensors": b"hello", "/k/config/config.json": b"{}"}

    def handler(request):
        if request.url.host == "r2.test":
            seen.setdefault("storage_auth", []).append(request.headers.get("authorization"))
            return httpx.Response(200, content=bodies[request.url.path])
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json=urls_payload)
    return handler


def test_asset_download_urls_and_download(tmp_path):
    seen = {}
    client = sync_client(_download_handler(DOWNLOAD_URLS, seen))
    d = client.asset_download_urls("asset_abc", paths=["config/*", "*.safetensors"])
    assert seen["path"] == "/v1/sdk/assets/asset_abc/download-urls"
    assert seen["query"] == "path=config%2F%2A&path=%2A.safetensors"
    assert d.complete and d.expires_in == 3600 and [f.path for f in d.files] == ["model.safetensors",
                                                                                 "config/config.json"]
    written = client.download_asset("asset_abc", tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in written] == ["model.safetensors", "config/config.json"]
    assert (tmp_path / "model.safetensors").read_bytes() == b"hello"
    assert not list(tmp_path.rglob("*.part"))
    assert seen["storage_auth"] == [None, None]          # the Meshive key doesn't go to storage


def test_download_refuses_paths_outside_dest_and_incomplete_signing(tmp_path):
    evil = {"expiresIn": 60, "items": [{"assetExternalId": "asset_abc", "name": "x", "expectedFileCount": 1,
                                        "files": [{"relativePath": "../escape.txt", "url": "https://r2.test/e"}]}]}
    client = sync_client(_download_handler(evil, {}, {"/e": b"x"}))
    with pytest.raises(ValueError, match="outside"):
        client.download_asset("asset_abc", tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()

    partial = {**DOWNLOAD_URLS, "items": [{**DOWNLOAD_URLS["items"][0], "expectedFileCount": 3}]}
    client = sync_client(_download_handler(partial, {}))
    with pytest.raises(MeshiveError, match="2 of 3 files"):
        client.download_asset("asset_abc", tmp_path)


def test_downloads_on_an_older_server_explain_themselves():
    client = sync_client(lambda r: httpx.Response(404, json={"detail": "Not Found"}))
    with pytest.raises(NotFoundError, match="does not support asset downloads"):
        client.asset_download_urls("asset_abc")
    with pytest.raises(NotFoundError, match="does not support task outputs"):
        client.task_outputs("task_1")
    # A 404 for a truly missing asset (with a title) stays as-is
    client = sync_client(lambda r: httpx.Response(404, json={"detail": {"title": "Asset Not Found",
                                                                        "message": "The asset does not exist."}}))
    with pytest.raises(NotFoundError, match="The asset does not exist"):
        client.asset_download_urls("asset_abc")


def test_task_outputs_and_async_download(tmp_path):
    payload = {"expired": False, "destination": {"provider": "meshive_r2"}, "files": [
        {"id": "task_1", "filename": "result.csv", "sizeBytes": 2, "url": "https://r2.test/inline",
         "downloadUrl": "https://r2.test/attach"}]}
    seen = {}
    handler = _download_handler(payload, seen, {"/attach": b"ok"})
    outs = sync_client(handler).task_outputs("task_1")
    assert seen["path"] == "/v1/sdk/tasks/task_1/outputs"
    assert outs.storage_provider == "meshive_r2" and not outs.expired
    assert [(f.path, f.url, f.size_bytes) for f in outs.files] == [("result.csv", "https://r2.test/attach", 2)]

    async def run():
        async with async_client(handler) as client:
            return await client.download_task_outputs("task_1", tmp_path)

    written = asyncio.run(run())
    assert [p.name for p in written] == ["result.csv"] and (tmp_path / "result.csv").read_bytes() == b"ok"
