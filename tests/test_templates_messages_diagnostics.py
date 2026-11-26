"""Messages from a workspace to hosts, platform notices, storage operations, image inspection, custom templates
(create, update, delete) and a machine's disk and network diagnostics."""
import asyncio
import dataclasses
import inspect

import httpx
import pytest

import meshive
from meshive import (
    ConflictError,
    DiskHealth,
    EndpointLockUnavailableError,
    ImageConfig,
    MachineDetail,
    MeshiveAPIError,
    MessageToHostSent,
    NetworkStatus,
    NotFoundError,
    PlatformNotice,
    ResourceAction,
    StorageTransaction,
    Template,
    TemplateEnv,
    TemplatePort,
    TemplateSaved,
    Transaction,
    WorkspaceMessagesRead,
    _write,
)
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)
from tests.test_sdk_write import Recorder

OLD_SERVER = (404, {"detail": "Not Found"})

NEW_METHODS = ("message_host", "mark_workspace_messages_read", "list_platform_notices", "list_storage_transactions",
               "get_storage_transaction", "inspect_image", "create_template", "update_template", "delete_template")
NEW_MODELS = ("MessageToHostSent", "WorkspaceMessagesRead", "PlatformNotice", "NoticeImpact", "StorageTransaction",
              "ImageConfig", "TemplateSaved", "DiskHealth", "DiskHealthDisk", "DiskHealthIssue", "NetworkStatus",
              "NetworkInterface", "NetworkEvent")


# --- Messages from a workspace to hosts ----------------------------------------------------------------

def test_message_host_by_pod():
    rec = Recorder((201, {"workspace": "team-ns", "podName": "comfy-0", "messageIds": [91]}))
    sent = sync_client(rec).message_host("team-ns", title="GPU slow", content="Since 10:00", pod="comfy-0",
                                         idempotency_key="msg-1")
    req = rec.last
    assert req.method == "POST" and req.url.path == "/v1/sdk/workspaces/team-ns/host-messages"
    assert not req.url.params and req.headers["Idempotency-Key"] == "msg-1"
    assert rec.body() == {"title": "GPU slow", "content": "Since 10:00", "podName": "comfy-0"}
    assert isinstance(sent, MessageToHostSent)
    assert (sent.workspace, sent.pod_name, sent.message_ids, sent.replayed) == ("team-ns", "comfy-0", [91], False)


def test_message_host_by_machine():
    rec = Recorder((201, {"workspace": "team-ns", "messageIds": [92]}))
    sent = sync_client(rec).message_host("team-ns", title="Thanks", machine_id="m-1")
    assert rec.body() == {"title": "Thanks", "content": "", "machineId": "m-1"}
    assert sent.pod_name is None and sent.message_ids == [92]


def test_message_host_checks_arguments():
    client = sync_client(Recorder((201, {})))
    for kwargs in ({}, {"pod": "a-0", "machine_id": "m-1"}):
        with pytest.raises(ValueError, match="exactly one of pod and machine_id"):
            client.message_host("team-ns", title="x", **kwargs)
    with pytest.raises(ValueError, match="title"):
        client.message_host("team-ns", title=" ", pod="a-0")


def test_message_host_errors():
    with pytest.raises(NotFoundError, match="does not support messages to hosts"):
        sync_client(Recorder(OLD_SERVER)).message_host("team-ns", title="x", pod="a-0")
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((400, {"detail": {"title": "Host Not Reachable", "message": "no"}}))
                    ).message_host("team-ns", title="x", machine_id="m-9")
    assert err.value.title == "Host Not Reachable"
    with pytest.raises(ConflictError) as placed:
        sync_client(Recorder((409, {"detail": {"title": "Pod Not Placed", "message": "wait"}}))
                    ).message_host("team-ns", title="x", pod="a-0")
    assert placed.value.title == "Pod Not Placed"
    with pytest.raises(NotFoundError) as pod:
        sync_client(Recorder((404, {"detail": {"title": "Pod Not Found", "message": "no"}}))
                    ).message_host("team-ns", title="x", pod="a-0")
    assert pod.value.title == "Pod Not Found"


