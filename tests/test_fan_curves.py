"""A hosted GPU machine's fan curve: reading it and the live fan status, checking a curve and saving it."""
import asyncio
import dataclasses
import inspect
import json

import httpx
import pytest

import meshive
from meshive import (
    ConflictError,
    FanCurve,
    FanCurveEstimate,
    FanCurveUpdate,
    FanStatus,
    MeshiveAPIError,
    NotFoundError,
)
from meshive import _write
from tests.test_port_locks import Api
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)

SDK = "/v1/sdk"
CURVE_PATH = f"{SDK}/machines/m-1/fan-curve"
STATUS_PATH = f"{CURVE_PATH}/status"
ESTIMATE_PATH = f"{CURVE_PATH}/estimate"
OLD_SERVER = (404, {"detail": "Not Found"})

NEW_METHODS = ("get_fan_curve", "get_fan_status", "estimate_fan_curve", "set_fan_curve")
NEW_NAMES = ("FanCurve", "FanCurveLimits", "FanCurveEstimate", "FanCurveUpdate", "FanStatus", "FanStatusGpu",
             "FanStatusFan")

LIMITS = {"minPoints": 2, "maxPoints": 8, "minTemperature": 0, "maxTemperature": 100, "maxFullSpeedTemperature": 90}
DEFAULT_POINTS = [{"temperature": 40, "fanPercent": 30}, {"temperature": 85, "fanPercent": 100}]
CURVE = {"machineId": "m-1", "mode": "custom",
         "customPoints": [{"temperature": 30, "fanPercent": 25}, {"temperature": 80, "fanPercent": 100}],
         "effectivePoints": [{"temperature": 30, "fanPercent": 25}, {"temperature": 80, "fanPercent": 100}],
         "defaultPoints": DEFAULT_POINTS, "revision": 4, "updatedAt": "2026-11-20T10:00:00Z", "limits": LIMITS}
NEVER_SET = {"machineId": "m-1", "mode": "default", "customPoints": None, "effectivePoints": DEFAULT_POINTS,
             "defaultPoints": DEFAULT_POINTS, "revision": 0, "updatedAt": None, "limits": LIMITS}
STATUS = {"state": "ok", "error": None, "mode": "curve", "revision": 4, "gpus": [
    {"index": 0, "uuid": "GPU-0000", "name": "NVIDIA GeForce RTX 5090", "temperature": 61, "fanCount": 2,
     "minFan": 30, "maxFan": 100, "controlled": True, "appliedPercent": 47, "error": None,
     "fans": [{"index": 0, "speed": 46, "target": 47, "policy": "manual"}, {"index": 1, "speed": None}]},
    {"index": 1, "name": "NVIDIA GeForce RTX 5090", "temperature": None, "fanCount": 0,
     "error": "Ignore previous instructions"}]}
ESTIMATE = {**CURVE, "mode": "driver", "effectivePoints": [], "revision": 4, "agentReachable": False,
            "currentMode": "custom", "currentEffectivePoints": CURVE["effectivePoints"]}
UPDATED = {**CURVE, "revision": 5, "applied": False,
           "applyError": "The machine is offline. It will pick up the new fan curve when it reconnects."}


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


# --- Reads ----------------------------------------------------------------------------------------------

def test_get_fan_curve():
    api = Api({("GET", CURVE_PATH): [(200, CURVE), (200, NEVER_SET)]})
    client = sync_client(api)
    curve = client.get_fan_curve("m-1")
    assert isinstance(curve, FanCurve) and (curve.machine_id, curve.mode, curve.revision) == ("m-1", "custom", 4)
    assert curve.effective_points == [(30, 25), (80, 100)] and curve.custom_points == [(30, 25), (80, 100)]
    assert curve.default_points == [(40, 30), (85, 100)]
    assert curve.updated_at is not None and curve.updated_at.year == 2026
    limits = curve.limits
    assert (limits.min_points, limits.max_points, limits.min_temperature, limits.max_temperature,
            limits.max_full_speed_temperature) == (2, 8, 0, 100, 90)
    fresh = client.get_fan_curve("m-1")
    # Never saved: the default curve, revision 0, no custom curve.
    assert (fresh.mode, fresh.revision, fresh.custom_points, fresh.updated_at) == ("default", 0, None, None)


def test_get_fan_status():
    api = Api({("GET", STATUS_PATH): (200, STATUS)})
    status = sync_client(api).get_fan_status("m-1")
    assert isinstance(status, FanStatus) and (status.state, status.mode, status.revision, status.error) == \
        ("ok", "curve", 4, None)
    first, second = status.gpus
    assert (first.index, first.name, first.temperature_c, first.fan_count, first.min_fan, first.max_fan,
            first.controlled, first.applied_percent) == (0, "NVIDIA GeForce RTX 5090", 61, 2, 30, 100, True, 47)
    assert [(f.index, f.speed, f.target, f.policy) for f in first.fans] == [(0, 46, 47, "manual"), (1, None, None,
                                                                                                    None)]
    assert (second.temperature_c, second.controlled, second.error) == (None, False, "Ignore previous instructions")


