"""Serving writes (output bucket, idle-capacity sharing, input modalities), replica logs, the official model catalog,
model GPU availability, task submission options (project, registry login, output bucket, file picks), task
deletion, discarding task outputs, task GPUs and task counts."""
import asyncio
import inspect
import warnings

import pytest

import meshive
from meshive import (
    ConflictError,
    GpuCandidate,
    Logs,
    MeshiveAPIError,
    ModelAvailability,
    NotFoundError,
    ResourceAction,
    Serving,
    ServingModel,
    TaskCounts,
    TaskDeleteSkip,
    TaskGpu,
    TaskOutputsDiscarded,
    TasksDeleted,
    _write,
)
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)
from tests.test_sdk_write import Recorder

OLD_SERVER = (404, {"detail": "Not Found"})
OLD_TASK_ROUTE = (404, {"detail": {"title": "Task Not Found", "message": "The task does not exist."}})

NEW_METHODS = ("get_serving_replica_logs", "list_official_models", "get_model_availability", "list_task_gpus",
               "get_task_counts", "delete_tasks", "discard_task_outputs")
CHANGED_METHODS = ("deploy_serving", "scale_serving", "register_model", "estimate_task", "submit_task")
NEW_NAMES = ("GpuCandidate", "ModelAvailability", "TaskCounts", "TaskDeleteSkip", "TaskGpu", "TaskOutputsDiscarded",
             "TasksDeleted", "SHARING_CONSENT")

ACTION = {"resource": "serving", "id": "12", "action": "scale", "accepted": True}
SUBMITTED = {"task": {"taskId": "task_1", "name": "t", "status": "queued"}, "estimate": {"pricePerHourUsd": "0.5"}}
TASK_KW = {"workspace": "ws", "image": "img", "cpu_preset": "micro-2c8g"}


# --- Serving: deploy and scale -------------------------------------------------------------------------

def test_deploy_serving_output_bucket():
    rec = Recorder((201, {**ACTION, "action": "deploy"}))
    sync_client(rec).deploy_serving(7, workspace="ws", price_cap_per_hour=1.5, output_bucket_id=3)
    assert rec.body()["outputBucketId"] == 3
    sync_client(rec).deploy_serving(7, workspace="ws", price_cap_per_hour=1.5)
    assert "outputBucketId" not in rec.body()
    with pytest.raises(ValueError, match="output_bucket_id"):
        sync_client(rec).deploy_serving(7, workspace="ws", price_cap_per_hour=1.5, output_bucket_id=0)


def test_deploy_serving_docs_name_the_first_start_replica_limit():
    # With autoscale off a new serving starts at most 10 replicas (400 'Too Many Replicas'); more come by scaling.
    import meshive
    for cls in (meshive.Meshive, meshive.AsyncMeshive):
        doc = cls.deploy_serving.__doc__
        assert "at most 10 replicas" in doc and "scale_serving" in doc, cls


def test_scale_serving_bucket_managed_and_sharing():
    rec = Recorder((200, ACTION))
    client = sync_client(rec)
    result = client.scale_serving(12, output_bucket_id=4, idempotency_key="scale-1")
    assert isinstance(result, ResourceAction)
    assert rec.last.method == "PATCH" and rec.last.url.path == "/v1/sdk/servings/12/scale"
    assert rec.body() == {"outputBucketId": 4} and rec.last.headers["Idempotency-Key"] == "scale-1"
    client.scale_serving(12, managed_output=True)
    assert rec.body() == {"managedOutput": True}
    client.scale_serving(12, share_idle_capacity=True)
    assert rec.body() == {"shareIdleCapacity": True}
    client.scale_serving(12, share_idle_capacity=False, max_replicas=2)
    assert rec.body() == {"maxReplicas": 2, "shareIdleCapacity": False}


def test_scale_serving_checks_arguments():
    client = sync_client(Recorder((200, ACTION)))
    with pytest.raises(ValueError, match="not both"):
        client.scale_serving(12, output_bucket_id=4, managed_output=True)
    with pytest.raises(ValueError, match="at least one"):
        client.scale_serving(12, managed_output=False)
    with pytest.raises(ValueError, match="share_idle_capacity"):
        client.scale_serving(12, share_idle_capacity="yes")


