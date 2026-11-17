"""Why a pod failed: Failure, Pod.restart_count / last_exit / creation_failure, Transaction.failure, Logs.previous,
list_pods(name=), wait_for_new_pod(pod_name=), and the CLI lines that show them."""
import asyncio
import importlib
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from meshive import (Failure, Logs, NotFoundError, Pod, PodCreationFailedError, PodExit, PreviousRun, Transaction,
                     WaitTimeoutError)
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)
from tests.test_sdk_write import POD_ROW, PULL_FAILED, Recorder, Server, _txn

cli = importlib.import_module("meshive.cli.main")
write_cli = importlib.import_module("meshive.cli._write")

OOM_LINE = "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 150.00 MiB."
LAST_EXIT = {"at": "2026-11-17T01:57:11Z", "reason": "Error", "oomKind": "vram", "exitCode": 1, "message": OOM_LINE,
             "cause": "gpu_out_of_memory", "summary": "The GPU ran out of memory."}
CREATION_FAILURE = {"reason": 'The command was not found (exit code 127). Last output: "/bin/sh: 1: exec: x: not found"',
                    "failedAt": "2026-11-17T01:44:01Z", "cause": "command_not_found",
                    "summary": "The command was not found (exit code 127).",
                    "output": "/bin/sh: 1: exec: x: not found", "exitCode": 127, "at": "2026-11-17T01:44:01Z",
                    "ref": None}


# --- models -------------------------------------------------------------------------------------------------------

def test_pod_reads_restart_count_and_last_exit():
    pod = Pod.from_dict({**POD_ROW, "status": "running", "restartCount": 3, "lastExit": LAST_EXIT})
    assert pod.restart_count == 3 and isinstance(pod.last_exit, PodExit)
    e = pod.last_exit
    assert (e.reason, e.oom_kind, e.exit_code, e.message) == ("Error", "vram", 1, OOM_LINE)
    assert (e.cause, e.summary, e.current_run) == ("gpu_out_of_memory", "The GPU ran out of memory.", None)
    assert e.at == datetime(2026, 11, 17, 1, 57, 11, tzinfo=timezone.utc)
    assert pod.creation_failure is None


def test_recent_exits_carry_cause_summary_and_current_run():
    pod = Pod.from_dict({"podName": "p-0", "recentExits": [{**LAST_EXIT, "currentRun": True},
                                                           {**LAST_EXIT, "currentRun": False}]})
    assert [e.current_run for e in pod.recent_exits] == [True, False]
    assert {e.cause for e in pod.recent_exits} == {"gpu_out_of_memory"}


def test_older_servers_leave_the_new_fields_none():
    pod = Pod.from_dict({**POD_ROW, "recentExits": [{"at": None, "reason": "Error", "exitCode": 1, "message": "x"}],
                         "creationFailure": {"reason": "CrashLoopBackOff", "failedAt": None}})
    assert (pod.restart_count, pod.last_exit, pod.creation_failure) == (None, None, None)
    assert pod.creation_failure_reason == "CrashLoopBackOff"          # still read for older servers
    e = pod.recent_exits[0]
    assert (e.cause, e.summary, e.current_run) == (None, None, None)
    assert Pod.from_dict({**POD_ROW, "lastExit": None, "restartCount": None}).last_exit is None


def test_creation_failure_is_a_failure():
    """A pod being created or whose creation failed before it existed: the server answers with a placeholder."""
    pod = Pod.from_dict({"podName": "8a3a-0", "userAlias": "c2", "status": "terminated", "transactionId": 17781,
                         "creationFailure": CREATION_FAILURE})
    f = pod.creation_failure
    assert isinstance(f, Failure)
    assert (f.cause, f.summary, f.output, f.exit_code, f.ref) == (
        "command_not_found", "The command was not found (exit code 127).", "/bin/sh: 1: exec: x: not found", 127, None)
    assert f.at == datetime(2026, 11, 17, 1, 44, 1, tzinfo=timezone.utc) and f.raw is CREATION_FAILURE
    assert pod.creation_failure_reason == CREATION_FAILURE["reason"]
    assert pod.transaction_id == 17781
    creating = Pod.from_dict({"podName": "8a3a-0", "userAlias": "c2", "status": "creating", "transactionId": 1})
    assert (creating.status, creating.creation_failure, creating.last_exit) == ("creating", None, None)
    assert creating.transaction_id == 1
    assert Pod.from_dict(POD_ROW).transaction_id is None