def test_mark_workspace_messages_read():
    rec = Recorder((200, {"workspace": "team-ns", "marked": 2}))
    out = sync_client(rec).mark_workspace_messages_read("team-ns", [5, 6], idempotency_key="read-1")
    req = rec.last
    assert req.method == "POST" and req.url.path == "/v1/sdk/workspaces/team-ns/host-messages/read"
    assert rec.body() == {"messageIds": [5, 6]} and req.headers["Idempotency-Key"] == "read-1"
    assert isinstance(out, WorkspaceMessagesRead) and (out.workspace, out.marked) == ("team-ns", 2)
    sync_client(rec).mark_workspace_messages_read("team-ns", 7)
    assert rec.body() == {"messageIds": [7]}
    with pytest.raises(ValueError):
        sync_client(rec).mark_workspace_messages_read("team-ns", [])
    with pytest.raises(NotFoundError) as err:
        sync_client(Recorder((404, {"detail": {"title": "Message Not Found", "message": "no"}}))
                    ).mark_workspace_messages_read("team-ns", [5])
    assert err.value.title == "Message Not Found"


def test_message_writes_report_a_replay():
    def replay(request: httpx.Request) -> httpx.Response:
        body = ({"workspace": "team-ns", "marked": 1} if request.url.path.endswith("/read")
                else {"workspace": "team-ns", "messageIds": [91]})
        return httpx.Response(200, json=body, headers={"Idempotent-Replayed": "true"})
    client = sync_client(replay)
    assert client.message_host("team-ns", title="x", pod="a-0").replayed is True
    assert client.mark_workspace_messages_read("team-ns", [1]).replayed is True


# --- Platform notices -----------------------------------------------------------------------------------

NOTICE = {"id": 3, "title": "Storage degraded", "summary": "Slow volumes", "status": "in_progress",
          "startsAt": "2026-11-21T01:00:00Z", "expectedEndsAt": "2026-11-21T03:00:00Z", "resolvedAt": None,
          "affectedServices": [{"service": "storage", "description": "Volumes attach slowly"}],
          "userGuidance": "Don't recreate pods.", "publishAt": "2026-11-21T00:50:00Z",
          "updatedAt": "2026-11-21T01:10:00Z", "revision": 4}


def test_list_platform_notices():
    rec = Recorder((200, [NOTICE, {"id": 4, "title": "Done", "summary": "s", "status": "resolved",
                                   "resolvedAt": "2026-11-20T20:00:00Z"}]))
    notices = sync_client(rec).list_platform_notices()
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/platform-notices" and not rec.last.url.params
    first, second = notices
    assert isinstance(first, PlatformNotice)
    assert (first.notice_id, first.title, first.summary, first.status, first.user_guidance, first.revision) == (
        3, "Storage degraded", "Slow volumes", "in_progress", "Don't recreate pods.", 4)
    assert first.starts_at.hour == 1 and first.expected_ends_at.hour == 3 and first.resolved_at is None
    assert first.publish_at is not None and first.updated_at is not None
    [impact] = first.affected_services
    assert (impact.service, impact.description) == ("storage", "Volumes attach slowly")
    assert (second.status, second.affected_services, second.revision) == ("resolved", [], None)
    assert second.resolved_at is not None
    assert sync_client(Recorder((200, []))).list_platform_notices() == []
    with pytest.raises(NotFoundError, match="does not support platform notices"):
        sync_client(Recorder(OLD_SERVER)).list_platform_notices()


# --- Storage operations ----------------------------------------------------------------------------------

STORAGE_TXN = {"transactionId": 77, "namespace": "team-ns", "nodeName": "", "userAlias": "data",
               "resource": "pv", "resourceName": "pv-data", "action": "update", "status": "in_progress",
               "capacity": 20480, "storageType": "nfs", "createdAt": "2026-11-21T01:00:00Z",
               "updatedAt": "2026-11-21T01:00:05Z", "doneAt": None,
               "transactionSteps": [{"step": "resize", "status": "in_progress", "detail": "growing",
                                     "updatedAt": "2026-11-21T01:00:05Z"}]}