def test_scale_serving_errors_keep_titles():
    for title in ("Sharing Not Available", "Storage Credential Not Found", "Storage Credential Not Verified",
                  "Serving Not Changeable", "Serving Not Found"):
        with pytest.raises(MeshiveAPIError) as err:
            sync_client(Recorder((400, {"detail": {"title": title, "message": "no"}}))).scale_serving(
                12, share_idle_capacity=True)
        assert err.value.title == title


def test_sharing_consent():
    assert len(meshive.SHARING_CONSENT) == 5 and all(isinstance(t, str) and t for t in meshive.SHARING_CONSENT)
    off = Serving.from_dict({"id": 1, "shareIdleCapacity": False})
    on = Serving.from_dict({"id": 1, "shareIdleCapacity": True, "sharedRolloutState": "ready"})
    assert off.sharing_needs_consent(True) is True
    assert off.sharing_needs_consent(False) is False and off.sharing_needs_consent(None) is False
    assert on.sharing_needs_consent(True) is False
    assert off.scale_raises_cost() is False   # sharing is not a cost raise


# --- Serving: register a model --------------------------------------------------------------------------

def test_register_model_input_modalities():
    rec = Recorder((201, {"resource": "model", "id": "5", "action": "register"}))
    client = sync_client(rec)
    client.register_model("org/vl", workspace="ws", input_modalities=["text", "image"])
    assert rec.body() == {"huggingfaceRepo": "org/vl", "inputModalities": ["text", "image"]}
    client.register_model("org/llm", workspace="ws")
    assert rec.body() == {"huggingfaceRepo": "org/llm"}   # no framework, no modalities: the server derives them
    for bad in ([], {"text": 1}, 5):
        with pytest.raises(ValueError, match="input_modalities"):
            client.register_model("org/x", workspace="ws", input_modalities=bad)


def test_register_model_modality_required():
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((422, {"detail": {"title": "Modality Required", "message": "say"}}))).register_model(
            "org/odd", workspace="ws")
    assert err.value.title == "Modality Required"


# --- Serving: replica logs ------------------------------------------------------------------------------

def test_serving_replica_logs():
    payload = {"podName": "", "workspace": "ws", "source": "live", "servingId": 12, "replicaId": 88, "count": 2,
               "lines": [{"line": "loading", "ts": "2026-11-21T00:00:00Z"}, {"line": "ready", "ts": None}],
               "previous": {"source": "previous_replica", "reason": "Error", "lines": [{"line": "boom"}]}}
    rec = Recorder((200, payload))
    logs = sync_client(rec).get_serving_replica_logs(12, 88, tail=50)
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/servings/12/replicas/88/logs"
    assert dict(rec.last.url.params) == {"tail": "50"}
    assert isinstance(logs, Logs) and logs.source == "live" and logs.text == "loading\nready"
    assert logs.previous is not None and logs.previous.text == "boom"
    with pytest.raises(ValueError, match="tail"):
        sync_client(rec).get_serving_replica_logs(12, 88, tail=0)
    with pytest.raises(ValueError, match="replica_id"):
        sync_client(rec).get_serving_replica_logs(12, "x/y")


def test_serving_replica_logs_errors():
    with pytest.raises(NotFoundError, match="does not support these replica logs"):
        sync_client(Recorder(OLD_SERVER)).get_serving_replica_logs(12, 88)
    with pytest.raises(NotFoundError) as err:
        sync_client(Recorder((404, {"detail": {"title": "Replica Not Found", "message": "no"}}))
                    ).get_serving_replica_logs(12, 88)
    assert err.value.title == "Replica Not Found"


# --- Serving: official catalog and availability -------------------------------------------------------

OFFICIAL = {"id": 31, "sourceId": 4, "modelName": "Qwen3-8B", "displayName": "Qwen3 8B", "modelType": "llm",
            "modality": "text", "framework": "vllm", "isOfficial": True, "processingMode": "sync"}

