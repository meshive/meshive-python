"""API-key activity log — list_activity request assembly (kind/keyId/outcome/action/since/until/limit/offset), parsing
of both kinds, older servers, and the `meshive activity` command."""
import asyncio
import importlib
import inspect
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import meshive
from meshive import ActivityEvent, ActivityPage, MeshiveAPIError, NotFoundError, ReadActivity
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)
from tests.test_sdk_write import Recorder

cli = importlib.import_module("meshive.cli.main")  # cli.__init__ exposes the main function, which shadows the submodule

UTC = timezone.utc

EVENT = {"id": 101, "createdAt": "2026-11-04T10:15:00Z", "keyId": 7, "keyName": "agent", "keyPrefix": "meshive_a1b2c3d4",
         "keyRevoked": False, "source": "MCP (claude-code 2.1)", "action": "pod.stop", "resourceType": "pod",
         "resourceId": "trainer", "workspace": "ws-1", "machineId": None, "statusCode": 200, "outcome": "accepted",
         "errorTitle": None, "operationId": "op-1", "replayed": False}
REFUSED = {**EVENT, "id": 100, "createdAt": "2026-11-04T10:00:00Z", "action": "machine.maintenance.schedule",
           "resourceType": "maintenance", "resourceId": None, "workspace": None, "machineId": "mac-1",
           "statusCode": 400, "outcome": "refused", "errorTitle": "Insufficient Notice Period", "keyRevoked": True,
           "keyName": None, "operationId": None}
REPLAYED = {**EVENT, "id": 99, "action": "pod.create", "resourceId": None, "statusCode": 202, "replayed": True,
            "source": "SDK & CLI"}
EVENTS = {"items": [EVENT, REFUSED, REPLAYED], "total": 120}
READ = {"hour": "2026-11-04T10:00:00Z", "keyId": 7, "keyName": "agent", "keyPrefix": "meshive_a1b2c3d4",
        "source": "SDK & CLI", "resourceType": "pods", "count": 1234, "lastAt": "2026-11-04T10:59:12Z"}
READS = {"items": [READ, {**READ, "hour": "2026-11-04T09:00:00Z", "resourceType": "machines", "count": 3,
                          "keyId": None, "keyName": None, "keyPrefix": None}]}


# --- requests ------------------------------------------------------------------------------------

def test_list_activity_defaults():
    rec = Recorder((200, EVENTS))
    page = sync_client(rec).list_activity()
    req = rec.last
    assert req.method == "GET" and req.url.path == "/v1/sdk/activity"
    assert dict(req.url.params) == {"kind": "events", "limit": "50", "offset": "0"}
    assert "Idempotency-Key" not in req.headers
    assert isinstance(page, ActivityPage) and page.total == 120 and len(page.items) == 3
    assert all(isinstance(item, ActivityEvent) for item in page.items) and page.raw == EVENTS


def test_list_activity_filters():
    rec = Recorder((200, READS))
    page = sync_client(rec).list_activity(kind="reads", key_id=7, since=date(2026, 10, 1), until="2026-11-04",
                                          limit=200, offset=400)
    assert dict(rec.last.url.params) == {"kind": "reads", "keyId": "7", "since": "2026-10-01", "until": "2026-11-04",
                                         "limit": "200", "offset": "400"}
    assert page.total is None and all(isinstance(item, ReadActivity) for item in page.items)


@pytest.mark.parametrize("outcome, action, params", [
    ("refused", "machine.", {"outcome": "refused", "action": "machine."}),         # a prefix ending in "."
    (" Error ", " pod.delete ", {"outcome": "error", "action": "pod.delete"}),    # trimmed; outcome lower-cased
    ("accepted", None, {"outcome": "accepted"}),
    (None, "machine.maintenance.schedule", {"action": "machine.maintenance.schedule"}),
])
def test_list_activity_outcome_and_action(outcome, action, params):
    rec = Recorder((200, EVENTS))
    sync_client(rec).list_activity(outcome=outcome, action=action)
    assert dict(rec.last.url.params) == {"kind": "events", "limit": "50", "offset": "0", **params}


