"""Write surface — request assembly (path/query/body), Idempotency-Key, retries, new exceptions, headers=, logs."""
import asyncio
import json
import warnings

import httpx
import pytest

from meshive import (
    MeshiveAPIError,
    NotFoundError,
    PermissionDeniedError,
    PodCreationFailedError,
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
    WaitTimeoutError,
)
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)

ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"}, "resources": {"gpu_model": "RTX 3060"},
            "availability": {"available_gpus": 2}, "template": {"id": 457}, "volumes": [], "note": "Estimated"}


class Recorder:
    """MockTransport handler that records requests and returns preset responses in order."""

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


# --- Pods --------------------------------------------------------------------

def test_estimate_pod_builds_request():
    rec = Recorder((200, ESTIMATE))
    est = sync_client(rec).estimate_pod("agent-pod", 457, workspace="ws 1", gpu_model="RTX 3060", gpu_count=2,
                                        gpu_vram_gb=12, rental_type="Spot", vcpu=8, ram_gb=24,
                                        volumes=[("pv-data", "/data"), {"storage": "pv-b", "mount_path": "/b"}],
                                        env={"A": "1", "TOKEN": "x"}, secret_keys=["TOKEN"],
                                        ports=[8888, {"port": 6006, "name": "tb", "external": False}],
                                        command="sleep infinity", max_price_per_hour=0.5)
    assert isinstance(est, PodEstimate) and est.price_per_hour == "0.068423" and est.availability["available_gpus"] == 2
    req = rec.last
    assert req.method == "POST" and req.url.path == "/v1/sdk/pods/estimate" and req.url.params["workspace"] == "ws 1"
    assert "Idempotency-Key" in req.headers
    body = rec.body()
    assert body == {
        "name": "agent-pod", "templateId": 457, "gpuModel": "RTX 3060", "gpuCount": 2, "gpuVramGb": 12,
        "rentalType": "spot", "vcpu": 8, "ramGb": 24,
        "volumes": [{"storage": "pv-data", "mountPath": "/data"}, {"storage": "pv-b", "mountPath": "/b"}],
        "env": {"A": "1", "TOKEN": "x"}, "secretKeys": ["TOKEN"],
        "ports": [{"port": 8888, "external": True}, {"port": 6006, "external": False, "name": "tb"}],
        "command": "sleep infinity", "internetPremium": False, "uptimePremium": False, "cpuPremium": False,
        "maxPricePerHourUsd": "0.5",
    }


def test_create_pod_uses_given_idempotency_key_and_parses():
    rec = Recorder((202, {"podName": None, "name": "agent-pod", "workspace": "ws", "accepted": True,
                          "transactionId": 14765, "estimate": ESTIMATE}))
    created = sync_client(rec).create_pod("agent-pod", 457, workspace="ws", idempotency_key="my-key-0001")
    assert isinstance(created, PodCreated) and created.transaction_id == 14765 and created.pod_name is None
    assert created.estimate.price_per_hour == "0.068423"
    assert rec.last.headers["Idempotency-Key"] == "my-key-0001" and rec.last.url.path == "/v1/sdk/pods"
    assert rec.body()["gpuCount"] == 1 and "gpuModel" not in rec.body()   # CPU pod: gpuModel omitted


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
        c.start_pod("p-0", "ws", placement=" ")


@pytest.mark.parametrize("placement", ["same_node", "any_node", "wait_same_node", "wait_any_node"])
def test_start_pod_sends_every_placement(placement):
    rec = Recorder((202, {"podName": "p-0", "workspace": "ws", "action": "start"}))
    sync_client(rec).start_pod("p-0", "ws", placement=f" {placement.upper()} ", allow_data_loss=True)
    assert rec.last.url.params["placement"] == placement and rec.last.url.params["allow_data_loss"] == "true"


def test_start_pod_leaves_unknown_placements_to_the_server():
    """The server owns the list of placements — a value the SDK doesn't know is sent, and the server's 422 comes back."""
    rec = Recorder((422, {"detail": [{"loc": ["query", "placement"], "msg": "Input should be 'same_node', ..."}]}))
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(rec).start_pod("p-0", "ws", placement="next_week")
    assert err.value.status_code == 422 and rec.last.url.params["placement"] == "next_week"


@pytest.mark.parametrize("kwargs", [
    dict(gpu_count=0), dict(gpu_count="2"), dict(gpu_count=True), dict(rental_type="hourly"),
    dict(env={"A": "1"}, secret_keys=["B"]), dict(volumes=[("pv", "data")]), dict(ports=[70000]),
    dict(max_price_per_hour="free"), dict(max_price_per_hour=0), dict(cpu_line=3),
    dict(local_volumes=[("scratch", "/scratch", 20)]),
    dict(local_volumes=[{"mount_path": "/scratch", "size_gb": 20}]),
    dict(local_volumes=[{"name": "scratch", "mount_path": "scratch", "size_gb": 20}]),
    dict(local_volumes=[{"name": "scratch", "mount_path": "/scratch"}]),
    dict(local_volumes=[{"name": "scratch", "mount_path": "/scratch", "size_gb": "20"}]),
    dict(local_volumes=[{"name": "scratch", "mount_path": "/scratch", "size_gb": 20, "usage_warning_threshold": "most"}]),
])
def test_pod_validation_errors(kwargs):
    rec = Recorder((200, ESTIMATE))
    with pytest.raises(ValueError):
        sync_client(rec).estimate_pod("p", 1, workspace="ws", **kwargs)
    assert rec.requests == []


def test_pod_limits_are_left_to_the_server():
    """Only the shape is checked locally — sizes and counts the server may accept go through (it answers with a 400)."""
    rec = Recorder((200, ESTIMATE))
    c = sync_client(rec)
    c.estimate_pod("p", 1, workspace="ws", gpu_model="H100", gpu_count=16)
    assert rec.body()["gpuCount"] == 16
    c.estimate_pod("p", 1, workspace="ws", local_volumes=[{"name": "tiny", "mount_path": "/t", "size_gb": 1,
                                                           "usage_warning_threshold": 5}])
    assert rec.body()["localVolumes"] == [{"name": "tiny", "mountPath": "/t", "sizeGb": 1, "usageWarningThreshold": 5.0}]
    c.estimate_pod("p", 1, workspace="ws", command="x" * 5000)
    assert len(rec.body()["command"]) == 5000


# --- Exceptions --------------------------------------------------------------------

def test_402_and_409_map_to_new_exceptions():
    rec = Recorder((409, {"detail": {"title": "Price Exceeds Cap", "message": "too expensive", "pricePerHourUsd": "0.1"}}))
    with pytest.raises(ConflictError) as exc:
        sync_client(rec).create_pod("p", 1, workspace="ws")
    assert exc.value.title == "Price Exceeds Cap" and exc.value.raw["detail"]["pricePerHourUsd"] == "0.1"
    with pytest.raises(InsufficientCreditError):
        sync_client(Recorder((402, {"detail": {"title": "Insufficient Credit", "message": "top up"}}))).start_pod("p", "ws")


# --- Storage / serving / tasks ---------------------------------------------------------

def test_storage_methods():
    rec = Recorder((202, {"name": "vol", "workspace": "ws", "transactionId": 5, "pvName": None,
                          "estimate": {"pricePerHourUsd": "0.000097", "pricePerGbMonthUsd": "0.07", "sizeGb": 1,
                                       "storageType": "nfs", "diskType": "NVMe", "maxSizeGb": 305}}))
    c = sync_client(rec)
    created = c.create_storage("vol", 1, workspace="ws", storage_type="NFS", encrypted=True)
    assert isinstance(created, StorageCreated) and created.estimate.max_size_gb == 305 and created.transaction_id == 5
    assert created.estimate.disk_type == "NVMe"
    assert rec.body() == {"name": "vol", "sizeGb": 1, "storageType": "nfs", "encrypted": True}   # no diskType/region
    c.delete_storage("pv-1", "ws")
    assert rec.last.method == "DELETE" and rec.last.url.path == "/v1/sdk/storages/pv-1"
    with pytest.raises(ValueError):
        c.estimate_storage("vol", 0, workspace="ws")
    with pytest.raises(ValueError):
        c.estimate_storage("vol", 1, workspace="ws", storage_type=" ")


def test_storage_types_are_left_to_the_server():
    """Which storage types can be created alone is the server's rule: hostPath (any spelling) and unknown types are sent, and the
    server's answer — "Local Storage Not Supported" for hostPath — comes back as it is."""
    rec = Recorder((400, {"detail": {"title": "Local Storage Not Supported",
                                     "message": "Create local storage together with a pod (local_volumes)."}}))
    c = sync_client(rec)
    with pytest.raises(MeshiveAPIError, match="local_volumes") as err:
        c.create_storage("vol", 10, workspace="ws", storage_type="hostpath", encrypted=True)
    assert err.value.title == "Local Storage Not Supported"
    assert rec.body() == {"name": "vol", "sizeGb": 10, "storageType": "hostPath", "encrypted": True}
    with pytest.raises(MeshiveAPIError):
        c.estimate_storage("vol", 10, workspace="ws", storage_type="floppy")
    assert rec.body()["storageType"] == "floppy"