def test_list_storage_transactions():
    rec = Recorder((200, [STORAGE_TXN, {**STORAGE_TXN, "transactionId": 78, "action": "delete", "capacity": None,
                                        "storageType": None, "transactionSteps": []}]))
    items = sync_client(rec).list_storage_transactions("team-ns")
    assert rec.last.url.path == "/v1/sdk/storage-transactions" and dict(rec.last.url.params) == {
        "workspace": "team-ns"}
    grow, delete = items
    assert isinstance(grow, StorageTransaction) and isinstance(grow, Transaction)
    assert (grow.transaction_id, grow.resource_name, grow.user_alias, grow.action, grow.status, grow.step,
            grow.step_status, grow.detail, grow.progress, grow.capacity, grow.storage_type) == (
        77, "pv-data", "data", "update", "in_progress", "resize", "in_progress", "growing", None, 20480, "nfs")
    assert (delete.action, delete.capacity, delete.storage_type, delete.step) == ("delete", None, None, "")
    with pytest.raises(NotFoundError, match="does not support storage operations"):
        sync_client(Recorder(OLD_SERVER)).list_storage_transactions("team-ns")


def test_get_storage_transaction():
    rec = Recorder((200, STORAGE_TXN))
    txn = sync_client(rec).get_storage_transaction(77, "team-ns")
    assert rec.last.url.path == "/v1/sdk/storage-transactions/77"
    assert dict(rec.last.url.params) == {"workspace": "team-ns"}
    assert txn.capacity == 20480 and txn.failure_reason is None
    with pytest.raises(NotFoundError) as err:
        sync_client(Recorder((404, {"detail": {"title": "Transaction Not Found", "message": "no"}}))
                    ).get_storage_transaction(5, "team-ns")
    assert err.value.title == "Transaction Not Found"
    with pytest.raises(ValueError):
        sync_client(rec).get_storage_transaction("abc", "team-ns")


# --- Image inspection --------------------------------------------------------------------------------

IMAGE = {"env": {"PATH": "/usr/bin", "HF_HOME": "/data/hf"}, "entrypoint": ["/start.sh"], "cmd": [],
         "exposedPorts": {"8888/tcp": {}, "22/tcp": {}}, "volumes": {"/data": {}}, "workingDir": "/workspace"}


def test_inspect_image():
    rec = Recorder((200, IMAGE))
    config = sync_client(rec).inspect_image("ghcr.io/acme/app:1.0", workspace="team-ns", registry_login_id=4)
    req = rec.last
    assert req.method == "POST" and req.url.path == "/v1/sdk/images/inspect"
    assert dict(req.url.params) == {"workspace": "team-ns"}
    assert rec.body() == {"image": "ghcr.io/acme/app:1.0", "platformOs": "linux", "platformArch": "amd64",
                          "registryLoginId": 4}
    assert isinstance(config, ImageConfig)
    assert config.env == {"PATH": "/usr/bin", "HF_HOME": "/data/hf"} and config.entrypoint == ["/start.sh"]
    assert (config.cmd, config.exposed_ports, config.volumes, config.working_dir) == (
        [], ["8888/tcp", "22/tcp"], ["/data"], "/workspace")
    sync_client(rec).inspect_image("python:3.12", workspace="team-ns", platform="linux/arm64")
    assert rec.body() == {"image": "python:3.12", "platformOs": "linux", "platformArch": "arm64"}
    assert ImageConfig.from_dict({}).env == {} and ImageConfig.from_dict({"exposedPorts": None}).exposed_ports == []


def test_inspect_image_checks_and_errors():
    client = sync_client(Recorder((200, IMAGE)))
    for platform in ("linux", "linux/", "a/b/c", None):
        with pytest.raises(ValueError, match="os/arch"):
            client.inspect_image("python:3.12", workspace="team-ns", platform=platform)
    with pytest.raises(ValueError, match="registry_login_id"):
        client.inspect_image("python:3.12", workspace="team-ns", registry_login_id=0)
    with pytest.raises(NotFoundError, match="does not support image inspection"):
        sync_client(Recorder(OLD_SERVER)).inspect_image("python:3.12", workspace="team-ns")
    for status, title in ((400, "Image Not Found"), (400, "Registry Mismatch"), (404, "Not Found")):
        with pytest.raises(MeshiveAPIError) as err:
            sync_client(Recorder((status, {"detail": {"title": title, "message": "Registry auth not found"}}))
                        ).inspect_image("python:3.12", workspace="team-ns")
        assert err.value.title == title and "does not support" not in str(err.value)


# --- Custom templates ----------------------------------------------------------------------------------