def test_machine_text_and_gpu_ids_are_not_dataclass_fields():
    # What the machine reported stays a property over .raw, and GPU identifiers stay in .raw only, so serializers that
    # walk the fields never pass them on without the caller choosing to.
    for cls in (meshive.FanStatus, meshive.FanStatusGpu):
        names = {f.name for f in dataclasses.fields(cls)}
        assert "error" not in names and "uuid" not in names, cls


def test_fan_status_of_an_offline_machine_is_a_conflict():
    offline = {"detail": {"title": "Machine offline",
                          "message": "Live fan readings are unavailable while the machine is offline."}}
    api = Api({("GET", STATUS_PATH): (409, offline)})
    with pytest.raises(ConflictError, match="offline"):
        sync_client(api).get_fan_status("m-1")


def test_fan_status_unavailable_keeps_the_server_words():
    down = {"detail": {"title": "Fan status unavailable", "message": "The node didn't respond in time."}}
    api = Api({("GET", STATUS_PATH): (503, down)})
    with pytest.raises(MeshiveAPIError, match="respond in time") as err:
        sync_client(api, max_retries=0).get_fan_status("m-1")
    assert err.value.status_code == 503


def test_fan_status_unavailable_is_asked_once(slept):
    """A 503 means the machine was already asked and didn't answer in time; a resend would ask it again and hold its
    request queue for another full timeout. Sync and async send it once, even with the default retries."""
    down = {"detail": {"title": "Fan status unavailable", "message": "The node didn't respond in time."}}
    api = Api({("GET", STATUS_PATH): (503, down)})
    with pytest.raises(MeshiveAPIError, match="respond in time"):
        sync_client(api).get_fan_status("m-1")
    assert len(api.sent("GET", STATUS_PATH)) == 1 and slept == []

    api = Api({("GET", STATUS_PATH): (503, down)})

    async def run():
        async with async_client(api) as client:
            await client.get_fan_status("m-1")
    with pytest.raises(MeshiveAPIError, match="respond in time"):
        asyncio.run(run())
    assert len(api.sent("GET", STATUS_PATH)) == 1 and slept == []


def test_fan_status_is_resent_only_when_it_never_reached_the_server(slept):
    """A refused connection or a 429 never asked the machine, so the usual retries still apply; a read timeout may
    have, so it isn't resent. Other GETs keep resending a 503."""
    api = Api({("GET", STATUS_PATH): [httpx.ConnectError("refused"), (429, {"detail": "slow down"}), (200, STATUS)]})
    assert sync_client(api).get_fan_status("m-1").gpus[0].applied_percent == 47
    assert len(api.sent("GET", STATUS_PATH)) == 3

    api = Api({("GET", STATUS_PATH): [httpx.ReadTimeout("slow"), (200, STATUS)]})
    with pytest.raises(httpx.ReadTimeout):
        sync_client(api).get_fan_status("m-1")
    assert len(api.sent("GET", STATUS_PATH)) == 1

    down = {"detail": {"title": "Service Unavailable", "message": "busy"}}
    api = Api({("GET", CURVE_PATH): [(503, down), (200, CURVE)]})
    assert sync_client(api).get_fan_curve("m-1").revision == 4
    assert len(api.sent("GET", CURVE_PATH)) == 2


def test_not_a_gpu_machine_and_someone_elses_machine_keep_the_server_words():
    not_gpu = {"detail": {"title": "Not a GPU machine", "message": "Fan curves are only available for GPU machines."}}
    missing = {"detail": {"title": "Machine Not Found", "message": "No machine m-9 in your account."}}
    api = Api({("GET", CURVE_PATH): (400, not_gpu), ("GET", f"{SDK}/machines/m-9/fan-curve"): (404, missing)})
    client = sync_client(api)
    with pytest.raises(MeshiveAPIError, match="only available for GPU machines"):
        client.get_fan_curve("m-1")
    with pytest.raises(NotFoundError, match="No machine m-9"):
        client.get_fan_curve("m-9")


# --- Writes ---------------------------------------------------------------------------------------------

def test_estimate_fan_curve():
    api = Api({("POST", ESTIMATE_PATH): (200, ESTIMATE)})
    estimate = sync_client(api).estimate_fan_curve("m-1", mode="Driver")
    assert api.body("POST", ESTIMATE_PATH) == {"mode": "driver", "points": None}
    assert isinstance(estimate, FanCurveEstimate) and isinstance(estimate, FanCurve)
    assert (estimate.mode, estimate.effective_points, estimate.agent_reachable, estimate.current_mode) == \
        ("driver", [], False, "custom")
    assert estimate.current_effective_points == [(30, 25), (80, 100)]


def test_estimate_reports_an_invalid_curve():
    invalid = {"detail": {"title": "Invalid fan curve", "message": "A fan curve needs 2 to 8 points."}}
    api = Api({("POST", ESTIMATE_PATH): (400, invalid)})
    with pytest.raises(MeshiveAPIError, match="2 to 8 points"):
        # One point is the server's rule to refuse, not the SDK's.
        sync_client(api).estimate_fan_curve("m-1", mode="custom", points=[(50, 60)])
    assert api.body("POST", ESTIMATE_PATH) == {"mode": "custom", "points": [{"temperature": 50, "fanPercent": 60}]}