FAILED_STEP = {"step": "waiting_for_ready", "status": "failed", "updatedAt": "2026-11-17T01:44:01Z",
               "detail": "The command was not found (exit code 127).",
               "failure": {k: CREATION_FAILURE[k] for k in ("cause", "summary", "output", "exitCode", "at", "ref")},
               "failureDiagnostic": {"failureReason": "The command was not found (exit code 127).", "events": [],
                                     "containerStatuses": []}}


def test_transaction_failure_comes_from_the_failed_step():
    steps = [{"step": "create_statefulset", "status": "done", "detail": "", "updatedAt": "2026-11-17T01:43:30Z"},
             FAILED_STEP]
    t = Transaction.from_dict(_txn("failed", steps))
    assert isinstance(t.failure, Failure) and t.failure.cause == "command_not_found" and t.failure.exit_code == 127
    assert t.failure.output == "/bin/sh: 1: exec: x: not found"
    assert t.detail == "The command was not found (exit code 127)."
    assert t.failure_reason == "failed at step waiting_for_ready: The command was not found (exit code 127)."
    assert Transaction.from_dict(_txn("failed", PULL_FAILED)).failure is None          # older servers
    assert Transaction.from_dict(_txn("done")).failure is None


def test_failure_reason_prefers_failure_over_the_diagnostic():
    step = {**FAILED_STEP, "failureDiagnostic": {"failureReason": "something else", "oomReason": "and this"}}
    t = Transaction.from_dict(_txn("failed", [step]))
    assert t.failure_reason == "failed at step waiting_for_ready: The command was not found (exit code 127)."


def test_failure_reason_never_uses_a_normal_event():
    step = {"step": "run", "status": "failed", "detail": "", "updatedAt": "2026-10-14T00:01:00Z",
            "failureDiagnostic": {"events": [{"type": "Normal", "reason": "Pulled", "message": "pulled image"}]}}
    assert Transaction.from_dict(_txn("failed", [step])).failure_reason == "failed at step run"
    step["detail"] = "node went away"
    assert Transaction.from_dict(_txn("failed", [step])).failure_reason == "failed at step run: node went away"


PREVIOUS = {"endedAt": "2026-11-17T01:57:11Z", "reason": "Error", "exitCode": 1, "cause": "gpu_out_of_memory",
            "summary": "The GPU ran out of memory.", "output": OOM_LINE,
            "lines": [{"line": "loading weights", "ts": None}, {"line": OOM_LINE, "ts": None}],
            "truncated": False, "unavailable": False}


def test_logs_previous():
    logs = Logs.from_dict({"podName": "p-0", "workspace": "ws", "source": "live", "count": 1,
                           "lines": [{"line": "vLLM starting"}], "previous": PREVIOUS})
    assert logs.text == "vLLM starting"                         # never mixed into the current run's lines
    p = logs.previous
    assert isinstance(p, PreviousRun)
    assert (p.reason, p.exit_code, p.cause, p.summary, p.output) == (
        "Error", 1, "gpu_out_of_memory", "The GPU ran out of memory.", OOM_LINE)
    assert p.ended_at == datetime(2026, 11, 17, 1, 57, 11, tzinfo=timezone.utc)
    assert p.text == f"loading weights\n{OOM_LINE}" and (p.truncated, p.unavailable) == (False, False)
    gone = Logs.from_dict({"source": "none", "lines": [], "previous": {**PREVIOUS, "lines": [], "unavailable": True}})
    assert gone.previous.unavailable and gone.previous.text == "" and gone.previous.lines == []
    assert Logs.from_dict({"source": "live", "lines": []}).previous is None                    # older servers
    assert Logs.from_dict({"source": "live", "lines": [], "previous": None}).previous is None


# --- list_pods(name=) ---------------------------------------------------------------------------------------------

def _pod(name, alias, status="running", **extra):
    return {**POD_ROW, "podName": name, "userAlias": alias, "status": status, **extra}


