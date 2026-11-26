"""Typed read fields: task detail, servings, workspace maintenance and host messages, machines, API keys,
storages, templates and serving models."""
import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import httpx

import meshive
from meshive import (
    ApiKey,
    Machine,
    MachineDetail,
    MachineMetrics,
    Serving,
    ServingModel,
    Storage,
    Task,
    Template,
    WorkspaceDetail,
)
from tests.test_sdk import (  # noqa: F401  (autouse fixtures)
    MACHINE,
    SERVING,
    TASK,
    WORKSPACE_DETAIL,
    _capture,
    async_client,
    isolate_config_dir,
    slept,
    sync_client,
)


# --- tasks --------------------------------------------------------------------

TASK_DETAIL = {
    **TASK,
    "templateId": 12, "cpuPreset": None, "faultClass": "user", "failureStage": "fetch", "liveMetrics": True,
    "refundedAmount": "0.25", "webhookUrl": "https://hooks.example/x",
    "fetchStartedAt": "2026-08-30T00:00:30Z", "fetchCompletedAt": None,
    "fetchProgress": {"downloadedBytes": 10, "totalBytes": 100, "bytesPerSecond": 1.5, "perAsset": []},
    "scriptContent": "print(1)", "requirementsContent": "numpy", "commandArgs": ["--epochs", "3"],
    "env": {"MODE": "fast", "HF_TOKEN": "***"}, "secretEnvKeys": ["HF_TOKEN"],
    "clusterName": "never-typed", "nodeName": "never-typed",
    "retentionDays": 30, "storageCredentialId": 7,
    "inputAssets": [{"assetExternalId": "asset_a", "name": "data", "targetDir": "/inputs/data", "versionNumber": 1,
                     "referenceMode": "external", "ingestSource": "hf_import", "includePaths": ["*.json"],
                     "stripPrefix": "data/", "isProject": False},
                    {"assetExternalId": "asset_p", "name": "proj", "targetDir": "/inputs/proj", "isProject": True}],
    "project": {"assetExternalId": "asset_p", "name": "proj", "targetDir": "/inputs/proj", "entrypoint": "train.py",
                "requirementsPath": "requirements.txt", "module": None},
    "usesEphemeralLv": True, "computeRatePerHour": "1.0", "storageRatePerHour": "0.02",
    "totalRatePerHour": "1.02", "taskStorageCapacityGb": 40,
}


def test_task_detail_fills_every_typed_field():
    seen = {}
    t = sync_client(_capture(TASK_DETAIL, seen)).get_task("task_abc")
    assert seen["path"] == "/v1/sdk/tasks/task_abc"
    assert (t.template_id, t.gpu_vram_gb, t.cpu_preset, t.max_duration_seconds) == (12, 24, None, 3600)
    assert t.webhook_url == "https://hooks.example/x" and t.refunded_amount == "0.25"
    assert (t.fault_class, t.failure_stage, t.live_metrics) == ("user", "fetch", True)
    assert t.fetch_started_at.second == 30 and t.fetch_completed_at is None
    assert t.fetch_progress["totalBytes"] == 100
    assert t.script == "print(1)" and t.requirements == "numpy" and t.args == ["--epochs", "3"]
    assert t.env == {"MODE": "fast", "HF_TOKEN": "***"} and t.secret_env_keys == ["HF_TOKEN"]
    assert t.output_bucket_id == 7 and t.log_retention_days == 30
    assert (t.compute_rate_per_hour, t.storage_rate_per_hour, t.total_rate_per_hour) == ("1.0", "0.02", "1.02")
    assert t.task_storage_gb == 40 and t.disk_billed_separately is True
    assert t.project.asset_id == "asset_p" and t.project.entrypoint == "train.py"
    assert t.project.requirements_path == "requirements.txt" and t.project.module is None
    data, proj = t.input_assets
    assert data.paths == ["*.json"] and data.strip_prefix == "data/" and data.is_project is False
    assert data.reference_mode == "external"
    assert proj.is_project is True and proj.paths is None and proj.strip_prefix is None
    # Where the task ran is never typed.
    names = {f.name for f in dataclasses.fields(Task)}
    assert not names & {"cluster_name", "node_name", "logs_mode"}