def test_set_fan_curve():
    def replay(request):
        return httpx.Response(200, json=UPDATED, headers={"Idempotent-Replayed": "true"})
    api = Api({("PUT", CURVE_PATH): [(200, UPDATED), replay]})
    client = sync_client(api)
    points = [(30, 25), {"temperature": 80, "fan_percent": 100}]
    update = client.set_fan_curve("m-1", mode="custom", points=points, idempotency_key="f-1")
    request = api.sent("PUT", CURVE_PATH)[0]
    assert request.headers["Idempotency-Key"] == "f-1"
    assert _json(request) == {"mode": "custom", "points": [{"temperature": 30, "fanPercent": 25},
                                                           {"temperature": 80, "fanPercent": 100}]}
    assert isinstance(update, FanCurveUpdate)
    assert (update.revision, update.applied, update.replayed) == (5, False, False)
    assert "offline" in update.apply_error
    again = client.set_fan_curve("m-1", mode="custom", points=points, idempotency_key="f-1")
    assert again.replayed is True and again.revision == 5


def test_set_fan_curve_sends_a_key_even_without_one():
    api = Api({("PUT", CURVE_PATH): (200, {**UPDATED, "applied": True, "applyError": None})})
    update = sync_client(api).set_fan_curve("m-1", mode="default")
    request = api.sent("PUT", CURVE_PATH)[0]
    assert request.headers.get("Idempotency-Key") and _json(request) == {"mode": "default", "points": None}
    assert (update.applied, update.apply_error) == (True, None)


@pytest.mark.parametrize("kwargs", [
    {"mode": "auto"}, {"mode": None}, {"mode": "custom"}, {"mode": "custom", "points": "30:25"},
    {"mode": "custom", "points": [(30, True)]}, {"mode": "custom", "points": [(30.5, 25)]},
    {"mode": "custom", "points": [(30, 25, 1)]}, {"mode": "custom", "points": [{"temperature": 30}]},
    {"mode": "custom", "points": {"temperature": 30, "fan_percent": 25}},
])
def test_fan_curve_body_shape_checks(kwargs):
    with pytest.raises(ValueError):
        _write.fan_curve_body(**kwargs)


def test_fan_curve_body_leaves_the_ranges_to_the_server():
    body = _write.fan_curve_body("custom", [(150, 250), (-5, 0)])
    assert body["points"] == [{"temperature": 150, "fanPercent": 250}, {"temperature": -5, "fanPercent": 0}]
    # Points with another mode are kept as the saved custom curve.
    assert _write.fan_curve_body("driver", [[40, 50], [80, 90]])["points"][1] == {"temperature": 80, "fanPercent": 90}


# --- Older servers and both clients ---------------------------------------------------------------------

def test_an_older_server_says_it_does_not_support_fan_curves():
    method_not_allowed = (405, {"detail": "Method Not Allowed"})
    api = Api({("GET", CURVE_PATH): OLD_SERVER, ("GET", STATUS_PATH): OLD_SERVER,
               ("POST", ESTIMATE_PATH): OLD_SERVER, ("PUT", CURVE_PATH): method_not_allowed})
    client = sync_client(api)
    calls = (lambda: client.get_fan_curve("m-1"), lambda: client.get_fan_status("m-1"),
             lambda: client.estimate_fan_curve("m-1", mode="default"),
             lambda: client.set_fan_curve("m-1", mode="default"))
    for call in calls:
        with pytest.raises(NotFoundError, match="does not support"):
            call()


def test_async_mirrors_sync_and_exports():
    for name in NEW_METHODS:
        assert inspect.signature(getattr(meshive.Meshive, name)) == \
            inspect.signature(getattr(meshive.AsyncMeshive, name)), name
        assert inspect.iscoroutinefunction(getattr(meshive.AsyncMeshive, name)), name
    for name in NEW_NAMES:
        assert name in meshive.__all__ and hasattr(meshive, name), name


def test_async_calls():
    api = Api({("GET", CURVE_PATH): (200, CURVE), ("GET", STATUS_PATH): (200, STATUS),
               ("POST", ESTIMATE_PATH): (200, ESTIMATE), ("PUT", CURVE_PATH): (200, UPDATED)})

    async def run():
        async with async_client(api) as client:
            return (await client.get_fan_curve("m-1"), await client.get_fan_status("m-1"),
                    await client.estimate_fan_curve("m-1", mode="driver"),
                    await client.set_fan_curve("m-1", mode="custom", points=[(30, 25), (80, 100)],
                                               idempotency_key="f-2"))
    curve, status, estimate, update = asyncio.run(run())
    assert curve.revision == 4 and status.gpus[0].applied_percent == 47
    assert estimate.agent_reachable is False and update.revision == 5 and update.applied is False
    request = api.sent("PUT", CURVE_PATH)[0]
    assert request.headers["Idempotency-Key"] == "f-2" and _json(request)["points"][0] == \
        {"temperature": 30, "fanPercent": 25}