def test_storage_disk_type_and_region_are_deprecated_and_not_sent():
    rec = Recorder((200, {"pricePerHourUsd": "0.0001", "sizeGb": 10, "storageType": "nfs", "diskType": "NVMe"}))
    c = sync_client(rec)
    with pytest.warns(DeprecationWarning, match="disk_type is ignored") as record:
        c.estimate_storage("vol", 10, workspace="ws", disk_type="SSD")
    assert record[0].filename == __file__          # the warning points at the caller, not at the SDK
    with pytest.warns(DeprecationWarning, match="region is ignored"):
        est = c.estimate_storage("vol", 10, workspace="ws", region="KR")
    assert est.disk_type == "NVMe" and not {"diskType", "region"} & rec.body().keys()
    with warnings.catch_warnings():
        warnings.simplefilter("error")             # the defaults (and an explicit NVMe) don't warn
        c.estimate_storage("vol", 10, workspace="ws")
        c.create_storage("vol", 10, workspace="ws", disk_type="nvme", region=None)


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
    with pytest.warns(DeprecationWarning, match="no longer have versions"):
        submitted = c.submit_task("train", "print('x', flush=True)", workspace="ws", image="python:3.12-slim",
                                  gpu_model="RTX 3060", env={"HF_TOKEN": "x"}, secret_keys=["HF_TOKEN"], args=["--epochs", "3"],
                                  input_assets=["asset_a", {"asset": "asset_b", "version": 2, "target_dir": "/inputs/b"}],
                                  max_duration=7200)
    assert isinstance(submitted, TaskSubmitted) and submitted.task.task_id == "task_1" and submitted.estimate.max_cost == "0.068"
    body = rec.body()
    assert body["gpuModel"] == "RTX 3060" and "cpuPreset" not in body and body["maxDurationS"] == 7200
    assert body["inputAssets"] == [{"asset": "asset_a"}, {"asset": "asset_b", "targetDir": "/inputs/b"}]   # version isn't sent
    assert body["args"] == ["--epochs", "3"] and body["secretKeys"] == ["HF_TOKEN"]
    c.stop_task("task_1")
    assert rec.last.url.path == "/v1/sdk/tasks/task_1/stop"
    for bad in (dict(), dict(gpu_model="x", cpu_preset="micro-2c8g"), dict(cpu_preset="micro-2c8g", max_duration=60),
                dict(cpu_preset="micro-2c8g", script=" ")):
        kwargs = {"image": "img", **bad}
        script = kwargs.pop("script", "print(1)")
        with pytest.raises(ValueError):
            c.estimate_task("t", script, workspace="ws", **kwargs)
    with pytest.raises(ValueError, match="256 KiB"):   # the limit is 256 × 1024 bytes (KiB, same as the server message)
        c.estimate_task("t", "x" * (256 * 1024 + 1), workspace="ws", image="img", cpu_preset="micro-2c8g")


# --- Logs --------------------------------------------------------------------

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


# --- async mirror --------------------------------------------------------------------

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


def test_generated_key_is_available_after_timeout_and_can_be_reused():
    seen = []
    def timeout(req):
        seen.append(req.headers['Idempotency-Key'])
        raise httpx.ReadTimeout('offline', request=req)
    client = sync_client(timeout, max_retries=0)
    with pytest.raises(httpx.ReadTimeout) as err:
        client.stop_task('task_1')
    key = err.value.idempotency_key
    assert key == seen[0] and err.value.operation_method == 'POST' and err.value.operation_path == '/tasks/task_1/stop'
    with pytest.raises(httpx.ReadTimeout):
        client.stop_task('task_1', idempotency_key=key)
    assert seen == [key, key]


def test_write_result_and_status_lookup_keep_operation_identity():
    rec = Recorder((202, {'podName': 'p', 'action': 'start'}))
    result = sync_client(rec).start_pod('p', 'ws', placement='any_node', allow_data_loss=True,
                                      idempotency_key='move-operation-0001')
    assert rec.last.url.params['allow_data_loss'] == 'true'
    assert result.raw['idempotencyKey'] == 'move-operation-0001' and result.raw['operationPath'] == '/pods/p/start'
    client = sync_client(Recorder((200, {'state': 'unknown'})))
    assert client.get_operation('move-operation-0001', method='POST', path='/pods/p/start')['state'] == 'unknown'


def test_async_data_loss_flag_and_error_key():
    async def run():
        rec = Recorder((409, {'detail': {'title': 'Data Loss Consent Required', 'message': 'consent required'}}))
        client = async_client(rec)
        with pytest.raises(ConflictError) as err:
            await client.start_pod('p', 'ws', placement='any_node', idempotency_key='async-move-0001')
        assert rec.last.url.params['allow_data_loss'] == 'false'
        assert err.value.idempotency_key == 'async-move-0001'
        await client.close()
    asyncio.run(run())


def test_data_loss_consent_does_not_coerce_a_string_to_true():
    rec = Recorder((202, {}))
    with pytest.raises(ValueError, match='explicit boolean'):
        sync_client(rec).start_pod('p', 'ws', placement='any_node', allow_data_loss='false')
    assert rec.requests == []


# --- Serving cost-increase decision (the CLI/MCP confirmation rule) ----------------------------------------------

def test_serving_scale_raises_cost_covers_range_autoscale_and_cap():
    """Only a wider range, turning on autoscale and a higher cap "can raise cost" — reductions or setting a value on an unlimited cap don't."""
    from meshive.models import Serving
    current = Serving.from_dict({"id": 42, "namespaceName": "ws", "framework": "vllm", "status": "active",
                                 "minReplicas": 1, "maxReplicas": 3, "currentReplicas": 2, "autoScaleEnabled": False,
                                 "priceCapPerHour": "1.5"})
    assert current.autoscale is False and current.price_cap_per_hour == "1.5"
    assert current.scale_raises_cost(max_replicas=4) and current.scale_raises_cost(min_replicas=2)
    assert current.scale_raises_cost(autoscale=True) and current.scale_raises_cost(price_cap_per_hour=2)
    assert current.scale_raises_cost(price_cap_per_hour="not a number")
    assert not current.scale_raises_cost(max_replicas=2, min_replicas=0, autoscale=False, price_cap_per_hour="1.5")
    assert not current.scale_raises_cost(price_cap_per_hour=0.9)
    unlimited = Serving.from_dict({"id": 1, "namespaceName": "ws", "framework": "vllm", "status": "active",
                                   "minReplicas": 1, "maxReplicas": 3, "currentReplicas": 1, "autoScaleEnabled": True})
    assert unlimited.price_cap_per_hour is None and not unlimited.scale_raises_cost(price_cap_per_hour=99, autoscale=True)


def test_storage_estimate_carries_disk_type():
    from meshive.models import StorageEstimate
    est = StorageEstimate.from_dict({"pricePerHourUsd": "0.0001", "pricePerGbMonthUsd": "0.07", "sizeGb": 10,
                                     "storageType": "nfs", "diskType": "SSD", "maxSizeGb": 300})
    assert est.disk_type == "SSD"
    assert StorageEstimate.from_dict({"sizeGb": 1, "maxSizeGb": 1}).disk_type == "NVMe"   # older server response


def test_model_registration_methods():
    model = {"id": 12, "sourceId": 3, "modelName": "Qwen3-0.6B", "displayName": "qwen-small", "apiModelId": "qwen-small-ab12",
             "modelType": "llm", "framework": "vllm", "processingMode": "realtime", "huggingfaceRepo": "Qwen/Qwen3-0.6B",
             "contextLength": 40960}
    rec = Recorder((200, [model]))
    c = sync_client(rec)
    m, = c.list_models("ws")
    assert rec.last.url.path == "/v1/sdk/models" and rec.last.url.params["workspace"] == "ws"
    assert (m.registration_id, m.name, m.api_model_id, m.context_length) == (12, "qwen-small", "qwen-small-ab12", 40960)

    rec = Recorder((200, {"status": "unsupported", "output": "text", "engine": "no", "detail": "GGUF is not supported",
                          "suggestedRepo": "Qwen/Qwen3-0.6B"}))
    d = sync_client(rec).detect_model(" Qwen/Qwen3-0.6B-GGUF ", workspace="ws", hf_token_id=4)
    assert rec.last.url.path == "/v1/sdk/models/detect"
    assert json.loads(rec.last.content) == {"huggingfaceRepo": "Qwen/Qwen3-0.6B-GGUF", "hfTokenId": 4}
    assert not d.ok and d.suggested_repo == "Qwen/Qwen3-0.6B" and d.context_length is None

    rec = Recorder((201, {"resource": "model", "id": "12", "workspace": "ws", "action": "register",
                          "result": {"title": "Already Registered", "registrationId": 12}}))
    r = sync_client(rec).register_model("Qwen/Qwen3-0.6B", workspace="ws", name="qwen-small", framework="sglang",
                                        context_length=8192)
    assert rec.last.method == "POST" and rec.last.headers["Idempotency-Key"]
    assert json.loads(rec.last.content) == {"huggingfaceRepo": "Qwen/Qwen3-0.6B", "modelName": "qwen-small",
                                            "framework": "sglang", "contextLength": 8192}
    assert r.id == "12" and r.resource == "model"
    with pytest.raises(ValueError):
        sync_client(rec).register_model("Qwen/Qwen3-0.6B", workspace="ws", framework="comfyui")

    rec = Recorder((200, {"resource": "model", "id": "12", "action": "delete", "result": {"title": "Success"}}))
    sync_client(rec).delete_model(12)
    assert rec.last.method == "DELETE" and rec.last.url.path == "/v1/sdk/models/12"

    rec = Recorder((200, [{"id": 4, "label": "hf-main", "createdAt": "2026-10-01T00:00:00Z", "usedByAssetCount": 2}]))
    t, = sync_client(rec).list_hf_tokens("ws")
    assert rec.last.url.path == "/v1/sdk/hf-tokens" and (t.token_id, t.label, t.used_by_asset_count) == (4, "hf-main", 2)

    old = sync_client(Recorder((404, {"detail": "Not Found"})))
    with pytest.raises(NotFoundError, match="does not support model registration"):
        old.list_models("ws")


