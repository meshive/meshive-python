"""Port locks and access keys: the lock values pod creates and updates send, servers that can't lock, and the
port lock / access key calls — SDK (sync and async) and CLI."""
import asyncio
import importlib
import io
import json
import re

import httpx
import pytest

from meshive import (AccessKey, AccessKeyCreated, BrowserSessionsRevoked, ConflictError,
                     EndpointLockUnavailableError, MeshiveAPIError, MeshiveError, NotFoundError, Pod, PodEndpoint,
                     PortLock, PortNotLockedError, PortPreflight, ResourceAction, WaitTimeoutError)
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)

cli = importlib.import_module("meshive.cli.main")

# A key value in the server's shape ("mshv_" + 43 characters). It must never show up outside `access_key`.
KEY = "mshv_" + "Zq7" * 14 + "x"
KEY_ITEM = {"id": 12, "endpointName": "a3f9", "containerPort": 3000, "portAlias": "console", "podName": "p-0",
            "keyName": "batch-script", "keyPrefix": "mshv_Zq7Zq7Zq", "createdBy": "dev@example.com",
            "createdAt": "2026-11-02T09:00:00Z", "issuedAt": "2026-11-02T09:00:00Z",
            "expiresAt": "2027-01-31T09:00:00Z", "previousValueValidUntil": None}
ROLLED = {**KEY_ITEM, "id": 31, "issuedAt": "2026-11-14T14:00:00Z", "expiresAt": "2027-02-12T14:00:00Z",
          "previousValueValidUntil": "2026-11-14T15:00:00Z", "accessKey": KEY}

# 8188: the author set no default (locked). 8080: the author chose unlocked. 9000: the author chose locked.
# 9100: internal.
TEMPLATE = {"id": 457, "name": "ComfyUI", "endpoints": [
    {"userAlias": "ComfyUI", "containerPort": 8188, "isExternal": True, "portType": "connect", "isLocked": None},
    {"userAlias": "ide", "containerPort": 8080, "isExternal": True, "portType": "http", "isLocked": False},
    {"userAlias": "api", "containerPort": 9000, "isExternal": True, "portType": "http", "isLocked": True},
    {"userAlias": "metrics", "containerPort": 9100, "isExternal": False, "portType": "http", "isLocked": None}]}
# The same template from a server that predates locks: no isLocked key at all.
OLD_TEMPLATE = {"id": 457, "name": "ComfyUI",
                "endpoints": [{k: v for k, v in e.items() if k != "isLocked"} for e in TEMPLATE["endpoints"]]}
CAPABILITIES = {"schemaVersion": 1, "assets": {}, "pods": {"readinessState": True, "endpointLock": True}}
SWITCHED_OFF = {"schemaVersion": 1, "assets": {}, "pods": {"readinessState": True, "endpointLock": False}}
OLD_CAPABILITIES = {"schemaVersion": 1, "assets": {}, "pods": {"readinessState": True}}
LOCK_DISABLED = {"detail": {"title": "Endpoint Lock Unavailable",
                            "message": "Endpoint lock is not available right now. Retry later, or create the port "
                                       "unlocked.", "code": "endpoint_lock_disabled"}}
ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"},
            "resources": {"gpu_model": "RTX 3060", "vram_gb": 12, "gpu_count": 1, "vcpu": 4, "ram_gb": 12,
                          "disk_gb": 25, "rental_type": "demand"},
            "availability": {"available_gpus": 2}, "template": {"id": 457, "name": "ComfyUI"}, "volumes": [],
            "note": "Estimated"}
CREATED = {"name": "p", "workspace": "ws", "transactionId": 8, "estimate": ESTIMATE}
UPDATE_ESTIMATE = {"podName": "p-0", "name": "p", "changes": ["ports: open 9000"], "restarts": True,
                   "pricePerHourUsd": "0.068", "currentPricePerHourUsd": "0.068"}
UPDATED = {"podName": "p-0", "workspace": "ws", "accepted": True, "transactionId": 9}

TEMPLATE_PATH, CAPABILITIES_PATH, POD_PATH = "/v1/sdk/templates/457", "/v1/capabilities", "/v1/sdk/pods/p-0"

# The pod the update tests change. 8188 is public and locked, 8080 is public and was unlocked on purpose, 9100 is
# internal.
POD = {"podName": "p-0", "namespaceName": "ws", "userAlias": "p", "status": "running", "template": {"endpoints": [
    {"userAlias": "ComfyUI", "portType": "connect", "containerPort": 8188, "isExternal": True, "isReady": True,
     "externalUrl": "https://0123456789abcdef.dev.meshive.ai", "endpointName": "a3f9", "isLocked": True,
     "lockDecidedAt": "2026-11-14T05:00:00Z", "accessKeyCount": 2},
    {"userAlias": "ide", "portType": "http", "containerPort": 8080, "isExternal": True, "isReady": True,
     "externalUrl": "https://fedcba9876543210.dev.meshive.ai", "endpointName": "b1c2", "isLocked": False,
     "lockDecidedAt": None, "accessKeyCount": 0},
    {"userAlias": "metrics", "portType": "http", "containerPort": 9100, "isExternal": False, "isReady": True,
     "internalUrl": "http://p-0.ws.svc:9100", "endpointName": "c3d4", "isLocked": False, "accessKeyCount": 0}]}}
OLD_POD = {**POD, "template": {"endpoints": [
    {k: v for k, v in e.items() if k not in ("endpointName", "isLocked", "lockDecidedAt", "accessKeyCount")}
    for e in POD["template"]["endpoints"]]}}
LOCK_PATH = "/v1/sdk/pods/p-0/ports/3000/lock"
KEYS_PATH = "/v1/sdk/pods/p-0/ports/3000/keys"


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_new_names_are_exported():
    import meshive

    for name in ("PortLock", "PortPreflight", "BrowserSessionsRevoked", "AccessKey", "AccessKeyCreated",
                 "EndpointLockUnavailableError", "PortNotLockedError"):
        assert name in meshive.__all__ and getattr(meshive, name).__name__ == name


class Api:
    """MockTransport handler: one answer per (method, path), every request recorded. An answer is (status, json), a
    callable, or a list of those used in order (the last one repeats)."""

    def __init__(self, routes=None, *, template=TEMPLATE, capabilities=CAPABILITIES):
        self.routes = {("GET", TEMPLATE_PATH): (200, template), ("GET", CAPABILITIES_PATH): (200, capabilities),
                       ("POST", "/v1/sdk/pods/estimate"): (200, ESTIMATE), ("POST", "/v1/sdk/pods"): (202, CREATED),
                       ("POST", "/v1/sdk/pods/p-0/estimate"): (200, UPDATE_ESTIMATE),
                       ("PATCH", "/v1/sdk/pods/p-0"): (202, UPDATED), ("GET", POD_PATH): (200, POD),
                       **(routes or {})}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            raise AssertionError(f"unexpected {request.method} {request.url}")
        answer = self.routes[key]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if callable(answer):
            return answer(request)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer[0], json=answer[1])

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]

    def body(self, method: str, path: str, index: int = -1) -> dict:
        return _json(self.sent(method, path)[index])

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def _locks(body: dict) -> dict:
    """{port: locked} for every port a body carries a lock value for (ports and templatePorts)."""
    found = {e["port"]: e["locked"] for e in body.get("ports", []) if "locked" in e}
    found.update({e["port"]: e["locked"] for e in body.get("templatePorts", []) if "locked" in e})
    return found


# =================================================================================================
# create_pod / estimate_pod: locked by default
# =================================================================================================

@pytest.mark.parametrize("method, path", [("create_pod", "/v1/sdk/pods"), ("estimate_pod", "/v1/sdk/pods/estimate")])
def test_new_pods_send_a_lock_for_every_exposed_port(method, path):
    api = Api()
    result = getattr(sync_client(api), method)("p", 457, workspace="ws", ports=[
        7000, {"port": 7001, "name": "tb"}, {"port": 7002, "external": False}])
    body = api.body("POST", path)
    # Ports opened by the caller: exposed ones are locked, the internal one carries no lock value.
    assert body["ports"] == [{"port": 7000, "external": True, "locked": True},
                             {"port": 7001, "external": True, "name": "tb", "locked": True},
                             {"port": 7002, "external": False}]
    # Ports the template opens: the author's choice where there is one, locked where there is none, and nothing for
    # the internal port. The server never applies these defaults itself, so each exposed port must be listed.
    assert body["templatePorts"] == [{"port": 8188, "locked": True}, {"port": 8080, "locked": False},
                                     {"port": 9000, "locked": True}]
    assert result.port_locks == {8188: True, 8080: False, 9000: True, 7000: True, 7001: True}
    # The template is read with the workspace (a custom template needs it), then the server's capabilities, then
    # the write: three requests, in that order.
    assert api.paths() == [("GET", TEMPLATE_PATH), ("GET", CAPABILITIES_PATH), ("POST", path)]
    assert api.requests[0].url.params["workspace"] == "ws"


def test_lock_public_ports_false_sends_every_exposed_port_as_unlocked():
    api = Api()
    created = sync_client(api).create_pod("p", 457, workspace="ws", ports=[7000, {"port": 7002, "external": False}],
                                          lock_public_ports=False)
    body = api.body("POST", "/v1/sdk/pods")
    assert body["ports"] == [{"port": 7000, "external": True, "locked": False}, {"port": 7002, "external": False}]
    assert body["templatePorts"] == [{"port": 8188, "locked": False}, {"port": 8080, "locked": False},
                                     {"port": 9000, "locked": False}]
    assert created.port_locks == {8188: False, 8080: False, 9000: False, 7000: False}
    # Nothing is to be locked, so the server isn't asked whether it can lock.
    assert not api.sent("GET", CAPABILITIES_PATH)


def test_a_port_can_choose_its_own_lock():
    api = Api()
    created = sync_client(api).create_pod(
        "p", 457, workspace="ws",
        ports=[{"port": 7000, "locked": False}, {"port": 7001, "locked": True}],
        template_ports=[{"port": 8188, "locked": False},                  # the template's undecided port: open
                        {"port": 8080, "locked": True},                   # the author's "unlocked": locked anyway
                        {"port": 9000, "external": False},                # hidden: no lock value
                        {"port": 9100, "external": True, "new_port": 9101}],   # exposed here: locked by default
        lock_public_ports=True)
    body = api.body("POST", "/v1/sdk/pods")
    assert body["ports"] == [{"port": 7000, "external": True, "locked": False},
                             {"port": 7001, "external": True, "locked": True}]
    assert body["templatePorts"] == [{"port": 8188, "locked": False}, {"port": 8080, "locked": True},
                                     {"port": 9000, "external": False},
                                     {"port": 9100, "newPort": 9101, "external": True, "locked": True}]
    # A renumbered port is listed under the number the pod will have.
    assert created.port_locks == {8188: False, 8080: True, 9101: True, 7000: False, 7001: True}


def test_per_port_lock_wins_over_lock_public_ports_false():
    api = Api()
    sync_client(api).create_pod("p", 457, workspace="ws", ports=[{"port": 7000, "locked": True}, 7001],
                                template_ports=[{"port": 8080, "locked": True}], lock_public_ports=False)
    body = api.body("POST", "/v1/sdk/pods")
    assert _locks(body) == {7000: True, 7001: False, 8188: False, 8080: True, 9000: False}
    assert api.sent("GET", CAPABILITIES_PATH)          # a lock is sent, so the server must be able to keep it


def test_a_lock_on_an_internal_port_is_left_to_the_server():
    api = Api({("POST", "/v1/sdk/pods"): (400, {"detail": {"title": "Not A Public Endpoint",
                                                          "message": "Port 7002 is internal (external=false), so it "
                                                                     "can't be locked.",
                                                          "code": "endpoint_not_public", "port": 7002}})})
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(api).create_pod("p", 457, workspace="ws", ports=[{"port": 7002, "external": False,
                                                                       "locked": True}])
    assert err.value.title == "Not A Public Endpoint"
    assert api.body("POST", "/v1/sdk/pods")["ports"] == [{"port": 7002, "external": False, "locked": True}]


def test_a_template_without_ports_and_no_ports_sends_no_lock_and_asks_nothing_more():
    api = Api(template={"id": 457, "name": "bare", "endpoints": []})
    created = sync_client(api).create_pod("p", 457, workspace="ws")
    body = api.body("POST", "/v1/sdk/pods")
    assert "templatePorts" not in body and body["ports"] == [] and created.port_locks == {}
    assert api.paths() == [("GET", TEMPLATE_PATH), ("POST", "/v1/sdk/pods")]


@pytest.mark.parametrize("kwargs, match", [
    ({"lock_public_ports": "no"}, "lock_public_ports"),
    ({"lock_public_ports": None}, "lock_public_ports"),
    ({"ports": [{"port": 7000, "locked": "false"}]}, r"ports\[\]\.locked"),
    ({"template_ports": [{"port": 8188, "locked": 0}]}, r"template_ports\[\]\.locked"),
])
def test_lock_values_must_be_real_booleans(kwargs, match):
    api = Api()
    with pytest.raises(ValueError, match=match):
        sync_client(api).create_pod("p", 457, workspace="ws", **kwargs)
    assert not api.requests                       # refused before anything was read or sent