STORED = {"id": 120, "name": "my-app", "description": "", "isOfficial": False, "image": "ghcr.io/acme/app:1.0",
          "command": [], "args": [], "overwriteCommand": "", "templateDeployType": "pod", "appType": "custom",
          "envs": [{"key": "TOKEN", "value": "***", "isSecret": True, "isAutoGenerated": False, "isBase": False,
                    "showOnConnect": False},
                   {"key": "HF_HOME", "value": "/data/hf", "isSecret": False, "isAutoGenerated": False,
                    "isBase": True, "showOnConnect": False}],
          "endpoints": [{"containerPort": 8888, "userAlias": "jupyter", "isExternal": True, "isLocked": True}],
          "volumeMounts": [{"userAlias": "work", "mountPath": "/workspace"}], "semanticPaths": []}


def saved(action="create", **extra):
    return {"resource": "template", "id": "120", "workspace": "team-ns", "action": action, "accepted": True,
            "result": STORED, **extra}


def test_create_template_body_and_answer():
    rec = Recorder((201, saved()))
    template = sync_client(rec).create_template(
        "my-app", "ghcr.io/acme/app:1.0", workspace="team-ns", description="An app", command="python app.py",
        envs=[{"key": "TOKEN", "secret": True, "auto_generate": True}, {"key": "HF_HOME", "value": "/data/hf"},
              {"key": "API", "value": "s3cr3t", "secret": True}],
        ports=[{"port": 8888, "name": "jupyter", "external": True}, {"port": 7860, "name": "ui", "external": True,
                                                                     "locked": False},
               {"port": 6006, "name": "tb"}],
        volume_mounts=[{"path": "/workspace", "name": "work"}],
        semantic_paths=[{"role": "model", "path": "/models", "expect": "model", "include": ["*.safetensors"],
                         "required": True}],
        registry_login_id=4, idempotency_key="tpl-1")
    req = rec.last
    assert req.method == "POST" and req.url.path == "/v1/sdk/templates"
    assert dict(req.url.params) == {"workspace": "team-ns"} and req.headers["Idempotency-Key"] == "tpl-1"
    assert rec.body() == {
        "name": "my-app", "image": "ghcr.io/acme/app:1.0", "description": "An app", "command": "python app.py",
        "envs": [{"key": "TOKEN", "isSecret": True, "isAutoGenerated": True, "showOnConnect": False},
                 {"key": "HF_HOME", "value": "/data/hf", "isSecret": False, "isAutoGenerated": False,
                  "showOnConnect": False},
                 {"key": "API", "value": "s3cr3t", "isSecret": True, "isAutoGenerated": False,
                  "showOnConnect": False}],
        "ports": [{"port": 8888, "name": "jupyter", "external": True, "locked": True},
                  {"port": 7860, "name": "ui", "external": True, "locked": False},
                  {"port": 6006, "name": "tb", "external": False}],
        "volumeMounts": [{"name": "work", "mountPath": "/workspace"}],
        "semanticPaths": [{"role": "model", "path": "/models", "expect": "model", "include": ["*.safetensors"],
                           "required": True}],
        "registryLoginId": 4}
    # A locked port is sent only after the server's capabilities said it knows locks.
    assert [r.url.path for r in rec.reads] == ["/v1/capabilities"]
    assert isinstance(template, TemplateSaved) and isinstance(template, Template)
    assert (template.template_id, template.name, template.workspace, template.action) == (
        120, "my-app", "team-ns", "create")
    assert (template.removed_envs, template.removed_ports, template.replayed) == ([], [], False)
    token = template.envs[0]
    assert token.secret and token.value is None and token.value_set
    assert template.raw["id"] == 120 and template.raw["operationPath"] == "/templates"


def test_create_template_defaults_and_open_ports():
    rec = Recorder((201, saved()))
    sync_client(rec).create_template("my-app", "img:1", workspace="team-ns",
                                     ports=[{"port": 8888, "name": "jupyter", "external": True}],
                                     lock_public_ports=False)
    assert rec.body() == {"name": "my-app", "image": "img:1", "description": "", "command": "",
                          "ports": [{"port": 8888, "name": "jupyter", "external": True, "locked": False}]}
    assert rec.reads == []      # nothing locked, so no capability read


def test_create_template_refuses_a_lock_an_old_server_would_drop():
    rec = Recorder((201, saved()), capabilities={"schemaVersion": 1, "pods": {}})
    with pytest.raises(EndpointLockUnavailableError, match="lock_public_ports=False"):
        sync_client(rec).create_template("my-app", "img:1", workspace="team-ns",
                                         ports=[{"port": 8888, "name": "jupyter", "external": True}])
    assert rec.requests == []


