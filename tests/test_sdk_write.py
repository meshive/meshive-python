"""0.1.0 쓰기 표면 — 요청 조립(경로/쿼리/본문), Idempotency-Key, 재시도, 새 예외, headers=, 로그."""
import asyncio
import json

import httpx
import pytest

from meshive import (
    NotFoundError,
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
                                        gpu_vram_gb=12, rental_type="Spot", vcpu=8, ram_gb=24,
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
        "rentalType": "spot", "vcpu": 8, "ramGb": 24,
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
    dict(gpu_count=0), dict(gpu_count=9), dict(rental_type="hourly"),
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
    created = c.create_storage("vol", 1, workspace="ws", storage_type="NFS", disk_type="ssd", encrypted=True)
    assert isinstance(created, StorageCreated) and created.estimate.max_size_gb == 305 and created.transaction_id == 5
    assert rec.body() == {"name": "vol", "sizeGb": 1, "storageType": "nfs", "diskType": "SSD", "encrypted": True}
    c.create_storage("vol", 1, workspace="ws", storage_type="hostpath")
    assert rec.body() == {"name": "vol", "sizeGb": 1, "storageType": "hostPath", "diskType": "NVMe", "encrypted": False}
    with pytest.raises(ValueError, match="nfs"):          # 암호화는 네트워크 스토리지만 (서버도 hostPath+encrypted 를 422 로 거절)
        c.estimate_storage("vol", 1, workspace="ws", storage_type="hostPath", encrypted=True)
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
    with pytest.warns(DeprecationWarning, match="no longer have versions"):
        submitted = c.submit_task("train", "print('x', flush=True)", workspace="ws", image="python:3.12-slim",
                                  gpu_model="RTX 3060", env={"HF_TOKEN": "x"}, secret_keys=["HF_TOKEN"], args=["--epochs", "3"],
                                  input_assets=["asset_a", {"asset": "asset_b", "version": 2, "target_dir": "/inputs/b"}],
                                  max_duration=7200)
    assert isinstance(submitted, TaskSubmitted) and submitted.task.task_id == "task_1" and submitted.estimate.max_cost == "0.068"
    body = rec.body()
    assert body["gpuModel"] == "RTX 3060" and "cpuPreset" not in body and body["maxDurationS"] == 7200
    assert body["inputAssets"] == [{"asset": "asset_a"}, {"asset": "asset_b", "targetDir": "/inputs/b"}]   # version 은 안 보낸다
    assert body["args"] == ["--epochs", "3"] and body["secretKeys"] == ["HF_TOKEN"]
    c.stop_task("task_1")
    assert rec.last.url.path == "/v1/sdk/tasks/task_1/stop"
    for bad in (dict(), dict(gpu_model="x", cpu_preset="micro-2c8g"), dict(cpu_preset="micro-2c8g", max_duration=60),
                dict(cpu_preset="micro-2c8g", script=" ")):
        kwargs = {"image": "img", **bad}
        script = kwargs.pop("script", "print(1)")
        with pytest.raises(ValueError):
            c.estimate_task("t", script, workspace="ws", **kwargs)
    with pytest.raises(ValueError, match="256 KiB"):   # 한도는 256 × 1024 바이트 (서버 메시지와 같은 KiB)
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


# --- 서빙 비용 증가 판정 (CLI/MCP 확인 기준) ----------------------------------------------

def test_serving_scale_raises_cost_covers_range_autoscale_and_cap():
    """범위 확대·autoscale 켜기·상한 인상만 "비용이 늘 수 있다" — 줄이거나 무제한 상한에 값을 주는 건 아니다."""
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
    assert StorageEstimate.from_dict({"sizeGb": 1, "maxSizeGb": 1}).disk_type == "NVMe"   # 구 서버 응답


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
    with pytest.raises(NotFoundError, match="does not support asset import"):
        sync_client(Recorder((404, {"detail": "Not Found"}))).import_asset("Qwen/Qwen3-0.6B", workspace="ws")


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
    assert not {"inputAssets", "watchedFolders", "harvestDestination"} & rec.body().keys()   # 안 쓰면 안 보낸다

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
