"""0.1.0 쓰기 표면 — 요청 조립(경로/쿼리/본문), Idempotency-Key, 재시도, 새 예외, headers=, 로그."""
import asyncio
import json

import httpx
import pytest

from meshive import (
    AsyncMeshive,
    ConflictError,
    InsufficientCreditError,
    Logs,
    Meshive,
    PodCreated,
    PodEstimate,
    ResourceAction,
    StorageCreated,
    TaskSubmitted,
)
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)

ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"}, "resources": {"gpu_model": "RTX 3060"},
            "availability": {"available_gpus": 2}, "template": {"id": 457}, "volumes": [], "note": "Estimated"}


class Recorder:
    """요청을 기록하고 미리 정한 응답을 순서대로 돌려주는 MockTransport 핸들러."""

    def __init__(self, *responses):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx.Response(status, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, index=-1) -> dict:
        return json.loads(self.requests[index].content)


# --- 파드 --------------------------------------------------------------------

def test_estimate_pod_builds_request():
    rec = Recorder((200, ESTIMATE))
    est = sync_client(rec).estimate_pod("agent-pod", 457, workspace="ws 1", gpu_model="RTX 3060", gpu_count=2,
                                        gpu_vram_gb=12, rental_type="Spot", vcpu=8, ram_gb=24, disk_gb=30,
                                        volumes=[("pv-data", "/data"), {"storage": "pv-b", "mount_path": "/b"}],
                                        env={"A": "1", "TOKEN": "x"}, secret_keys=["TOKEN"],
                                        ports=[8888, {"port": 6006, "name": "tb", "external": False}],
                                        command="sleep infinity", region="KR", max_price_per_hour=0.5)
    assert isinstance(est, PodEstimate) and est.price_per_hour == "0.068423" and est.availability["available_gpus"] == 2
    req = rec.last
    assert req.method == "POST" and req.url.path == "/v1/sdk/pods/estimate" and req.url.params["workspace"] == "ws 1"
    assert "Idempotency-Key" in req.headers
    body = rec.body()
    assert body == {
        "name": "agent-pod", "templateId": 457, "gpuModel": "RTX 3060", "gpuCount": 2, "gpuVramGb": 12,
        "rentalType": "spot", "vcpu": 8, "ramGb": 24, "diskGb": 30,
        "volumes": [{"storage": "pv-data", "mountPath": "/data"}, {"storage": "pv-b", "mountPath": "/b"}],
        "env": {"A": "1", "TOKEN": "x"}, "secretKeys": ["TOKEN"],
        "ports": [{"port": 8888, "external": True}, {"port": 6006, "external": False, "name": "tb"}],
        "command": "sleep infinity", "internetPremium": False, "uptimePremium": False, "cpuPremium": False,
        "region": "KR", "maxPricePerHourUsd": "0.5",
    }


def test_create_pod_uses_given_idempotency_key_and_parses():
    rec = Recorder((202, {"podName": None, "name": "agent-pod", "workspace": "ws", "accepted": True,
                          "transactionId": 14765, "estimate": ESTIMATE}))
    created = sync_client(rec).create_pod("agent-pod", 457, workspace="ws", idempotency_key="my-key-0001")
    assert isinstance(created, PodCreated) and created.transaction_id == 14765 and created.pod_name is None
    assert created.estimate.price_per_hour == "0.068423"
    assert rec.last.headers["Idempotency-Key"] == "my-key-0001" and rec.last.url.path == "/v1/sdk/pods"
    assert rec.body()["gpuCount"] == 1 and "gpuModel" not in rec.body()   # CPU 파드: gpuModel 생략


def test_write_retries_reuse_the_same_idempotency_key(slept):
    rec = Recorder((503, {"detail": {"title": "Scheduling Busy", "message": "busy"}}),
                   (202, {"name": "p", "workspace": "ws", "transactionId": 1, "estimate": ESTIMATE}))
    created = sync_client(rec).create_pod("p", 1, workspace="ws")
    assert created.transaction_id == 1 and len(rec.requests) == 2
    assert rec.requests[0].headers["Idempotency-Key"] == rec.requests[1].headers["Idempotency-Key"]
    assert slept == [0.5]


def test_pod_lifecycle_paths_and_params():
    rec = Recorder((202, {"podName": "p-0", "workspace": "ws", "action": "stop", "accepted": True, "result": 7}))
    c = sync_client(rec)
    action = c.stop_pod("p-0", "ws")
    assert isinstance(action, ResourceAction) and action.resource == "pod" and action.id == "p-0" and action.result == 7
    assert rec.last.url.path == "/v1/sdk/pods/p-0/stop"
    c.start_pod("p-0", "ws", placement="any_node")
    assert rec.last.url.path == "/v1/sdk/pods/p-0/start" and rec.last.url.params["placement"] == "any_node"
    c.restart_pod("p-0", "ws")
    assert rec.last.url.path == "/v1/sdk/pods/p-0/restart"
    c.delete_pod("p-0", "ws", delete_local_storages=["pv-a", " pv-b "])
    assert rec.last.method == "DELETE" and rec.last.url.params["deleteLocalStorages"] == "pv-a,pv-b"
    with pytest.raises(ValueError):
        c.start_pod("p-0", "ws", placement="moon")