def test_a_missing_template_fails_before_the_create():
    api = Api({("GET", TEMPLATE_PATH): (404, {"detail": {"title": "Not Found", "message": "Template not found"}})})
    with pytest.raises(NotFoundError, match="Template not found"):
        sync_client(api).create_pod("p", 457, workspace="ws")
    assert not api.sent("POST", "/v1/sdk/pods")


# =================================================================================================
# Servers that can't lock: nothing is created, and never created open instead
# =================================================================================================

@pytest.mark.parametrize("capabilities", [
    (200, OLD_CAPABILITIES),                                   # the server knows capabilities, not locks
    (200, {"schemaVersion": 1}),                               # no pods block at all
    (404, {"detail": "Not Found"}),                            # a server without the route
    (200, ["unexpected"]),
])
@pytest.mark.parametrize("method", ["create_pod", "estimate_pod"])
def test_a_server_that_predates_locks_is_not_sent_a_locked_pod(capabilities, method):
    # Such a server would accept the request, drop `locked` without a word and expose the ports to everyone.
    api = Api({("GET", CAPABILITIES_PATH): capabilities}, template=OLD_TEMPLATE)
    with pytest.raises(EndpointLockUnavailableError) as err:
        getattr(sync_client(api), method)("p", 457, workspace="ws", ports=[7000])
    assert err.value.reason == "unsupported" and err.value.status_code == 409
    assert isinstance(err.value, ConflictError)
    assert "lock_public_ports=False" in str(err.value) and "nothing was sent" in str(err.value)
    assert not [p for p in api.paths() if p[0] == "POST"]


def test_a_server_that_predates_locks_still_creates_open_pods_when_asked_to():
    api = Api({("GET", CAPABILITIES_PATH): (200, OLD_CAPABILITIES)}, template=OLD_TEMPLATE)
    created = sync_client(api).create_pod("p", 457, workspace="ws", ports=[7000], lock_public_ports=False)
    assert created.transaction_id == 8 and not any(created.port_locks.values())
    assert _locks(api.body("POST", "/v1/sdk/pods")) == {7000: False, 8188: False, 8080: False, 9000: False}


def test_an_old_template_payload_is_read_as_no_author_choice():
    # No isLocked key on the template's ports: every exposed port is locked, as when the author chose nothing.
    api = Api(template=OLD_TEMPLATE)
    sync_client(api).create_pod("p", 457, workspace="ws")
    assert api.body("POST", "/v1/sdk/pods")["templatePorts"] == [
        {"port": 8188, "locked": True}, {"port": 8080, "locked": True}, {"port": 9000, "locked": True}]


def test_a_failing_capability_check_stops_the_create(slept):
    api = Api({("GET", CAPABILITIES_PATH): (500, {"detail": "Internal Server Error"})})
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(api).create_pod("p", 457, workspace="ws", ports=[7000])
    assert err.value.status_code == 500 and not isinstance(err.value, EndpointLockUnavailableError)
    assert not api.sent("POST", "/v1/sdk/pods")


@pytest.mark.parametrize("method, path, capabilities", [
    ("create_pod", "/v1/sdk/pods", SWITCHED_OFF),
    ("estimate_pod", "/v1/sdk/pods/estimate", SWITCHED_OFF),
    ("create_pod", "/v1/sdk/pods", CAPABILITIES),       # the switch went off between the check and the write
])
def test_locking_turned_off_refuses_the_pod_and_says_what_to_do(slept, method, path, capabilities):
    api = Api({("POST", path): (409, LOCK_DISABLED)}, capabilities=capabilities)
    with pytest.raises(EndpointLockUnavailableError) as err:
        getattr(sync_client(api), method)("p", 457, workspace="ws", ports=[7000])
    assert err.value.reason == "disabled" and err.value.title == "Endpoint Lock Unavailable"
    assert err.value.raw == LOCK_DISABLED                            # the server's answer is kept
    message = str(err.value)
    assert "lock_public_ports=False" in message and "locked=False" in message and "try again later" in message
    assert "nothing was created" in message
    # One write, not resent with the ports open, and its identity is kept for a lookup.
    assert len(api.sent("POST", path)) == 1 and slept == []
    assert err.value.idempotency_key == api.sent("POST", path)[0].headers["Idempotency-Key"]


def test_other_conflicts_are_not_mistaken_for_a_lock_refusal():
    busy = {"detail": {"title": "No Capacity", "message": "No machine has room.", "code": "no_capacity"}}
    api = Api({("POST", "/v1/sdk/pods"): (409, busy)})
    with pytest.raises(ConflictError) as err:
        sync_client(api).create_pod("p", 457, workspace="ws", ports=[7000])
    assert not isinstance(err.value, EndpointLockUnavailableError) and err.value.title == "No Capacity"


# =================================================================================================
# update_pod: the ports a change newly makes public are locked; ports that are public already are left alone
# =================================================================================================

UPDATES = [("update_pod", "PATCH", "/v1/sdk/pods/p-0"), ("estimate_pod_update", "POST", "/v1/sdk/pods/p-0/estimate")]


@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_locks_a_new_public_port(method, verb, path):
    api = Api()
    result = getattr(sync_client(api), method)("p-0", "ws", ports=[9000, {"port": 9001, "external": False}])
    assert api.body(verb, path)["ports"] == [{"port": 9000, "external": True, "locked": True},
                                             {"port": 9001, "external": False}]
    assert result.port_locks == {9000: True}
    # A port in `ports` is new by definition, so the pod isn't read: only the check that the server can lock.
    assert api.paths() == [("GET", CAPABILITIES_PATH), (verb, path)]


@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_locks_an_internal_port_it_exposes(method, verb, path):
    api = Api()
    result = getattr(sync_client(api), method)("p-0", "ws", template_ports=[{"port": 9100, "external": True}])
    assert api.body(verb, path)["templatePorts"] == [{"port": 9100, "external": True, "locked": True}]
    assert result.port_locks == {9100: True}
    # The pod is read to learn that 9100 is internal today.
    assert api.paths() == [("GET", POD_PATH), ("GET", CAPABILITIES_PATH), (verb, path)]
    assert api.sent("GET", POD_PATH)[0].url.params["workspace"] == "ws"


@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_never_adds_a_lock_to_a_port_that_is_public_already(method, verb, path):
    # 8188 is locked, 8080 was unlocked on purpose. Saying "external" again, renaming or renumbering them must not
    # send a lock value: a default would lock 8080 again behind the user's back.
    api = Api()
    result = getattr(sync_client(api), method)("p-0", "ws", template_ports=[
        {"port": 8188, "external": True}, {"port": 8080, "external": True, "name": "editor"}])
    body = api.body(verb, path)
    assert body["templatePorts"] == [{"port": 8188, "external": True}, {"port": 8080, "external": True,
                                                                         "name": "editor"}]
    assert "locked" not in json.dumps(body) and result.port_locks == {}
    assert api.paths() == [("GET", POD_PATH), (verb, path)]         # nothing to lock: no capability check
    # The same with lock_public_ports=False: it must not unlock the locked port either.
    api = Api()
    getattr(sync_client(api), method)("p-0", "ws", template_ports=[{"port": 8188, "external": True}],
                                      lock_public_ports=False)
    assert "locked" not in json.dumps(api.body(verb, path))


@pytest.mark.parametrize("kwargs", [
    {"name": "renamed"},
    {"env": {"A": "1"}},
    {"gpu_model": "RTX 4090"},
    {"remove_ports": [7000]},
    {"ports": [{"port": 9001, "external": False}]},                              # a new port, kept internal
    {"template_ports": [{"port": 8080, "name": "editor"}]},                      # a rename
    {"template_ports": [{"port": 8188, "new_port": 8189}]},                      # a renumber: the lock follows
    {"template_ports": [{"port": 8080, "external": False}]},                     # hiding a port
    {"command": "sleep infinity", "detach_storages": ["pv-a"]},
])
@pytest.mark.parametrize("method, verb, path", UPDATES)
@pytest.mark.parametrize("lock", [True, False])
def test_an_update_that_makes_nothing_public_is_sent_as_before(kwargs, method, verb, path, lock):
    api = Api()
    result = getattr(sync_client(api), method)("p-0", "ws", lock_public_ports=lock, **kwargs)
    assert "locked" not in json.dumps(api.body(verb, path)) and result.port_locks == {}
    assert api.paths() == [(verb, path)]              # one request, as in 1.9: nothing is read first


@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_to_another_template_locks_the_public_ports_it_adds(method, verb, path):
    # The new template exposes 8188, 8080 and 9000. The pod serves 8188 and 8080 publicly already, so those keep
    # their locks (8080 stays unlocked); 9000 is new and follows the template author's choice (locked).
    api = Api()
    result = getattr(sync_client(api), method)("p-0", "ws", template_id=457)
    assert api.body(verb, path)["templatePorts"] == [{"port": 9000, "locked": True}]
    assert result.port_locks == {9000: True}
    assert api.paths() == [("GET", POD_PATH), ("GET", TEMPLATE_PATH), ("GET", CAPABILITIES_PATH), (verb, path)]
    assert api.sent("GET", TEMPLATE_PATH)[0].url.params["workspace"] == "ws"


def test_update_to_another_template_follows_the_authors_choice_for_new_ports():
    # A pod with no public port yet: every exposed port of the new template is new. 8188 (no choice) and 9000
    # (locked) are locked, 8080 (the author chose unlocked) is sent as unlocked, and an internal port the call
    # exposes is locked.
    bare = {**POD, "template": {"endpoints": [POD["template"]["endpoints"][2]]}}
    api = Api({("GET", POD_PATH): (200, bare)})
    updated = sync_client(api).update_pod("p-0", "ws", template_id=457,
                                          template_ports=[{"port": 9100, "external": True, "new_port": 9101}])
    assert api.body("PATCH", POD_PATH)["templatePorts"] == [
        {"port": 9100, "newPort": 9101, "external": True, "locked": True}, {"port": 8188, "locked": True},
        {"port": 8080, "locked": False}, {"port": 9000, "locked": True}]
    assert updated.port_locks == {9101: True, 8188: True, 8080: False, 9000: True}
    # A template port renumbered onto a number the pod already serves publicly keeps that port's lock.
    api = Api()
    sync_client(api).update_pod("p-0", "ws", template_id=457, template_ports=[{"port": 9000, "new_port": 8080}])
    assert api.body("PATCH", POD_PATH)["templatePorts"] == [{"port": 9000, "newPort": 8080}]


def test_update_a_ports_own_lock_wins():
    api = Api()
    updated = sync_client(api).update_pod(
        "p-0", "ws", ports=[{"port": 9000, "locked": False}, 9001],
        template_ports=[{"port": 9100, "external": True, "locked": False},     # exposed, and left open on purpose
                        {"port": 8080, "locked": True},                        # public already: locked because named
                        {"port": 8188, "locked": False}])                      # public already: unlocked because named
    body = api.body("PATCH", POD_PATH)
    assert body["ports"] == [{"port": 9000, "external": True, "locked": False},
                             {"port": 9001, "external": True, "locked": True}]
    assert body["templatePorts"] == [{"port": 9100, "external": True, "locked": False},
                                     {"port": 8080, "locked": True}, {"port": 8188, "locked": False}]
    assert updated.port_locks == {9100: False, 8080: True, 8188: False, 9000: False, 9001: True}
    # Every lock is spelled out, so the pod needn't be read; a lock is sent, so the server is checked.
    assert api.paths() == [("GET", CAPABILITIES_PATH), ("PATCH", POD_PATH)]
    # A lock-only change of a port the pod already exposes: exactly that, and nothing read when it only unlocks.
    api = Api()
    sync_client(api).update_pod("p-0", "ws", template_ports=[{"port": 8188, "locked": False}])
    assert api.body("PATCH", POD_PATH) == {"templatePorts": [{"port": 8188, "locked": False}]}
    assert api.paths() == [("PATCH", POD_PATH)]


@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_lock_public_ports_false_leaves_new_public_ports_open(method, verb, path):
    api = Api()
    result = getattr(sync_client(api), method)(
        "p-0", "ws", ports=[9000, {"port": 9001, "locked": True}],
        template_ports=[{"port": 9100, "external": True}, {"port": 8188, "external": True}], lock_public_ports=False)
    body = api.body(verb, path)
    assert body["ports"] == [{"port": 9000, "external": True, "locked": False},
                             {"port": 9001, "external": True, "locked": True}]          # its own value wins
    # 9100 becomes public: sent as unlocked. 8188 is public (and locked) already: no value, so it stays locked.
    assert body["templatePorts"] == [{"port": 9100, "external": True, "locked": False},
                                     {"port": 8188, "external": True}]
    assert result.port_locks == {9100: False, 9000: False, 9001: True}
    api = Api()
    getattr(sync_client(api), method)("p-0", "ws", template_id=457, lock_public_ports=False)
    assert api.body(verb, path)["templatePorts"] == [{"port": 9000, "locked": False}]
    assert api.paths() == [("GET", POD_PATH), ("GET", TEMPLATE_PATH), (verb, path)]     # nothing to lock: no check