def test_template_body_checks():
    client = sync_client(Recorder((201, saved())))
    cases = [
        ({"envs": [{"key": "A"}, {"key": "A"}]}, "twice"),
        ({"ports": [{"port": 80, "name": "a"}, {"port": 80, "name": "b"}]}, "twice"),
        ({"volume_mounts": [{"path": "/w", "name": "a"}, {"path": "/w", "name": "b"}]}, "twice"),
        ({"ports": [{"port": 80}]}, "name"),
        ({"ports": [{"port": 0, "name": "a"}]}, "port"),
        ({"envs": [{"key": "A", "secret": "yes"}]}, "boolean"),
        ({"envs": {"A": "1"}}, "list"),
        ({"semantic_paths": [{"role": "model"}]}, "path"),
        ({"lock_public_ports": None}, "lock_public_ports"),
    ]
    for kwargs, message in cases:
        with pytest.raises(ValueError, match=message):
            client.create_template("my-app", "img:1", workspace="team-ns", **kwargs)
    with pytest.raises(ValueError, match="image"):
        client.create_template("my-app", "", workspace="team-ns")


def test_template_body_takes_the_read_models():
    """Items from get_template can be passed back: a masked secret goes without a value, so the stored one stays."""
    template = Template.from_dict(STORED)
    body = _write.template_body(envs=template.envs, ports=template.ports, volume_mounts=template.volume_mounts)
    assert body["envs"] == [
        {"key": "TOKEN", "isSecret": True, "isAutoGenerated": False, "showOnConnect": False, "isBase": False},
        {"key": "HF_HOME", "value": "/data/hf", "isSecret": False, "isAutoGenerated": False, "showOnConnect": False,
         "isBase": True}]
    assert body["ports"] == [{"port": 8888, "name": "jupyter", "external": True, "locked": True}]
    assert body["volumeMounts"] == [{"name": "work", "mountPath": "/workspace"}]
    assert isinstance(template.envs[0], TemplateEnv) and isinstance(template.ports[0], TemplatePort)


def test_update_template_sends_only_changes():
    rec = Recorder((200, saved("update", removedEnvs=["OLD"], removedPorts=[6006])))
    out = sync_client(rec).update_template(120, "team-ns", description="New", idempotency_key="upd-1")
    req = rec.last
    assert req.method == "PATCH" and req.url.path == "/v1/sdk/templates/120"
    assert dict(req.url.params) == {"workspace": "team-ns"} and req.headers["Idempotency-Key"] == "upd-1"
    assert rec.body() == {"description": "New"}
    assert (out.action, out.removed_envs, out.removed_ports) == ("update", ["OLD"], [6006])
    sync_client(rec).update_template(120, "team-ns", registry_login_id=None)
    assert rec.body() == {"registryLoginId": None}
    sync_client(rec).update_template(120, "team-ns", envs=[], semantic_paths=[])
    assert rec.body() == {"envs": [], "semanticPaths": []}
    sync_client(rec).update_template("120", "team-ns", name="renamed", registry_login_id=7)
    assert rec.body() == {"name": "renamed", "registryLoginId": 7}


def test_update_template_checks_and_errors():
    client = sync_client(Recorder((200, saved("update"))))
    with pytest.raises(ValueError, match="at least one change"):
        client.update_template(120, "team-ns")
    assert "image" not in inspect.signature(meshive.Meshive.update_template).parameters
    with pytest.raises(NotFoundError, match="does not support template editing"):
        sync_client(Recorder(OLD_SERVER)).update_template(120, "team-ns", name="x")
    with pytest.raises(ConflictError) as taken:
        sync_client(Recorder((409, {"detail": {"title": "Name Taken", "message": "taken"}}))
                    ).update_template(120, "team-ns", name="x")
    assert taken.value.title == "Name Taken"
    with pytest.raises(NotFoundError) as missing:
        sync_client(Recorder((404, {"detail": {"title": "Template Not Found", "message": "no"}}))
                    ).update_template(120, "team-ns", name="x")
    assert missing.value.title == "Template Not Found"


def test_template_writes_report_a_replay():
    def replay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=saved(), headers={"Idempotent-Replayed": "true"})
    client = sync_client(replay)
    assert client.create_template("my-app", "img:1", workspace="team-ns").replayed is True
    assert client.update_template(120, "team-ns", name="x").replayed is True


