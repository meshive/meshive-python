"""send_feedback (SDK, async SDK, `meshive feedback`) and a pod's recent exits (`Pod.recent_exits`, `meshive pod`)."""
import asyncio
import importlib
import json

import httpx
import pytest

from meshive import FeedbackSent, NotFoundError, Pod, PodExit, RateLimitError
from meshive import _write
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)
from tests.test_sdk_write import Recorder

cli = importlib.import_module("meshive.cli.main")

SENT = {"received": True, "kind": "bug", "tool": "create_pod", "errorCode": "server_error"}


# --- send_feedback ---------------------------------------------------------------------------------------------

def test_send_feedback_builds_the_request():
    rec = Recorder((200, SENT))
    sent = sync_client(rec).send_feedback("create_pod failed", kind="Bug", details="Did: x\nExpected: y",
                                          tool="create_pod", error_code="server_error", idempotency_key="k-fb-1")
    req = rec.last
    assert (req.method, req.url.path) == ("POST", "/v1/sdk/feedback")
    assert req.headers["Idempotency-Key"] == "k-fb-1"
    assert json.loads(req.content) == {"summary": "create_pod failed", "kind": "bug", "details": "Did: x\nExpected: y",
                                       "tool": "create_pod", "errorCode": "server_error"}
    assert isinstance(sent, FeedbackSent)
    assert (sent.received, sent.kind, sent.tool, sent.error_code) == (True, "bug", "create_pod", "server_error")


def test_send_feedback_sends_only_what_is_given():
    rec = Recorder((200, {"received": True}))
    sent = sync_client(rec).send_feedback("  Please add H200s  ", details="  ", tool="")
    assert json.loads(rec.last.content) == {"summary": "Please add H200s"}
    assert rec.last.headers["Idempotency-Key"]          # generated when not given
    assert (sent.kind, sent.tool, sent.error_code) == (None, None, None)


@pytest.mark.parametrize("kwargs,message", [
    ({"summary": " "}, "summary"),
    ({"summary": "x", "kind": "praise"}, "kind must be one of bug, idea, complaint"),
    ({"summary": "x", "details": 3}, "details must be a string"),
])
def test_send_feedback_checks_arguments_before_sending(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _write.feedback_body(**kwargs)


def test_async_send_feedback():
    rec = Recorder((200, SENT))
    sent = asyncio.run(_send_async(rec))
    assert json.loads(rec.last.content)["summary"] == "it broke"
    assert sent.error_code == "server_error"


async def _send_async(rec):
    async with async_client(rec) as client:
        return await client.send_feedback("it broke", kind="bug")


def test_feedback_turned_off_and_older_servers():
    off = Recorder((404, {"detail": {"title": "Feedback Unavailable", "code": "feedback_disabled",
                                     "message": "Meshive isn't taking feedback through the API right now."}}))
    with pytest.raises(NotFoundError) as err:
        sync_client(off).send_feedback("x")
    assert err.value.title == "Feedback Unavailable"
    older = Recorder((404, {"detail": "Not Found"}))
    with pytest.raises(NotFoundError, match="does not support feedback yet"):
        sync_client(older).send_feedback("x")


def test_feedback_rate_limit_is_not_waited_out():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "300"},
                              json={"detail": {"title": "Too Many Requests", "message": "Rate limit exceeded."}})
    with pytest.raises(RateLimitError):
        sync_client(handler).send_feedback("x")
    assert len(requests) == 1                          # a 5-minute wait is left to the caller


# --- Pod.recent_exits ---------------------------------------------------------------------------------------------

EXITS = [
    {"at": "2026-11-17T03:40:00Z", "reason": "Error", "oomKind": "vram", "exitCode": 1,
     "message": "GPU out of memory: torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB"},
    {"at": "2026-11-17T03:10:00Z", "reason": "OOMKilled", "oomKind": "ram", "exitCode": 137, "message": None},
    {"at": None, "reason": "ContainerCannotRun", "oomKind": None, "exitCode": None, "message": "exec failed"},
]


def test_pod_detail_parses_recent_exits():
    pod = Pod.from_dict({"podName": "p-0", "recentExits": EXITS + ["junk"]})
    assert [type(e) for e in pod.recent_exits] == [PodExit] * 3
    vram, ram, crash = pod.recent_exits
    assert (vram.reason, vram.oom_kind, vram.exit_code) == ("Error", "vram", 1)
    assert vram.at.isoformat() == "2026-11-17T03:40:00+00:00"
    assert (ram.oom_kind, ram.exit_code, ram.message) == ("ram", 137, None)
    assert (crash.at, crash.exit_code) == (None, None)


def test_list_items_and_older_servers_have_no_exits():
    assert Pod.from_dict({"podName": "p-0"}).recent_exits is None
    assert Pod.from_dict({"podName": "p-0", "recentExits": []}).recent_exits == []


def test_pod_detail_prints_the_exits(capsys):
    cli._print_pod(Pod.from_dict({"podName": "p-0", "userAlias": "trainer", "recentExits": EXITS}), False)
    out = capsys.readouterr().out
    assert "EXITED" in out and "REASON" in out
    assert "Error (out of GPU memory)" in out and "OOMKilled (out of RAM)" in out and "ContainerCannotRun" in out
    assert "137" in out and "GPU out of memory: torch.OutOfMemoryError" in out
    cli._print_pod(Pod.from_dict({"podName": "p-0", "userAlias": "trainer"}), False)
    assert "EXITED" not in capsys.readouterr().out


# --- meshive feedback -------------------------------------------------------------------------------------------

class FakeClient:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    def close(self):
        pass

    def send_feedback(self, summary, **kw):
        FakeClient.calls.append((summary, kw))
        return FeedbackSent.from_dict({"received": True, "kind": kw.get("kind")})


@pytest.fixture
def fake(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(cli, "Meshive", FakeClient)
    return FakeClient


def test_cli_feedback_asks_first(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["feedback", "The pod page is slow"]) == 2
    assert fake.calls == []
    assert cli.main(["feedback", "The pod page is slow", "--kind", "complaint", "--details", "Takes 10 s", "--yes"]) == 0
    assert fake.calls == [("The pod page is slow", {"kind": "complaint", "details": "Takes 10 s"})]
    assert "reviews every report" in capsys.readouterr().out


def test_cli_feedback_json(fake, capsys):
    assert cli.main(["feedback", "x", "--yes", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"received": True, "kind": None}