def test_update_lock_arguments_are_checked_before_sending():
    api = Api()
    client = sync_client(api)
    for alone in (True, False):
        with pytest.raises(ValueError, match="at least one change"):
            client.update_pod("p-0", "ws", lock_public_ports=alone)       # a lock default alone changes nothing
    for bad in ("yes", None, 1):
        with pytest.raises(ValueError, match="lock_public_ports"):
            client.update_pod("p-0", "ws", ports=[9000], lock_public_ports=bad)
    with pytest.raises(ValueError, match=r"ports\[\]\.locked"):
        client.update_pod("p-0", "ws", ports=[{"port": 9000, "locked": "true"}])
    with pytest.raises(ValueError, match=r"template_ports\[\]\.locked"):
        client.update_pod("p-0", "ws", template_ports=[{"port": 8188, "locked": 1}])
    assert not api.requests


@pytest.mark.parametrize("pod_answer", [(500, {"detail": "Internal Server Error"}),
                                        (404, {"detail": {"title": "Pod Not Found", "message": "No pod named p-0."}}),
                                        httpx.ConnectError("refused")])
@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_does_not_guess_when_the_pod_cannot_be_read(slept, pod_answer, method, verb, path):
    # Without the pod's ports the SDK can't tell a new public port from one that is public already. It sends
    # nothing rather than send the port open.
    api = Api({("GET", POD_PATH): pod_answer})
    with pytest.raises((MeshiveAPIError, httpx.HTTPError)):
        getattr(sync_client(api), method)("p-0", "ws", template_ports=[{"port": 9100, "external": True}])
    with pytest.raises((MeshiveAPIError, httpx.HTTPError)):
        getattr(sync_client(api), method)("p-0", "ws", template_id=457)
    assert not api.sent(verb, path)


def test_update_treats_a_pod_without_listed_ports_as_having_none_public():
    # No port is listed as public, so the exposed port counts as new and is locked (the closed side).
    api = Api({("GET", POD_PATH): (200, {"podName": "p-0", "namespaceName": "ws", "status": "stopped"})})
    sync_client(api).update_pod("p-0", "ws", template_ports=[{"port": 8188, "external": True}])
    assert api.body("PATCH", POD_PATH)["templatePorts"] == [{"port": 8188, "external": True, "locked": True}]


@pytest.mark.parametrize("kwargs", [{"ports": [9000]}, {"template_ports": [{"port": 9100, "external": True}]},
                                    {"template_id": 457}, {"template_ports": [{"port": 8080, "locked": True}]}])
@pytest.mark.parametrize("method, verb, path", UPDATES)
def test_update_that_locks_is_refused_like_a_create(method, verb, path, kwargs):
    # A server that predates locks: nothing is sent.
    api = Api({("GET", CAPABILITIES_PATH): (200, OLD_CAPABILITIES)})
    with pytest.raises(EndpointLockUnavailableError) as err:
        getattr(sync_client(api), method)("p-0", "ws", **kwargs)
    assert err.value.reason == "unsupported" and "lock_public_ports=False" in str(err.value)
    assert "nothing was sent" in str(err.value) and not api.sent(verb, path)
    # Locking turned off: the server's 409, said in the SDK's terms, and not resent with the port open.
    api = Api({(verb, path): (409, LOCK_DISABLED)})
    with pytest.raises(EndpointLockUnavailableError) as err:
        getattr(sync_client(api), method)("p-0", "ws", **kwargs)
    assert err.value.reason == "disabled" and "nothing was changed" in str(err.value)
    assert "lock_public_ports=False" in str(err.value) and len(api.sent(verb, path)) == 1


def test_update_with_open_ports_goes_through_on_a_server_that_cannot_lock():
    api = Api({("GET", CAPABILITIES_PATH): (200, OLD_CAPABILITIES)})
    updated = sync_client(api).update_pod("p-0", "ws", ports=[9000], lock_public_ports=False)
    assert updated.port_locks == {9000: False} and api.paths() == [("PATCH", POD_PATH)]
    # ...and so does a change that makes nothing public.
    sync_client(api).update_pod("p-0", "ws", env={"A": "1"})
    assert len(api.sent("PATCH", POD_PATH)) == 2 and not api.sent("GET", CAPABILITIES_PATH)


def test_update_on_a_server_without_pod_editing_still_says_so():
    api = Api({("PATCH", "/v1/sdk/pods/p-0"): (405, {"detail": "Method Not Allowed"})})
    with pytest.raises(NotFoundError, match="does not support pod editing"):
        sync_client(api).update_pod("p-0", "ws", env={"A": "1"})


# =================================================================================================
# Reading a pod's locks
# =================================================================================================

def test_pod_endpoints_carry_their_lock():
    comfy, ide, metrics = Pod.from_dict(POD).endpoints
    assert (comfy.locked, comfy.access, comfy.access_key_count) == (True, "locked", 2)
    assert comfy.lock_decided_at.isoformat() == "2026-11-14T05:00:00+00:00"
    assert (ide.locked, ide.access, ide.lock_decided_at, ide.access_key_count) == (False, "unlocked", None, 0)
    assert (metrics.access, metrics.is_external) == ("internal", False)


def test_a_pod_from_a_server_without_locks_still_parses():
    # The new fields are simply missing: None, never a made-up False or 0. Such a server has no locks, so an
    # exposed port reads as unlocked.
    comfy, ide, metrics = Pod.from_dict(OLD_POD).endpoints
    for endpoint in (comfy, ide, metrics):
        assert endpoint.locked is None and endpoint.lock_decided_at is None and endpoint.access_key_count is None
    assert (comfy.access, ide.access, metrics.access) == ("unlocked", "unlocked", "internal")
    assert comfy.readiness == "ready" and comfy.external_url.startswith("https://")
    # Odd values don't become a lock either.
    assert PodEndpoint.from_dict({"containerPort": 1, "isExternal": True, "isLocked": "yes",
                                  "accessKeyCount": True}).locked is None


def _pods_api(pod):
    return Api({("GET", "/v1/sdk/pods"): (200, {"pods": [pod]}), ("GET", "/v1/sdk/pods/p-0"): (200, pod)})


def test_wait_for_new_pod_checks_the_locks_that_were_asked_for():
    client = sync_client(_pods_api(POD))
    assert client.wait_for_new_pod("p", "ws", port_locks={8188: True, 8080: False}).pod_name == "p-0"
    assert client.wait_for_new_pod("p", "ws").pod_name == "p-0"                    # no check without port_locks
    # A port the pod doesn't list, or keeps internal, can't be reached from outside: not a mismatch.
    assert client.wait_for_new_pod("p", "ws", port_locks={7777: True, 9100: True}).pod_name == "p-0"
    with pytest.raises(PortNotLockedError) as err:
        client.wait_for_new_pod("p", "ws", port_locks={8188: True, 8080: True})
    assert err.value.ports == [8080] and err.value.pod_name == "p-0" and err.value.pod.pod_name == "p-0"
    assert "set_port_lock" in str(err.value) and "8080" in str(err.value)


def test_wait_for_new_pod_treats_an_unreported_lock_as_not_locked():
    # A server that dropped the lock doesn't report one either: the port that should be locked is open.
    with pytest.raises(PortNotLockedError) as err:
        sync_client(_pods_api(OLD_POD)).wait_for_new_pod("p", "ws", port_locks={8188: True})
    assert err.value.ports == [8188]


# =================================================================================================
# set_port_lock
# =================================================================================================

@pytest.mark.parametrize("locked", [True, False])
def test_set_port_lock_request_and_result(locked):
    api = Api({("PATCH", LOCK_PATH): (200, {"port": 3000, "locked": locked})})
    result = sync_client(api).set_port_lock("p-0", "ws", 3000, locked, idempotency_key="lock-1")
    request = api.sent("PATCH", LOCK_PATH)[0]
    assert request.url.params["workspace"] == "ws" and _json(request) == {"locked": locked}
    assert request.headers["Idempotency-Key"] == "lock-1"
    assert isinstance(result, PortLock) and (result.port, result.locked, result.replayed) == (3000, locked, False)


@pytest.mark.parametrize("args", [("p-0", "ws", 3000, "false"), ("p-0", "ws", 3000, 1), ("p-0", "ws", 3000, None),
                                  ("p-0", "ws", 0, True), ("p-0", "ws", "3000", True), ("p-0", "ws", True, True),
                                  ("p-0", "ws", 70000, True), ("", "ws", 3000, True), ("p-0", " ", 3000, True)])
def test_set_port_lock_checks_arguments_before_sending(args):
    api = Api()
    with pytest.raises(ValueError):
        sync_client(api).set_port_lock(*args)
    assert not api.requests


def test_set_port_lock_is_resent_after_a_timeout(slept):
    # Setting a lock twice ends in the same state, and the request carries an Idempotency-Key: safe to resend.
    api = Api({("PATCH", LOCK_PATH): [lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("no answer", request=r)),
                                      (200, {"port": 3000, "locked": True})]})
    assert sync_client(api).set_port_lock("p-0", "ws", 3000, True).locked is True
    sent = api.sent("PATCH", LOCK_PATH)
    assert len(sent) == 2 and sent[0].headers["Idempotency-Key"] == sent[1].headers["Idempotency-Key"]


def test_port_routes_on_servers_that_cannot_lock():
    # A server without the routes: FastAPI's bare 404.
    api = Api({("PATCH", LOCK_PATH): (404, {"detail": "Not Found"}), ("GET", KEYS_PATH): (404, {"detail": "Not Found"}),
               ("POST", KEYS_PATH): (405, {"detail": "Method Not Allowed"})})
    client = sync_client(api)
    for call in (lambda: client.set_port_lock("p-0", "ws", 3000, True),
                 lambda: client.list_access_keys("p-0", "ws", 3000),
                 lambda: client.create_access_key("p-0", "ws", 3000, "ci")):
        with pytest.raises(NotFoundError, match="does not support port locks and access keys yet"):
            call()
    # A server that has them, with locking turned off: its 404 carries the code.
    off = {"detail": {"title": "Not Found", "message": "Endpoint lock is not available.",
                      "code": "endpoint_lock_disabled"}}
    api = Api({("PATCH", LOCK_PATH): (404, off), ("POST", KEYS_PATH): (404, off),
               ("POST", f"{KEYS_PATH}/12/roll"): (404, off)})
    client = sync_client(api)
    for call in (lambda: client.set_port_lock("p-0", "ws", 3000, True),
                 lambda: client.create_access_key("p-0", "ws", 3000, "ci"),
                 lambda: client.roll_access_key("p-0", "ws", 3000, 12)):
        with pytest.raises(NotFoundError, match="turned off on this Meshive server right now") as err:
            call()
        assert err.value.raw == off
    # A real "not found" keeps the server's words.
    missing = {"detail": {"title": "Port Not Found", "message": "Pod 'p-0' doesn't open port 3000."}}
    with pytest.raises(NotFoundError, match="doesn't open port 3000"):
        sync_client(Api({("PATCH", LOCK_PATH): (404, missing)})).set_port_lock("p-0", "ws", 3000, True)


# =================================================================================================
# Access keys: the value is returned once and shows up nowhere else
# =================================================================================================

def test_list_access_keys():
    listed = {"keys": [KEY_ITEM, {**ROLLED, "accessKey": None}, {"id": 40, "keyName": "old", "keyPrefix": "mshv_x"}]}
    api = Api({("GET", KEYS_PATH): (200, listed)})
    first, rolled, sparse = sync_client(api).list_access_keys("p-0", "ws", 3000)
    assert api.sent("GET", KEYS_PATH)[0].url.params["workspace"] == "ws"
    assert isinstance(first, AccessKey) and not isinstance(first, AccessKeyCreated)
    assert (first.id, first.name, first.prefix, first.port, first.port_name) == (12, "batch-script", "mshv_Zq7Zq7Zq",
                                                                                 3000, "console")
    assert (first.pod_name, first.created_by) == ("p-0", "dev@example.com")
    assert first.expires_at.isoformat() == "2027-01-31T09:00:00+00:00" and first.previous_value_valid_until is None
    assert rolled.previous_value_valid_until.isoformat() == "2026-11-14T15:00:00+00:00"
    assert not hasattr(first, "access_key")                 # a listed key has no place for a value
    # A sparse item from another server version still parses; what is missing is None.
    assert (sparse.id, sparse.port, sparse.expires_at, sparse.created_at) == (40, None, None, None)
    assert sync_client(Api({("GET", KEYS_PATH): (200, {})})).list_access_keys("p-0", "ws", 3000) == []