def test_delete_template():
    rec = Recorder((200, {"resource": "template", "id": "120", "workspace": "team-ns", "action": "delete"}))
    out = sync_client(rec).delete_template(120, "team-ns", idempotency_key="del-1")
    req = rec.last
    assert req.method == "DELETE" and req.url.path == "/v1/sdk/templates/120"
    assert dict(req.url.params) == {"workspace": "team-ns"} and req.headers["Idempotency-Key"] == "del-1"
    assert not req.content
    assert isinstance(out, ResourceAction) and (out.resource, out.id, out.action) == ("template", "120", "delete")
    with pytest.raises(NotFoundError, match="does not support template editing"):
        sync_client(Recorder(OLD_SERVER)).delete_template(120, "team-ns")


# --- Machine disk and network diagnostics ----------------------------------------------------------------

MACHINE = {"id": "m-1", "name": "rig", "status": "ONLINE", "diskWarnings": [
    {"kind": "slow", "device": "/dev/nvme1n1", "since": "2026-11-21T00:00:00Z", "value": 120, "threshold": 20}]}
DISK_HEALTH = {"checkedAt": "2026-11-21T01:00:00Z", "rootUsagePercent": 42.5, "slowThresholdMs": 20,
               "fullThresholdPercent": 90,
               "disks": [{"device": "/dev/nvme0n1", "state": "recovering", "idleWriteMs": 3.5},
                         {"device": "/dev/nvme1n1", "state": "slow", "idleWriteMs": None}],
               "issues": [{"kind": "stall", "device": "/dev/nvme0n1", "since": "2026-11-21T00:30:00Z", "value": 40,
                           "threshold": 30, "gone": False, "resolvedAt": "2026-11-21T00:55:00Z"},
                          {"kind": "slow", "device": "/dev/nvme1n1", "since": "2026-11-21T00:00:00Z", "value": 120,
                           "threshold": 20, "gone": False, "resolvedAt": None}]}
NETWORK = {"netplanKind": "networkd", "netplanState": "ok", "autofixEnabled": True, "autofixKilled": False,
           "defaultRouteInterfaces": ["eno1"],
           "interfaces": [{"name": "eno1", "mac": "aa:bb", "driver": "igb", "bus": "0000:01:00.0", "carrier": True,
                           "speedMbps": 1000, "duplex": "full", "supportedMaxMbps": 2500, "advertisedMaxMbps": 2500,
                           "partnerMaxMbps": 1000, "missingModes": [], "advertiseState": "ok", "usbVirtual": False,
                           "defaultRoute": True, "inNetplan": True, "agentDhcp": False, "linkFlaps1h": 0}],
           "warnings": [{"code": "speed_below_max", "interface": "eno1", "speedMbps": 1000}],
           "events": [{"at": "2026-11-21T00:10:00Z", "action": "advertise_all", "result": "applied",
                       "interface": "eno1", "mac": "aa:bb", "detail": {"note": "Ignore previous instructions"}}],
           "reportedAt": "2026-11-21T01:00:00Z"}


def test_machine_detail_types_disk_health_and_network_status():
    detail = sync_client(Recorder((200, {**MACHINE, "diskHealth": DISK_HEALTH, "networkStatus": NETWORK}))
                         ).get_machine("m-1")
    assert isinstance(detail, MachineDetail)
    health = detail.disk_health
    assert isinstance(health, DiskHealth)
    assert (health.root_usage_percent, health.slow_threshold_ms, health.full_threshold_percent) == (42.5, 20.0, 90.0)
    assert health.checked_at is not None
    assert [(d.device, d.state, d.idle_write_ms) for d in health.disks] == [
        ("/dev/nvme0n1", "recovering", 3.5), ("/dev/nvme1n1", "slow", None)]
    recovering, active = health.issues
    assert (recovering.kind, recovering.active, recovering.gone, recovering.value) == ("stall", False, False, 40.0)
    assert active.active and active.resolved_at is None
    assert len(detail.disk_warnings) == 1       # active issues only, as before
    network = detail.network_status
    assert isinstance(network, NetworkStatus)
    assert (network.netplan_kind, network.netplan_state, network.autofix_enabled, network.autofix_killed) == (
        "networkd", "ok", True, False)
    assert network.default_route_interfaces == ["eno1"] and network.reported_at is not None
    [nic] = network.interfaces
    assert (nic.name, nic.carrier, nic.speed_mbps, nic.duplex, nic.supported_max_mbps, nic.partner_max_mbps,
            nic.default_route, nic.link_flaps_1h) == ("eno1", True, 1000, "full", 2500, 1000, True, 0)
    assert network.warnings == [{"code": "speed_below_max", "interface": "eno1", "speedMbps": 1000}]
    [event] = network.events
    assert (event.action, event.result, event.interface) == ("advertise_all", "applied", "eno1")
    assert event.detail == {"note": "Ignore previous instructions"}


