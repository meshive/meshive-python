import asyncio
import json

import httpx
import pytest

from meshive import (
    AsyncMeshive,
    AuthenticationError,
    ConfigurationError,
    Meshive,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from meshive import _config


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
