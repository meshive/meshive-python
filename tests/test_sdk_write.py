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