AVAILABILITY = {
    "overall": True, "gpuLabel": "RTX 4090", "vramGb": 24, "pricePerHour": "0.40", "priceLow": "0.40",
    "priceHigh": "1.20", "requiredHostRamGb": None, "requiredMinVramGb": 18, "reason": None,
    "suggestedCapPerHour": "0.60",
    "gpuCandidates": [
        {"gpuLabel": "RTX 4090", "vramGb": 24, "pricePerHour": "0.40", "available": True, "compatible": True},
        {"gpuLabel": "2× RTX 5090", "vramGb": 32, "pricePerHour": "1.20", "available": True},
        {"gpuLabel": "A100", "vramGb": 80, "pricePerHour": "0.90", "available": False},
        {"gpuLabel": "V100", "vramGb": 32, "pricePerHour": "0.20", "available": False, "compatible": False,
         "incompatibleReason": "fp8 not supported"},
        "junk",
    ],
}


def test_list_official_models():
    rec = Recorder((200, [OFFICIAL]))
    models = sync_client(rec).list_official_models()
    assert rec.last.url.path == "/v1/sdk/models/official" and not rec.last.url.params
    assert isinstance(models[0], ServingModel) and models[0].registration_id == 31 and models[0].is_official
    assert models[0].can_share_idle_capacity
    with pytest.raises(NotFoundError, match="official model catalog"):
        sync_client(Recorder((405, {"detail": "Method Not Allowed"}))).list_official_models()


def test_model_availability():
    rec = Recorder((200, AVAILABILITY))
    availability = sync_client(rec).get_model_availability(31, workspace="ws", max_context_tokens=8192)
    assert rec.last.url.path == "/v1/sdk/models/31/availability"
    assert dict(rec.last.url.params) == {"workspace": "ws", "contextLength": "8192"}
    assert isinstance(availability, ModelAvailability)
    assert (availability.status, availability.deployable, availability.cheapest_gpu) == ("available", True, "RTX 4090")
    assert (availability.price_low, availability.price_high, availability.suggested_cap_per_hour) == \
        ("0.40", "1.20", "0.60")
    assert availability.required_vram_gb == 18 and availability.required_host_ram_gb is None
    assert len(availability.gpu_candidates) == 4 and isinstance(availability.gpu_candidates[0], GpuCandidate)
    v100 = availability.gpu_candidates[3]
    assert (v100.compatible, v100.incompatible_reason, v100.available) == (False, "fp8 not supported", False)
    assert [c.gpu_label for c in availability.within_cap("1.0")] == ["RTX 4090"]
    assert [c.gpu_label for c in availability.within_cap(2)] == ["RTX 4090", "2× RTX 5090"]
    assert availability.within_cap("0.1") == []
    with pytest.raises(ValueError, match="price_cap_per_hour"):
        availability.within_cap("cheap")
    sync_client(rec).get_model_availability(31, workspace="ws")
    assert dict(rec.last.url.params) == {"workspace": "ws"}


def test_model_availability_status_and_errors():
    assert ModelAvailability.from_dict({"overall": False, "reason": "error"}).status == "error"
    bare = ModelAvailability.from_dict({"overall": False})
    assert bare.status == "unavailable" and bare.gpu_candidates == [] and bare.price_low is None
    with pytest.raises(NotFoundError, match="model availability"):
        sync_client(Recorder(OLD_SERVER)).get_model_availability(31, workspace="ws")
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((403, {"detail": {"title": "Forbidden", "message": "role"}}))).get_model_availability(
            31, workspace="ws")
    assert err.value.status_code == 403
    with pytest.raises(ValueError, match="max_context_tokens"):
        sync_client(Recorder((200, AVAILABILITY))).get_model_availability(31, workspace="ws", max_context_tokens=0)


# --- Tasks: submit options -------------------------------------------------------------------------------