def test_asset_import_picks_the_source():
    from meshive import _write

    assert _write.asset_import_body("Qwen/Qwen3-0.6B", paths="*.safetensors", hf_token_id=2) == {
        "source": "huggingface", "hfRepo": "Qwen/Qwen3-0.6B", "hfTokenId": 2, "pathFilters": ["*.safetensors"]}
    assert _write.asset_import_body("https://huggingface.co/Qwen/Qwen3-0.6B/tree/main", revision="v1")["hfRepo"] == \
        "Qwen/Qwen3-0.6B"
    assert _write.asset_import_body("https://civitai.com/models/4384", asset_type="Checkpoint") == {
        "source": "civitai", "sourceUrl": "https://civitai.com/models/4384", "assetType": "checkpoint"}
    assert _write.asset_import_body("https://example.com/w/a.bin")["source"] == "url"
    for bad in ("not a repo", "a/b/c", "https://huggingface.co/onlyowner"):
        with pytest.raises(ValueError):
            _write.asset_import_body(bad)

    rec = Recorder((201, {"assetExternalId": "asset_abc", "name": "Qwen3-0.6B", "status": "active",
                          "ingestSource": "hf_import", "fileCount": 9, "totalBytes": 1500, "isGated": False,
                          "resolvedCommit": "c0ffee"}))
    a = sync_client(rec).import_asset("Qwen/Qwen3-0.6B", workspace="ws")
    assert rec.last.url.path == "/v1/sdk/assets/import" and rec.last.headers["Idempotency-Key"]
    assert (a.asset_id, a.file_count, a.resolved_commit) == ("asset_abc", 9, "c0ffee")
    # Older servers: POST /assets/import collides with GET /assets/{id}, so it's 405, not 404 (observed 2026-10-03)
    with pytest.raises(NotFoundError, match="does not support asset import"):
        sync_client(Recorder((405, {"detail": "Method Not Allowed"}))).import_asset("Qwen/Qwen3-0.6B", workspace="ws")
    with pytest.raises(NotFoundError, match="does not support watched folders"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).set_watched_folders("p-0", "ws", expected_version=1)
    with pytest.raises(PermissionDeniedError):     # other errors pass through
        sync_client(Recorder((403, {"detail": {"title": "Forbidden", "message": "admin only"}}))).import_asset(
            "Qwen/Qwen3-0.6B", workspace="ws")


def test_pod_assets_and_watched_folders():
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 1, "estimate": {"pricePerHourUsd": "1"}}))
    sync_client(rec).create_pod("p", 457, workspace="ws", gpu_model="RTX 3060",
                                input_assets=["asset_a", {"asset": "asset_b", "target_dir": "/workspace/models",
                                                          "role": "checkpoint", "paths": "unet/"}],
                                watched_folders=["/workspace/out", {"path": "/workspace/logs", "include": "*.txt",
                                                                    "include_existing": True}],
                                harvest_destination={"mode": "user_s3", "credential_id": 5})
    body = rec.body()
    assert body["inputAssets"] == [{"asset": "asset_a"}, {"asset": "asset_b", "targetDir": "/workspace/models",
                                                          "role": "checkpoint", "includePaths": ["unet/"]}]
    assert body["watchedFolders"] == [{"path": "/workspace/out"},
                                      {"path": "/workspace/logs", "include": ["*.txt"], "includeExisting": True}]
    assert body["harvestDestination"] == {"mode": "user_s3", "credentialId": 5}
    rec = Recorder((200, {"pricePerHourUsd": "1"}))
    sync_client(rec).estimate_pod("p", 457, workspace="ws")
    assert not {"inputAssets", "watchedFolders", "harvestDestination"} & rec.body().keys()   # not sent when unused

    # The console's create screen: Keep structure, a folder added turned off, and the template's own folders.
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 2, "estimate": {"pricePerHourUsd": "1"}}))
    sync_client(rec).create_pod("p", 457, workspace="ws",
                                input_assets=[{"asset": "asset_b", "paths": ["unet/"], "strip_prefix": ""}],
                                watched_folders=[{"path": "/workspace/later", "enabled": False}],
                                watched_template_folders={"/outputs": {"enabled": False},
                                                          "/workspace/config": {"include": ["*.yaml"]}})
    body = rec.body()
    assert body["inputAssets"] == [{"asset": "asset_b", "includePaths": ["unet/"], "stripPrefix": ""}]
    assert body["watchedFolders"] == [{"path": "/workspace/later", "includeExisting": False, "enabled": False}]
    assert body["watchedTemplateFolders"] == {"/outputs": {"enabled": False},
                                              "/workspace/config": {"enabled": True, "include": ["*.yaml"]}}
    for bad in (dict(input_assets=[{"asset": "asset_b", "include_paths": ["unet/"]}]),
                dict(input_assets=[{"asset": "asset_b", "strip_prefix": True}]),
                dict(watched_template_folders=["/outputs"]),
                dict(watched_template_folders={"/outputs": False})):
        with pytest.raises(ValueError):
            sync_client(Recorder()).create_pod("p", 457, workspace="ws", **bad)
    with pytest.raises(ValueError, match="unknown key.*include_paths.*paths"):
        sync_client(Recorder()).estimate_pod("p", 457, workspace="ws", input_assets=[{"asset": "a", "include_paths": "x"}])

    config = {"version": "3:ab", "editable": True, "applied": True, "roots": [
        {"role": "output", "path": "/workspace/outputs", "origin": "template", "enabled": True, "include": ["*.png"],
         "includeOverride": None},
        {"role": "user", "path": "/data", "origin": "user", "enabled": False, "include": [], "includeOverride": ["*.csv"],
         "since": "2026-10-03T00:00:00Z", "blockedReason": "network_storage"}]}
    rec = Recorder((200, config))
    w = sync_client(rec).get_watched_folders("p-0", "ws")
    assert rec.last.url.path == "/v1/sdk/pods/p-0/harvest" and w.revision == 3 and w.applied is True
    assert [(f.path, f.origin, f.enabled, f.blocked_reason) for f in w.folders] == [
        ("/workspace/outputs", "template", True, None), ("/data", "user", False, "network_storage")]

    rec = Recorder((200, config))
    sync_client(rec).set_watched_folders("p-0", "ws", expected_version=3,
                                         template={"/workspace/outputs": {"enabled": False}},
                                         user=[{"path": "/data", "include": ["*.csv"], "enabled": False}])
    assert rec.last.method == "PUT" and rec.last.headers["Idempotency-Key"]
    assert rec.body() == {"expectedVersion": 3, "template": {"/workspace/outputs": {"enabled": False}},
                          "user": [{"path": "/data", "include": ["*.csv"], "includeExisting": False, "enabled": False}]}
    with pytest.raises(ValueError):
        sync_client(rec).set_watched_folders("p-0", "ws", expected_version=-1)
    assert sync_client(Recorder((200, {"editable": False, "editableReason": "undecidable", "roots": None}))) \
        .get_watched_folders("p-0", "ws").folders is None


def test_ssh_access():
    rec = Recorder((200, {"password": "pw-123", "sshUrl": "ssh -p 2222 root@m.machine.c.meshive.ai",
                          "sshWebUrl": "https://m.machine.c.meshive.ai/?password=cHctMTIz", "expiredAt": 1_900_000_000}))
    a = sync_client(rec).ssh_access("p-0", "ws")
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/pods/p-0/ssh"
    assert rec.last.url.params["workspace"] == "ws"
    assert a.command.startswith("ssh -p 2222") and a.password == "pw-123" and a.expires_at.year == 2030
    assert "pw-123" not in repr(a) and "cHctMTIz" not in repr(a)
    with pytest.raises(NotFoundError, match="does not support SSH access"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).ssh_access("p-0", "ws")


# --- 1.1.0: local volumes, CPU lines, deprecated region ------------------------------------------------

def test_create_pod_sends_local_volumes_and_cpu_line():
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 8, "estimate": ESTIMATE}))
    sync_client(rec).create_pod("p", 457, workspace="ws", cpu_line=" amd-epyc-4-ddr5 ", volumes=[("pv-net", "/data")],
                                local_volumes=[{"name": "scratch", "mount_path": "/scratch", "size_gb": 50},
                                               {"name": "keys", "mountPath": "/keys", "sizeGb": 10, "encrypted": True,
                                                "usage_warning": True, "usage_warning_threshold": 0.8}])
    body = rec.body()
    assert body["cpuLine"] == "amd-epyc-4-ddr5" and "gpuModel" not in body
    assert body["volumes"] == [{"storage": "pv-net", "mountPath": "/data"}]
    assert body["localVolumes"] == [
        {"name": "scratch", "mountPath": "/scratch", "sizeGb": 50},     # unset options are left to the server's defaults
        {"name": "keys", "mountPath": "/keys", "sizeGb": 10, "usageWarning": True, "usageWarningThreshold": 0.8,
         "encrypted": True}]
    sync_client(rec).create_pod("p", 457, workspace="ws", cpu_line=" ", local_volumes=[])
    assert not {"cpuLine", "localVolumes", "region"} & rec.body().keys()


def test_region_is_deprecated_and_not_sent():
    rec = Recorder((200, ESTIMATE))
    with pytest.warns(DeprecationWarning, match="region is ignored by the server") as record:
        sync_client(rec).estimate_pod("p", 457, workspace="ws", region="KR")
    assert record[0].filename == __file__ and "removed in meshive 2.0" in str(record[0].message)
    assert "region" not in rec.body()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sync_client(rec).create_pod("p", 457, workspace="ws", region=None)


def test_estimate_includes_new_local_storage():
    payload = {**ESTIMATE, "pricePerHourUsd": "0.071", "breakdown": {"gpu": "0.068423", "local_storage": "0.002577"},
               "volumes": [{"storage": "pv-net", "mount_path": "/data", "storage_type": "nfs"},
                           {"storage": None, "name": "scratch", "mount_path": "/scratch", "storage_type": "hostPath",
                            "size_gb": 50, "encrypted": False, "new": True}]}
    est = sync_client(Recorder((200, payload))).estimate_pod(
        "p", 457, workspace="ws", gpu_model="RTX 3060", volumes=[("pv-net", "/data")],
        local_volumes=[{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}])
    assert est.price_per_hour == "0.071" and est.breakdown["local_storage"] == "0.002577"
    assert est.volumes[1]["new"] is True and est.volumes[1]["storage"] is None