def test_create_access_key_request_and_result():
    api = Api({("POST", KEYS_PATH): (201, {**KEY_ITEM, "accessKey": KEY})})
    key = sync_client(api).create_access_key("p-0", "ws", 3000, "  batch-script ")
    request = api.sent("POST", KEYS_PATH)[0]
    assert request.url.params["workspace"] == "ws"
    assert _json(request) == {"name": "batch-script", "expiresInDays": 90}        # 90 days unless asked otherwise
    assert isinstance(key, AccessKeyCreated) and isinstance(key, AccessKey)
    assert key.access_key == KEY and (key.id, key.name, key.port) == (12, "batch-script", 3000)


def test_the_key_value_is_only_in_access_key():
    api = Api({("POST", KEYS_PATH): (201, {**KEY_ITEM, "accessKey": KEY}),
               ("POST", f"{KEYS_PATH}/12/roll"): (201, ROLLED)})
    client = sync_client(api)
    for key in (client.create_access_key("p-0", "ws", 3000, "ci"), client.roll_access_key("p-0", "ws", 3000, 12)):
        assert key.access_key == KEY
        assert KEY not in repr(key) and KEY not in str(key) and KEY not in f"{key}" and KEY not in f"{key!r}"
        assert "accessKey" not in key.raw and KEY not in json.dumps(key.raw)
        assert KEY not in repr([key]) and KEY not in repr({"key": key})
        assert key.prefix in repr(key)                      # the prefix is there to tell keys apart


@pytest.mark.parametrize("days, sent", [(30, 30), (365, 365), (0, 0), (7, 7)])
def test_access_key_lifetimes_are_the_servers_rule(days, sent):
    # 0 is a key that never expires. Which other values exist is the server's call (7 comes back as a 422).
    api = Api({("POST", KEYS_PATH): (201, {**KEY_ITEM, "accessKey": KEY})})
    sync_client(api).create_access_key("p-0", "ws", 3000, "ci", expires_in_days=days)
    assert api.body("POST", KEYS_PATH)["expiresInDays"] == sent


@pytest.mark.parametrize("kwargs", [{"name": " "}, {"name": None}, {"name": 3},
                                    {"name": "ci", "expires_in_days": None},      # never read as "never expires"
                                    {"name": "ci", "expires_in_days": True}, {"name": "ci", "expires_in_days": "90"},
                                    {"name": "ci", "expires_in_days": -1}, {"name": "ci", "expires_in_days": 1.5}])
def test_create_access_key_checks_arguments_before_sending(kwargs):
    api = Api()
    name = kwargs.pop("name")
    with pytest.raises(ValueError):
        sync_client(api).create_access_key("p-0", "ws", 3000, name, **kwargs)
    assert not api.requests


def test_roll_access_key_request_and_result():
    api = Api({("POST", f"{KEYS_PATH}/12/roll"): (201, ROLLED)})
    client = sync_client(api)
    key = client.roll_access_key("p-0", "ws", 3000, 12)
    assert api.body("POST", f"{KEYS_PATH}/12/roll") == {"overlapSeconds": 3600}       # an hour unless asked otherwise
    assert api.sent("POST", f"{KEYS_PATH}/12/roll")[0].url.params["workspace"] == "ws"
    assert (key.id, key.access_key) == (31, KEY)                                 # the rolled key has a new id
    assert key.previous_value_valid_until.isoformat() == "2026-11-14T15:00:00+00:00"
    client.roll_access_key("p-0", "ws", 3000, "12", overlap_seconds=0)
    assert api.body("POST", f"{KEYS_PATH}/12/roll") == {"overlapSeconds": 0}
    for bad in (None, True, "3600", -1):
        with pytest.raises(ValueError, match="overlap_seconds"):
            client.roll_access_key("p-0", "ws", 3000, 12, overlap_seconds=bad)
    with pytest.raises(ValueError, match="key_id"):
        client.roll_access_key("p-0", "ws", 3000, "../31")


def test_revoke_access_key():
    api = Api({("DELETE", f"{KEYS_PATH}/12"): (200, {"title": "Success", "message": "Access key revoked."})})
    action = sync_client(api).revoke_access_key("p-0", "ws", 3000, 12, idempotency_key="revoke-1")
    request = api.sent("DELETE", f"{KEYS_PATH}/12")[0]
    assert request.url.params["workspace"] == "ws" and request.headers["Idempotency-Key"] == "revoke-1"
    assert isinstance(action, ResourceAction)
    assert (action.resource, action.id, action.action, action.workspace) == ("access_key", "12", "revoke", "ws")
    gone = {"detail": {"title": "Not Found", "message": "Access key not found."}}
    with pytest.raises(NotFoundError, match="Access key not found"):
        sync_client(Api({("DELETE", f"{KEYS_PATH}/12"): (404, gone)})).revoke_access_key("p-0", "ws", 3000, 12)


def _timeout(request):
    raise httpx.ReadTimeout("no answer", request=request)


def _cut_off(request):
    raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)


@pytest.mark.parametrize("call, path", [
    (lambda c: c.create_access_key("p-0", "ws", 3000, "ci"), KEYS_PATH),
    (lambda c: c.roll_access_key("p-0", "ws", 3000, 12), f"{KEYS_PATH}/12/roll"),
])
@pytest.mark.parametrize("failure, raised", [
    (_timeout, httpx.ReadTimeout),                                          # the server may have issued the value
    (_cut_off, httpx.RemoteProtocolError),
    ((500, {"detail": "Internal Server Error"}), MeshiveAPIError),
    ((502, {"detail": "Bad Gateway"}), MeshiveAPIError),
    ((504, {"detail": "Gateway Timeout"}), MeshiveAPIError),
])
def test_issuing_a_key_is_never_sent_twice_once_the_server_may_have_started(slept, call, path, failure, raised):
    # The server keeps no record to replay: a second request would issue a second value.
    api = Api({("POST", path): [failure, (201, ROLLED)]})
    with pytest.raises(raised):
        call(sync_client(api))
    assert len(api.sent("POST", path)) == 1 and slept == []


@pytest.mark.parametrize("call, path", [
    (lambda c: c.create_access_key("p-0", "ws", 3000, "ci"), KEYS_PATH),
    (lambda c: c.roll_access_key("p-0", "ws", 3000, 12), f"{KEYS_PATH}/12/roll"),
])
@pytest.mark.parametrize("failure", [
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)),      # nothing was sent
    lambda r: (_ for _ in ()).throw(httpx.PoolTimeout("no connection", request=r)),
    (503, {"detail": {"title": "Busy", "message": "busy"}}),                         # refused before it started
    (429, {"detail": {"title": "Too Many Requests", "message": "slow down"}}),
])
def test_issuing_a_key_is_resent_only_when_it_surely_never_started(slept, call, path, failure):
    api = Api({("POST", path): [failure, (201, ROLLED)]})
    assert call(sync_client(api)).access_key == KEY
    assert len(api.sent("POST", path)) == 2 and slept == [0.5]


def test_errors_of_the_key_calls_carry_no_key_value():
    # The value exists only in a successful answer, so no error can hold it. Checked on the errors a caller sees.
    not_locked = {"detail": {"title": "Endpoint Not Locked", "message": "Lock the port before creating a key.",
                             "code": "endpoint_not_locked"}}
    api = Api({("POST", KEYS_PATH): [(400, not_locked), _timeout],
               ("POST", f"{KEYS_PATH}/12/roll"): (400, {"detail": {"title": "Too Many Keys", "message": "limit"}})})
    client = sync_client(api, max_retries=0)
    errors = []
    for call in (lambda: client.create_access_key("p-0", "ws", 3000, "ci"),
                 lambda: client.create_access_key("p-0", "ws", 3000, "ci"),
                 lambda: client.roll_access_key("p-0", "ws", 3000, 12)):
        with pytest.raises((MeshiveAPIError, httpx.HTTPError)) as err:
            call()
        errors.append(err.value)
    assert errors[0].title == "Endpoint Not Locked" and isinstance(errors[1], httpx.ReadTimeout)
    for error in errors:
        assert "mshv_" not in str(error) and "mshv_" not in repr(error)
        assert "mshv_" not in json.dumps(getattr(error, "raw", None))


# =================================================================================================
# Async client
# =================================================================================================

def test_async_create_and_estimate_lock_by_default():
    api = Api()

    async def run():
        async with async_client(api) as c:
            estimate = await c.estimate_pod("p", 457, workspace="ws", ports=[7000])
            created = await c.create_pod("p", 457, workspace="ws", ports=[7000, {"port": 7002, "external": False}])
            opened = await c.create_pod("p", 457, workspace="ws", ports=[7000], lock_public_ports=False)
            return estimate, created, opened

    estimate, created, opened = asyncio.run(run())
    assert estimate.port_locks == {8188: True, 8080: False, 9000: True, 7000: True} == created.port_locks
    body = api.body("POST", "/v1/sdk/pods", 0)
    assert body["ports"] == [{"port": 7000, "external": True, "locked": True}, {"port": 7002, "external": False}]
    assert body["templatePorts"] == [{"port": 8188, "locked": True}, {"port": 8080, "locked": False},
                                     {"port": 9000, "locked": True}]
    assert _locks(api.body("POST", "/v1/sdk/pods", 1)) == {7000: False, 8188: False, 8080: False, 9000: False}
    assert opened.port_locks == {8188: False, 8080: False, 9000: False, 7000: False}
    assert api.paths() == [("GET", TEMPLATE_PATH), ("GET", CAPABILITIES_PATH), ("POST", "/v1/sdk/pods/estimate"),
                           ("GET", TEMPLATE_PATH), ("GET", CAPABILITIES_PATH), ("POST", "/v1/sdk/pods"),
                           ("GET", TEMPLATE_PATH), ("POST", "/v1/sdk/pods")]


def test_async_servers_that_cannot_lock():
    old = Api({("GET", CAPABILITIES_PATH): (200, OLD_CAPABILITIES)}, template=OLD_TEMPLATE)
    off = Api({("POST", "/v1/sdk/pods"): (409, LOCK_DISABLED), ("PATCH", "/v1/sdk/pods/p-0"): (409, LOCK_DISABLED)})

    async def run():
        reasons = []
        async with async_client(old) as c:
            with pytest.raises(EndpointLockUnavailableError) as err:
                await c.create_pod("p", 457, workspace="ws", ports=[7000])
            reasons.append(err.value.reason)
        async with async_client(off) as c:
            for call in (c.create_pod("p", 457, workspace="ws", ports=[7000]),
                         c.update_pod("p-0", "ws", ports=[9000])):
                with pytest.raises(EndpointLockUnavailableError) as err:
                    await call
                reasons.append(err.value.reason)
        return reasons

    assert asyncio.run(run()) == ["unsupported", "disabled", "disabled"]
    assert not [p for p in old.paths() if p[0] == "POST"]


def test_async_update_locks_what_it_newly_makes_public():
    api = Api()

    async def run():
        async with async_client(api) as c:
            first = await c.update_pod("p-0", "ws", ports=[9000],
                                       template_ports=[{"port": 9100, "external": True},
                                                       {"port": 8080, "external": True}])
            estimate = await c.estimate_pod_update("p-0", "ws", template_id=457)
            await c.update_pod("p-0", "ws", env={"A": "1"})
            opened = await c.update_pod("p-0", "ws", ports=[9000], lock_public_ports=False)
            return first, estimate, opened

    first, estimate, opened = asyncio.run(run())
    body = api.body("PATCH", POD_PATH, 0)
    assert body["ports"] == [{"port": 9000, "external": True, "locked": True}]
    # 9100 is exposed by this call: locked. 8080 is public (and unlocked) already: no value.
    assert body["templatePorts"] == [{"port": 9100, "external": True, "locked": True},
                                     {"port": 8080, "external": True}]
    assert first.port_locks == {9100: True, 9000: True}
    assert api.body("POST", "/v1/sdk/pods/p-0/estimate")["templatePorts"] == [{"port": 9000, "locked": True}]
    assert estimate.port_locks == {9000: True}
    assert "locked" not in json.dumps(api.body("PATCH", POD_PATH, 1))
    assert api.body("PATCH", POD_PATH, 2)["ports"] == [{"port": 9000, "external": True, "locked": False}]
    assert opened.port_locks == {9000: False}
    assert api.paths() == [
        ("GET", POD_PATH), ("GET", CAPABILITIES_PATH), ("PATCH", POD_PATH),
        ("GET", POD_PATH), ("GET", TEMPLATE_PATH), ("GET", CAPABILITIES_PATH), ("POST", "/v1/sdk/pods/p-0/estimate"),
        ("PATCH", POD_PATH),                             # nothing made public: nothing read
        ("PATCH", POD_PATH)]                             # opened on purpose: nothing to check


def test_async_update_does_not_guess_when_the_pod_cannot_be_read(slept):
    api = Api({("GET", POD_PATH): (500, {"detail": "Internal Server Error"})})

    async def run():
        async with async_client(api) as c:
            with pytest.raises(MeshiveAPIError):
                await c.update_pod("p-0", "ws", template_ports=[{"port": 9100, "external": True}])

    asyncio.run(run())
    assert not api.sent("PATCH", POD_PATH)