@pytest.mark.parametrize("value, since, until", [
    (date(2026, 11, 2), "2026-11-02", "2026-11-02"),                                   # a date: the whole day
    ("2026-11-02", "2026-11-02", "2026-11-02"),
    (datetime(2026, 11, 2, 10, 30, tzinfo=UTC), "2026-11-02T10:30:00+00:00", "2026-11-02T10:30:00+00:00"),
    (datetime(2026, 11, 2, 10, 30), "2026-11-02T10:30:00+00:00", "2026-11-02T10:30:00+00:00"),   # naive is UTC
    ("2026-11-02T10:30:00Z", "2026-11-02T10:30:00+00:00", "2026-11-02T10:30:00+00:00"),
    # another offset is sent in UTC, so the server doesn't drop it
    (datetime(2026, 11, 2, 0, 0, tzinfo=timezone(timedelta(hours=9))), "2026-11-01T15:00:00+00:00",
     "2026-11-01T15:00:00+00:00"),
    # midnight UTC: since is that day; until is "up to that midnight", i.e. through the day before
    (datetime(2026, 11, 2, tzinfo=UTC), "2026-11-02", "2026-11-01"),
    ("2026-11-02T00:00:00Z", "2026-11-02", "2026-11-01"),
    ("2026-11-02T00:00", "2026-11-02", "2026-11-01"),
])
def test_activity_bounds(value, since, until):
    rec = Recorder((200, EVENTS))
    sync_client(rec).list_activity(since=value, until=value)
    params = dict(rec.last.url.params)
    assert (params["since"], params["until"]) == (since, until)


@pytest.mark.parametrize("kwargs", [
    {"kind": "actions"}, {"kind": "EVENTS"}, {"kind": None},
    {"limit": 0}, {"limit": 201}, {"limit": True}, {"limit": "50"},
    {"offset": -1}, {"offset": 1.5},
    {"key_id": True}, {"key_id": "abc"}, {"key_id": -3},
    {"since": "yesterday"}, {"since": ""}, {"until": 1730000000}, {"until": "2026-13-01"},
    {"outcome": "denied"}, {"outcome": ""}, {"outcome": 1}, {"action": ""}, {"action": "  "}, {"action": 5},
    # reads are hourly counts: they have no outcome or action to filter by
    {"kind": "reads", "outcome": "accepted"}, {"kind": "reads", "action": "pod.stop"},
])
def test_activity_arguments_are_checked_before_sending(kwargs):
    rec = Recorder((200, EVENTS))
    with pytest.raises(ValueError):
        sync_client(rec).list_activity(**kwargs)
    assert rec.requests == []


def test_async_list_activity_mirrors_sync():
    rec = Recorder((200, READS))

    async def main():
        async with async_client(rec) as c:
            reads = await c.list_activity(kind="reads", key_id="7", until=datetime(2026, 11, 5, tzinfo=UTC))
            reads_params = dict(rec.last.url.params)
            await c.list_activity(outcome="refused", action="machine.")
            return reads, reads_params, dict(rec.last.url.params)

    page, reads_params, events_params = asyncio.run(main())
    assert reads_params == {"kind": "reads", "keyId": "7", "until": "2026-11-04", "limit": "50", "offset": "0"}
    assert events_params == {"kind": "events", "outcome": "refused", "action": "machine.", "limit": "50",
                             "offset": "0"}
    assert page.total is None and page.items[0].count == 1234
    assert inspect.signature(meshive.Meshive.list_activity) == inspect.signature(meshive.AsyncMeshive.list_activity)
    assert inspect.iscoroutinefunction(meshive.AsyncMeshive.list_activity)


# --- parsing -------------------------------------------------------------------------------------