def test_list_pods_by_name_sends_the_name():
    rec = Recorder((200, {"pods": [_pod("a-0", "Trainer", "creating")]}))
    pods = sync_client(rec).list_pods("ws", name=" trainer ")
    assert dict(rec.last.url.params) == {"workspace": "ws", "name": " trainer "}
    assert [p.pod_name for p in pods] == ["a-0"]


def test_list_pods_by_name_filters_what_an_older_server_returns():
    """An older server ignores `name` and returns every pod: only the ones with that display name are kept."""
    everything = [_pod("a-0", "trainer"), _pod("b-0", " Trainer "), _pod("c-0", "other"),
                  _pod("d-0", "trainer", isDownloader=True)]
    rec = Recorder((200, {"pods": everything}))
    assert [p.pod_name for p in sync_client(rec).list_pods("ws", name="TRAINER")] == ["a-0", "b-0"]
    assert sync_client(rec).list_pods("ws", name="nope") == []


def test_list_pods_without_a_name_is_unchanged():
    rec = Recorder((200, {"pods": [_pod("a-0", "x"), _pod("b-0", "y")]}))
    assert len(sync_client(rec).list_pods("ws")) == 2 and dict(rec.last.url.params) == {"workspace": "ws"}
    assert len(sync_client(rec).list_pods("ws", name="  ")) == 2 and dict(rec.last.url.params) == {"workspace": "ws"}


def test_async_list_pods_by_name():
    rec = Recorder((200, {"pods": [_pod("a-0", "trainer"), _pod("c-0", "other")]}))

    async def run():
        async with async_client(rec) as c:
            return await c.list_pods("ws", name="trainer")
    assert [p.pod_name for p in asyncio.run(run())] == ["a-0"]
    assert rec.last.url.params["name"] == "trainer"


# --- wait_for_new_pod(pod_name=) ------------------------------------------------------------------------------------

class PodServer:
    """GET /pods answers from `lists` (advancing per call); GET /pods/{name} from `details`, a (status, body) list."""

    def __init__(self, lists, details):
        self.lists, self.details, self.paths = list(lists), list(details), []

    @staticmethod
    def _next(items):
        return items.pop(0) if len(items) > 1 else items[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/sdk")
        self.paths.append(path)
        if path == "/pods":
            return httpx.Response(200, json={"pods": self._next(self.lists)})
        status, body = self._next(self.details)
        return httpx.Response(status, json=body)


NOT_FOUND = (404, {"detail": {"title": "Pod Not Found", "message": "No pod named 'new-0' in workspace 'ws'."}})


def test_wait_for_new_pod_matches_by_pod_name(slept):
    """An older pod with the same display name (being deleted) is not the one that was created."""
    old, new = _pod("old-0", "p", "terminating"), _pod("new-0", "p", "creating")
    server = PodServer(lists=[[old], [old, new]], details=[(200, _pod("new-0", "p", "running"))])
    pod = sync_client(server).wait_for_new_pod("p", "ws", pod_name="new-0", interval=5)
    assert pod.pod_name == "new-0" and pod.status == "running"
    assert server.paths == ["/pods", "/pods", "/pods/new-0"]
    by_alias = PodServer(lists=[[old, new]], details=[(200, _pod("old-0", "p", "terminating"))])
    with pytest.raises(WaitTimeoutError):  # without pod_name the first pod with the name is taken (today's behaviour)
        sync_client(by_alias).wait_for_new_pod("p", "ws", timeout=0)
    assert by_alias.paths[-1] == "/pods/old-0"


def test_a_404_inside_the_wait_is_not_yet(slept):
    server = PodServer(lists=[[_pod("new-0", "p", "creating")]],
                       details=[NOT_FOUND, NOT_FOUND, (200, _pod("new-0", "p", "running"))])
    assert sync_client(server).wait_for_new_pod("p", "ws", pod_name="new-0", interval=5).status == "running"
    assert server.paths == ["/pods", "/pods/new-0", "/pods/new-0", "/pods/new-0"] and slept == [5, 5]


def test_wait_for_pod_still_raises_not_found():
    with pytest.raises(NotFoundError):
        sync_client(PodServer(lists=[[]], details=[NOT_FOUND])).wait_for_pod("new-0", "ws")


def test_a_failed_creation_still_raises_with_the_server_sentence(slept):
    steps = [FAILED_STEP]
    server = Server(pods=[[]], transactions=[(200, _txn("in_progress")), (200, _txn("failed", steps))])
    with pytest.raises(PodCreationFailedError) as err:
        sync_client(server).wait_for_new_pod("p", "ws", transaction_id=99, pod_name="abc123-0", interval=5)
    assert err.value.reason == ("creation failed at step waiting_for_ready: "
                                "The command was not found (exit code 127).")


def test_async_wait_for_new_pod_by_pod_name(slept):
    server = PodServer(lists=[[_pod("other-0", "p"), _pod("new-0", "p", "creating")]],
                       details=[NOT_FOUND, (200, _pod("new-0", "p", "running"))])

    async def run():
        async with async_client(server) as c:
            return await c.wait_for_new_pod("p", "ws", pod_name="new-0")
    assert asyncio.run(run()).pod_name == "new-0"
    assert server.paths == ["/pods", "/pods/new-0", "/pods/new-0"]


# --- CLI ------------------------------------------------------------------------------------------------------------

def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_pod_shows_the_last_exit_under_the_status(capsys):
    at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    cli._print_pod(Pod.from_dict({**POD_ROW, "status": "running", "restartCount": 3,
                                  "lastExit": {**LAST_EXIT, "at": at}}), False)
    lines = capsys.readouterr().out.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("status:"))
    assert lines[i + 1] == "Restarted 3 times · The GPU ran out of memory. · 2 minutes ago"
    assert lines[i + 2] == f"  {OOM_LINE}"