def test_task_body_output_bucket_values():
    def body(**kw):
        return _write.task_body("t", "print(1)", image="img", template_id=None, requirements=None, env=None,
                                secret_keys=None, args=None, gpu_model=None, gpu_count=None, gpu_vram_gb=None,
                                cpu_preset="micro-2c8g", max_duration=3600, webhook_url=None, input_assets=None,
                                max_price_per_hour=None, **kw)
    assert "outputBucketId" not in body()                         # the workspace's default
    assert "outputBucketId" in body(output_bucket="managed") and body(output_bucket="managed")["outputBucketId"] is None
    assert body(output_bucket=9)["outputBucketId"] == 9
    for bad in ("bucket", 0, True, 1.5):
        with pytest.raises(ValueError, match="output_bucket"):
            body(output_bucket=bad)


def test_submit_task_with_project_and_registry():
    rec = Recorder((202, SUBMITTED))
    sync_client(rec).submit_task("t", project={"asset": "asset_code", "entrypoint": "train.py",
                                               "requirements_path": "requirements.txt"},
                                 registry_login_id=6, output_bucket="managed", **TASK_KW)
    body = rec.body()
    assert "script" not in body
    assert body["project"] == {"asset": "asset_code", "entrypoint": "train.py", "requirementsPath": "requirements.txt"}
    assert body["registryLoginId"] == 6 and body["outputBucketId"] is None
    sync_client(rec).submit_task("t", project={"asset": "asset_code", "entrypoint": "main.py"}, **TASK_KW)
    assert rec.body()["project"] == {"asset": "asset_code", "entrypoint": "main.py"}
    assert "registryLoginId" not in rec.body()


def test_submit_task_script_or_project():
    client = sync_client(Recorder((202, SUBMITTED)))
    with pytest.raises(ValueError, match="exactly one of script"):
        client.submit_task("t", **TASK_KW)
    with pytest.raises(ValueError, match="exactly one of script"):
        client.submit_task("t", "print(1)", project={"asset": "a", "entrypoint": "x.py"}, **TASK_KW)
    with pytest.raises(ValueError, match="unknown key"):
        client.submit_task("t", project={"asset": "a", "entrypoint": "x.py", "entry": "y"}, **TASK_KW)
    with pytest.raises(ValueError, match="project.entrypoint"):
        client.submit_task("t", project={"asset": "a"}, **TASK_KW)
    with pytest.raises(ValueError, match="project must be"):
        client.submit_task("t", project="asset_a", **TASK_KW)
    with pytest.raises(ValueError, match="registry_login_id"):
        client.submit_task("t", "print(1)", workspace="ws", template_id=5, cpu_preset="micro-2c8g",
                           registry_login_id=6)


def test_task_input_asset_file_picks():
    rec = Recorder((200, {"pricePerHourUsd": "0.1", "maxDurationS": 3600}))
    sync_client(rec).estimate_task("t", "print(1)", input_assets=[
        "asset_a", {"asset": "asset_b", "target_dir": "/data", "paths": "models/x.safetensors", "strip_prefix": ""},
        {"asset_id": "asset_c", "paths": ["a/", "b.txt"]}], **TASK_KW)
    assert rec.last.url.path == "/v1/sdk/tasks/estimate"
    assert rec.body()["inputAssets"] == [
        {"asset": "asset_a"},
        {"asset": "asset_b", "targetDir": "/data", "includePaths": ["models/x.safetensors"], "stripPrefix": ""},
        {"asset": "asset_c", "includePaths": ["a/", "b.txt"]}]
    with pytest.raises(ValueError, match="unknown key"):
        sync_client(rec).estimate_task("t", "print(1)", input_assets=[{"asset": "a", "role": "model"}], **TASK_KW)
    with pytest.raises(ValueError, match="strip_prefix"):
        sync_client(rec).estimate_task("t", "print(1)", input_assets=[{"asset": "a", "strip_prefix": 1}], **TASK_KW)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sync_client(rec).estimate_task("t", "print(1)", input_assets=[{"asset": "a", "version": 2}], **TASK_KW)
    assert rec.body()["inputAssets"] == [{"asset": "a"}]
    assert any(issubclass(w.category, DeprecationWarning) and w.filename == __file__ for w in caught)


def test_script_still_positional_and_unchanged():
    rec = Recorder((202, SUBMITTED))
    sync_client(rec).submit_task("t", "print(1)", **TASK_KW)
    body = rec.body()
    assert body["script"] == "print(1)"
    assert not {"project", "registryLoginId", "outputBucketId"} & set(body)