def test_task_list_item_leaves_detail_only_fields_none():
    t = Task.from_dict(TASK)
    assert t.max_duration_seconds == 3600 and t.gpu_vram_gb == 24 and t.refunded_amount == "0"
    for name in ("script", "requirements", "args", "env", "secret_env_keys", "output_bucket_id", "project",
                 "compute_rate_per_hour", "storage_rate_per_hour", "total_rate_per_hour", "task_storage_gb",
                 "disk_billed_separately", "log_retention_days"):
        assert getattr(t, name) is None, name


def test_task_from_an_older_server_keeps_defaults():
    t = Task.from_dict({"externalId": "task_x"})
    assert t.template_id is None and t.refunded_amount == "0" and t.live_metrics is False
    assert t.fault_class is None and t.fetch_progress is None and t.input_assets == []


def test_task_detail_null_storage_credential_means_meshive_storage():
    t = Task.from_dict({**TASK_DETAIL, "storageCredentialId": None, "requirementsContent": None})
    assert t.output_bucket_id is None and t.requirements is None and t.script == "print(1)"


# --- servings -------------------------------------------------------------------

def _step(step, status, at, **extra):
    return {"step": step, "status": status, "updatedAt": at, **extra}


SERVING_FULL = {
    **SERVING,
    "modelRetiring": True, "isOfficial": True, "endpointType": "chat", "modality": "text",
    "inputModalities": ["text", "image"], "defaultStorageCredentialId": 9, "defaultStorageLabel": "my bucket",
    "shareIdleCapacity": True, "sharedRolloutState": "ready", "sharedEarnedThisMonth": 1.5,
    "scaleUpBlocked": True, "scaleUpBlockReason": "price_cap", "scaleUpDesired": 3,
    "capMigration": {"replaced": 1, "total": 2}, "priceCapPerHour": "2.0",
    "bucket": "llm", "throughput": 120.5, "latencyP95Ms": 300, "concurrent": 4, "errorRate": 0.01,
    "queueDepth": 0, "inFlight": None, "gpuUtil": 0.7,
    "status": "error",
    "failureDiagnostic": {
        "oomKind": "vram", "oomReason": "CUDA out of memory", "failureReason": None,
        "containerStatuses": [{"name": "engine", "state": "waiting", "reason": "CrashLoopBackOff",
                               "restartCount": 3, "lastTerminatedReason": "Error", "lastTerminatedExitCode": 1}],
        "events": [{"type": "Warning", "reason": "BackOff", "message": "restarting", "count": 5,
                    "lastTimestamp": "2026-08-30T00:05:00Z"}],
        "initLogs": [{"container": "source-fetch", "lines": ["a", "b"], "previous": True}]},
    "failureReplica": {"replicaIndex": 1, "podName": "srv-1-0"},
    "stoppedReplicas": [{"replicaIndex": 0, "podName": "srv-0-0", "stoppedAt": "2026-08-30T01:00:00Z"}],
    "replicas": [
        {"replicaIndex": 0, "status": "running", "statefulsetName": "srv-0", "pricePerHour": "1.25",
         "billingActive": True, "gpuModel": "NVIDIA H100", "gpuNumber": 1, "gpuVram": 80, "stage": "warming_up",
         "downloadPercent": 40, "downloadUsedBytes": 400, "downloadExpectedBytes": 1000,
         "concurrency": 2, "reachable": True, "gpuUtil": 0.5, "kvCacheUsage": 0.1, "queue": 0.0,
         "tokensPerS": 55.0, "ttftMs": 120.0,
         "transaction": {"id": 3, "action": "create", "status": "in_progress", "steps": [
             _step("image_pull", "done", "2026-08-30T00:02:00Z"),
             _step("waiting_for_ready", "in_progress", "2026-08-30T00:03:00Z")]}},
        {"replicaIndex": 1, "status": "failed", "statefulsetName": "srv-1", "pricePerHour": 1.25,
         "transaction": {"id": 4, "action": "create", "status": "failed", "steps": [
             _step("image_pull", "done", "2026-08-30T00:02:00Z"),
             _step("waiting_for_ready", "failed", "2026-08-30T00:04:00Z", detail="engine exited",
                   failureDiagnostic={"failureReason": "exit code 1", "containerStatuses": [], "events": []})]}},
        {"replicaIndex": 2, "status": "provisioning", "statefulsetName": None, "logReplicaId": 77,
         "setupStep": "bootstrapping", "pricePerHour": None},
    ],
}