def test_pod_line_for_one_restart_and_for_no_live_run(capsys):
    cli._print_pod(Pod.from_dict({**POD_ROW, "restartCount": 1, "lastExit": {**LAST_EXIT, "message": None}}), False)
    out = capsys.readouterr().out
    assert "Restarted 1 time · The GPU ran out of memory. · " in out and "Restarted 1 times" not in out
    cli._print_pod(Pod.from_dict({**POD_ROW, "status": "stopped", "restartCount": None, "lastExit": LAST_EXIT}), False)
    out = capsys.readouterr().out
    assert "Restarted" not in out and "\nThe GPU ran out of memory. · " in out


def test_pod_shows_a_failed_creation(capsys):
    cli._print_pod(Pod.from_dict({"podName": "8a3a-0", "userAlias": "c2", "status": "terminated",
                                  "creationFailure": CREATION_FAILURE, "lastExit": LAST_EXIT}), True)
    out = _plain(capsys.readouterr().out)
    assert "\nCreate failed: The command was not found (exit code 127).\n  /bin/sh: 1: exec: x: not found\n" in out
    assert "GPU ran out of memory" not in out                     # one failure line: the creation wins


def test_pod_without_a_failure_has_no_extra_line(capsys):
    cli._print_pod(Pod.from_dict({**POD_ROW, "status": "running", "restartCount": 0}), False)
    out = capsys.readouterr().out
    assert "Restarted" not in out and "Create failed" not in out
    cli._print_pod(Pod.from_dict({**POD_ROW, "creationFailure": {"reason": "old server text"}}), False)
    assert "Create failed" not in capsys.readouterr().out


def test_logs_print_the_previous_run_first(capsys):
    logs = Logs.from_dict({"source": "live", "count": 1, "lines": [{"line": "vLLM starting"}],
                           "previous": {**PREVIOUS, "endedAt": "2026-11-17T02:01:35Z"}})
    write_cli._print_logs(logs, False)
    assert capsys.readouterr().out.splitlines() == [
        "--- previous run (ended 02:01:35Z, exit 1: The GPU ran out of memory.) ---",
        "loading weights", OOM_LINE, "--- current run ---", "vLLM starting"]


def test_logs_of_a_pod_that_is_not_running(capsys):
    note = "Pod is not running; its last run is in previous."
    logs = Logs.from_dict({"source": "none", "count": 0, "lines": [], "note": note,
                           "previous": {**PREVIOUS, "endedAt": None, "lines": [], "unavailable": True}})
    write_cli._print_logs(logs, False)
    assert capsys.readouterr().out.splitlines() == ["--- previous run (exit 1: The GPU ran out of memory.) ---", note]
    write_cli._print_logs(Logs.from_dict({"source": "live", "lines": [{"line": "x"}]}), False)
    assert capsys.readouterr().out == "x\n"                           # no frames without a previous run