def test_async_pod_writes_take_the_new_arguments():
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 3, "estimate": ESTIMATE}))
    start = Recorder((202, {"podName": "p-0", "action": "start"}))

    async def main():
        async with async_client(rec) as c:
            with pytest.warns(DeprecationWarning, match="region"):
                await c.create_pod("p", 1, workspace="ws", cpu_line="intel-xeon-3-ddr4", region="KR",
                                   local_volumes=[{"name": "s", "mount_path": "/s", "size_gb": 20}])
        async with async_client(start) as c:
            await c.start_pod("p-0", "ws", placement="wait_same_node")
        storage = Recorder((200, {"pricePerHourUsd": "0", "sizeGb": 10, "storageType": "nfs"}))
        async with async_client(storage) as c:
            with pytest.warns(DeprecationWarning, match="disk_type"):
                await c.estimate_storage("v", 10, workspace="ws", storage_type="hostPath", disk_type="HDD")
        return storage

    storage = asyncio.run(main())
    assert rec.body()["cpuLine"] == "intel-xeon-3-ddr4" and "region" not in rec.body()
    assert rec.body()["localVolumes"] == [{"name": "s", "mountPath": "/s", "sizeGb": 20}]
    assert start.last.url.params["placement"] == "wait_same_node"
    assert storage.body() == {"name": "v", "sizeGb": 10, "storageType": "hostPath", "encrypted": False}


# --- 1.1.0: CPU lines -------------------------------------------------------------------------------------

CPU_LINE = {"rentalType": "demand", "cpuModel": "amd-epyc-4-ddr5", "cpuProductLine": "EPYC", "cpuVendor": "AMD",
            "cpuGeneration": 4, "ddrType": 5, "cpuPricePerCore": "0.012", "ramPricePerGb": "0.002",
            "baseEphemeralStorage": 5, "ephemeralStoragePerCore": 2, "scratchDiskInitialGb": 20,
            "combinations": [{"maxCpu": 64, "maxRam": 262144, "maxStorage": 2000},
                             {"maxCpu": 96, "maxRam": 131072, "maxStorage": 1000}]}


def test_list_cpus_params_and_summary():
    from meshive import CpuLine

    rec = Recorder((200, [CPU_LINE]))
    line, = sync_client(rec).list_cpus(rental_type="Spot")
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/cpus"
    assert dict(rec.last.url.params) == {"rentalType": "spot"}
    assert isinstance(line, CpuLine) and line.cpu_line == "amd-epyc-4-ddr5"
    assert (line.vendor, line.product_line, line.generation, line.ddr_type) == ("AMD", "EPYC", 4, 5)
    assert (line.price_per_core, line.ram_price_per_gb) == ("0.012", "0.002")
    assert (line.max_vcpus_per_pod, line.max_ram_per_pod, line.machine_count) == (96, 262144, 2)
    assert line.raw["scratchDiskInitialGb"] == 20
    sync_client(rec).list_cpus()
    assert dict(rec.last.url.params) == {"rentalType": "demand"}
    with pytest.raises(ValueError):
        sync_client(rec).list_cpus(rental_type="reserved")


def test_cpu_line_missing_values_are_none_not_zero():
    from meshive import CpuLine

    line = CpuLine.from_dict({"cpuModel": "x"})
    assert line.generation is None and line.ddr_type is None and line.price_per_core is None
    assert line.max_vcpus_per_pod == 0 and line.machine_count == 0


def test_list_cpus_on_an_older_server_and_async():
    with pytest.raises(NotFoundError, match="does not support CPU lines"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).list_cpus()

    async def run():
        async with async_client(Recorder((200, [CPU_LINE]))) as c:
            return await c.list_cpus()
    assert asyncio.run(run())[0].max_vcpus_per_pod == 96


# --- 1.1.0: one transaction by id ------------------------------------------------------------------------

def _txn(status, steps=None, **extra):
    return {"transactionId": 99, "namespace": "ws", "userAlias": "p", "resource": "statefulset",
            "resourceName": "abc123", "action": "create", "status": status,
            "createdAt": "2026-10-14T00:00:00Z", "updatedAt": "2026-10-14T00:01:00Z",
            "transactionSteps": steps or [], **extra}


PULL_FAILED = [
    {"step": "create_statefulset", "status": "done", "detail": "", "updatedAt": "2026-10-14T00:00:10Z"},
    {"step": "image_pull", "status": "failed", "detail": "", "updatedAt": "2026-10-14T00:01:00Z",
     "failureDiagnostic": {"failureReason": "image pull stalled for 600s", "events": [], "containerStatuses": []}},
]


def test_get_transaction_reads_one_operation():
    rec = Recorder((200, _txn("failed", PULL_FAILED)))
    t = sync_client(rec).get_transaction(99, "ws")
    assert rec.last.url.path == "/v1/sdk/transactions/99" and dict(rec.last.url.params) == {"workspace": "ws"}
    assert t.transaction_id == 99 and t.status == "failed" and t.step == "image_pull"
    assert t.failure_reason == "failed at step image_pull: image pull stalled for 600s"
    sync_client(rec).get_transaction("99", "ws")        # PodCreated.transaction_id as the server sent it
    assert rec.last.url.path == "/v1/sdk/transactions/99"
    for bad in (None, "abc", -1, True):
        with pytest.raises(ValueError):
            sync_client(rec).get_transaction(bad, "ws")


def test_get_transaction_not_found_and_older_servers():
    missing = Recorder((404, {"detail": {"title": "Transaction Not Found", "message": "No pod transaction 5 in 'ws'."}}))
    with pytest.raises(NotFoundError) as err:
        sync_client(missing).get_transaction(5, "ws")
    assert err.value.title == "Transaction Not Found"
    with pytest.raises(NotFoundError, match="does not support transaction lookup"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).get_transaction(5, "ws")