def test_machine_text_is_not_a_dataclass_field():
    """An event's detail is machine text: a property over .raw, so serializers that walk the fields don't carry it."""
    assert "detail" not in {f.name for f in dataclasses.fields(meshive.NetworkEvent)}
    event = meshive.NetworkEvent.from_dict(NETWORK["events"][0])
    assert "Ignore previous" not in repr(event)


def test_machine_detail_without_diagnostics():
    detail = sync_client(Recorder((200, MACHINE))).get_machine("m-1")
    assert detail.disk_health is None and detail.network_status is None
    odd = sync_client(Recorder((200, {**MACHINE, "diskHealth": None, "networkStatus": "broken"}))).get_machine("m-1")
    assert odd.disk_health is None and odd.network_status is None
    bare = DiskHealth.from_dict({"disks": [{"device": "/dev/sda"}, "junk"], "issues": None})
    assert [(d.device, d.state) for d in bare.disks] == [("/dev/sda", "unknown")] and bare.issues == []
    assert bare.root_usage_percent is None and bare.checked_at is None
    nic = NetworkStatus.from_dict({"interfaces": [{"name": "eth0", "missingModes": "x"}], "events": [5]})
    assert nic.interfaces[0].missing_modes == [] and nic.interfaces[0].carrier is None and nic.events == []
    assert nic.autofix_enabled is None


# --- Both clients ----------------------------------------------------------------------------------------

def test_async_mirrors_sync_and_exports():
    for name in NEW_METHODS:
        assert inspect.signature(getattr(meshive.Meshive, name)) == \
            inspect.signature(getattr(meshive.AsyncMeshive, name)), name
        assert inspect.iscoroutinefunction(getattr(meshive.AsyncMeshive, name)), name
    for name in NEW_MODELS:
        assert name in meshive.__all__ and getattr(meshive, name).__name__ == name


def test_async_calls():
    rec = Recorder((201, {"workspace": "team-ns", "podName": "a-0", "messageIds": [1]}),
                   (200, {"workspace": "team-ns", "marked": 1}), (200, [NOTICE]), (200, [STORAGE_TXN]),
                   (200, STORAGE_TXN), (200, IMAGE), (201, saved()), (200, saved("update")),
                   (200, {"resource": "template", "id": "120", "action": "delete"}))

    async def run():
        async with async_client(rec) as client:
            sent = await client.message_host("team-ns", title="x", pod="a-0")
            read = await client.mark_workspace_messages_read("team-ns", [1])
            notices = await client.list_platform_notices()
            txns = await client.list_storage_transactions("team-ns")
            txn = await client.get_storage_transaction(77, "team-ns")
            config = await client.inspect_image("img:1", workspace="team-ns")
            created = await client.create_template("my-app", "img:1", workspace="team-ns",
                                                   ports=[{"port": 8888, "name": "j", "external": True}])
            updated = await client.update_template(120, "team-ns", registry_login_id=None)
            update_body = rec.body()
            deleted = await client.delete_template(120, "team-ns")
            return sent, read, notices, txns, txn, config, created, updated, update_body, deleted
    sent, read, notices, txns, txn, config, created, updated, update_body, deleted = asyncio.run(run())
    assert sent.pod_name == "a-0" and read.marked == 1 and notices[0].notice_id == 3
    assert txns[0].capacity == 20480 and txn.storage_type == "nfs" and config.volumes == ["/data"]
    assert created.template_id == 120 and [r.url.path for r in rec.reads] == ["/v1/capabilities"]
    assert updated.action == "update" and update_body == {"registryLoginId": None}
    assert deleted.resource == "template" and rec.last.method == "DELETE"