def test_activity_event_fields():
    event, refused, replayed = ActivityPage.from_dict(EVENTS).items
    assert (event.id, event.key_id, event.key_name, event.key_prefix, event.key_revoked) == (
        101, 7, "agent", "meshive_a1b2c3d4", False)
    assert (event.source, event.action, event.resource_type, event.resource_id, event.workspace) == (
        "MCP (claude-code 2.1)", "pod.stop", "pod", "trainer", "ws-1")
    assert (event.status_code, event.outcome, event.error_title, event.operation_id, event.replayed) == (
        200, "accepted", None, "op-1", False)
    assert event.created_at == datetime(2026, 11, 4, 10, 15, tzinfo=UTC) and event.machine_id is None
    assert (refused.outcome, refused.status_code, refused.error_title, refused.key_revoked) == (
        "refused", 400, "Insufficient Notice Period", True)
    assert refused.machine_id == "mac-1" and refused.resource_id is None and refused.key_name is None
    assert replayed.replayed is True and replayed.raw["source"] == "SDK & CLI"


def test_read_activity_fields():
    page = ActivityPage.from_dict(READS, kind="reads")
    first, second = page.items
    assert (first.hour, first.key_id, first.source, first.resource_type, first.count) == (
        datetime(2026, 11, 4, 10, tzinfo=UTC), 7, "SDK & CLI", "pods", 1234)
    assert first.last_at == datetime(2026, 11, 4, 10, 59, 12, tzinfo=UTC)
    assert (second.key_id, second.key_name, second.key_prefix) == (None, None, None)       # the key row is gone


def test_activity_page_tolerates_odd_payloads():
    assert ActivityPage.from_dict({}).items == [] and ActivityPage.from_dict({}).total is None
    page = ActivityPage.from_dict({"items": ["x", None, {"id": 1}], "total": 1})
    assert [e.id for e in page.items] == [1] and page.items[0].outcome == "" and page.items[0].replayed is False
    naive = ActivityEvent.from_dict({**EVENT, "createdAt": "2026-11-04T10:15:00"})
    assert naive.created_at == datetime(2026, 11, 4, 10, 15, tzinfo=UTC)


def test_activity_models_are_exported():
    for name in ("ActivityPage", "ActivityEvent", "ReadActivity"):
        assert name in meshive.__all__ and getattr(meshive, name).__name__ == name


# --- server answers ------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [(405, {"detail": "Method Not Allowed"}), (404, {"detail": "Not Found"})])
@pytest.mark.parametrize("kind", ["events", "reads"])
def test_list_activity_on_an_older_server(answer, kind):
    with pytest.raises(NotFoundError, match="does not support the activity log"):
        sync_client(Recorder(answer)).list_activity(kind=kind)


def test_async_list_activity_on_an_older_server():
    async def main():
        async with async_client(Recorder((404, {"detail": "Not Found"}))) as c:
            await c.list_activity()

    with pytest.raises(NotFoundError, match="does not support the activity log"):
        asyncio.run(main())


def test_range_refusal_passes_through():
    refusal = {"detail": {"title": "Range Too Long", "message": "Activity can be listed for at most 180 days."}}
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((400, refusal))).list_activity(since="2025-01-01")
    assert err.value.status_code == 400 and err.value.title == "Range Too Long"
    assert "does not support" not in str(err.value)


# --- CLI -----------------------------------------------------------------------------------------

@pytest.fixture
def cli_recorder(monkeypatch):
    holder: dict = {}

    def use(*responses):
        rec = Recorder(*responses)
        holder["rec"] = rec
        monkeypatch.setattr(cli, "Meshive", lambda *a, **kw: sync_client(rec))
        return rec

    return use


def test_cli_activity_table(capsys, cli_recorder):
    rec = cli_recorder((200, EVENTS))
    assert cli.main(["activity"]) == 0
    assert dict(rec.last.url.params) == {"kind": "events", "limit": "50", "offset": "0"}
    out = capsys.readouterr().out
    header = out.splitlines()[0].split()
    assert header == ["TIME", "KEY", "SOURCE", "ACTION", "TARGET", "RESULT"]
    assert "2026-11-04 10:15 UTC" in out and "pod.stop" in out and "pod trainer in ws-1" in out
    assert "MCP (claude-code 2.1)" in out and "accepted" in out
    assert "meshive_a1b2c3d4 (revoked)" in out                       # no name: the prefix, with the revoked tag
    assert "maintenance on machine mac-1" in out and "refused (400 Insufficient Notice Period)" in out
    assert "pod in ws-1" in out and "accepted, replayed" in out
    assert "Showing 1-3 of 120" in out and "--offset 3" in out