@pytest.mark.parametrize("diagnostic, detail, expected", [
    ({"failureReason": "image pull stalled", "oomReason": "RAM limit 16Gi reached"}, "",
     "failed at step run: image pull stalled; RAM limit 16Gi reached"),
    ({"oomReason": "CUDA out of memory"}, "", "failed at step run: CUDA out of memory"),
    ({"events": [{"type": "Normal", "reason": "Pulled", "message": "pulled"},
                 {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container"}]}, "",
     "failed at step run: Back-off restarting failed container"),
    ({"events": [{"type": "Normal", "reason": "Scheduled", "message": None}]}, "", "failed at step run: Scheduled"),
    ({}, "node went away", "failed at step run: node went away"),
    (None, "", "failed at step run"),
])
def test_transaction_failure_reason(diagnostic, detail, expected):
    from meshive import Transaction

    step = {"step": "run", "status": "failed", "detail": detail, "updatedAt": "2026-10-14T00:01:00Z"}
    if diagnostic is not None:
        step["failureDiagnostic"] = diagnostic
    assert Transaction.from_dict(_txn("failed", [step])).failure_reason == expected
    assert Transaction.from_dict(_txn("in_progress", [step])).failure_reason is None
    assert Transaction.from_dict(_txn("failed")).failure_reason == "failed"


def test_async_get_transaction():
    async def run():
        async with async_client(Recorder((200, _txn("done")))) as c:
            return await c.get_transaction(99, "ws")
    assert asyncio.run(run()).status == "done"


# --- 1.1.0: wait_for_new_pod follows the creation transaction ---------------------------------------------

POD_ROW = {"podName": "p-0", "namespaceName": "ws", "userAlias": "p", "status": "creating", "rentalType": "demand",
           "pricePerHour": "0.1", "isMaintenance": False}


class Server:
    """A fake API for waits: GET /pods, /pods/{name} and /transactions/{id} answer from lists that advance per call."""

    def __init__(self, *, pods, transactions=None):
        self.pods = list(pods)            # each item: a list of pods for GET /pods, or a status for GET /pods/p-0
        self.transactions = list(transactions or [])
        self.paths: list[str] = []

    @staticmethod
    def _next(items):
        return items.pop(0) if len(items) > 1 else items[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/sdk")
        self.paths.append(path)
        if path.startswith("/transactions/"):
            status, body = self._next(self.transactions)
            return httpx.Response(status, json=body)
        item = self._next(self.pods)
        if path == "/pods":
            return httpx.Response(200, json={"pods": item if isinstance(item, list) else [{**POD_ROW, "status": item}]})
        return httpx.Response(200, json={**POD_ROW, "status": item if isinstance(item, str) else "creating"})


def test_wait_for_new_pod_reports_a_failed_creation_at_once(slept):
    server = Server(pods=[[]], transactions=[(200, _txn("in_progress")), (200, _txn("failed", PULL_FAILED))])
    with pytest.raises(PodCreationFailedError) as err:
        sync_client(server).wait_for_new_pod("p", "ws", transaction_id=99, interval=5)
    assert err.value.pod_name == "p"
    assert err.value.reason == "creation failed at step image_pull: image pull stalled for 600s"
    assert server.paths == ["/pods", "/transactions/99", "/pods", "/transactions/99"] and slept == [5]


def test_wait_for_new_pod_reports_a_cancelled_creation(slept):
    server = Server(pods=[[]], transactions=[(200, _txn("superseded"))])
    with pytest.raises(PodCreationFailedError, match="cancelled because the pod was deleted"):
        sync_client(server).wait_for_new_pod("p", "ws", transaction_id=99)
    assert server.paths == ["/pods", "/transactions/99"]


def test_wait_for_new_pod_keeps_following_after_the_pod_appears(slept):
    server = Server(pods=[[{**POD_ROW}], "creating"],
                    transactions=[(200, _txn("in_progress")), (200, _txn("failed", PULL_FAILED))])
    with pytest.raises(PodCreationFailedError, match="image pull stalled"):
        sync_client(server).wait_for_new_pod("p", "ws", transaction_id="99")
    assert server.paths == ["/pods", "/transactions/99", "/pods/p-0", "/transactions/99"]


def test_wait_for_new_pod_stops_following_a_finished_transaction(slept):
    server = Server(pods=[[], "creating", "running"], transactions=[(200, _txn("in_progress")), (200, _txn("done"))])
    pod = sync_client(server).wait_for_new_pod("p", "ws", transaction_id=99)
    assert pod.status == "running"
    assert server.paths == ["/pods", "/transactions/99", "/pods", "/transactions/99", "/pods/p-0"]   # no lookups after done


@pytest.mark.parametrize("answer", [
    (404, {"detail": {"title": "Transaction Not Found", "message": "No pod transaction 99"}}),
    (404, {"detail": "Not Found"}),           # a server without the lookup
])
def test_wait_for_new_pod_falls_back_when_the_transaction_cannot_be_read(slept, answer):
    server = Server(pods=[[], [{**POD_ROW}], "running"], transactions=[answer])
    assert sync_client(server).wait_for_new_pod("p", "ws", transaction_id=99).status == "running"
    assert server.paths == ["/pods", "/transactions/99", "/pods", "/pods/p-0"]


def test_wait_for_new_pod_without_transaction_id_watches_the_pod_only(slept):
    server = Server(pods=[[], [{**POD_ROW}], "running"], transactions=[(200, _txn("failed", PULL_FAILED))])
    assert sync_client(server).wait_for_new_pod("p", "ws").status == "running"
    assert "/transactions/99" not in server.paths
    with pytest.raises(ValueError):
        sync_client(server).wait_for_new_pod("p", "ws", transaction_id="not-a-number")


def test_wait_for_new_pod_until_a_terminal_status_is_not_cut_short(slept):
    """Waiting *for* terminated means a failed creation is the answer, not an error."""
    server = Server(pods=[[{**POD_ROW, "status": "terminated"}], "terminated"],
                    transactions=[(200, _txn("failed", PULL_FAILED))])
    pod = sync_client(server).wait_for_new_pod("p", "ws", until="terminated", transaction_id=99)
    assert pod.status == "terminated" and "/transactions/99" not in server.paths


def test_async_wait_for_new_pod_follows_the_transaction(slept):
    server = Server(pods=[[]], transactions=[(200, _txn("failed", PULL_FAILED))])
    fallback = Server(pods=[[{**POD_ROW}], "running"], transactions=[(404, {"detail": "Not Found"})])

    async def run():
        async with async_client(server) as c:
            with pytest.raises(PodCreationFailedError, match="image_pull"):
                await c.wait_for_new_pod("p", "ws", transaction_id=99)
        async with async_client(fallback) as c:
            return await c.wait_for_new_pod("p", "ws", transaction_id=99)

    assert asyncio.run(run()).status == "running"
    assert server.paths == ["/pods", "/transactions/99"] and fallback.paths == ["/pods", "/transactions/99", "/pods/p-0"]


# --- 1.1.0: server-chosen start placement, template ports, storage usage warnings ------------------------

def test_start_pod_without_placement_lets_the_server_choose():
    """No placement → the query parameter is left out (not sent empty), and the server's choice comes back."""
    rec = Recorder((202, {"podName": "p-0", "workspace": "ws", "action": "start", "accepted": True,
                          "placement": "wait_same_node"}))
    c = sync_client(rec)
    action = c.start_pod("p-0", "ws")
    assert dict(rec.last.url.params) == {"workspace": "ws", "allow_data_loss": "false"}
    assert "placement=" not in str(rec.last.url)
    assert isinstance(action, ResourceAction) and action.placement == "wait_same_node"
    c.start_pod("p-0", "ws", placement=None, allow_data_loss=True)
    assert dict(rec.last.url.params) == {"workspace": "ws", "allow_data_loss": "true"}
    c.start_pod("p-0", "ws", placement="Same_Node")
    assert rec.last.url.params["placement"] == "same_node"      # an explicit placement is still sent


def test_resource_action_placement_is_none_when_not_reported():
    rec = Recorder((202, {"podName": "p-0", "action": "stop"}))
    assert sync_client(rec).stop_pod("p-0", "ws").placement is None
    assert sync_client(Recorder((202, {"podName": "p-0", "action": "start"}))).start_pod("p-0", "ws").placement is None
    # Added after raw, so 1.0 code that builds one positionally keeps working.
    action = ResourceAction("pod", "p-0", "start", "ws", True, 7, {"podName": "p-0"})
    assert action.raw == {"podName": "p-0"} and action.placement is None


def test_start_pod_consent_refusal_names_the_servers_placement():
    rec = Recorder((409, {"detail": {"title": "Data Loss Consent Required", "message": "consent required",
                                     "placement": "any_node"}}))
    with pytest.raises(ConflictError) as err:
        sync_client(rec).start_pod("p-0", "ws")
    assert err.value.title == "Data Loss Consent Required" and err.value.raw["detail"]["placement"] == "any_node"
    assert "placement" not in rec.last.url.params and rec.last.url.params["allow_data_loss"] == "false"


def test_create_and_estimate_pod_send_template_ports():
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 8, "estimate": ESTIMATE}))
    c = sync_client(rec)
    ports = [{"port": 8080, "name": " ide "}, {"port": 8888, "newPort": 9999}, {"port": 22, "external": False},
             {"port": 7860, "name": "ui", "new_port": 7861, "external": 1}, {"port": 3000}]
    expected = [{"port": 8080, "name": "ide"}, {"port": 8888, "newPort": 9999}, {"port": 22, "external": False},
                {"port": 7860, "name": "ui", "newPort": 7861, "external": True}, {"port": 3000}]
    c.create_pod("p", 457, workspace="ws", template_ports=ports, ports=[9000])
    assert rec.body()["templatePorts"] == expected and rec.body()["ports"] == [{"port": 9000, "external": True}]
    c.estimate_pod("p", 457, workspace="ws", template_ports=ports)
    assert rec.last.url.path == "/v1/sdk/pods/estimate" and rec.body()["templatePorts"] == expected
    c.create_pod("p", 457, workspace="ws", template_ports=[])
    assert "templatePorts" not in rec.body()
    c.create_pod("p", 457, workspace="ws")
    assert "templatePorts" not in rec.body()


@pytest.mark.parametrize("template_ports", [
    [8080], [("8080", "ide")], [{"name": "ide"}], [{"port": 0}], [{"port": True}], [{"port": "8080"}],
    [{"port": 8080, "new_port": 70000}], [{"port": 8080, "newPort": "8443"}], [{"port": 8080, "name": " "}],
])
def test_template_ports_shape_errors(template_ports):
    rec = Recorder((200, ESTIMATE))
    with pytest.raises(ValueError, match="template_ports"):
        sync_client(rec).estimate_pod("p", 457, workspace="ws", template_ports=template_ports)
    assert rec.requests == []


def test_template_port_rules_are_the_servers():
    """Whether the port is the template's, or clashes with another, is the server's check — its 400 comes back."""
    rec = Recorder((400, {"detail": {"title": "Port Not In Template", "message": "8081 is not a port of this template",
                                     "templatePorts": [8081]}}))
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(rec).create_pod("p", 457, workspace="ws", template_ports=[{"port": 8081, "name": "x"}])
    assert err.value.status_code == 400 and err.value.title == "Port Not In Template"
    assert err.value.raw["detail"]["templatePorts"] == [8081]


def test_storage_usage_warning_is_sent_only_when_given():
    rec = Recorder((202, {"name": "vol", "workspace": "ws", "transactionId": 5, "estimate": {}}))
    c = sync_client(rec)
    c.create_storage("vol", 10, workspace="ws", usage_warning=True, usage_warning_threshold=0.8)
    assert rec.body() == {"name": "vol", "sizeGb": 10, "storageType": "nfs", "encrypted": False,
                          "usageWarning": True, "usageWarningThreshold": 0.8}
    c.estimate_storage("vol", 10, workspace="ws", usage_warning=False)
    assert rec.body()["usageWarning"] is False and "usageWarningThreshold" not in rec.body()
    c.estimate_storage("vol", 10, workspace="ws", usage_warning_threshold="0.75")
    assert rec.body()["usageWarningThreshold"] == 0.75 and "usageWarning" not in rec.body()
    c.create_storage("vol", 10, workspace="ws")
    assert not {"usageWarning", "usageWarningThreshold"} & rec.body().keys()
    for bad in ("most", True):
        with pytest.raises(ValueError, match="usage_warning_threshold"):
            c.create_storage("vol", 10, workspace="ws", usage_warning=True, usage_warning_threshold=bad)


def test_storage_minimum_size_and_threshold_range_are_the_servers():
    rec = Recorder((422, {"detail": [{"loc": ["body", "sizeGb"], "msg": "Input should be greater than or equal to 10"}]}))
    c = sync_client(rec)
    with pytest.raises(MeshiveAPIError) as err:
        c.create_storage("vol", 5, workspace="ws")
    assert err.value.status_code == 422 and rec.body()["sizeGb"] == 5
    with pytest.raises(MeshiveAPIError):
        c.estimate_storage("vol", 10, workspace="ws", usage_warning=True, usage_warning_threshold=1.5)
    assert rec.body()["usageWarningThreshold"] == 1.5


def test_async_client_takes_the_new_start_port_and_storage_arguments():
    start = Recorder((202, {"podName": "p-0", "action": "start", "placement": "any_node"}))
    pod = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 3, "estimate": ESTIMATE}))
    storage = Recorder((202, {"name": "vol", "workspace": "ws", "transactionId": 5, "estimate": {}}))

    async def run():
        async with async_client(start) as c:
            action = await c.start_pod("p-0", "ws", allow_data_loss=True)
        async with async_client(pod) as c:
            await c.estimate_pod("p", 457, workspace="ws", template_ports=[{"port": 8080, "external": True}])
            await c.create_pod("p", 457, workspace="ws", template_ports=[{"port": 8080, "new_port": 8443}])
        async with async_client(storage) as c:
            await c.estimate_storage("vol", 20, workspace="ws", usage_warning=True)
            await c.create_storage("vol", 20, workspace="ws", usage_warning=True, usage_warning_threshold=0.5)
        return action

    action = asyncio.run(run())
    assert action.placement == "any_node" and "placement" not in start.last.url.params
    assert pod.body(0)["templatePorts"] == [{"port": 8080, "external": True}]
    assert pod.body(1)["templatePorts"] == [{"port": 8080, "newPort": 8443}]
    assert storage.body(0)["usageWarning"] is True and "usageWarningThreshold" not in storage.body(0)
    assert storage.body(1)["usageWarningThreshold"] == 0.5


# --- 1.2.0: storage edit -------------------------------------------------------------------------------------