@pytest.mark.parametrize("kwargs", [
    dict(gpu_count=0), dict(gpu_count=9), dict(rental_type="hourly"), dict(disk_gb=1),
    dict(env={"A": "1"}, secret_keys=["B"]), dict(volumes=[("pv", "data")]), dict(ports=[70000]),
    dict(max_price_per_hour="free"), dict(max_price_per_hour=0),
])
def test_pod_validation_errors(kwargs):
    with pytest.raises(ValueError):
        sync_client(Recorder((200, ESTIMATE))).estimate_pod("p", 1, workspace="ws", **kwargs)


# --- 예외 --------------------------------------------------------------------

def test_402_and_409_map_to_new_exceptions():
    rec = Recorder((409, {"detail": {"title": "Price Exceeds Cap", "message": "too expensive", "pricePerHourUsd": "0.1"}}))
    with pytest.raises(ConflictError) as exc:
        sync_client(rec).create_pod("p", 1, workspace="ws")
    assert exc.value.title == "Price Exceeds Cap" and exc.value.raw["detail"]["pricePerHourUsd"] == "0.1"
    with pytest.raises(InsufficientCreditError):
        sync_client(Recorder((402, {"detail": {"title": "Insufficient Credit", "message": "top up"}}))).start_pod("p", "ws")


# --- 스토리지 / 서빙 / 태스크 ---------------------------------------------------------

def test_storage_methods():
    rec = Recorder((202, {"name": "vol", "workspace": "ws", "transactionId": 5, "pvName": None,
                          "estimate": {"pricePerHourUsd": "0.000097", "pricePerGbMonthUsd": "0.07", "sizeGb": 1,
                                       "storageType": "nfs", "maxSizeGb": 305}}))
    c = sync_client(rec)
    created = c.create_storage("vol", 1, workspace="ws", storage_type="hostpath", disk_type="ssd", encrypted=True)
    assert isinstance(created, StorageCreated) and created.estimate.max_size_gb == 305 and created.transaction_id == 5
    assert rec.body() == {"name": "vol", "sizeGb": 1, "storageType": "hostPath", "diskType": "SSD", "encrypted": True}
    c.delete_storage("pv-1", "ws")
    assert rec.last.method == "DELETE" and rec.last.url.path == "/v1/sdk/storages/pv-1"
    with pytest.raises(ValueError):
        c.estimate_storage("vol", 0, workspace="ws")
    with pytest.raises(ValueError):
        c.estimate_storage("vol", 1, workspace="ws", storage_type="floppy")


def test_serving_methods():
    rec = Recorder((201, {"resource": "serving", "id": "42", "workspace": "ws", "action": "deploy", "result": {"id": 42}}))
    c = sync_client(rec)
    action = c.deploy_serving(5, workspace="ws", price_cap_per_hour="1.5", min_replicas=1, max_replicas=2,
                              autoscale=False, max_context_tokens=8192)
    assert action.resource == "serving" and action.id == "42" and action.action == "deploy"
    assert rec.body() == {"modelRegistrationId": 5, "minReplicas": 1, "maxReplicas": 2, "autoscale": False,
                          "priceCapPerHourUsd": "1.5", "maxContextTokens": 8192, "shareIdleCapacity": False}
    c.scale_serving(42, max_replicas=4)
    assert rec.last.method == "PATCH" and rec.last.url.path == "/v1/sdk/servings/42/scale" and rec.body() == {"maxReplicas": 4}
    c.pause_serving(42, paused=False)
    assert rec.last.url.path == "/v1/sdk/servings/42/pause" and rec.body() == {"paused": False}
    c.delete_serving("42")
    assert rec.last.method == "DELETE" and rec.last.url.path == "/v1/sdk/servings/42"
    with pytest.raises(ValueError):
        c.deploy_serving(5, workspace="ws", price_cap_per_hour=None)
    with pytest.raises(ValueError):
        c.deploy_serving(5, workspace="ws", price_cap_per_hour=1, min_replicas=3, max_replicas=1)
    with pytest.raises(ValueError):
        c.scale_serving(42)