def test_cli_activity_filters_and_empty(capsys, cli_recorder):
    rec = cli_recorder((200, {"items": [], "total": 0}))
    assert cli.main(["activity", "--key", "7", "--since", "2026-10-01", "--until", "2026-11-04T12:00:00Z",
                     "--limit", "10", "--offset", "20"]) == 0
    assert dict(rec.last.url.params) == {"kind": "events", "keyId": "7", "since": "2026-10-01",
                                         "until": "2026-11-04T12:00:00+00:00", "limit": "10", "offset": "20"}
    assert "No API-key activity in this period." in capsys.readouterr().out


def test_cli_activity_outcome_and_action(capsys, cli_recorder):
    rec = cli_recorder((200, EVENTS))
    assert cli.main(["activity", "--outcome", "Refused", "--action", "machine."]) == 0
    assert dict(rec.last.url.params) == {"kind": "events", "outcome": "refused", "action": "machine.", "limit": "50",
                                         "offset": "0"}
    assert "--outcome/--action" in capsys.readouterr().out          # the paging hint names every filter


def test_cli_activity_unknown_outcome_is_refused_by_the_parser(capsys, cli_recorder):
    rec = cli_recorder((200, EVENTS))
    with pytest.raises(SystemExit) as exc:
        cli.main(["activity", "--outcome", "denied"])
    assert exc.value.code == 2 and "invalid choice" in capsys.readouterr().err and rec.requests == []


def test_cli_activity_reads(capsys, cli_recorder):
    rec = cli_recorder((200, READS))
    assert cli.main(["activity", "--reads"]) == 0
    assert rec.last.url.params["kind"] == "reads"
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["HOUR", "KEY", "SOURCE", "RESOURCE", "COUNT"]
    assert "2026-11-04 10:00 UTC" in out and "1,234" in out and "machines" in out and "Showing" not in out


def test_cli_activity_output_formats(capsys, cli_recorder):
    cli_recorder((200, EVENTS))
    assert cli.main(["activity", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == EVENTS
    assert cli.main(["activity", "-o", "name"]) == 0
    assert capsys.readouterr().out.split() == ["101", "100", "99"]
    cli_recorder((200, READS))
    assert cli.main(["activity", "--reads", "-o", "name"]) == 0
    assert capsys.readouterr().out.split() == ["2026-11-04T10:00:00+00:00", "2026-11-04T09:00:00+00:00"]


def test_cli_activity_strips_terminal_escapes(capsys, cli_recorder):
    cli_recorder((200, {"items": [{**EVENT, "keyName": "evil\x1b[31m", "errorTitle": "x\x1b]0;t\x07"}], "total": 1}))
    assert cli.main(["activity"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out


@pytest.mark.parametrize("argv", [["activity", "--since", "last week"], ["activity", "--limit", "0"],
                                  ["activity", "--offset", "-1"], ["activity", "--reads", "--outcome", "refused"],
                                  ["activity", "--reads", "--action", "pod.stop"], ["activity", "--action", " "]])
def test_cli_activity_bad_arguments_are_usage_errors(capsys, cli_recorder, argv):
    rec = cli_recorder((200, EVENTS))
    assert cli.main(argv) == 2
    assert "Error:" in capsys.readouterr().err and rec.requests == []


def test_cli_activity_on_an_older_server(capsys, cli_recorder):
    cli_recorder((404, {"detail": "Not Found"}))
    assert cli.main(["activity"]) == 1
    assert "does not support the activity log" in capsys.readouterr().err


def test_help_lists_activity(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "meshive activity" in capsys.readouterr().out