# --- Tasks: delete, discard outputs ------------------------------------------------------------------------

def test_delete_tasks():
    answer = {"deleted": ["task_1", "task_2"],
              "skipped": [{"taskId": "task_3", "reason": "active", "message": "Stop it first."},
                          {"externalId": "task_4", "reason": "uploading", "message": "Wait."}]}
    rec = Recorder((200, answer))
    result = sync_client(rec).delete_tasks("ws", ["task_1", "task_2", "task_1", "task_3", "task_4"],
                                           idempotency_key="del-1")
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/tasks/delete"
    assert dict(rec.last.url.params) == {"workspace": "ws"} and rec.last.headers["Idempotency-Key"] == "del-1"
    assert rec.body() == {"taskIds": ["task_1", "task_2", "task_3", "task_4"]}
    assert isinstance(result, TasksDeleted) and result.deleted == ["task_1", "task_2"] and not result.replayed
    assert result.skipped == [TaskDeleteSkip("task_3", "active", "Stop it first."),
                              TaskDeleteSkip("task_4", "uploading", "Wait.")]
    sync_client(rec).delete_tasks("ws", "task_9")
    assert rec.body() == {"taskIds": ["task_9"]}


def test_delete_tasks_checks_and_errors():
    client = sync_client(Recorder((200, {"deleted": [], "skipped": []})))
    for bad in ([], None, [""], {"task_1": 1}):
        with pytest.raises(ValueError):
            client.delete_tasks("ws", bad)
    with pytest.raises(ValueError, match="at most 100"):
        client.delete_tasks("ws", [f"task_{i}" for i in range(101)])
    client.delete_tasks("ws", [f"task_{i}" for i in range(100)])
    with pytest.raises(NotFoundError, match="does not support deleting tasks"):
        sync_client(Recorder((405, {"detail": "Method Not Allowed"}))).delete_tasks("ws", ["task_1"])
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((403, {"detail": {"title": "Forbidden", "message": "role"}}))).delete_tasks(
            "ws", ["task_1"])
    assert err.value.status_code == 403


def test_discard_task_outputs():
    answer = {"externalId": "task_1", "appliedBy": "watchdog", "outputUploadState": "uploading",
              "deadlineAt": "2026-11-21T00:00:30Z", "message": "Discarding."}
    rec = Recorder((200, answer))
    result = sync_client(rec).discard_task_outputs("task_1", idempotency_key="disc-1")
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/tasks/task_1/outputs/discard"
    assert not rec.last.content and rec.last.headers["Idempotency-Key"] == "disc-1"
    assert isinstance(result, TaskOutputsDiscarded)
    assert (result.task_id, result.output_upload_state, result.message) == ("task_1", "uploading", "Discarding.")
    assert result.deadline_at is not None and result.deadline_at.second == 30


def test_discard_task_outputs_errors():
    with pytest.raises(ConflictError) as err:
        sync_client(Recorder((409, {"detail": {"title": "Upload Not Active", "message": "done",
                                               "code": "upload_not_active"}}))).discard_task_outputs("task_1")
    assert err.value.raw["detail"]["code"] == "upload_not_active"
    with pytest.raises(NotFoundError) as missing:
        sync_client(Recorder(OLD_TASK_ROUTE)).discard_task_outputs("task_1")
    assert missing.value.title == "Task Not Found"          # a missing task, not an old server
    with pytest.raises(NotFoundError, match="discarding task outputs"):
        sync_client(Recorder(OLD_SERVER)).discard_task_outputs("task_1")


# --- Tasks: GPUs and counts ----------------------------------------------------------------------------

def test_list_task_gpus():
    rec = Recorder((200, [{"gpuModel": "RTX 4090", "vram": 24, "gpuPrice": "0.35", "available": True,
                           "maxGpuCount": 4, "cpuCores": 8, "ramGb": 32}, "junk"]))
    gpus = sync_client(rec).list_task_gpus()
    assert rec.last.url.path == "/v1/sdk/tasks/gpu-availability" and not rec.last.url.params
    assert gpus == [TaskGpu("RTX 4090", 24, "0.35", 4, 8, 32, raw=gpus[0].raw)]
    assert TaskGpu.from_dict({"gpuModel": "A"}).price_per_gpu_hour is None