def test_task_methods():
    task = {"externalId": "task_1", "name": "train", "namespaceName": "ws", "status": "queued", "podName": "task-1",
            "image": "python:3.12-slim", "gpuModel": "RTX 3060", "gpuCount": 1, "cpuCores": 4, "ramGb": 12,
            "pricePerHour": "0.068", "costSoFar": "0", "totalCost": "0"}
    rec = Recorder((202, {"task": task, "estimate": {"pricePerHourUsd": "0.068", "maxCostUsd": "0.068", "maxDurationS": 3600,
                                                    "resources": {"gpu_count": 1}}}))
    c = sync_client(rec)
    submitted = c.submit_task("train", "print('x', flush=True)", workspace="ws", image="python:3.12-slim",
                              gpu_model="RTX 3060", env={"HF_TOKEN": "x"}, secret_keys=["HF_TOKEN"], args=["--epochs", "3"],
                              input_assets=["asset_a", {"asset": "asset_b", "version": 2, "target_dir": "/inputs/b"}],
                              max_duration=7200)
    assert isinstance(submitted, TaskSubmitted) and submitted.task.task_id == "task_1" and submitted.estimate.max_cost == "0.068"
    body = rec.body()
    assert body["gpuModel"] == "RTX 3060" and "cpuPreset" not in body and body["maxDurationS"] == 7200
    assert body["inputAssets"] == [{"asset": "asset_a"}, {"asset": "asset_b", "version": 2, "targetDir": "/inputs/b"}]
    assert body["args"] == ["--epochs", "3"] and body["secretKeys"] == ["HF_TOKEN"]
    c.stop_task("task_1")
    assert rec.last.url.path == "/v1/sdk/tasks/task_1/stop"
    for bad in (dict(), dict(gpu_model="x", cpu_preset="micro-2c8g"), dict(cpu_preset="micro-2c8g", max_duration=60),
                dict(cpu_preset="micro-2c8g", script=" ")):
        kwargs = {"image": "img", **bad}
        script = kwargs.pop("script", "print(1)")
        with pytest.raises(ValueError):
            c.estimate_task("t", script, workspace="ws", **kwargs)
    with pytest.raises(ValueError):
        c.estimate_task("t", "x" * (256 * 1024 + 1), workspace="ws", image="img", cpu_preset="micro-2c8g")


# --- 로그 --------------------------------------------------------------------

def test_logs():
    rec = Recorder((200, {"podName": "p-0", "workspace": "ws", "source": "live", "count": 2, "truncated": False,
                          "lines": [{"line": "a", "ts": "2026-09-08T00:00:00Z"}, {"line": "b"}]}))
    c = sync_client(rec)
    logs = c.get_pod_logs("p-0", "ws", tail=50, wait=3, container="main")
    assert isinstance(logs, Logs) and logs.text == "a\nb" and logs.lines[0].ts.startswith("2026") and logs.source == "live"
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/pods/p-0/logs"
    assert dict(rec.last.url.params) == {"tail": "50", "wait": "3", "container": "main", "workspace": "ws"}
    rec.responses = [(200, {"podName": "task-1", "workspace": "ws", "source": "external", "count": 0, "lines": [],
                            "taskId": "task_1", "finished": True, "nextCursor": 12})]
    logs = c.get_task_logs("task_1", cursor=12)
    assert logs.finished is True and logs.next_cursor == 12 and rec.last.url.params["cursor"] == "12"
    with pytest.raises(ValueError):
        c.get_pod_logs("p-0", "ws", tail=0)
    with pytest.raises(ValueError):
        c.get_pod_logs("p-0", "ws", wait=99)


# --- headers= ------------------------------------------------------------------

def test_custom_headers_are_sent_but_cannot_override_auth():
    rec = Recorder((200, ESTIMATE))
    client = sync_client(rec, headers={"X-Meshive-Client": "claude-code/2.1", "Authorization": "Bearer fake"})
    client.estimate_pod("p", 1, workspace="ws")
    assert rec.last.headers["X-Meshive-Client"] == "claude-code/2.1"
    assert rec.last.headers["Authorization"] == "Bearer meshive_test"
    assert rec.last.headers["User-Agent"].startswith("meshive-python/")


# --- async 미러 --------------------------------------------------------------------

def test_async_write_methods_mirror_sync():
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 3, "estimate": ESTIMATE}))
    stop = Recorder((202, {"podName": "p-0", "workspace": "ws", "action": "stop"}))

    async def main():
        async with async_client(rec) as c:
            created = await c.create_pod("p", 1, workspace="ws", gpu_model="RTX 3060", idempotency_key="k-1")
        async with async_client(stop) as c:
            action = await c.stop_pod("p-0", "ws")
            logs_rec = Recorder((200, {"podName": "p-0", "workspace": "ws", "source": "none", "count": 0, "lines": []}))
            c._client = httpx.AsyncClient(transport=httpx.MockTransport(logs_rec))
            logs = await c.get_pod_logs("p-0", "ws")
        return created, action, logs

    created, action, logs = asyncio.run(main())
    assert created.transaction_id == 3 and rec.last.headers["Idempotency-Key"] == "k-1"
    assert action.action == "stop" and logs.source == "none"