STORAGE_UPDATE_ESTIMATE = {"pvName": "pv-1", "name": "data", "sizeGb": 200, "currentSizeGb": 100,
                           "pricePerHourUsd": "0.0194", "currentPricePerHourUsd": "0.0097", "usageWarning": True,
                           "usageWarningThreshold": 0.8, "note": "Billed at the new size."}


def test_update_storage_sends_only_what_is_given():
    from meshive import StorageUpdated

    rec = Recorder((202, {"pvName": "pv-1", "workspace": "ws", "accepted": True, "transactionId": 77,
                          "estimate": STORAGE_UPDATE_ESTIMATE}))
    c = sync_client(rec)
    updated = c.update_storage("pv-1", "ws", size_gb=200, idempotency_key="grow-0001")
    assert rec.last.method == "PATCH" and rec.last.url.path == "/v1/sdk/storages/pv-1"
    assert dict(rec.last.url.params) == {"workspace": "ws"} and rec.last.headers["Idempotency-Key"] == "grow-0001"
    assert rec.body() == {"sizeGb": 200}
    assert isinstance(updated, StorageUpdated) and updated.transaction_id == 77 and updated.accepted
    assert (updated.estimate.size_gb, updated.estimate.current_size_gb) == (200, 100)
    assert updated.estimate.price_per_hour == "0.0194" and updated.estimate.current_price_per_hour == "0.0097"
    c.update_storage("pv-1", "ws", name=" archive ", usage_warning=False)
    assert rec.body() == {"name": "archive", "usageWarning": False}
    c.update_storage("pv-1", "ws", usage_warning=True, usage_warning_threshold="0.75")
    assert rec.body() == {"usageWarning": True, "usageWarningThreshold": 0.75}


def test_update_storage_without_a_transaction():
    """A rename or a warning change alone is applied at once — no transaction (or an earlier one)."""
    rec = Recorder((202, {"pvName": "pv-1", "workspace": "ws", "accepted": True, "transactionId": None,
                          "estimate": {"pvName": "pv-1", "name": "archive", "sizeGb": 100}}))
    updated = sync_client(rec).update_storage("pv-1", "ws", name="archive")
    assert updated.transaction_id is None and updated.estimate.current_price_per_hour is None
    assert updated.estimate.usage_warning is None and updated.estimate.usage_warning_threshold is None


def test_estimate_storage_update():
    rec = Recorder((200, STORAGE_UPDATE_ESTIMATE))
    est = sync_client(rec).estimate_storage_update("pv-1", "ws", size_gb=200, usage_warning=True,
                                                   usage_warning_threshold=0.8)
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/storages/pv-1/estimate"
    assert rec.body() == {"sizeGb": 200, "usageWarning": True, "usageWarningThreshold": 0.8}
    assert (est.pv_name, est.name, est.usage_warning, est.usage_warning_threshold) == ("pv-1", "data", True, 0.8)
    assert est.note == "Billed at the new size." and est.raw["currentSizeGb"] == 100


@pytest.mark.parametrize("kwargs", [
    dict(), dict(size_gb=0), dict(size_gb="200"), dict(size_gb=True), dict(name=" "), dict(name=5),
    dict(usage_warning_threshold="most"), dict(usage_warning_threshold=True),
])
def test_storage_update_shape_errors(kwargs):
    rec = Recorder((202, {}))
    c = sync_client(rec)
    with pytest.raises(ValueError):
        c.update_storage("pv-1", "ws", **kwargs)
    with pytest.raises(ValueError):
        c.estimate_storage_update("pv-1", "ws", **kwargs)
    assert rec.requests == []


def test_storage_update_rules_are_the_servers():
    """The minimum size and "can only grow" are the server's checks: the request goes out and its error comes back."""
    rec = Recorder((400, {"detail": {"title": "Storage Cannot Shrink", "message": "Storage can only grow.",
                                     "currentSizeGb": 100}}))
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(rec).update_storage("pv-1", "ws", size_gb=50)
    assert err.value.title == "Storage Cannot Shrink" and err.value.raw["detail"]["currentSizeGb"] == 100
    assert rec.body() == {"sizeGb": 50}
    rec = Recorder((422, {"detail": [{"loc": ["body", "sizeGb"], "msg": "Input should be greater than or equal to 10"}]}))
    with pytest.raises(MeshiveAPIError):
        sync_client(rec).estimate_storage_update("pv-1", "ws", size_gb=5)
    assert rec.body() == {"sizeGb": 5}


def test_storage_update_not_found_and_older_servers():
    with pytest.raises(NotFoundError) as err:
        sync_client(Recorder((404, {"detail": {"title": "Storage Not Found", "message": "No storage pv-9"}}))) \
            .update_storage("pv-9", "ws", name="x")
    assert err.value.title == "Storage Not Found"
    # PATCH /storages/{pv} collides with GET/DELETE of the same path on an older server → 405
    with pytest.raises(NotFoundError, match="does not support storage editing"):
        sync_client(Recorder((405, {"detail": "Method Not Allowed"}))).update_storage("pv-1", "ws", name="x")
    with pytest.raises(NotFoundError, match="does not support storage editing"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).estimate_storage_update("pv-1", "ws", size_gb=20)


# --- 1.2.0: storage connect ----------------------------------------------------------------------------------

SSH = {"password": "pw-123", "sshUrl": "ssh -p 2222 gateway@m.example", "sshWebUrl": "https://m.example/?p=x",
       "expiredAt": 1_900_000_000}


def _conn(phase, **extra):
    return {"pvName": "pv-1", "workspace": "ws", "encrypted": True, "phase": phase, **extra}


class ConnectServer:
    """POST/GET /storages/pv-1/connect answer from a list that advances per call; POST /pods/{pod}/ssh answers SSH."""

    def __init__(self, *phases):
        self.phases = list(phases)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/sdk")
        self.calls.append((request.method, path))
        if path.endswith("/ssh"):
            return httpx.Response(200, json=SSH)
        status, body = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        return httpx.Response(status, json=body)


def test_connect_storage_waits_until_ready_and_gets_ssh(slept):
    from meshive import SshAccess, StorageConnection

    ready = _conn("ready", podName="pv-1-access-0", transactionId=31, autoLockMinutes=30, remainingSeconds=1790)
    server = ConnectServer((200, _conn("preparing", transactionId=31)), (200, _conn("preparing")), (200, ready))
    conn = sync_client(server).connect_storage("pv-1", "ws", interval=2)
    assert server.calls == [("POST", "/storages/pv-1/connect"), ("GET", "/storages/pv-1/connect"),
                            ("GET", "/storages/pv-1/connect"), ("POST", "/pods/pv-1-access-0/ssh")]
    assert slept == [2, 2]
    assert isinstance(conn, StorageConnection) and conn.ready and conn.phase == "ready" and conn.encrypted is True
    assert (conn.pod_name, conn.transaction_id, conn.auto_lock_minutes, conn.remaining_seconds) == \
        ("pv-1-access-0", 31, 30, 1790)
    assert isinstance(conn.ssh, SshAccess) and conn.ssh.command.startswith("ssh -p 2222")
    assert conn.ssh.password == "pw-123" and "pw-123" not in repr(conn)


def test_connect_storage_ready_at_once_and_plain_storage(slept):
    server = ConnectServer((200, {**_conn("ready", podName="acc-0"), "encrypted": False}))
    conn = sync_client(server).connect_storage("pv-1", "ws")
    assert server.calls == [("POST", "/storages/pv-1/connect"), ("POST", "/pods/acc-0/ssh")] and slept == []
    assert conn.encrypted is False and conn.auto_lock_minutes is None and conn.ssh.password == "pw-123"


def test_connect_storage_without_waiting(slept):
    server = ConnectServer((200, _conn("preparing")))
    conn = sync_client(server).connect_storage("pv-1", "ws", wait=False, idempotency_key="connect-0001")
    assert server.calls == [("POST", "/storages/pv-1/connect")] and conn.ssh is None and not conn.ready
    server = ConnectServer((200, _conn("ready", podName="acc-0")))
    assert sync_client(server).connect_storage("pv-1", "ws", wait=False).ssh is not None


def test_connect_storage_error_phase(slept):
    from meshive import StorageConnectionError

    server = ConnectServer((200, _conn("preparing")), (200, _conn("error")))
    with pytest.raises(StorageConnectionError, match="could not be opened for SSH access") as err:
        sync_client(server).connect_storage("pv-1", "ws")
    assert err.value.pv_name == "pv-1" and err.value.connection.phase == "error"
    assert not any(path.endswith("/ssh") for _, path in server.calls)
    with pytest.raises(StorageConnectionError):
        sync_client(ConnectServer((200, _conn("error")))).connect_storage("pv-1", "ws", wait=False)


def test_connect_storage_times_out(slept):
    server = ConnectServer((200, _conn("preparing")))
    with pytest.raises(WaitTimeoutError, match="not ready for SSH access within 0s .last phase: preparing") as err:
        sync_client(server).connect_storage("pv-1", "ws", timeout=0)
    assert isinstance(err.value, TimeoutError) and server.calls == [("POST", "/storages/pv-1/connect")]


def test_connect_storage_keeps_waiting_through_unknown_phases(slept):
    server = ConnectServer((200, _conn("unlocking")), (200, _conn("ready", podName="acc-0")))
    assert sync_client(server).connect_storage("pv-1", "ws").ready


def test_get_storage_connection_and_older_servers():
    rec = Recorder((200, _conn("preparing", podName=None)))
    conn = sync_client(rec).get_storage_connection("pv-1", "ws")
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/storages/pv-1/connect"
    assert dict(rec.last.url.params) == {"workspace": "ws"} and conn.pod_name is None and conn.ssh is None
    for call in (lambda c: c.get_storage_connection("pv-1", "ws"), lambda c: c.connect_storage("pv-1", "ws")):
        with pytest.raises(NotFoundError, match="does not support storage SSH access"):
            call(sync_client(Recorder((404, {"detail": "Not Found"}))))
    with pytest.raises(NotFoundError) as err:
        sync_client(Recorder((404, {"detail": {"title": "Storage Not Found", "message": "x"}}))) \
            .connect_storage("pv-9", "ws")
    assert err.value.title == "Storage Not Found"