def test_task_counts():
    rec = Recorder((200, {"byStatus": {"running": 2, "succeeded": 5}, "total": 7,
                          "latestUpdatedAt": "2026-11-21T01:02:03Z"}))
    counts = sync_client(rec).get_task_counts("ws")
    assert rec.last.url.path == "/v1/sdk/tasks/counts" and dict(rec.last.url.params) == {"workspace": "ws"}
    assert isinstance(counts, TaskCounts) and counts.by_status == {"running": 2, "succeeded": 5} and counts.total == 7
    assert counts.latest_updated_at is not None
    assert TaskCounts.from_dict({"byStatus": {"failed": 1}}).total == 1
    assert TaskCounts.from_dict({}).by_status == {}


def test_task_literal_routes_on_an_older_server():
    """An older server reads "counts" / "gpu-availability" as a task id and answers 'Task Not Found'."""
    with pytest.raises(NotFoundError, match="does not support task counts"):
        sync_client(Recorder(OLD_TASK_ROUTE)).get_task_counts("ws")
    with pytest.raises(NotFoundError, match="does not support task GPU availability"):
        sync_client(Recorder(OLD_TASK_ROUTE)).list_task_gpus()
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((403, {"detail": {"title": "Forbidden", "message": "member"}}))).get_task_counts("ws")
    assert err.value.status_code == 403


# --- Both clients ----------------------------------------------------------------------------------------

def test_async_mirrors_sync_and_exports():
    for name in NEW_METHODS + CHANGED_METHODS:
        assert inspect.signature(getattr(meshive.Meshive, name)) == \
            inspect.signature(getattr(meshive.AsyncMeshive, name)), name
        assert inspect.iscoroutinefunction(getattr(meshive.AsyncMeshive, name)), name
    for name in NEW_NAMES:
        assert name in meshive.__all__ and hasattr(meshive, name), name
    for name in ("project", "registry_login_id", "output_bucket"):
        assert name in inspect.signature(meshive.Meshive.submit_task).parameters
        assert name in inspect.signature(meshive.Meshive.estimate_task).parameters
    assert inspect.signature(meshive.Meshive.submit_task).parameters["script"].default is None


def test_async_calls():
    rec = Recorder((200, {"podName": "", "workspace": "ws", "source": "none", "lines": []}), (200, [OFFICIAL]),
                   (200, AVAILABILITY), (200, [{"gpuModel": "A", "gpuPrice": "1"}]), (200, {"byStatus": {}, "total": 0}),
                   (200, {"deleted": ["task_1"], "skipped": []}),
                   (200, {"externalId": "task_1", "message": "ok"}), (200, ACTION), (202, SUBMITTED))

    async def run():
        async with async_client(rec) as client:
            logs = await client.get_serving_replica_logs(12, 88)
            official = await client.list_official_models()
            availability = await client.get_model_availability(31, workspace="ws")
            gpus = await client.list_task_gpus()
            counts = await client.get_task_counts("ws")
            deleted = await client.delete_tasks("ws", ["task_1"])
            discarded = await client.discard_task_outputs("task_1")
            await client.scale_serving(12, managed_output=True)
            scale_body = rec.body()
            await client.submit_task("t", project={"asset": "a", "entrypoint": "x.py"}, output_bucket=3, **TASK_KW)
            return logs, official, availability, gpus, counts, deleted, discarded, scale_body, rec.body()
    logs, official, availability, gpus, counts, deleted, discarded, scale_body, submit_body = asyncio.run(run())
    assert logs.source == "none" and official[0].registration_id == 31 and availability.status == "available"
    assert gpus[0].price_per_gpu_hour == "1" and counts.total == 0 and deleted.deleted == ["task_1"]
    assert discarded.task_id == "task_1" and scale_body == {"managedOutput": True}
    assert submit_body["project"] == {"asset": "a", "entrypoint": "x.py"} and submit_body["outputBucketId"] == 3