def test_serving_parses_replicas_failure_metrics_and_sharing():
    s = sync_client(_capture(SERVING_FULL, {})).get_serving(5)
    assert s.registration_id == 1 and s.model_retiring is True and s.is_official is True
    assert (s.endpoint_type, s.modality, s.input_modalities) == ("chat", "text", ["text", "image"])
    assert (s.output_bucket_id, s.output_bucket_label) == (9, "my bucket")
    assert s.share_idle_capacity is True and s.sharing_state == "ready" and s.shared_earned_this_month == "1.5"
    assert (s.scale_up_blocked, s.scale_up_block_reason, s.scale_up_desired) == (True, "price_cap", 3)
    assert (s.cap_migration.replaced, s.cap_migration.total) == (1, 2)
    assert s.metrics.bucket == "llm" and s.metrics.throughput == 120.5 and s.metrics.in_flight is None
    assert s.metrics.latency_p95_ms == 300.0 and s.metrics.queue_depth == 0

    assert s.failure.oom_kind == "vram" and s.failure.reason == "CUDA out of memory"
    assert s.failure.failed_step is None
    engine = s.failure.containers[0]
    assert (engine.name, engine.reason, engine.restart_count, engine.last_terminated_exit_code) == (
        "engine", "CrashLoopBackOff", 3, 1)
    assert s.failure.events[0].count == 5 and s.failure.events[0].last_timestamp.minute == 5
    assert s.failure.init_logs[0].lines == ["a", "b"] and s.failure.init_logs[0].previous is True
    assert (s.failed_replica.replica_index, s.failed_replica.pod_name) == (1, "srv-1-0")
    assert s.stopped_replicas[0].pod_name == "srv-0-0" and s.stopped_replicas[0].stopped_at.hour == 1
    assert s.has_failure is True

    running, failed, external = s.replicas
    assert running.pod_name == "srv-0-0" and running.log_replica_id is None
    assert (running.gpu_model, running.gpu_count, running.vram_gb) == ("NVIDIA H100", 1, 80)
    assert running.price_per_hour == "1.25" and running.billing_active is True
    assert (running.download_percent, running.download_used_bytes, running.download_expected_bytes) == (40, 400, 1000)
    assert (running.step, running.step_status) == ("waiting_for_ready", "in_progress")
    assert running.stage == "warming_up" and running.current_stage == "warming_up"
    assert running.metrics.tokens_per_s == 55.0 and running.metrics.reachable is True
    assert running.metrics.queue == 0.0 and running.failure is None

    assert failed.price_per_hour == "1.25" and failed.metrics is None
    assert failed.failure.failed_step == "waiting_for_ready" and failed.failure.failed_step_detail == "engine exited"
    assert failed.failure.reason == "exit code 1" and failed.current_stage == "waiting_for_ready"

    assert external.pod_name is None and external.log_replica_id == 77 and external.price_per_hour is None
    assert external.setup_step == "bootstrapping" and external.current_stage == "bootstrapping"
    assert external.step is None and external.failure is None

    assert s.replica(2) is external and s.replica(9) is None