def test_async_connect_storage(slept):
    from meshive import StorageConnectionError

    server = ConnectServer((200, _conn("preparing")), (200, _conn("ready", podName="acc-0")))
    failing = ConnectServer((200, _conn("preparing")), (200, _conn("error")))

    async def run():
        async with async_client(server) as c:
            conn = await c.connect_storage("pv-1", "ws", interval=1)
            status = await c.get_storage_connection("pv-1", "ws")
        async with async_client(failing) as c:
            with pytest.raises(StorageConnectionError):
                await c.connect_storage("pv-1", "ws")
        async with async_client(ConnectServer((200, _conn("preparing")))) as c:
            with pytest.raises(WaitTimeoutError):
                await c.connect_storage("pv-1", "ws", timeout=0)
        return conn, status

    conn, status = asyncio.run(run())
    assert conn.ssh.password == "pw-123" and status.ready and slept == [1, 3]
    assert server.calls[:3] == [("POST", "/storages/pv-1/connect"), ("GET", "/storages/pv-1/connect"),
                                ("POST", "/pods/acc-0/ssh")]


# --- 1.2.0: model sets ---------------------------------------------------------------------------------------

MODEL_SETS = {"templateId": 457, "selectable": True, "modelSets": [{
    "slug": "sdxl-basics", "name": "SDXL basics", "description": "SDXL with a VAE", "family": "sdxl",
    "modality": "image", "task": "text-to-image", "totalSizeBytes": 7_000_000_000, "minVramGb": 12,
    "minVramEstimated": True, "bestResolution": "1024x1024",
    "licenses": [{"license": "openrail++", "licenseUrl": "https://example.com/l"}], "licenseUnknownCount": 1,
    "workflows": [{"slug": "t2i", "name": "Text to image", "task": "text-to-image"}],
    "items": [{"entrySlug": "sdxl-base", "name": "sd_xl_base_1.0.safetensors", "category": "checkpoints",
               "sizeBytes": 6_900_000_000, "defaultRole": "checkpoint", "license": "openrail++",
               "licenseUrl": "https://example.com/l", "alreadyInWorkspace": True},
              {"entrySlug": "sdxl-vae", "name": "sdxl_vae.safetensors", "category": "vae", "sizeBytes": None}]}]}


def test_list_model_sets_parses():
    from meshive import ModelSet, ModelSetItem, ModelSetList

    rec = Recorder((200, MODEL_SETS))
    sets = sync_client(rec).list_model_sets("ws", 457)
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/model-sets"
    assert dict(rec.last.url.params) == {"workspace": "ws", "templateId": "457"}
    assert isinstance(sets, ModelSetList) and sets.selectable and sets.reason is None and len(sets) == 1
    s, = sets
    assert isinstance(s, ModelSet) and (s.slug, s.name, s.task, s.total_size_bytes) == \
        ("sdxl-basics", "SDXL basics", "text-to-image", 7_000_000_000)
    assert (s.min_vram_gb, s.min_vram_estimated, s.best_resolution, s.license_unknown_count) == (12.0, True, "1024x1024", 1)
    assert s.licenses[0].license == "openrail++" and s.workflows[0].slug == "t2i"
    base, vae = s.items
    assert isinstance(base, ModelSetItem) and base.already_in_workspace is True and base.size_bytes == 6_900_000_000
    assert vae.size_bytes is None and vae.already_in_workspace is None and vae.license is None
    sync_client(rec).list_model_sets("ws", "457")
    assert rec.last.url.params["templateId"] == "457"
    for bad in (None, "abc", -1, True):
        with pytest.raises(ValueError):
            sync_client(rec).list_model_sets("ws", bad)


def test_list_model_sets_not_selectable_and_older_servers():
    rec = Recorder((200, {"templateId": 12, "selectable": False, "reason": "This template adds its own model set.",
                          "modelSets": []}))
    sets = sync_client(rec).list_model_sets("ws", 12)
    assert not sets.selectable and sets.reason == "This template adds its own model set." and list(sets) == []
    with pytest.raises(NotFoundError, match="does not support model sets"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).list_model_sets("ws", 12)


MODEL_SET_ESTIMATE = {**ESTIMATE, "modelSet": {"slug": "sdxl-basics", "name": "SDXL basics", "file_count": 2,
                                               "total_size_bytes": 7_000_000_000, "min_vram_gb": 12,
                                               "min_vram_estimated": True}}


def test_estimate_pod_with_a_model_set():
    rec = Recorder((200, MODEL_SET_ESTIMATE))
    est = sync_client(rec).estimate_pod("p", 457, workspace="ws", gpu_model="RTX 4090", model_set=" sdxl-basics ")
    assert rec.body()["modelSet"] == "sdxl-basics" and est.model_set["file_count"] == 2
    assert rec.last.extensions["timeout"]["read"] < 900          # only the create waits long
    est = sync_client(Recorder((200, ESTIMATE))).estimate_pod("p", 457, workspace="ws", model_set=" ")
    assert est.model_set is None
    with pytest.raises(ValueError, match="model_set"):
        sync_client(rec).estimate_pod("p", 457, workspace="ws", model_set=3)


def test_create_pod_with_a_model_set_waits_long():
    rec = Recorder((202, {"name": "p", "workspace": "ws", "transactionId": 5, "estimate": MODEL_SET_ESTIMATE}))
    created = sync_client(rec).create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
    assert rec.body()["modelSet"] == "sdxl-basics" and created.estimate.model_set["slug"] == "sdxl-basics"
    assert rec.last.extensions["timeout"] == {"connect": 30.0, "read": 900.0, "write": 30.0, "pool": 30.0}
    sync_client(rec, timeout=1200.0).create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
    assert rec.last.extensions["timeout"]["read"] == 1200.0     # a longer client timeout is kept
    sync_client(rec).create_pod("p", 457, workspace="ws")
    assert "modelSet" not in rec.body() and rec.last.extensions["timeout"]["read"] < 900


def test_create_pod_with_a_model_set_is_not_resent_after_a_timeout(slept):
    seen = []

    def timeout(request):
        seen.append(request.headers["Idempotency-Key"])
        raise httpx.ReadTimeout("no answer", request=request)

    with pytest.raises(httpx.ReadTimeout) as err:
        sync_client(timeout).create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
    assert len(seen) == 1 and slept == []
    assert err.value.idempotency_key == seen[0] and err.value.operation_path == "/pods"
    seen.clear()
    with pytest.raises(httpx.ReadTimeout):
        sync_client(timeout).create_pod("p", 457, workspace="ws")       # without a model set: retried as before
    assert len(seen) == 3 and len(set(seen)) == 1


@pytest.mark.parametrize("first, resent", [
    ((504, {"detail": "Gateway Timeout"}), False),       # the first request may still be running
    ((502, {"detail": "Bad Gateway"}), False),
    ((500, {"detail": "Internal Server Error"}), False),
    ((503, {"detail": {"title": "Scheduling Busy", "message": "busy"}}), True),   # refused before it started
    ((429, {"detail": {"title": "Too Many Requests", "message": "slow down"}}), True),
])
def test_create_pod_with_a_model_set_is_resent_only_when_it_never_started(slept, first, resent):
    rec = Recorder(first, (202, {"name": "p", "workspace": "ws", "transactionId": 5, "estimate": ESTIMATE}))
    c = sync_client(rec)
    if resent:
        assert c.create_pod("p", 457, workspace="ws", model_set="sdxl-basics").transaction_id == 5
        assert len(rec.requests) == 2
        assert rec.requests[0].headers["Idempotency-Key"] == rec.requests[1].headers["Idempotency-Key"]
    else:
        with pytest.raises(MeshiveAPIError):
            c.create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
        assert len(rec.requests) == 1


def test_create_pod_with_a_model_set_is_resent_when_the_connection_failed(slept):
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(202, json={"name": "p", "workspace": "ws", "transactionId": 6, "estimate": ESTIMATE})

    assert sync_client(flaky).create_pod("p", 457, workspace="ws", model_set="s").transaction_id == 6
    assert len(attempts) == 2 and slept == [0.5]


def test_model_set_errors_come_back_as_they_are():
    rec = Recorder((400, {"detail": {"title": "Model Set Unavailable", "message": "1 file could not be fetched.",
                                     "modelSet": "sdxl-basics",
                                     "failed": [{"slug": "sdxl-vae", "name": "sdxl_vae", "error": "403 from source"}]}}))
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(rec).create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
    assert err.value.title == "Model Set Unavailable" and err.value.raw["detail"]["failed"][0]["slug"] == "sdxl-vae"
    rec = Recorder((409, {"detail": {"title": "Operation Outcome Unknown",
                                     "message": "A request with this key is still running."}}))
    with pytest.raises(ConflictError) as err:
        sync_client(rec).create_pod("p", 457, workspace="ws", model_set="sdxl-basics", idempotency_key="k-1")
    assert err.value.title == "Operation Outcome Unknown" and len(rec.requests) == 1


def test_async_model_sets(slept):
    sets = Recorder((200, MODEL_SETS))
    pod = Recorder((200, MODEL_SET_ESTIMATE),
                   (202, {"name": "p", "workspace": "ws", "transactionId": 5, "estimate": MODEL_SET_ESTIMATE}))
    seen = []

    def timeout(request):
        seen.append(request)
        raise httpx.ReadTimeout("no answer", request=request)

    async def run():
        async with async_client(sets) as c:
            listed = await c.list_model_sets("ws", 457)
        async with async_client(pod) as c:
            est = await c.estimate_pod("p", 457, workspace="ws", model_set="sdxl-basics")
            await c.create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
        async with async_client(timeout) as c:
            with pytest.raises(httpx.ReadTimeout):
                await c.create_pod("p", 457, workspace="ws", model_set="sdxl-basics")
        return listed, est

    listed, est = asyncio.run(run())
    assert listed.model_sets[0].slug == "sdxl-basics" and est.model_set["name"] == "SDXL basics"
    assert pod.last.extensions["timeout"]["read"] == 900.0 and len(seen) == 1