def test_async_port_lock_and_access_keys(slept):
    api = Api({("PATCH", LOCK_PATH): (200, {"port": 3000, "locked": True}),
               ("GET", KEYS_PATH): (200, {"keys": [KEY_ITEM]}),
               ("POST", KEYS_PATH): (201, {**KEY_ITEM, "accessKey": KEY}),
               ("POST", f"{KEYS_PATH}/12/roll"): [_timeout, (201, ROLLED)],
               ("DELETE", f"{KEYS_PATH}/12"): (200, {"title": "Success", "message": "Access key revoked."}),
               ("GET", "/v1/sdk/pods"): (200, {"pods": [POD]}), ("GET", "/v1/sdk/pods/p-0"): (200, POD)})

    async def run():
        async with async_client(api) as c:
            lock = await c.set_port_lock("p-0", "ws", 3000, True)
            keys = await c.list_access_keys("p-0", "ws", 3000)
            key = await c.create_access_key("p-0", "ws", 3000, "ci", expires_in_days=0)
            with pytest.raises(httpx.ReadTimeout):
                await c.roll_access_key("p-0", "ws", 3000, 12, overlap_seconds=86400)
            action = await c.revoke_access_key("p-0", "ws", 3000, 12)
            with pytest.raises(PortNotLockedError):
                await c.wait_for_new_pod("p", "ws", port_locks={8080: True})
            pod = await c.wait_for_new_pod("p", "ws", port_locks={8188: True})
            return lock, keys, key, action, pod

    lock, keys, key, action, pod = asyncio.run(run())
    assert lock.locked is True and [k.id for k in keys] == [12] and pod.pod_name == "p-0"
    assert key.access_key == KEY and KEY not in repr(key) and "accessKey" not in key.raw
    assert api.body("POST", KEYS_PATH) == {"name": "ci", "expiresInDays": 0}
    assert api.body("POST", f"{KEYS_PATH}/12/roll") == {"overlapSeconds": 86400}
    assert len(api.sent("POST", f"{KEYS_PATH}/12/roll")) == 1 and slept == []     # the roll was not sent again
    assert action.id == "12" and action.resource == "access_key"


# =================================================================================================
# CLI
# =================================================================================================

class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def api(monkeypatch):
    """The CLI on top of the real client, with the HTTP layer answered by an Api."""
    handler = Api({("PATCH", LOCK_PATH): lambda r: httpx.Response(200, json={"port": 3000, "locked": _json(r)["locked"]}),
                   ("GET", KEYS_PATH): (200, {"keys": [KEY_ITEM, {**ROLLED, "accessKey": None}]}),
                   ("POST", KEYS_PATH): (201, {**KEY_ITEM, "accessKey": KEY}),
                   ("POST", f"{KEYS_PATH}/12/roll"): (201, ROLLED),
                   ("DELETE", f"{KEYS_PATH}/12"): (200, {"title": "Success", "message": "Access key revoked."}),
                   ("GET", "/v1/sdk/pods"): (200, {"pods": [POD]}), ("GET", "/v1/sdk/pods/p-0"): (200, POD),
                   ("GET", "/v1/sdk/transactions/8"): (200, {"transactionId": 8, "status": "done",
                                                             "transactionSteps": []})})
    monkeypatch.setattr(cli, "Meshive", lambda *a, **kw: sync_client(handler))
    return handler


@pytest.fixture
def non_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


def test_cli_pod_shows_how_each_port_can_be_reached(api, capsys):
    assert cli.main(["pod", "ws", "p-0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.startswith("ENDPOINT"))
    assert header.split() == ["ENDPOINT", "PORT", "ACCESS", "URL", "STATE"]
    access = {line.split()[1]: line.split()[2] for line in lines if line.split()[:1] in (["ComfyUI"], ["ide"],
                                                                                      ["metrics"])}
    assert access == {"8188": "Locked", "8080": "Unlocked", "9100": "Internal"}


def test_cli_pod_from_a_server_without_locks(api, capsys):
    api.routes[("GET", "/v1/sdk/pods/p-0")] = (200, OLD_POD)
    assert cli.main(["pod", "ws", "p-0"]) == 0
    out = capsys.readouterr().out
    assert "Unlocked" in out and "Internal" in out and "Locked" not in out


def test_cli_pod_create_locks_public_ports_and_shows_it(api, capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--port", "7000", "--port", "7002::internal",
                     "--yes"]) == 0
    captured = capsys.readouterr()
    assert "ports:     7000 locked, 8080 unlocked, 8188 locked, 9000 locked" in captured.out
    assert "port-key-create" in captured.out                    # how a program gets in is said with the estimate
    # 8080 is open because the template's author chose so: said before the question, on stderr.
    assert "Port 8080 will be open to anyone with the URL (not locked)." in captured.err
    for path in ("/v1/sdk/pods/estimate", "/v1/sdk/pods"):
        body = api.body("POST", path)
        assert body["ports"] == [{"port": 7000, "external": True, "locked": True}, {"port": 7002, "external": False}]
        assert body["templatePorts"] == [{"port": 8188, "locked": True}, {"port": 8080, "locked": False},
                                         {"port": 9000, "locked": True}]


def test_cli_pod_create_no_lock_says_so_and_asks(api, capsys, non_tty, monkeypatch):
    argv = ["pod-create", "ws", "p", "--template", "457", "--port", "7000", "--no-lock"]
    assert cli.main(argv) == 2                                 # no terminal to ask on and no --yes
    captured = capsys.readouterr()
    assert "Port 7000, 8080, 8188, 9000 will be open to anyone with the URL (not locked)." in captured.err
    assert "--yes" in captured.err and not api.sent("POST", "/v1/sdk/pods")
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    assert cli.main(argv) == 2 and not api.sent("POST", "/v1/sdk/pods")
    assert "will be open to anyone" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(argv) == 0
    assert _locks(api.body("POST", "/v1/sdk/pods")) == {7000: False, 8188: False, 8080: False, 9000: False}
    assert not api.sent("GET", CAPABILITIES_PATH)


def test_cli_pod_create_lock_and_unlock_one_port(api, capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--port", "7000", "--port", "7001",
                     "--template-port", "9100::9101:external", "--unlock-port", "7000", "--unlock-port", "9101",
                     "--lock-port", "8080", "--yes"]) == 0
    body = api.body("POST", "/v1/sdk/pods")
    assert body["ports"] == [{"port": 7000, "external": True, "locked": False},
                             {"port": 7001, "external": True, "locked": True}]
    assert body["templatePorts"] == [{"port": 9100, "newPort": 9101, "external": True, "locked": False},
                                     {"port": 8080, "locked": True}, {"port": 8188, "locked": True},
                                     {"port": 9000, "locked": True}]
    assert "Port 7000, 9101 will be open to anyone" in capsys.readouterr().err
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--lock-port", "8080", "--unlock-port", "8080",
                     "--yes"]) == 2
    assert "both name port 8080" in capsys.readouterr().err


@pytest.mark.parametrize("routes, why", [
    ({("GET", CAPABILITIES_PATH): (200, OLD_CAPABILITIES)}, "doesn't support locking ports yet"),
    ({("POST", "/v1/sdk/pods/estimate"): (409, LOCK_DISABLED)}, "port locking is turned off"),
])
def test_cli_pod_create_on_a_server_that_cannot_lock(api, capsys, routes, why):
    api.routes.update(routes)
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--port", "7000", "--yes"]) == 1
    err = capsys.readouterr().err
    assert why in err and "--no-lock" in err and "nothing was created" in err
    assert "lock_public_ports" not in err                       # the CLI names its own flags
    assert not api.sent("POST", "/v1/sdk/pods")                 # and never creates the pod open by itself


def test_cli_pod_create_wait_fails_when_a_port_is_not_locked(api, capsys):
    # The pod came up with 8080 open although the request locked it.
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--lock-port", "8080", "--wait", "running",
                     "--yes"]) == 1
    captured = capsys.readouterr()
    assert "port 8080 of pod p-0 is exposed without the lock" in captured.err
    assert "meshive port-lock ws p-0 PORT" in captured.err and "p-0" in captured.out
    # With the locks in place the same wait succeeds.
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--wait", "running", "--yes"]) == 0


def test_cli_pod_update_locks_a_new_public_port_and_says_so(api, capsys):
    assert cli.main(["pod-update", "ws", "p-0", "--port", "9000", "--port", "9002::internal", "--yes"]) == 0
    assert api.body("PATCH", POD_PATH)["ports"] == [{"port": 9000, "external": True, "locked": True},
                                                   {"port": 9002, "external": False}]
    captured = capsys.readouterr()
    assert "ports:     9000 locked" in captured.out                       # with the estimate
    # Said before the question, with how a program gets in. Nothing is opened to everyone, so no such warning.
    assert "Port 9000 will be locked" in captured.err and "port-key-create" in captured.err
    assert "open to anyone" not in captured.err
    # An internal port the update exposes, and a move to another template (9000 is the port it adds).
    assert cli.main(["pod-update", "ws", "p-0", "--template-port", "9100:::external", "--yes"]) == 0
    assert api.body("PATCH", POD_PATH)["templatePorts"] == [{"port": 9100, "external": True, "locked": True}]
    assert "Port 9100 will be locked" in capsys.readouterr().err
    assert cli.main(["pod-update", "ws", "p-0", "--template", "457", "--yes"]) == 0
    assert api.body("PATCH", POD_PATH)["templatePorts"] == [{"port": 9000, "locked": True}]
    assert "Port 9000 will be locked" in capsys.readouterr().err


def test_cli_pod_update_leaves_public_ports_and_other_changes_alone(api, capsys):
    # Saying "external" again on ports that are public already, and a change that has nothing to do with ports.
    assert cli.main(["pod-update", "ws", "p-0", "--template-port", "8080:editor::external", "--yes"]) == 0
    assert api.body("PATCH", POD_PATH) == {"templatePorts": [{"port": 8080, "name": "editor", "external": True}]}
    assert cli.main(["pod-update", "ws", "p-0", "--env", "A=1", "--yes"]) == 0
    assert api.body("PATCH", POD_PATH) == {"env": {"A": "1"}}
    captured = capsys.readouterr()
    assert "will be locked" not in captured.err and "open to anyone" not in captured.err
    assert "ports:     " not in captured.out                  # no lock line with the estimate either
    assert not api.sent("GET", CAPABILITIES_PATH) and not api.sent("GET", TEMPLATE_PATH)
    assert cli.main(["pod-update", "ws", "p-0", "--no-lock", "--yes"]) == 2       # --no-lock alone: no change
    assert "nothing to change" in capsys.readouterr().err


def test_cli_pod_update_no_lock_and_unlock_port_warn_and_ask(api, capsys, non_tty):
    argv = ["pod-update", "ws", "p-0", "--port", "9000", "--template-port", "9100:::external", "--no-lock"]
    assert cli.main(argv) == 2                                  # no terminal to ask on and no --yes
    err = capsys.readouterr().err
    assert "Port 9000, 9100 will be open to anyone with the URL (not locked)." in err and "--yes" in err
    assert not api.sent("PATCH", POD_PATH)
    assert cli.main(argv + ["--yes"]) == 0
    body = api.body("PATCH", POD_PATH)
    assert body["ports"] == [{"port": 9000, "external": True, "locked": False}]
    assert body["templatePorts"] == [{"port": 9100, "external": True, "locked": False}]
    capsys.readouterr()
    # One port left open, the other locked by default: each is said.
    assert cli.main(["pod-update", "ws", "p-0", "--port", "9000", "--port", "9001", "--unlock-port", "9001",
                     "--yes"]) == 0
    assert api.body("PATCH", POD_PATH)["ports"] == [{"port": 9000, "external": True, "locked": True},
                                                   {"port": 9001, "external": True, "locked": False}]
    err = capsys.readouterr().err
    assert "Port 9000 will be locked" in err and "Port 9001 will be open to anyone" in err
    with pytest.raises(SystemExit):
        cli.main(["pod-update", "ws", "p-0", "--port", "9000", "--lock"])        # there is no --lock: it is the default
    capsys.readouterr()


def test_cli_pod_update_unlocking_a_port_warns_and_asks(api, capsys, non_tty):
    argv = ["pod-update", "ws", "p-0", "--unlock-port", "8188"]
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "Port 8188 will be open to anyone with the URL (not locked)." in err and "--yes" in err
    assert not api.sent("PATCH", "/v1/sdk/pods/p-0")
    assert cli.main(argv + ["--yes"]) == 0
    assert api.body("PATCH", "/v1/sdk/pods/p-0") == {"templatePorts": [{"port": 8188, "locked": False}]}
    capsys.readouterr()
    # Locking a port that is public already, by naming it.
    assert cli.main(["pod-update", "ws", "p-0", "--lock-port", "8080", "--yes"]) == 0
    assert api.body("PATCH", POD_PATH) == {"templatePorts": [{"port": 8080, "locked": True}]}
    assert "Port 8080 will be locked" in capsys.readouterr().err
    for routes, why in (({("POST", "/v1/sdk/pods/p-0/estimate"): (409, LOCK_DISABLED)}, "port locking is turned off"),
                        ({("GET", CAPABILITIES_PATH): (200, OLD_CAPABILITIES)}, "doesn't support locking ports yet")):
        saved = dict(api.routes)
        api.routes.update(routes)
        sent = len(api.sent("PATCH", POD_PATH))
        assert cli.main(["pod-update", "ws", "p-0", "--port", "9000", "--yes"]) == 1
        err = capsys.readouterr().err
        assert why in err and "nothing was changed" in err and "--no-lock" in err and "--unlock-port" in err
        assert len(api.sent("PATCH", POD_PATH)) == sent           # and the port is never opened instead
        api.routes = saved