def _walk_fields(obj):
    """What a generic serializer built on dataclasses.fields() (such as an already released MCP server) sends:
    every field except `raw`, recursively."""
    if dataclasses.is_dataclass(obj):
        return {f.name: _walk_fields(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.name != "raw"}
    if isinstance(obj, list):
        return [_walk_fields(x) for x in obj]
    return obj if isinstance(obj, (str, int, float, bool, type(None), dict)) else str(obj)


def test_secret_and_program_output_stay_out_of_fields_and_repr():
    # Task script/requirements/env can hold secrets and a serving's failure the program's own output: they are
    # properties over raw, so repr() and field-walking serializers never carry them, while the attributes work.
    t = Task.from_dict(TASK_DETAIL)
    assert t.script == "print(1)" and t.env["MODE"] == "fast"
    names = {f.name for f in dataclasses.fields(Task)}
    assert not names & {"script", "requirements", "env"}
    walked = str(_walk_fields(t))
    for leaked in ("print(1)", "numpy", "fast"):
        assert leaked not in walked and leaked not in repr(t)

    s = Serving.from_dict(SERVING_FULL)
    assert s.failure is not None and s.replicas[1].failure is not None and s.has_failure is True
    assert "failure" not in {f.name for f in dataclasses.fields(Serving)}
    assert "failure" not in {f.name for f in dataclasses.fields(type(s.replicas[0]))}
    walked = str(_walk_fields(s))
    for leaked in ("CUDA out of memory", "exit code 1", "engine exited", "CrashLoopBackOff"):
        assert leaked not in walked and leaked not in repr(s)


def test_serving_from_an_older_server_keeps_defaults():
    s = Serving.from_dict({"id": 5, "replicas": [{"replicaIndex": 0, "status": "running"}]})
    assert s.registration_id is None and s.shared_earned_this_month == "0" and s.sharing_state is None
    assert s.failure is None and s.failed_replica is None and s.stopped_replicas == []
    assert s.metrics is None and s.cap_migration is None and s.has_failure is False
    r = s.replicas[0]
    assert r.pod_name is None and r.step is None and r.metrics is None and r.current_stage is None


def test_list_servings_types_the_replicas():
    s = sync_client(_capture([SERVING], {})).list_servings("team-ns")[0]
    assert s.replicas[0].replica_index == 0 and s.replicas[0].status == "running"
    assert s.registration_id == 1


# --- workspaces ------------------------------------------------------------------

def _window(i, pod, start, end, *, is_start=False, **extra):
    return {"id": i, "podName": pod, "userAlias": pod.upper(), "reason": "disk swap", "description": "new SSD",
            "startTime": start.isoformat(), "endTime": end.isoformat(), "isStart": is_start, **extra}


def test_workspace_detail_types_maintenance_and_host_messages():
    now = datetime.now(timezone.utc)
    hour = timedelta(hours=1)
    payload = {
        **WORKSPACE_DETAIL,
        "maintenanceSchedule": [
            _window(1, "pod-a", now + hour, now + 2 * hour, machineId="mac-1", isEmergency=False),
            _window(1, "pv-b", now + hour, now + 2 * hour, machineId="mac-1", isEmergency=False),
            _window(2, "pod-c", now - hour, now + hour, is_start=True, machineId="mac-2", isEmergency=True),
            _window(3, "pod-d", now - hour, now + hour),
            _window(4, "pod-e", now - 2 * hour, now - hour),
        ],
        "messageFromHost": [
            {"id": 10, "title": "heads up", "content": "rebooting", "sender": "mac-1", "receiver": "team-ns",
             "senderType": "host", "isRead": False, "createdAt": "2026-08-30T00:00:00Z",
             "relatedPods": [{"resourceName": "pod-a", "resourceType": "statefulset", "userAlias": "A"}],
             "relatedPvs": []},
            {"id": 11, "title": "ok", "content": "thanks", "sender": "team-ns", "receiver": "mac-1",
             "senderType": "client", "isRead": True, "createdAt": "2026-08-30T00:01:00Z"},
        ],
    }
    ws = sync_client(_capture(payload, {})).get_workspace("team-ns")
    statuses = [(m.id, m.resource_name, m.status) for m in ws.maintenance]
    assert statuses == [(1, "pod-a", "upcoming"), (1, "pv-b", "upcoming"), (2, "pod-c", "in_progress"),
                        (3, "pod-d", "overdue"), (4, "pod-e", "expired")]
    first = ws.maintenance[0]
    assert first.user_alias == "POD-A" and first.reason == "disk swap" and first.description == "new SSD"
    assert first.machine_id == "mac-1" and first.is_emergency is False and first.start_time.tzinfo is not None
    assert ws.maintenance[2].is_emergency is True and ws.maintenance[2].is_start is True
    assert ws.maintenance[3].machine_id is None and ws.maintenance[3].is_emergency is None

    incoming, outgoing = ws.host_messages
    assert (incoming.machine_id, incoming.direction, incoming.is_read) == ("mac-1", "in", False)
    assert incoming.title == "heads up" and incoming.related_pods[0]["resourceName"] == "pod-a"
    assert (outgoing.machine_id, outgoing.direction) == ("mac-1", "out") and outgoing.related_pods == []


def test_workspace_detail_from_an_older_server_has_empty_lists():
    ws = WorkspaceDetail.from_dict({"namespaceName": "ns"})
    assert ws.maintenance == [] and ws.host_messages == []


# --- machines --------------------------------------------------------------------

def _machine(machine_type, **extra):
    return {
        "id": "mac-1", "name": "node-a", "machineType": machine_type, "ip": "100.1.2.3", "gpuSupported": True,
        "hostTier": "gold", "uptimeRate": "0.99",
        "state": {"name": "INITIALIZING", "stage": "INSTALL_KERNEL", "setupStep": 3, "totalSetupStep": 9,
                  "stageState": "FAILED", "log": "apt failed", "nodeReady": False, "machineReady": True,
                  "systemComponentsReady": False, "updatedAt": "2026-08-30T00:00:00Z", "graceEndsAt": None,
                  "pendingReverifyReason": "SPEC_GPU"},
        "specs": {"cpu": "EPYC", "cpuCores": 32, "cpuThreads": 64, "ram": 262144, "ramType": 5, "ramSpeed": 4800,
                  "motherboard": "MB", "gpu": "NVIDIA H100", "gpuNumber": 2, "nvlinkCapable": True,
                  "disks": [{"name": "nvme0n1", "diskPath": "/dev/nvme0n1", "diskType": "nvme",
                             "connectionType": "pcie", "totalSize": 1907729.0, "isPv": True, "isBoot": False,
                             "sequentialRead": 3000000000, "sequentialWrite": None, "hwmonChip": "nvme-pci-0100",
                             "temperature": 41, "criticalTemperature": 84, "powerOnHours": 1200,
                             "powerCycleCount": 30, "lifeLeft": 98, "testPassed": "PASSED"}],
                  "volumeGroupTotalSize": 1800000, "volumeGroupAvailableSize": 900000,
                  "gpuPcieLanes": [{"pcieSlot": "0000:a1:00.0", "currentPcieLane": 16, "limitPcieLane": 16,
                                    "pcieVersion": 4, "latestPcieVersion": 5, "nvlinkConnected": True,
                                    "nvlinkLinkCount": 18}],
                  "internetDownloadSpeed": 940000000, "internetUploadSpeed": 500000000,
                  "internetLinkSpeed": 1000000000, "internetSpeedMeasuredAt": "2026-08-29T00:00:00Z",
                  "allocatableCpu": 30, "allocatableRam": 250000},
        "earning": {"hourly": 2.5, "daily": 30.0, "weekly": 120.0},
        "price": {"demandPrice": "1.99", "spotPrice": "1.20", "diskPrice": "0.05"},
        "unreadMessages": 1, **extra}


def test_machine_parses_price_specs_setup():
    m = Machine.from_dict(_machine("gpu"))
    assert m.ip == "100.1.2.3" and m.gpu_supported is True
    assert m.earning_daily == 30.0 and m.pending_payout == 120.0
    assert (m.price.unit, m.price.demand_per_hour, m.price.spot_per_hour, m.price.storage_per_gb_month) == (
        "gpu", "1.99", "1.20", "0.05")
    s = m.setup
    assert (s.state, s.stage, s.stage_state, s.setup_step, s.total_setup_steps) == (
        "INITIALIZING", "INSTALL_KERNEL", "FAILED", 3, 9)
    assert s.log == "apt failed" and s.machine_ready is True and s.pending_reverify_reason == "SPEC_GPU"
    assert s.updated_at.year == 2026 and s.grace_ends_at is None
    spec = m.specs
    assert (spec.cpu, spec.cpu_cores, spec.cpu_threads, spec.ram_mib, spec.ram_type) == ("EPYC", 32, 64, 262144, 5)
    assert spec.gpu_count == 2 and spec.nvlink_capable is True
    assert (spec.rentable_disk_mib, spec.rentable_disk_available_mib) == (1800000, 900000)
    assert spec.internet_download_bps == 940000000 and spec.internet_link_mbps == 1000
    assert (spec.allocatable_cpu, spec.allocatable_ram_mib) == (30, 250000)
    disk = spec.disks[0]
    assert (disk.name, disk.path, disk.total_size_mib, disk.is_pv) == ("nvme0n1", "/dev/nvme0n1", 1907729.0, True)
    assert disk.sequential_read == 3000000000 and disk.sequential_write is None and disk.random_read is None
    assert (disk.temperature, disk.critical_temperature, disk.life_left) == (41, 84, 98)
    assert disk.hwmon_chip == "nvme-pci-0100" and disk.test_passed == "PASSED"
    slot = spec.gpu_slots[0]
    assert (slot.current_pcie_lanes, slot.max_pcie_lanes, slot.pcie_version, slot.max_pcie_version) == (16, 16, 4, 5)
    assert slot.nvlink_connected is True and slot.nvlink_link_count == 18

    assert Machine.from_dict(_machine("cpu")).price.unit == "cpu"
    storage = Machine.from_dict(_machine("storage"))
    assert storage.price.unit == "storage" and storage.price.storage_per_gb_month == "0.05"


def test_machine_detail_and_list_carry_the_new_fields():
    seen = {}
    detail = sync_client(_capture({**_machine("gpu"), "clients": {"pods": [], "storages": []},
                                   "schedules": {"upcoming": [], "ongoing": None}, "platformFee": 0.2},
                                  seen)).get_machine("mac-1")
    assert isinstance(detail, MachineDetail) and detail.specs.disks[0].name == "nvme0n1"
    assert detail.price.unit == "gpu" and detail.platform_fee == 0.2
    listed = sync_client(_capture([_machine("gpu")], seen)).list_machines()
    assert listed[0].setup.stage == "INSTALL_KERNEL"


def test_machine_from_an_older_server_has_no_price_specs_setup():
    m = Machine.from_dict(MACHINE)
    assert m.price is None and m.ip == "" and m.gpu_supported is False and m.pending_payout == 0.0
    assert m.earning_daily == 60.0
    assert m.setup.state == "ONLINE" and m.setup.stage is None and m.setup.log == ""
    assert m.specs.gpu == "NVIDIA H100" and m.specs.disks == [] and m.specs.internet_link_mbps is None
    bare = Machine.from_dict({"id": "x"})
    assert bare.setup is None and bare.specs is None and bare.price is None


def test_machine_tolerates_malformed_specs():
    m = Machine.from_dict(_machine("gpu", specs={"disks": "nope", "gpuPcieLanes": [1, None],
                                                 "ram": "lots", "allocatableCpu": "n/a"}, state="weird"))
    assert m.specs.disks == [] and m.specs.gpu_slots == [] and m.specs.ram_mib == 0
    assert m.specs.allocatable_cpu is None and m.setup is None and m.status == ""
    disk = Machine.from_dict(_machine("gpu", specs={"disks": [{"name": "sda", "temperature": "hot",
                                                                "totalSize": "big"}, "junk"]})).specs.disks
    assert len(disk) == 1 and disk[0].temperature is None and disk[0].total_size_mib == 0.0


def test_machine_metrics_parses_disk_temperatures_and_interface():
    m = sync_client(_capture({"machineId": "mac-1", "networkIo": {"ip": "10.0.0.2", "interface": "eth0",
                                                                    "receive": 10, "transmit": 20},
                              "diskTemperatures": {"nvme-pci-0100": 41.5, "bad": None}}, {})).get_machine_metrics(
        "mac-1")
    assert m.disk_temperatures == {"nvme-pci-0100": 41.5}
    assert (m.network_interface, m.network_ip, m.network_receive) == ("eth0", "10.0.0.2", 10.0)
    old = MachineMetrics.from_dict({"machineId": "mac-1"})
    assert old.disk_temperatures == {} and old.network_interface == "" and old.network_ip == ""


# --- account API keys ----------------------------------------------------------------

def test_api_key_is_current_and_expired():
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    keys = sync_client(_capture([
        {"id": 1, "keyName": "ci", "keyPrefix": "meshive_a1", "scopes": ["read"], "status": "active",
         "isCurrent": True},
        {"id": 2, "keyName": "old", "keyPrefix": "meshive_b2", "scopes": ["read"], "status": "active",
         "isCurrent": False, "expiresAt": past},
    ], {})).list_api_keys()
    assert [k.is_current for k in keys] == [True, False]
    assert [k.expired for k in keys] == [False, True]
    assert ApiKey.from_dict({"id": 3}).is_current is None


# --- storages -------------------------------------------------------------------------

STORAGE_FULL = {
    "pvName": "pv-1", "namespaceName": "team-ns", "userAlias": "data", "storageType": "nfs", "status": "active",
    "totalSize": 10240, "availableSize": 5120, "usageRate": 0.5, "pricePerHour": "0.01", "linkedPod": [],
    "isMaintenance": True, "maintenanceStart": "2026-08-30T00:00:00Z", "maintenanceEnd": "2026-08-30T02:00:00Z",
    "usageWarning": True, "usageWarningThreshold": 0.8, "createdAt": "2026-08-01T00:00:00Z",
    "downloaderState": "running",
    "downloaderRestarts": [{"id": 4, "reason": "OOMKilled", "message": None, "occurredAt": "2026-08-29T00:00:00Z"}],
    "machine": {"hostId": 9, "machineId": "mac-9", "nodeName": "node-9", "location": "Seoul",
                "maintenanceSchedule": [{"id": 1, "reason": "power", "description": "UPS swap",
                                         "startTime": "2026-09-01T00:00:00Z", "endTime": "2026-09-01T01:00:00Z",
                                         "isStart": False}]},
}


def test_storage_types_maintenance_warning_and_downloader():
    st = sync_client(_capture(STORAGE_FULL, {})).get_storage("pv-1", "team-ns")
    assert st.usage_warning is True and st.usage_warning_threshold == 0.8
    assert st.maintenance_start.hour == 0 and st.maintenance_end.hour == 2
    window = st.upcoming_maintenance[0]
    assert (window.reason, window.description, window.started) == ("power", "UPS swap", False)
    assert window.start_time.day == 1 and window.end_time.hour == 1
    assert st.location == "Seoul" and st.downloader_state == "running"
    assert st.downloader_restarts[0].reason == "OOMKilled" and st.downloader_restarts[0].occurred_at.day == 29
    names = {f.name for f in dataclasses.fields(Storage)}
    assert not names & {"machine_id", "host_id", "node_name"}


def test_storage_from_an_older_server_keeps_defaults():
    st = Storage.from_dict({"pvName": "pv-1"})
    assert st.usage_warning is False and st.usage_warning_threshold == 0.9
    assert st.upcoming_maintenance == [] and st.location is None
    assert st.downloader_state is None and st.downloader_restarts == []


# --- templates -------------------------------------------------------------------------

TEMPLATE_FULL = {
    "id": 21, "name": "My app", "description": "d", "isOfficial": False, "templateDeployType": "pod",
    "appType": "custom", "image": "ghcr.io/me/app:1", "command": ["bash", "-c"], "args": ["serve"],
    "overwriteCommand": "bash -c serve", "hardwareType": "gpu",
    "envs": [{"key": "TOKEN", "value": "***", "isSecret": True},
             {"key": "EMPTY_SECRET", "value": "", "isSecret": True},
             {"key": "MODE", "value": "fast", "isBase": True, "showOnConnect": True},
             {"key": "PASSWORD", "value": "abc", "isAutoGenerated": True}],
    "endpoints": [{"userAlias": "Web", "containerPort": 8080, "isExternal": True, "portType": "connect",
                   "isLocked": True, "templatePortId": 5},
                  {"userAlias": "metrics", "containerPort": 9090, "isExternal": False, "portType": "custom"}],
    "volumeMounts": [{"userAlias": "data", "mountPath": "/data"}],
    "semanticPaths": [{"role": "input", "path": "/models", "expect": "model", "include": ["*.safetensors"],
                       "required": True, "cardinality": "many", "placement": "preserve_root"}],
    "volumeRequired": True,
    "auth": {"id": 3, "userAlias": "ghcr", "server": "ghcr.io", "username": "me"},
}


def test_template_types_the_spec():
    t = sync_client(_capture(TEMPLATE_FULL, {})).get_template(21, "team-ns")
    token, empty, mode, password = t.envs
    assert token.secret is True and token.value is None and token.value_set is True
    assert empty.value == "" and empty.value_set is False
    assert mode.from_image is True and mode.show_on_connect is True and mode.value == "fast" and mode.value_set
    assert password.auto_generated is True and password.value == "abc"
    assert "***" not in repr(t.envs)
    web, metrics = t.ports
    assert (web.port, web.name, web.external, web.connect, web.locked, web.template_port_id) == (
        8080, "Web", True, True, True, 5)
    assert (metrics.external, metrics.connect, metrics.locked, metrics.template_port_id) == (False, False, None, None)
    assert t.volume_mounts[0].name == "data" and t.volume_mounts[0].mount_path == "/data"
    sp = t.semantic_paths[0]
    assert (sp.role, sp.path, sp.expect, sp.include, sp.required) == ("input", "/models", "model",
                                                                    ["*.safetensors"], True)
    assert sp.cardinality == "many" and sp.placement == "preserve_root"
    assert t.command == "bash -c serve" and t.command_argv == ["bash", "-c"] and t.args == ["serve"]
    assert t.volume_required is True
    login = t.registry_login
    assert (login.registry_login_id, login.label, login.server, login.username) == (3, "ghcr", "ghcr.io", "me")


def test_official_template_without_auth_has_no_registry_login():
    t = Template.from_dict({"id": 1, "name": "PyTorch", "image": "pytorch", "isOfficial": True, "auth": None})
    assert t.registry_login is None and t.envs == [] and t.ports == [] and t.command == ""
    assert t.command_argv == [] and t.args == [] and t.volume_required is False


# --- serving models ---------------------------------------------------------------------

def test_serving_model_typed_fields_and_properties():
    m = sync_client(_capture([{
        "id": 4, "sourceId": 2, "modelName": "qwen", "displayName": "Qwen 7B", "modelType": "llm",
        "modality": "text", "inputModalities": ["text", "image"], "framework": "vllm", "endpointType": "chat",
        "contextLength": 32768, "recommendedMaxModelLen": 8192, "displayVramGb": 18, "isOfficial": True,
        "namespaceName": None, "supportedSizes": None, "nativeFps": None}], {})).list_models("team-ns")[0]
    assert (m.modality, m.input_modalities, m.is_official, m.workspace) == ("text", ["text", "image"], True, None)
    assert m.endpoint_type == "chat" and m.recommended_context_length == 8192 and m.display_vram_gb == 18
    assert m.takes_image is True and m.can_share_idle_capacity is True

    video = ServingModel.from_dict({"id": 5, "modality": "video", "framework": "comfyui", "isOfficial": True,
                                    "namespaceName": "team-ns", "supportedResolutions": ["704x1280"],
                                    "nativeNumFrames": 81, "nativeFps": 16, "supportedSizes": ["512x512"]})
    assert video.supported_resolutions == ["704x1280"] and (video.native_num_frames, video.native_fps) == (81, 16)
    assert video.supported_sizes == ["512x512"] and video.workspace == "team-ns"
    assert video.takes_image is False and video.can_share_idle_capacity is False


def test_serving_model_from_an_older_server_keeps_defaults():
    m = ServingModel.from_dict({"id": 1, "framework": "vllm"})
    assert m.input_modalities == ["text"] and m.is_official is False and m.modality is None
    assert m.takes_image is False and m.can_share_idle_capacity is False


# --- parity and exports ------------------------------------------------------------------

def test_async_client_parses_the_same_fields():
    async def run():
        async with async_client(_capture(TASK_DETAIL, {})) as client:
            task = await client.get_task("task_abc")
        async with async_client(_capture(SERVING_FULL, {})) as client:
            serving = await client.get_serving(5)
        async with async_client(_capture(TEMPLATE_FULL, {})) as client:
            template = await client.get_template(21, "team-ns")
        async with async_client(_capture([_machine("storage")], {})) as client:
            machines = await client.list_machines()
        return task, serving, template, machines

    task, serving, template, machines = asyncio.run(run())
    assert task.project.entrypoint == "train.py" and serving.replicas[1].failure.failed_step == "waiting_for_ready"
    assert template.ports[0].locked is True and machines[0].price.unit == "storage"


def test_new_read_models_are_exported():
    for name in ("TaskProject", "ServingReplica", "ServingFailure", "ServingFailureContainer", "ServingFailureEvent",
                 "ServingInitLog", "ServingStoppedReplica", "ServingMetrics", "ServingReplicaMetrics",
                 "CapMigration", "WorkspaceMaintenance", "WorkspaceMessage", "MachinePrice", "MachineSetupState",
                 "MachineDisk", "MachineGpuSlot", "MachineSpecs", "MaintenanceWindowInfo", "StorageAccessRestart",
                 "TemplateEnv", "TemplatePort", "TemplateVolumeMount", "TemplateSemanticPath", "RegistryLogin"):
        assert name in meshive.__all__ and hasattr(meshive, name), name