# --- 1.2.0: pod edit -----------------------------------------------------------------------------------------

POD_UPDATE_ESTIMATE = {"podName": "p-0", "name": "agent-pod", "changes": ["GPU: 1x RTX 3060 -> 1x RTX 4090",
                                                                          "Environment: adds HF_HOME"],
                       "restarts": True, "pricePerHourUsd": "0.31", "currentPricePerHourUsd": "0.068",
                       "breakdown": {"gpu": "0.31"}, "resources": {"gpu_model": "RTX 4090"}, "volumes": [],
                       "note": "The running pod restarts."}


def test_update_pod_sends_every_change_in_the_servers_shape():
    from meshive import PodUpdated

    rec = Recorder((202, {"podName": "p-0", "workspace": "ws", "accepted": True, "transactionId": 900,
                          "renamedOnly": False, "estimate": POD_UPDATE_ESTIMATE}))
    updated = sync_client(rec).update_pod(
        "p-0", "ws", name=" agent-pod ", template_id=458, rental_type="Spot", gpu_model=" RTX 4090 ", gpu_vram_gb=24,
        gpu_count=2, vcpu=16, ram_gb=64, cpu_line="amd-epyc-4-ddr5", env={"HF_HOME": "/data/hf", "TOKEN": "x"},
        secret_keys=["TOKEN"], remove_env="OLD", ports=[9000, {"port": 7000, "name": "api", "external": False}],
        template_ports=[{"port": 8080, "new_port": 8443}], remove_ports=[6006], command="",
        volumes=[("pv-net", "/data")], local_volumes=[{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}],
        detach_storages=["pv-old"], allow_node_migration=True, allow_data_loss=False,
        drop_watched_folders="/workspace/out", max_price_per_hour=0.5, idempotency_key="edit-0001")
    req = rec.last
    assert req.method == "PATCH" and req.url.path == "/v1/sdk/pods/p-0" and dict(req.url.params) == {"workspace": "ws"}
    assert req.headers["Idempotency-Key"] == "edit-0001"
    assert rec.body() == {
        "name": "agent-pod", "templateId": 458, "rentalType": "spot", "gpuModel": "RTX 4090", "gpuVramGb": 24,
        "gpuCount": 2, "vcpu": 16, "ramGb": 64, "cpuLine": "amd-epyc-4-ddr5",
        "env": {"HF_HOME": "/data/hf", "TOKEN": "x"}, "secretKeys": ["TOKEN"], "removeEnv": ["OLD"],
        "ports": [{"port": 9000, "external": True}, {"port": 7000, "external": False, "name": "api"}],
        "templatePorts": [{"port": 8080, "newPort": 8443}], "removePorts": [6006], "command": "",
        "volumes": [{"storage": "pv-net", "mountPath": "/data"}],
        "localVolumes": [{"name": "scratch", "mountPath": "/scratch", "sizeGb": 50}], "detachStorages": ["pv-old"],
        "allowNodeMigration": True, "allowDataLoss": False, "dropWatchedFolders": ["/workspace/out"],
        "maxPricePerHourUsd": "0.5",
    }
    assert isinstance(updated, PodUpdated) and updated.transaction_id == 900 and updated.renamed_only is False
    assert updated.estimate.changes[0].startswith("GPU") and updated.estimate.restarts is True


@pytest.mark.parametrize("kwargs, body", [
    (dict(name="renamed"), {"name": "renamed"}),
    (dict(command=""), {"command": ""}),                              # "" = back to the template's command
    (dict(command="sleep infinity"), {"command": "sleep infinity"}),
    (dict(env={"A": 1}), {"env": {"A": "1"}}),
    (dict(remove_ports=8888), {"removePorts": [8888]}),
    (dict(detach_storages="pv-a", max_price_per_hour="0.2"), {"detachStorages": ["pv-a"], "maxPricePerHourUsd": "0.2"}),
    (dict(gpu_count=3, env={}, ports=[], volumes=None, remove_env=[]), {"gpuCount": 3}),   # empty means nothing
])
def test_update_pod_sends_only_what_is_given(kwargs, body):
    rec = Recorder((202, {"podName": "p-0", "workspace": "ws", "accepted": True, "transactionId": None,
                          "renamedOnly": True, "estimate": {}}))
    updated = sync_client(rec).update_pod("p-0", "ws", **kwargs)
    assert rec.body() == body
    assert updated.renamed_only is True and updated.transaction_id is None


def test_estimate_pod_update():
    from meshive import PodUpdateEstimate

    rec = Recorder((200, POD_UPDATE_ESTIMATE))
    est = sync_client(rec).estimate_pod_update("p-0", "ws", gpu_model="RTX 4090", env={"HF_HOME": "/data/hf"})
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/pods/p-0/estimate"
    assert rec.body() == {"gpuModel": "RTX 4090", "env": {"HF_HOME": "/data/hf"}}
    assert isinstance(est, PodUpdateEstimate) and est.pod_name == "p-0" and est.name == "agent-pod"
    assert est.price_per_hour == "0.31" and est.current_price_per_hour == "0.068" and est.restarts is True
    assert len(est.changes) == 2 and est.breakdown == {"gpu": "0.31"} and est.note == "The running pod restarts."
    bare = PodUpdateEstimate.from_dict({"podName": "p-0"})          # an older or partial answer
    assert bare.restarts is None and bare.current_price_per_hour is None and bare.changes == []


@pytest.mark.parametrize("kwargs", [
    dict(), dict(allow_node_migration=True, allow_data_loss=True), dict(drop_watched_folders=["/x"]),
    dict(max_price_per_hour=1), dict(name="p", allow_data_loss="false"), dict(name="p", allow_node_migration=1),
    dict(name=" "), dict(gpu_model=""), dict(cpu_line=" "), dict(template_id=True), dict(template_id=-1),
    dict(gpu_count=0), dict(vcpu="8"), dict(rental_type="hourly"), dict(env={"A": "1"}, secret_keys=["B"]),
    dict(secret_keys=["A"]), dict(remove_env=[""]), dict(remove_ports=[70000]), dict(remove_ports=["80"]),
    dict(ports=[True]), dict(template_ports=[8080]), dict(volumes=[("pv", "data")]), dict(command=5),
    dict(local_volumes=[{"name": "s", "mount_path": "/s"}]), dict(detach_storages=[None]),
    dict(name="p", max_price_per_hour="free"),
])
def test_pod_update_shape_errors(kwargs):
    rec = Recorder((202, {}))
    c = sync_client(rec)
    with pytest.raises(ValueError):
        c.update_pod("p-0", "ws", **kwargs)
    with pytest.raises(ValueError):
        c.estimate_pod_update("p-0", "ws", **kwargs)
    assert rec.requests == []


@pytest.mark.parametrize("status, detail, exc", [
    (409, {"title": "Node Migration Required", "message": "The pod's machine can't fit this change."}, ConflictError),
    (409, {"title": "Data Loss Consent Required", "message": "consent required"}, ConflictError),
    (409, {"title": "Resource values need confirmation", "message": "pass ramGb"}, ConflictError),
    (400, {"title": "Watched folders would be removed", "message": "…", "code": "harvest_roots_orphaned",
           "paths": ["/workspace/out"]}, MeshiveAPIError),
    (400, {"title": "Nothing To Change", "message": "The pod already has these settings."}, MeshiveAPIError),
    (402, {"title": "Insufficient Credit", "message": "top up"}, InsufficientCreditError),
])
def test_pod_update_refusals_come_back_as_they_are(status, detail, exc):
    rec = Recorder((status, {"detail": detail}))
    with pytest.raises(exc) as err:
        sync_client(rec).update_pod("p-0", "ws", gpu_model="RTX 4090")
    assert err.value.title == detail["title"] and err.value.raw["detail"] == detail and len(rec.requests) == 1


def test_pod_update_on_older_servers():
    with pytest.raises(NotFoundError, match="does not support pod editing"):
        sync_client(Recorder((405, {"detail": "Method Not Allowed"}))).update_pod("p-0", "ws", name="x")
    with pytest.raises(NotFoundError, match="does not support pod editing"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).estimate_pod_update("p-0", "ws", name="x")
    with pytest.raises(NotFoundError) as err:      # a missing pod keeps the server's titled 404
        sync_client(Recorder((404, {"detail": {"title": "Pod Not Found", "message": "x"}}))).update_pod(
            "p-9", "ws", name="x")
    assert err.value.title == "Pod Not Found"


def test_async_pod_and_storage_edit():
    pod = Recorder((200, POD_UPDATE_ESTIMATE),
                   (202, {"podName": "p-0", "workspace": "ws", "accepted": True, "transactionId": 900,
                          "estimate": POD_UPDATE_ESTIMATE}))
    storage = Recorder((200, STORAGE_UPDATE_ESTIMATE),
                       (202, {"pvName": "pv-1", "workspace": "ws", "transactionId": 77, "estimate": STORAGE_UPDATE_ESTIMATE}))

    async def run():
        async with async_client(pod) as c:
            est = await c.estimate_pod_update("p-0", "ws", gpu_model="RTX 4090")
            updated = await c.update_pod("p-0", "ws", gpu_model="RTX 4090", allow_node_migration=True,
                                         idempotency_key="edit-0002")
        async with async_client(storage) as c:
            s_est = await c.estimate_storage_update("pv-1", "ws", size_gb=200)
            s_upd = await c.update_storage("pv-1", "ws", size_gb=200)
        with pytest.raises(ValueError):
            await async_client(pod).update_pod("p-0", "ws")
        return est, updated, s_est, s_upd

    est, updated, s_est, s_upd = asyncio.run(run())
    assert est.restarts and updated.transaction_id == 900
    assert pod.requests[1].method == "PATCH" and pod.body(1) == {"gpuModel": "RTX 4090", "allowNodeMigration": True}
    assert pod.requests[1].headers["Idempotency-Key"] == "edit-0002"
    assert s_est.size_gb == 200 and s_upd.transaction_id == 77 and storage.requests[1].method == "PATCH"