def test_cli_port_lock_does_not_ask(api, capsys, non_tty):
    assert cli.main(["port-lock", "ws", "p-0", "3000"]) == 0
    assert api.body("PATCH", LOCK_PATH) == {"locked": True}
    out = capsys.readouterr().out
    assert "Locked" in out and "meshive port-key-create ws p-0 3000 --name NAME" in out
    assert cli.main(["port-lock", "ws", "p-0", "3000", "-o", "name"]) == 0
    assert capsys.readouterr().out == "3000\n"


def test_cli_port_unlock_asks_first(api, capsys, non_tty, monkeypatch):
    assert cli.main(["port-unlock", "ws", "p-0", "3000"]) == 2
    err = capsys.readouterr().err
    assert "opens port 3000 of pod p-0 to anyone with its URL" in err and "--yes" in err
    assert not api.sent("PATCH", LOCK_PATH)
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    assert cli.main(["port-unlock", "ws", "p-0", "3000"]) == 2 and not api.sent("PATCH", LOCK_PATH)
    assert "Unlock port 3000 of pod p-0? [y/N]" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["port-unlock", "ws", "p-0", "3000"]) == 0
    assert api.body("PATCH", LOCK_PATH) == {"locked": False}
    out = capsys.readouterr().out
    assert "Unlocked" in out and "access keys are kept" in out
    assert cli.main(["port-unlock", "ws", "p-0", "3000", "--yes", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["locked"] is False


def test_cli_port_keys_lists_prefixes_never_values(api, capsys):
    assert cli.main(["port-keys", "ws", "p-0", "3000"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[:4] == ["ID", "NAME", "KEY", "EXPIRES"]
    assert "mshv_Zq7Zq7Zq…" in out and "batch-script" in out and "dev@example.com" in out and KEY not in out
    assert "2026-11-14 15:00 UTC" in out                       # until when the rolled key's old value works
    assert cli.main(["port-keys", "ws", "p-0", "3000", "-o", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [k["id"] for k in listed] == [12, 31] and all("accessKey" not in k for k in listed)
    assert cli.main(["port-keys", "ws", "p-0", "3000", "-o", "name"]) == 0
    assert capsys.readouterr().out == "12\n31\n"
    api.routes[("GET", KEYS_PATH)] = (200, {"keys": []})
    assert cli.main(["port-keys", "ws", "p-0", "3000"]) == 0
    assert "No access keys for port 3000" in capsys.readouterr().out


def test_cli_port_key_create_prints_the_value_once(api, capsys, non_tty):
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "batch-script"]) == 0      # doesn't ask
    captured = capsys.readouterr()
    assert captured.out.count(KEY) == 1 and KEY not in captured.err
    assert "shown only now" in captured.out and "Authorization: Bearer <key>" in captured.out
    assert "X-Meshive-Key" in captured.out and "batch-script" in captured.out
    assert api.body("POST", KEYS_PATH) == {"name": "batch-script", "expiresInDays": 90}
    # -o name: only the value, for KEY=$(…). -o json: the server's answer, value included, as other secrets are.
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci", "-o", "name"]) == 0
    assert capsys.readouterr().out == KEY + "\n"
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci", "--expires", "never", "-o", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accessKey"] == KEY and payload["id"] == 12
    assert api.body("POST", KEYS_PATH)["expiresInDays"] == 0
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci", "--expires", "365"]) == 0
    assert api.body("POST", KEYS_PATH)["expiresInDays"] == 365
    capsys.readouterr()
    for bad in ("soon", "0", "-5"):
        before = len(api.sent("POST", KEYS_PATH))
        assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci", "--expires", bad]) == 2
        assert "--expires expects" in capsys.readouterr().err and len(api.sent("POST", KEYS_PATH)) == before


def test_cli_port_key_create_is_not_repeated_after_a_timeout(api, capsys, slept):
    api.routes[("POST", KEYS_PATH)] = _timeout
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci"]) == 1
    captured = capsys.readouterr()
    assert "isn't known whether the key was created" in captured.err and "It was not sent again" in captured.err
    assert "meshive port-keys ws p-0 3000" in captured.err and "port-key-revoke" in captured.err
    assert len(api.sent("POST", KEYS_PATH)) == 1 and captured.out == ""
    # A connection that broke mid-request is the same case: the server may have issued the value.
    api.routes[("POST", KEYS_PATH)] = _cut_off
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci"]) == 1
    assert "isn't known whether the key was created" in capsys.readouterr().err
    assert len(api.sent("POST", KEYS_PATH)) == 2
    # A request that never reached the server is an ordinary connection error: nothing was created.
    api.routes[("POST", KEYS_PATH)] = lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r))
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci"]) == 1
    err = capsys.readouterr().err
    assert "could not reach the Meshive API" in err and "isn't known" not in err


def test_cli_port_key_roll_asks_and_prints_the_new_value_once(api, capsys, non_tty, monkeypatch):
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12"]) == 2
    err = capsys.readouterr().err
    assert "its current value stops working in 1 hour" in err and "--yes" in err
    assert not api.sent("POST", f"{KEYS_PATH}/12/roll")
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12", "--yes"]) == 0
    captured = capsys.readouterr()
    assert captured.out.count(KEY) == 1 and KEY not in captured.err
    assert "works until 2026-11-14 15:00 UTC" in captured.out and "31" in captured.out
    assert api.body("POST", f"{KEYS_PATH}/12/roll") == {"overlapSeconds": 3600}
    for text, seconds, words in (("now", 0, "at once"), ("24h", 86400, "in 24 hours"), ("7d", 604800, "in 7 days")):
        assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12", "--overlap", text, "--yes", "-o", "name"]) == 0
        captured = capsys.readouterr()
        assert captured.out == KEY + "\n" and f"stops working {words}" in captured.err
        assert api.body("POST", f"{KEYS_PATH}/12/roll") == {"overlapSeconds": seconds}
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12", "--overlap", "soon", "--yes"]) == 2
    assert "--overlap expects" in capsys.readouterr().err
    api.routes[("POST", f"{KEYS_PATH}/12/roll")] = _timeout
    sent = len(api.sent("POST", f"{KEYS_PATH}/12/roll"))
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12", "--yes"]) == 1
    assert "isn't known whether the key was rolled" in capsys.readouterr().err
    assert len(api.sent("POST", f"{KEYS_PATH}/12/roll")) == sent + 1


def test_cli_port_key_revoke_asks_first(api, capsys, non_tty, monkeypatch):
    assert cli.main(["port-key-revoke", "ws", "p-0", "3000", "12"]) == 2
    err = capsys.readouterr().err
    assert "can't be undone" in err and "--yes" in err and not api.sent("DELETE", f"{KEYS_PATH}/12")
    monkeypatch.setattr("sys.stdin", _Tty("no\n"))
    assert cli.main(["port-key-revoke", "ws", "p-0", "3000", "12"]) == 2
    assert "Revoke access key 12 of port 3000 on pod p-0? [y/N]" in capsys.readouterr().err
    assert not api.sent("DELETE", f"{KEYS_PATH}/12")
    assert cli.main(["port-key-revoke", "ws", "p-0", "3000", "12", "--yes"]) == 0
    assert len(api.sent("DELETE", f"{KEYS_PATH}/12")) == 1 and "revoked key" in capsys.readouterr().out
    assert cli.main(["port-key-revoke", "ws", "p-0", "3000", "12", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == "12\n"


def test_cli_port_commands_explain_a_server_that_cannot_lock(api, capsys):
    api.routes[("PATCH", LOCK_PATH)] = (404, {"detail": "Not Found"})
    assert cli.main(["port-lock", "ws", "p-0", "3000"]) == 1
    assert "does not support port locks and access keys yet" in capsys.readouterr().err
    api.routes[("POST", KEYS_PATH)] = (404, {"detail": {"title": "Not Found", "message": "Endpoint lock is not "
                                                       "available.", "code": "endpoint_lock_disabled"}})
    assert cli.main(["port-key-create", "ws", "p-0", "3000", "--name", "ci"]) == 1
    assert "turned off on this Meshive server right now" in capsys.readouterr().err


# =================================================================================================
# Lock state: a choice that is not in effect yet
# =================================================================================================

def _pod_with_state(*states):
    """The pod, with port 3000's lockState set to each of `states` in turn (None = the field is missing)."""
    answers = []
    for state in states:
        endpoint = {"userAlias": "console", "portType": "http", "containerPort": 3000, "isExternal": True,
                    "isReady": True, "externalUrl": "https://0123456789abcdef.dev.meshive.ai", "isLocked": True,
                    "lockDecidedAt": "2026-11-16T05:00:00Z", "accessKeyCount": 0}
        if state is not None:
            endpoint["lockState"] = state
        answers.append((200, {**POD, "template": {"endpoints": [endpoint]}}))
    return answers


@pytest.fixture
def clock(monkeypatch, slept):
    """A fake clock that only moves when the code sleeps, so a wait that times out takes no real time."""
    from meshive import _client

    now = [1000.0]
    monkeypatch.setattr(_client.time, "monotonic", lambda: now[0])

    def _sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    async def _async_sleep(seconds):
        _sleep(seconds)

    monkeypatch.setattr(_client.time, "sleep", _sleep)
    monkeypatch.setattr(_client.asyncio, "sleep", _async_sleep)
    return now


def test_endpoint_and_lock_results_carry_the_state():
    endpoints = POD["template"]["endpoints"]
    parsed = Pod.from_dict({**POD, "template": {"endpoints": [
        {**endpoints[0], "lockState": "locking"}, {**endpoints[1], "lockState": "unlocked"},
        {**endpoints[2], "lockState": None}, {**endpoints[0], "lockState": "half-locked"}]}}).endpoints
    assert [e.lock_state for e in parsed] == ["locking", "unlocked", None, None]
    assert parsed[0].access == "locked" and parsed[0].locked is True      # the choice itself is unchanged
    lock = PortLock.from_dict({"port": 3000, "locked": True, "state": "locking",
                               "lockDecidedAt": "2026-11-16T05:00:00Z"})
    assert lock.state == "locking" and lock.lock_decided_at.isoformat() == "2026-11-16T05:00:00+00:00"


def test_a_server_without_lock_states_still_parses():
    assert all(e.lock_state is None for e in Pod.from_dict(POD).endpoints)
    assert all(e.lock_state is None for e in Pod.from_dict(OLD_POD).endpoints)
    old = PortLock.from_dict({"port": 3000, "locked": True})
    assert old.state is None and old.lock_decided_at is None


def test_set_port_lock_returns_the_state():
    api = Api({("PATCH", LOCK_PATH): (200, {"port": 3000, "locked": True, "state": "locking",
                                            "lockDecidedAt": "2026-11-16T05:00:00Z"})})
    assert sync_client(api).set_port_lock("p-0", "ws", 3000, True).state == "locking"


def test_wait_for_port_lock_polls_until_the_state_settles(clock, slept):
    api = Api({("GET", POD_PATH): _pod_with_state("locking", "locking", "locked")})
    endpoint = sync_client(api).wait_for_port_lock("p-0", "ws", 3000)
    assert isinstance(endpoint, PodEndpoint) and endpoint.lock_state == "locked"
    assert len(api.sent("GET", POD_PATH)) == 3 and slept == [1.0, 1.0]
    assert api.sent("GET", POD_PATH)[0].url.params["workspace"] == "ws"


def test_wait_for_port_lock_unlocking_waits_too(clock):
    api = Api({("GET", POD_PATH): _pod_with_state("unlocking", "unlocked")})
    assert sync_client(api).wait_for_port_lock("p-0", "ws", 3000).lock_state == "unlocked"


@pytest.mark.parametrize("state", ["locked", "unlocked", None])
def test_wait_for_port_lock_returns_at_once_when_nothing_is_pending(clock, slept, state):
    # None: a server that doesn't report states, whose lock is in effect at once.
    api = Api({("GET", POD_PATH): _pod_with_state(state)})
    assert sync_client(api).wait_for_port_lock("p-0", "ws", 3000).lock_state == state
    assert len(api.sent("GET", POD_PATH)) == 1 and slept == []


def test_wait_for_port_lock_raises_on_timeout_with_the_last_endpoint(clock, slept):
    api = Api({("GET", POD_PATH): _pod_with_state("locking")})
    with pytest.raises(WaitTimeoutError) as caught:
        sync_client(api).wait_for_port_lock("p-0", "ws", 3000, timeout=5.0, interval=2.0)
    assert slept == [2.0, 2.0, 1.0] and len(api.sent("GET", POD_PATH)) == 4
    error = caught.value
    assert isinstance(error, MeshiveError) and isinstance(error, TimeoutError)     # like the other waits
    assert isinstance(error.last, PodEndpoint) and error.last.lock_state == "locking" and error.last.port == 3000
    assert "last state: locking; the port may still be open" in str(error)


def test_wait_for_port_lock_with_no_time_reads_once(clock, slept):
    api = Api({("GET", POD_PATH): _pod_with_state("locking")})
    with pytest.raises(WaitTimeoutError):
        sync_client(api).wait_for_port_lock("p-0", "ws", 3000, timeout=0)
    assert len(api.sent("GET", POD_PATH)) == 1 and slept == []
    # Nothing pending: no time is needed.
    api = Api({("GET", POD_PATH): _pod_with_state("locked")})
    assert sync_client(api).wait_for_port_lock("p-0", "ws", 3000, timeout=0).lock_state == "locked"


def test_other_waits_still_raise_wait_timeout_without_last():
    assert WaitTimeoutError("x").last is None and str(WaitTimeoutError("x")) == "x"


def test_wait_for_port_lock_checks_the_port_and_the_pod():
    api = Api({("GET", POD_PATH): (200, POD)})
    with pytest.raises(NotFoundError, match="doesn't open port 4000"):
        sync_client(api).wait_for_port_lock("p-0", "ws", 4000)
    with pytest.raises(ValueError):
        sync_client(api).wait_for_port_lock("p-0", "ws", "3000")
    with pytest.raises(ValueError):
        sync_client(api).wait_for_port_lock("", "ws", 3000)


def test_wait_for_port_lock_async(clock, slept):
    api = Api({("GET", POD_PATH): _pod_with_state("locking", "locked")})
    endpoint = asyncio.run(async_client(api).wait_for_port_lock("p-0", "ws", 3000))
    assert endpoint.lock_state == "locked" and slept == [1.0]
    api = Api({("GET", POD_PATH): _pod_with_state("unlocking")})
    slept.clear()
    with pytest.raises(WaitTimeoutError) as caught:
        asyncio.run(async_client(api).wait_for_port_lock("p-0", "ws", 3000, timeout=2.0, interval=1.0))
    assert caught.value.last.lock_state == "unlocking" and slept == [1.0, 1.0]
    assert "requests may still be refused" in str(caught.value)


def test_cli_pod_shows_locking_and_unlocking(api, capsys):
    endpoints = POD["template"]["endpoints"]
    api.routes[("GET", POD_PATH)] = (200, {**POD, "template": {"endpoints": [
        {**endpoints[0], "lockState": "locking"}, {**endpoints[1], "isLocked": False, "lockState": "unlocking"},
        endpoints[2]]}})
    assert cli.main(["pod", "ws", "p-0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    access = {line.split()[1]: line.split()[2] for line in lines if line.split()[:1] in (["ComfyUI"], ["ide"],
                                                                                      ["metrics"])}
    assert access == {"8188": "Locking…", "8080": "Unlocking…", "9100": "Internal"}


def _toggle(state):
    return lambda r: httpx.Response(200, json={"port": 3000, "locked": _json(r)["locked"], "state": state,
                                               "lockDecidedAt": "2026-11-16T05:00:00Z"})


def test_cli_port_lock_waits_until_it_is_in_effect(api, capsys, non_tty, clock):
    api.routes[("PATCH", LOCK_PATH)] = _toggle("locking")
    api.routes[("GET", POD_PATH)] = _pod_with_state("locking", "locked")
    assert cli.main(["port-lock", "ws", "p-0", "3000"]) == 0
    captured = capsys.readouterr()
    assert "access: Locked" in captured.out and "Locking" not in captured.out
    assert "Waiting for the port to lock (up to 60 seconds)" in captured.err and "Waiting" not in captured.out
    assert len(api.sent("GET", POD_PATH)) == 2
    api.routes[("GET", POD_PATH)] = _pod_with_state("locked")
    assert cli.main(["port-lock", "ws", "p-0", "3000", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "locked"


def test_cli_port_lock_that_is_not_confirmed_in_time_says_so(api, capsys, non_tty, clock):
    api.routes[("PATCH", LOCK_PATH)] = _toggle("locking")
    api.routes[("GET", POD_PATH)] = _pod_with_state("locking")
    assert cli.main(["port-lock", "ws", "p-0", "3000"]) == 1                      # not confirmed: exit 1
    out = capsys.readouterr().out
    assert ("access: Locking… (not confirmed yet; the port may still be open — check again with "
            "`meshive pod ws p-0`)") in out
    assert clock[0] - 1000.0 == 60.0                                              # waited the 60 seconds


def test_cli_port_lock_no_wait_does_not_poll(api, capsys, non_tty, clock):
    api.routes[("PATCH", LOCK_PATH)] = _toggle("locking")
    assert cli.main(["port-lock", "ws", "p-0", "3000", "--no-wait"]) == 0
    captured = capsys.readouterr()
    assert "access: Locking… (not confirmed yet" in captured.out and "Waiting" not in captured.err
    assert not api.sent("GET", POD_PATH)


def test_cli_port_unlock_waits_too(api, capsys, non_tty, clock):
    api.routes[("PATCH", LOCK_PATH)] = _toggle("unlocking")
    api.routes[("GET", POD_PATH)] = _pod_with_state("unlocking", "unlocked")
    assert cli.main(["port-unlock", "ws", "p-0", "3000", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "access: Unlocked" in captured.out and "Waiting for the port to open" in captured.err
    api.routes[("GET", POD_PATH)] = _pod_with_state("unlocking")
    assert cli.main(["port-unlock", "ws", "p-0", "3000", "--yes"]) == 1
    assert "access: Unlocking… (not confirmed yet; requests may still be refused" in capsys.readouterr().out
    assert cli.main(["port-unlock", "ws", "p-0", "3000", "--yes", "--no-wait"]) == 0
    assert "Unlocking…" in capsys.readouterr().out


def test_cli_port_lock_on_a_server_without_states_does_not_wait(api, capsys, non_tty, clock):
    # The default `api` toggle answers {port, locked} only: nothing to wait for, no extra request.
    assert cli.main(["port-lock", "ws", "p-0", "3000"]) == 0
    captured = capsys.readouterr()
    assert "access: Locked" in captured.out and "Waiting" not in captured.err
    assert not api.sent("GET", POD_PATH)


def test_cli_port_lock_survives_a_failing_check(api, capsys, non_tty, clock):
    api.routes[("PATCH", LOCK_PATH)] = _toggle("locking")
    api.routes[("GET", POD_PATH)] = (500, {"detail": "boom"})
    assert cli.main(["port-lock", "ws", "p-0", "3000"]) == 1                      # the state is unknown
    captured = capsys.readouterr()
    assert "Could not check whether it took effect" in captured.err and "Locking…" in captured.out


def test_cli_port_lock_json_output_on_timeout_still_says_the_state(api, capsys, non_tty, clock):
    api.routes[("PATCH", LOCK_PATH)] = _toggle("locking")
    api.routes[("GET", POD_PATH)] = _pod_with_state("locking")
    assert cli.main(["port-lock", "ws", "p-0", "3000", "-o", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "locking"


# =================================================================================================
# Expired access keys
# =================================================================================================

EXPIRED_ITEM = {**KEY_ITEM, "id": 44, "keyName": "old-ci", "expiresAt": "2026-11-10T09:00:00Z", "expired": True}


def test_access_key_expired_flag():
    live, expired, no_field_past, no_field_future, never = (AccessKey.from_dict(d) for d in (
        {**KEY_ITEM, "expired": False}, EXPIRED_ITEM,
        {**KEY_ITEM, "expiresAt": "2020-01-01T00:00:00Z"}, {**KEY_ITEM, "expiresAt": "2999-01-01T00:00:00Z"},
        {**KEY_ITEM, "expiresAt": None}))
    assert [live.expired, expired.expired] == [False, True]
    assert (no_field_past.expired, no_field_future.expired, never.expired) == (True, False, False)
    # The server's word wins over the clock, and a value that isn't a boolean is ignored.
    assert AccessKey.from_dict({**KEY_ITEM, "expired": False, "expiresAt": "2020-01-01T00:00:00Z"}).expired is False
    assert AccessKey.from_dict({**KEY_ITEM, "expired": "yes"}).expired is False
    assert AccessKeyCreated.from_dict({**ROLLED, "expired": False}).expired is False
    assert AccessKey.from_dict({"id": 40, "keyName": "old"}).expired is False


def test_list_access_keys_reports_expired_keys():
    api = Api({("GET", KEYS_PATH): (200, {"keys": [KEY_ITEM, EXPIRED_ITEM]})})
    live, expired = sync_client(api).list_access_keys("p-0", "ws", 3000)
    assert (live.expired, expired.expired, expired.id) == (False, True, 44)
    assert expired.raw["expired"] is True


def test_rolling_an_expired_key_has_no_previous_value():
    new = {**ROLLED, "id": 45, "previousValueValidUntil": None}
    api = Api({("POST", f"{KEYS_PATH}/44/roll"): (201, new)})
    rolled = sync_client(api).roll_access_key("p-0", "ws", 3000, 44, overlap_seconds=0)
    assert rolled.previous_value_valid_until is None and rolled.access_key == KEY and rolled.expired is False


def test_cli_port_keys_shows_expired_keys(api, capsys):
    api.routes[("GET", KEYS_PATH)] = (200, {"keys": [KEY_ITEM, EXPIRED_ITEM]})
    assert cli.main(["port-keys", "ws", "p-0", "3000"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if "old-ci" in line)
    assert "expired " in row and " ago (2026-11-10 09:00 UTC)" in row
    assert "expired" not in next(line for line in out.splitlines() if "batch-script" in line)
    assert cli.main(["port-keys", "ws", "p-0", "3000", "-o", "json"]) == 0
    assert [k["expired"] for k in json.loads(capsys.readouterr().out) if "expired" in k] == [True]


def test_cli_port_key_roll_of_an_expired_key(api, capsys, non_tty):
    api.routes[("GET", KEYS_PATH)] = (200, {"keys": [KEY_ITEM, EXPIRED_ITEM]})
    api.routes[("POST", f"{KEYS_PATH}/44/roll")] = (201, {**ROLLED, "id": 45, "previousValueValidUntil": None})
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "44", "--overlap", "7d", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "Key 44 has expired; rolling issues a new value." in captured.err
    assert "stops working" not in captured.err and "old value" not in captured.out
    assert api.body("POST", f"{KEYS_PATH}/44/roll") == {"overlapSeconds": 0}
    assert captured.out.count(KEY) == 1
    # A key that is still good keeps the overlap warning, and a bad --overlap is still refused.
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12", "--yes"]) == 0
    assert "its current value stops working in 1 hour" in capsys.readouterr().err
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "44", "--overlap", "soon", "--yes"]) == 2


def test_cli_port_key_roll_works_when_the_key_list_cannot_be_read(api, capsys, non_tty, slept):
    api.routes[("GET", KEYS_PATH)] = (500, {"detail": "boom"})
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "12", "--yes"]) == 0
    assert "its current value stops working in 1 hour" in capsys.readouterr().err


def test_cli_port_lock_that_is_in_effect_at_once_does_not_wait(api, capsys, non_tty, clock):
    # Locking a port that is locked already answers with the current state: nothing to wait for.
    for command, state, shown in (("port-lock", "locked", "Locked"), ("port-unlock", "unlocked", "Unlocked")):
        api.routes[("PATCH", LOCK_PATH)] = _toggle(state)
        assert cli.main([command, "ws", "p-0", "3000", "--yes"] if command == "port-unlock"
                        else [command, "ws", "p-0", "3000"]) == 0
        captured = capsys.readouterr()
        assert f"access: {shown}\n" in captured.out and "Waiting" not in captured.err
    assert not api.sent("GET", POD_PATH)


# =================================================================================================
# When an access key was last used
# =================================================================================================

def test_access_key_last_used_at():
    used = AccessKey.from_dict({**KEY_ITEM, "lastUsedAt": "2026-11-16T07:30:00Z"})
    assert used.last_used_at.isoformat() == "2026-11-16T07:30:00+00:00"
    assert AccessKey.from_dict({**KEY_ITEM, "lastUsedAt": None}).last_used_at is None    # never used
    assert AccessKey.from_dict(KEY_ITEM).last_used_at is None                             # an older server
    assert AccessKeyCreated.from_dict({**ROLLED, "lastUsedAt": "2026-11-16T07:30:00Z"}).last_used_at is not None


def test_cli_port_keys_shows_when_each_key_was_last_used(api, capsys):
    from datetime import datetime, timedelta, timezone

    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=5)).isoformat()
    api.routes[("GET", KEYS_PATH)] = (200, {"keys": [{**KEY_ITEM, "lastUsedAt": two_hours_ago},
                                                     {**EXPIRED_ITEM, "lastUsedAt": None}]})
    assert cli.main(["port-keys", "ws", "p-0", "3000"]) == 0
    lines = capsys.readouterr().out.splitlines()
    header = lines[0]
    assert header.index("EXPIRES") < header.index("LAST USED") < header.index("ISSUED BY")
    column = header.index("LAST USED")
    used = next(line for line in lines if "batch-script" in line)
    unused = next(line for line in lines if "old-ci" in line)
    assert used[column:].startswith("2 hours ago") and unused[column:].startswith("never")


def test_cli_port_keys_has_no_last_used_column_on_an_older_server(api, capsys):
    api.routes[("GET", KEYS_PATH)] = (200, {"keys": [KEY_ITEM]})
    assert cli.main(["port-keys", "ws", "p-0", "3000"]) == 0
    assert "LAST USED" not in capsys.readouterr().out


# =================================================================================================
# Preflight requests on a locked port
# =================================================================================================

PREFLIGHT_PATH = "/v1/sdk/pods/p-0/ports/3000/preflight"


def test_pod_endpoint_allow_preflight():
    endpoints = POD["template"]["endpoints"]
    parsed = Pod.from_dict({**POD, "template": {"endpoints": [
        {**endpoints[0], "allowPreflight": True}, {**endpoints[1], "allowPreflight": False},
        {**endpoints[2], "allowPreflight": None}, {**endpoints[0], "allowPreflight": "true"}]}}).endpoints
    assert [e.allow_preflight for e in parsed] == [True, False, None, None]
    assert all(e.allow_preflight is None for e in Pod.from_dict(POD).endpoints)         # an older server


@pytest.mark.parametrize("allow", [True, False])
def test_set_port_preflight_request_and_result(allow):
    api = Api({("PATCH", PREFLIGHT_PATH): (200, {"port": 3000, "allowPreflight": allow})})
    result = sync_client(api).set_port_preflight("p-0", "ws", 3000, allow, idempotency_key="pf-1")
    request = api.sent("PATCH", PREFLIGHT_PATH)[0]
    assert request.url.params["workspace"] == "ws" and _json(request) == {"allowPreflight": allow}
    assert request.headers["Idempotency-Key"] == "pf-1"
    assert isinstance(result, PortPreflight) and (result.port, result.allow_preflight) == (3000, allow)


def test_set_port_preflight_async():
    api = Api({("PATCH", PREFLIGHT_PATH): (200, {"port": 3000, "allowPreflight": True})})
    result = asyncio.run(async_client(api).set_port_preflight("p-0", "ws", 3000, True))
    assert result.allow_preflight is True and _json(api.sent("PATCH", PREFLIGHT_PATH)[0]) == {"allowPreflight": True}


@pytest.mark.parametrize("args", [("p-0", "ws", 3000, "on"), ("p-0", "ws", 3000, 1), ("p-0", "ws", 3000, None),
                                  ("p-0", "ws", 0, True), ("", "ws", 3000, True), ("p-0", " ", 3000, True)])
def test_set_port_preflight_checks_arguments_before_sending(args):
    api = Api()
    with pytest.raises(ValueError):
        sync_client(api).set_port_preflight(*args)
    assert not api.requests


def test_set_port_preflight_on_older_or_switched_off_servers():
    with pytest.raises(NotFoundError, match="does not support the preflight setting of locked ports yet"):
        sync_client(Api({("PATCH", PREFLIGHT_PATH): (404, {"detail": "Not Found"})})).set_port_preflight(
            "p-0", "ws", 3000, True)
    off = {"detail": {"title": "Not Found", "message": "Endpoint lock is not available.",
                      "code": "endpoint_lock_disabled"}}
    with pytest.raises(NotFoundError, match="preflight requests can't be let through"):
        sync_client(Api({("PATCH", PREFLIGHT_PATH): (404, off)})).set_port_preflight("p-0", "ws", 3000, True)
    internal = {"detail": {"title": "Not A Public Endpoint", "message": "Only a public port has this setting."}}
    with pytest.raises(MeshiveAPIError, match="Only a public port"):
        sync_client(Api({("PATCH", PREFLIGHT_PATH): (400, internal)})).set_port_preflight("p-0", "ws", 3000, True)


def _preflight_echo(request):
    return httpx.Response(200, json={"port": 3000, "allowPreflight": _json(request)["allowPreflight"]})


def test_cli_port_preflight_on_asks_first(api, capsys, non_tty, monkeypatch):
    api.routes[("PATCH", PREFLIGHT_PATH)] = _preflight_echo
    assert cli.main(["port-preflight", "ws", "p-0", "3000", "on"]) == 2
    err = capsys.readouterr().err
    assert "will reach the app without a key" in err and "A key in a web page is visible to its visitors" in err
    assert not api.sent("PATCH", PREFLIGHT_PATH)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["port-preflight", "ws", "p-0", "3000", "on"]) == 0
    captured = capsys.readouterr()
    assert "Allow preflight requests on port 3000 of pod p-0? [y/N]" in captured.err
    assert "preflight: allowed without a key" in captured.out
    assert api.body("PATCH", PREFLIGHT_PATH) == {"allowPreflight": True}
    assert cli.main(["port-preflight", "ws", "p-0", "3000", "on", "--yes", "-o", "json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["port"], printed["allowPreflight"]) == (3000, True)


def test_cli_port_preflight_off_does_not_ask(api, capsys, non_tty):
    api.routes[("PATCH", PREFLIGHT_PATH)] = _preflight_echo
    assert cli.main(["port-preflight", "ws", "p-0", "3000", "off"]) == 0
    captured = capsys.readouterr()
    assert "preflight: needs a key, like other requests" in captured.out and "[y/N]" not in captured.err
    assert api.body("PATCH", PREFLIGHT_PATH) == {"allowPreflight": False}
    with pytest.raises(SystemExit):
        cli.main(["port-preflight", "ws", "p-0", "3000", "maybe"])


def test_cli_pod_says_which_locked_ports_let_preflight_through(api, capsys):
    endpoints = POD["template"]["endpoints"]
    api.routes[("GET", POD_PATH)] = (200, {**POD, "template": {"endpoints": [
        {**endpoints[0], "allowPreflight": True},                       # locked: said
        {**endpoints[1], "allowPreflight": True},                       # unlocked: the setting does nothing
        {**endpoints[2], "allowPreflight": None}]}})
    assert cli.main(["pod", "ws", "p-0"]) == 0
    out = capsys.readouterr().out
    assert "Preflight (OPTIONS) requests reach the app without a key on port 8188." in out
    header = next(line for line in out.splitlines() if line.startswith("ENDPOINT"))
    assert header.split() == ["ENDPOINT", "PORT", "ACCESS", "URL", "STATE"]          # the table is unchanged
    api.routes[("GET", POD_PATH)] = (200, POD)
    assert cli.main(["pod", "ws", "p-0"]) == 0
    assert "Preflight" not in capsys.readouterr().out


# =================================================================================================
# Signing out a pod's browser sessions
# =================================================================================================

SESSIONS_PATH = "/v1/sdk/pods/p-0/browser-sessions/revoke"
REVOKED = {"podName": "p-0", "endpointCount": 2, "revokedAt": "2026-11-16T08:00:00Z"}


def test_revoke_browser_sessions_request_and_result():
    api = Api({("POST", SESSIONS_PATH): (200, REVOKED)})
    result = sync_client(api).revoke_browser_sessions("p-0", "ws", idempotency_key="rs-1")
    request = api.sent("POST", SESSIONS_PATH)[0]
    assert request.url.params["workspace"] == "ws" and request.headers["Idempotency-Key"] == "rs-1"
    assert isinstance(result, BrowserSessionsRevoked)
    assert (result.pod_name, result.endpoint_count) == ("p-0", 2)
    assert result.revoked_at.isoformat() == "2026-11-16T08:00:00+00:00"
    result = asyncio.run(async_client(Api({("POST", SESSIONS_PATH): (200, REVOKED)})).revoke_browser_sessions(
        "p-0", "ws"))
    assert result.endpoint_count == 2


def test_revoke_browser_sessions_checks_arguments_and_older_servers():
    api = Api()
    for args in (("", "ws"), ("p-0", " ")):
        with pytest.raises(ValueError):
            sync_client(api).revoke_browser_sessions(*args)
    assert not api.requests
    with pytest.raises(NotFoundError, match="does not support signing out the browser sessions of a pod yet"):
        sync_client(Api({("POST", SESSIONS_PATH): (404, {"detail": "Not Found"})})).revoke_browser_sessions("p-0", "ws")
    missing = {"detail": {"title": "Pod Not Found", "message": "No pod named 'p-0' in workspace 'ws'."}}
    with pytest.raises(NotFoundError, match="No pod named"):
        sync_client(Api({("POST", SESSIONS_PATH): (404, missing)})).revoke_browser_sessions("p-0", "ws")


def test_cli_pod_browser_sessions_revoke_asks_first(api, capsys, non_tty):
    api.routes[("POST", SESSIONS_PATH)] = (200, REVOKED)
    assert cli.main(["pod-browser-sessions-revoke", "ws", "p-0"]) == 2
    err = capsys.readouterr().err
    assert "Access keys keep working" in err and "already open stay open" in err and "--yes" in err
    assert not api.sent("POST", SESSIONS_PATH)
    assert cli.main(["pod-browser-sessions-revoke", "ws", "p-0", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "public ports: 2" in out and "2026-11-16 08:00 UTC" in out and "few seconds" in out
    assert cli.main(["pod-browser-sessions-revoke", "ws", "p-0", "--yes", "-o", "json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert {k: printed[k] for k in REVOKED} == REVOKED


def test_old_command_names_are_gone(capsys):
    for old in ("pod-lock", "pod-unlock", "pod-keys", "pod-key-create", "pod-key-roll", "pod-key-revoke", "forward"):
        with pytest.raises(SystemExit):
            cli.main([old, "ws", "p-0", "3000"])
    capsys.readouterr()


def test_cli_port_key_roll_of_a_key_no_longer_listed_stops_before_the_overlap_line(api, capsys, non_tty):
    # A key revoked, or expired more than 30 days ago, is off the list: say so first, ask nothing, send nothing.
    api.routes[("GET", KEYS_PATH)] = (200, {"keys": [KEY_ITEM]})
    assert cli.main(["port-key-roll", "ws", "p-0", "3000", "77", "--yes"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: port 3000 of pod p-0 has no access key 77.")
    assert "stops working" not in captured.err and "Roll access key" not in captured.err
    assert "meshive port-key-create ws p-0 3000 --name NAME" in captured.err and captured.out == ""
    assert not api.sent("POST", f"{KEYS_PATH}/77/roll")


WS_SESSIONS_PATH = "/v1/sdk/workspaces/ws/browser-sessions/revoke"
WS_REVOKED = {"podName": None, "endpointCount": 5, "revokedAt": "2026-11-17T08:00:00Z"}


def test_revoke_workspace_browser_sessions_request_and_result():
    api = Api({("POST", WS_SESSIONS_PATH): (200, WS_REVOKED)})
    result = sync_client(api).revoke_workspace_browser_sessions("ws", idempotency_key="ws-1")
    request = api.sent("POST", WS_SESSIONS_PATH)[0]
    assert request.headers["Idempotency-Key"] == "ws-1" and "workspace" not in request.url.params
    assert isinstance(result, BrowserSessionsRevoked)
    assert (result.pod_name, result.endpoint_count) == ("", 5)
    assert result.revoked_at.isoformat() == "2026-11-17T08:00:00+00:00"
    result = asyncio.run(async_client(Api({("POST", WS_SESSIONS_PATH): (200, WS_REVOKED)}))
                         .revoke_workspace_browser_sessions("ws"))
    assert result.endpoint_count == 5


def test_revoke_workspace_browser_sessions_checks_arguments_and_older_servers():
    api = Api()
    with pytest.raises(ValueError):
        sync_client(api).revoke_workspace_browser_sessions(" ")
    assert not api.requests
    with pytest.raises(NotFoundError, match="does not support signing out the browser sessions of a whole workspace"):
        sync_client(Api({("POST", WS_SESSIONS_PATH): (404, {"detail": "Not Found"})})
                    ).revoke_workspace_browser_sessions("ws")


def test_cli_workspace_browser_sessions_revoke_asks_first(api, capsys, non_tty):
    api.routes[("POST", WS_SESSIONS_PATH)] = (200, WS_REVOKED)
    assert cli.main(["workspace-browser-sessions-revoke", "ws"]) == 2
    err = capsys.readouterr().err
    assert "any pod in workspace ws" in err and "Access keys keep working" in err and "--yes" in err
    assert not api.sent("POST", WS_SESSIONS_PATH)
    assert cli.main(["workspace-browser-sessions-revoke", "ws", "--yes"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"workspace: +ws", out) and "public ports: 5" in out and "2026-11-17 08:00 UTC" in out
    assert cli.main(["workspace-browser-sessions-revoke", "ws", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == "ws\n"
