"""CLI hosting commands — machine detail blocks, maintenance, listing, retries, removal, history, messages, credentials,
install commands and payouts: arguments → SDK calls, confirmation (--yes / non-interactive / typed ID), output formats."""
import importlib
import io
import json

import pytest

cli = importlib.import_module("meshive.cli.main")  # cli.__init__ exposes the main function, which shadows the submodule
from meshive import NotFoundError
from meshive.models import (DowntimeEvent, HostMessage, InstallCommand, ListingCheck, ListingResult, Machine,
                            MachineCredentials, MachineDeleteCheck, MachineDeleted, MachineDetail, MaintenanceAction,
                            MaintenanceEstimate, MaintenanceWindow, MessageSent, MessagesRead, Payout, PayoutAccount,
                            RetryResult, StressTestResult)
from tests.test_sdk_host import (ACTION, DELETE_CHECK, DELETED, DOWNTIME, ESTIMATE, GRAFANA, INSTALL, IPMI,
                                 LISTING, LISTING_CHECK, MACHINE_DETAIL, MESSAGE, OLD_MACHINE, PAYOUT_ACCOUNT, PAYOUTS,
                                 READ, RETRY, SENT, SSH, STRESS, WINDOW)

UPCOMING = {**WINDOW, "startTime": "2099-01-10T09:00:00Z", "endTime": "2099-01-10T12:00:00Z"}
PAST = {**WINDOW, "id": 6, "isStart": True, "isDone": True, "reason": "Fan swap"}


class FakeClient:
    instances: list["FakeClient"] = []
    detail = MACHINE_DETAIL
    windows: list | Exception = [UPCOMING, PAST]
    listing_check = LISTING_CHECK
    delete_check = DELETE_CHECK
    credentials = SSH
    messages = [MESSAGE]

    def __init__(self, *a, **kw):
        self.calls: list[tuple[str, tuple, dict]] = []
        FakeClient.instances.append(self)

    def _rec(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def close(self):
        pass

    def get_machine(self, *a, **kw):
        self._rec("get_machine", *a, **kw); return MachineDetail.from_dict(self.detail)

    def list_maintenance(self, *a, **kw):
        self._rec("list_maintenance", *a, **kw)
        if isinstance(self.windows, Exception):
            raise self.windows
        return [MaintenanceWindow.from_dict(w) for w in self.windows]

    def estimate_maintenance(self, *a, **kw):
        self._rec("estimate_maintenance", *a, **kw); return MaintenanceEstimate.from_dict(ESTIMATE)

    def _action(self, name, action, *a, **kw):
        self._rec(name, *a, **kw)
        return MaintenanceAction.from_dict({**ACTION, "action": action, "window": None if action == "cancel" else WINDOW})

    def schedule_maintenance(self, *a, **kw):
        return self._action("schedule_maintenance", "schedule", *a, **kw)

    def update_maintenance(self, *a, **kw):
        return self._action("update_maintenance", "edit", *a, **kw)

    def end_maintenance(self, *a, **kw):
        return self._action("end_maintenance", "end", *a, **kw)

    def cancel_maintenance(self, *a, **kw):
        return self._action("cancel_maintenance", "cancel", *a, **kw)

    def extend_maintenance(self, *a, **kw):
        return self._action("extend_maintenance", "extend", *a, **kw)

    def check_listing(self, *a, **kw):
        self._rec("check_listing", *a, **kw); return ListingCheck.from_dict(self.listing_check)

    def set_machine_listing(self, machine_id, listed, **kw):
        self._rec("set_machine_listing", machine_id, listed, **kw)
        return ListingResult.from_dict({**LISTING, "listed": listed})

    def retry_machine_setup(self, *a, **kw):
        self._rec("retry_machine_setup", *a, **kw); return RetryResult.from_dict(RETRY)

    def retry_port_check(self, *a, **kw):
        self._rec("retry_port_check", *a, **kw)
        return RetryResult.from_dict({**RETRY, "check": "ports", "message": "Port check requested."})

    def check_machine_delete(self, *a, **kw):
        self._rec("check_machine_delete", *a, **kw); return MachineDeleteCheck.from_dict(self.delete_check)

    def delete_machine(self, *a, **kw):
        self._rec("delete_machine", *a, **kw); return MachineDeleted.from_dict(DELETED)

    def get_downtime_history(self, *a, **kw):
        self._rec("get_downtime_history", *a, **kw); return [DowntimeEvent.from_dict(d) for d in DOWNTIME]

    def get_stress_test(self, *a, **kw):
        self._rec("get_stress_test", *a, **kw); return StressTestResult.from_dict(STRESS)

    def list_host_messages(self, *a, **kw):
        self._rec("list_host_messages", *a, **kw); return [HostMessage.from_dict(m) for m in self.messages]

    def send_host_message(self, *a, **kw):
        self._rec("send_host_message", *a, **kw); return MessageSent.from_dict(SENT)

    def mark_host_messages_read(self, *a, **kw):
        self._rec("mark_host_messages_read", *a, **kw); return MessagesRead.from_dict(READ)

    def reveal_machine_credentials(self, *a, **kw):
        self._rec("reveal_machine_credentials", *a, **kw); return MachineCredentials.from_dict(self.credentials)

    def issue_install_command(self, *a, **kw):
        self._rec("issue_install_command", *a, **kw); return InstallCommand.from_dict(INSTALL)

    def get_payout_account(self, *a, **kw):
        self._rec("get_payout_account", *a, **kw); return PayoutAccount.from_dict(PAYOUT_ACCOUNT)

    def list_payouts(self, *a, **kw):
        self._rec("list_payouts", *a, **kw); return [Payout.from_dict(p) for p in PAYOUTS]


@pytest.fixture(autouse=True)
def patch_client(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(cli, "Meshive", FakeClient)


@pytest.fixture
def non_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _calls(name=None):
    calls = FakeClient.instances[-1].calls
    return [c for c in calls if name is None or c[0] == name]


def _last(name):
    found = _calls(name)
    assert found, f"{name} not called"
    return found[-1]


# --- reads -----------------------------------------------------------------------------

def test_machine_shows_clients_maintenance_and_diagnostics(capsys):
    assert cli.main(["machine", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "name:      node-a" in out and "fee:       20.0% (promotional until 2026-12-31)" in out
    assert "messages:  2 unread (meshive host-messages --machine mac-1 --unread)" in out
    assert "Clients (other users' workspaces on this machine)" in out
    assert "renter-ws" in out and "sts-a-0" in out and "serverless" in out and "$0.450" in out
    assert "data-ws" in out and "pv-1" in out and "50 GiB" in out and "60.0%" in out
    assert "Maintenance" in out and "RAM swap" in out and "2099-01-10 09:00 UTC" in out
    assert "Fan swap" not in out and "1 past window(s): meshive maintenance mac-1" in out
    assert "Diagnostics" in out and "boot:" in out and "ports:" in out and "speed_below_max" in out
    assert _calls("list_maintenance")


def test_machine_json_is_the_raw_detail_and_skips_the_maintenance_call(capsys):
    assert cli.main(["machine", "mac-1", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["clients"]["pods"][0]["resourceName"] == "sts-a-0"
    assert not _calls("list_maintenance")
    assert cli.main(["machine", "mac-1", "-o", "name"]) == 0
    assert capsys.readouterr().out == "mac-1\n"


def test_machine_on_an_older_server(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "detail", OLD_MACHINE)
    monkeypatch.setattr(FakeClient, "windows", NotFoundError(404, "This Meshive API server does not support machine "
                                                                  "maintenance yet"))
    assert cli.main(["machine", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "status:    ● ONLINE" in out and "No pods or volumes of other users." in out
    assert "fee:" not in out and "messages:" not in out and "Maintenance" not in out
    assert "stage" in out           # what the state already says is still shown


def test_machine_with_a_failed_setup_and_boot_errors(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "detail", {
        **MACHINE_DETAIL, "clients": {"pods": [], "storages": []},
        "state": {"name": "AGENT_NOT_READY", "stage": "START_UP", "stageState": "FAILED", "setupStep": 2,
                  "totalSetupStep": 5},
        "bootReport": {"bootErrors": [{"step": "gpu_driver", "severity": "error", "message": "nvidia-smi \x1b[31mfailed"}],
                       "portCheckResults": [{"port": 2222, "protocol": "tcp", "reachable": False}],
                       "isBootSuccessful": False, "isPortCheckPassed": False},
        "networkWarnings": []})
    monkeypatch.setattr(FakeClient, "windows", [])
    assert cli.main(["machine", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "setup step:" in out and "2/5" in out and "1 error(s)" in out and "unreachable: 2222/tcp" in out
    assert "gpu_driver: nvidia-smi [31mfailed" in out and "\x1b" not in out
    assert "meshive machine-retry mac-1" in out and "No upcoming maintenance." in out


def test_maintenance_lists_every_window(capsys):
    assert cli.main(["maintenance", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "ID" in out and "STATUS" in out and "upcoming" in out and "done" in out and "Fan swap" in out
    assert cli.main(["maintenance", "mac-1", "-o", "name"]) == 0
    assert capsys.readouterr().out == "7\n6\n"
    assert cli.main(["maintenance", "mac-1", "-o", "json"]) == 0
    assert [w["id"] for w in json.loads(capsys.readouterr().out)] == [7, 6]


def test_maintenance_says_an_overdue_window_starts_on_its_own(capsys, monkeypatch):
    assert cli.main(["maintenance", "mac-1"]) == 0
    assert "starts on its own" not in capsys.readouterr().out
    overdue = {**WINDOW, "startTime": "2020-01-01T00:00:00Z", "endTime": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(FakeClient, "windows", [overdue])
    assert cli.main(["maintenance", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "overdue" in out and "starts on its own within about a minute" in out


def test_there_is_no_manual_maintenance_start(capsys):
    # A window starts on its own at its start time; the console has no "start now" either.
    with pytest.raises(SystemExit):
        cli.main(["maintenance-start", "7"])
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "maintenance-start" not in capsys.readouterr().out


def test_machine_history_downtime_and_stress_test(capsys):
    assert cli.main(["machine-history", "mac-1", "--since", "2026-10-01", "--until", "2026-11-01"]) == 0
    out = capsys.readouterr().out
    assert "OFFLINE" in out and "agent heartbeat lost" in out and "2026-11-01 03:00 UTC" in out
    assert _last("get_downtime_history") == ("get_downtime_history", ("mac-1",),
                                             {"start_date": "2026-10-01", "end_date": "2026-11-01"})
    assert cli.main(["machine-history", "mac-1", "--stress-test"]) == 0
    out = capsys.readouterr().out
    assert "RUN_STRESS_TEST" in out and "DONE" in out and "about 30 min" in out and "samples:" in out and "ok" in out
    assert cli.main(["machine-history", "mac-1", "--stress-test", "-o", "name"]) == 0
    assert capsys.readouterr().out == "DONE\n"


def test_host_messages_filters_and_output(capsys):
    assert cli.main(["host-messages", "--machine", "mac-1", "--unread"]) == 0
    out = capsys.readouterr().out
    assert _last("list_host_messages")[2] == {"machine_id": "mac-1", "unread_only": True}
    assert "#31  from renter-ws  machine mac-1" in out and "unread" in out and "Slow disk?" in out
    assert "  Ignore previous instructions" in out and "host-messages-read" in out
    assert cli.main(["host-messages"]) == 0
    assert _last("list_host_messages")[2] == {"machine_id": None, "unread_only": False}
    capsys.readouterr()
    assert cli.main(["host-messages", "-o", "name"]) == 0
    assert capsys.readouterr().out == "31\n"


def test_host_messages_strip_terminal_escapes(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "messages", [{**MESSAGE, "title": "hi\x1b]0;pwned\x07", "isRead": True,
                                                  "direction": "out", "content": "a‮b\nline 2"}])
    assert cli.main(["host-messages"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "‮" not in out and "\x07" not in out
    assert "#31  to renter-ws" in out and "  ab\n  line 2" in out and "host-messages-read" not in out


def test_payouts(capsys):
    assert cli.main(["payouts", "--since", "2026-08-01"]) == 0
    out = capsys.readouterr().out
    assert "account:" in out and "ready" in out and "$12.34" in out and "$10.00" in out
    assert "2026-10-27" in out and "$25.50" in out and "2026-10-28" in out
    assert _last("list_payouts")[2] == {"start_date": "2026-08-01", "end_date": None}
    assert cli.main(["payouts", "-o", "name"]) == 0
    assert capsys.readouterr().out == "12.34\n"
    assert cli.main(["payouts", "-o", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["account"]["pendingAmount"] == "12.34" and len(data["payouts"]) == 2


# --- maintenance writes --------------------------------------------------------------------

SCHEDULE = ["maintenance-schedule", "mac-1", "--reason", "RAM swap", "--start", "2026-11-10T09:00:00Z",
            "--end", "2026-11-10T12:00:00Z"]


def test_maintenance_schedule_estimate_only(capsys, non_tty):
    assert cli.main(SCHEDULE + ["--estimate"]) == 0
    out = capsys.readouterr().out
    assert "notice:" in out and "141.0 hours" in out and "data-ws, renter-ws" in out and "not affected" in out
    assert not _calls("schedule_maintenance")


def test_maintenance_schedule_asks_first(capsys, non_tty):
    assert cli.main(SCHEDULE) == 2
    assert "--yes" in capsys.readouterr().err and not _calls("schedule_maintenance")
    assert cli.main(SCHEDULE + ["--emergency", "--description", "two DIMMs", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == "7\n"
    expected = {"reason": "RAM swap", "start": "2026-11-10T09:00:00Z", "end": "2026-11-10T12:00:00Z",
                "description": "two DIMMs", "emergency": True}
    assert _last("estimate_maintenance") == ("estimate_maintenance", ("mac-1",), expected)
    assert _last("schedule_maintenance") == ("schedule_maintenance", ("mac-1",), expected)


def test_maintenance_schedule_question_names_who_is_emailed(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(SCHEDULE + ["-o", "json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["action"] == "schedule"          # the estimate isn't mixed into the JSON
    assert "Schedule regular maintenance 'RAM swap' on machine mac-1" in captured.err
    assert "2 workspace(s) using it are emailed now. [y/N]" in captured.err


def test_maintenance_update(capsys, non_tty):
    assert cli.main(["maintenance-update", "7", "--yes"]) == 2
    assert "nothing to change" in capsys.readouterr().err and not _calls("update_maintenance")
    assert cli.main(["maintenance-update", "7", "--reason", "New"]) == 2
    assert not _calls("update_maintenance")
    assert cli.main(["maintenance-update", "7", "--end", "2026-11-10T13:00:00Z", "--yes"]) == 0
    assert _last("update_maintenance") == ("update_maintenance", (7,), {"end": "2026-11-10T13:00:00Z"})
    assert "action:" in capsys.readouterr().out


@pytest.mark.parametrize("command, method, words", [
    ("maintenance-end", "end_maintenance", "billed again from now on"),
    ("maintenance-cancel", "cancel_maintenance", "don't get a cancellation email"),
])
def test_maintenance_window_actions_ask_first(capsys, monkeypatch, command, method, words):
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    assert cli.main([command, "7"]) == 2
    assert words in capsys.readouterr().err and not _calls(method)
    assert cli.main([command, "7", "--yes"]) == 0
    assert _last(method) == (method, (7,), {})
    out = capsys.readouterr().out
    assert "maintenance:" in out
    if command == "maintenance-end":
        assert "meshive machine-list mac-1" in out


def test_maintenance_extend(capsys, non_tty):
    assert cli.main(["maintenance-extend", "7", "--hours", "1.5", "--reason", "Parts late"]) == 2
    assert cli.main(["maintenance-extend", "7", "--hours", "1.5", "--reason", "Parts late", "--yes"]) == 0
    assert _last("extend_maintenance") == ("extend_maintenance", (7,),
                                           {"hours": 1.5, "reason": "Parts late", "description": ""})


# --- listing, retries, removal ------------------------------------------------------------------------

def test_machine_list_refused_says_why_and_sends_nothing(capsys, non_tty):
    assert cli.main(["machine-list", "mac-1"]) == 1
    err = capsys.readouterr().err
    assert "can't be listed" in err and "Listing Failed: Set prices for this GPU first." in err
    assert not _calls("set_machine_listing")


def test_machine_list_needs_no_confirmation(capsys, non_tty, monkeypatch):
    monkeypatch.setattr(FakeClient, "listing_check", {"machineId": "mac-1", "listed": False, "listable": True})
    assert cli.main(["machine-list", "mac-1"]) == 0
    assert _last("set_machine_listing") == ("set_machine_listing", ("mac-1", True), {})
    assert "listed:" in capsys.readouterr().out
    monkeypatch.setattr(FakeClient, "listing_check", {"machineId": "mac-1", "listed": True, "listable": False,
                                                      "title": "Already Listed"})
    assert cli.main(["machine-list", "mac-1"]) == 0
    assert "already listed" in capsys.readouterr().out and not _calls("set_machine_listing")


def test_machine_unlist_asks_with_the_running_pods(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["machine-unlist", "mac-1", "-o", "json"]) == 0
    captured = capsys.readouterr()
    assert "Unlist machine 'node-a'?" in captured.err and "the 1 running pod(s) keep running" in captured.err
    assert json.loads(captured.out)["listed"] is False and _last("set_machine_listing")[1] == ("mac-1", False)


def test_machine_unlist_without_yes_when_not_a_tty(capsys, non_tty):
    assert cli.main(["machine-unlist", "mac-1"]) == 2
    assert not _calls("set_machine_listing")


def test_machine_retry(capsys, non_tty):
    assert cli.main(["machine-retry", "mac-1"]) == 0
    assert _calls("retry_machine_setup") and "rebooting" in capsys.readouterr().out
    assert cli.main(["machine-retry", "mac-1", "--ports"]) == 0
    assert _calls("retry_port_check") and not _calls("retry_machine_setup")


def test_machine_delete_shows_the_check_and_asks_for_the_id(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("node-a\n"))          # the name is not the ID
    assert cli.main(["machine-delete", "mac-1"]) == 2
    captured = capsys.readouterr()
    assert "after a grace period" in captured.out and "3 (1 serverless replica(s))" in captured.out
    assert "48-hour grace period" in captured.err and "can't be undone" in captured.err
    assert "Type the machine ID (mac-1)" in captured.err and "didn't match" in captured.err
    assert not _calls("delete_machine")
    monkeypatch.setattr("sys.stdin", _Tty("mac-1\n"))
    assert cli.main(["machine-delete", "mac-1"]) == 0
    assert _last("delete_machine") == ("delete_machine", ("mac-1",), {})
    assert "grace ends:" in capsys.readouterr().out


def test_machine_delete_with_yes_and_without_a_tty(capsys, non_tty, monkeypatch):
    assert cli.main(["machine-delete", "mac-1"]) == 2
    assert "--yes" in capsys.readouterr().err and not _calls("delete_machine")
    monkeypatch.setattr(FakeClient, "delete_check", {**DELETE_CHECK, "mode": "immediate", "podCount": 0, "pvCount": 0})
    assert cli.main(["machine-delete", "mac-1", "--yes", "-o", "name"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "mac-1\n" and "grace period starts" not in captured.err


def test_machine_delete_already_in_progress_stops_before_asking(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "delete_check", {**DELETE_CHECK, "alreadyDeleting": True})
    assert cli.main(["machine-delete", "mac-1", "--yes"]) == 1
    assert "already being removed" in capsys.readouterr().err and not _calls("delete_machine")


# --- messages, credentials, install ------------------------------------------------------------------

def test_host_message_send_asks_and_sends(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["host-message-send", "mac-1", "--to", "all", "--title", "Reboot", "--content", "5 min"]) == 0
    captured = capsys.readouterr()
    assert "to every workspace using the machine now? They are notified by email." in captured.err
    assert _last("send_host_message") == ("send_host_message", ("mac-1",),
                                          {"to": "all", "title": "Reboot", "content": "5 min"})
    assert "41, 42" in captured.out


def test_host_message_send_needs_yes_when_not_a_tty(capsys, non_tty):
    assert cli.main(["host-message-send", "mac-1", "--to", "renter-ws", "--title", "Hi"]) == 2
    assert not _calls("send_host_message")
    assert cli.main(["host-message-send", "mac-1", "--to", "renter-ws", "--title", "Hi", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == "41\n42\n"
    assert _last("send_host_message")[2]["content"] == ""


@pytest.mark.parametrize("to", ["All", " ALL ", "all"])
def test_host_message_send_lowercases_all(capsys, monkeypatch, to):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["host-message-send", "mac-1", "--to", to, "--title", "Hi"]) == 0
    assert "to every workspace using the machine now?" in capsys.readouterr().err
    assert _last("send_host_message")[2]["to"] == "all"


def test_host_message_send_keeps_a_workspace_id_as_given(capsys):
    assert cli.main(["host-message-send", "mac-1", "--to", "Renter-WS", "--title", "Hi", "--yes"]) == 0
    assert _last("send_host_message")[2]["to"] == "Renter-WS"


def test_host_messages_read_needs_no_confirmation(capsys, non_tty):
    assert cli.main(["host-messages-read", "mac-1", "31", "32"]) == 0
    assert _last("mark_host_messages_read") == ("mark_host_messages_read", ("mac-1", [31, 32]), {})
    assert "marked read:" in capsys.readouterr().out


@pytest.mark.parametrize("flag, payload, secret, name_out", [
    ("--ssh", SSH, "s3cret-ssh", "ssh host@203.0.113.5 -p 2222\n"),
    ("--ipmi", IPMI, "s3cret-ipmi", "ADMIN\n"),
    ("--grafana", GRAFANA, "s3cret-graf", "https://grafana.example/d/mac-1\n"),
])
def test_machine_credentials_warns_asks_and_prints(capsys, non_tty, monkeypatch, flag, payload, secret, name_out):
    monkeypatch.setattr(FakeClient, "credentials", payload)
    assert cli.main(["machine-credentials", "mac-1", flag]) == 2
    err = capsys.readouterr().err
    assert "emails the account owner" in err and "--yes" in err and not _calls("reveal_machine_credentials")
    assert cli.main(["machine-credentials", "mac-1", flag, "--yes"]) == 0
    assert _last("reveal_machine_credentials") == ("reveal_machine_credentials", ("mac-1", flag[2:]), {})
    assert secret in capsys.readouterr().out
    assert cli.main(["machine-credentials", "mac-1", flag, "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == name_out


def test_machine_credentials_needs_one_kind(capsys):
    with pytest.raises(SystemExit):
        cli.main(["machine-credentials", "mac-1"])
    with pytest.raises(SystemExit):
        cli.main(["machine-credentials", "mac-1", "--ssh", "--ipmi"])


def test_host_install_asks_and_prints_the_command(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["host-install", "--storage"]) == 0
    captured = capsys.readouterr()
    assert "about 10 minutes" in captured.err and "share it with no one" in captured.err
    assert "new storage machine? [y/N]" in captured.err
    assert _last("issue_install_command") == ("issue_install_command", ("storage",), {})
    assert "curl -fsSL" in captured.out and "meshive machines" in captured.out


def test_host_install_defaults_to_compute_and_needs_yes(capsys, non_tty):
    assert cli.main(["host-install"]) == 2
    assert not _calls("issue_install_command")
    assert cli.main(["host-install", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == INSTALL["command"] + "\n"
    assert _last("issue_install_command")[1] == ("compute",)


@pytest.mark.parametrize("argv", [
    ["maintenance-update", "7", "--reason", "x"],
    ["maintenance-end", "7"], ["maintenance-cancel", "7"],
    ["maintenance-extend", "7", "--hours", "1", "--reason", "x"],
    ["machine-unlist", "mac-1"],
    ["host-message-send", "mac-1", "--to", "all", "--title", "t"],
    ["machine-credentials", "mac-1", "--ssh"],
    ["host-install"],
])
def test_confirm_question_goes_to_stderr(capsys, monkeypatch, argv):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(argv + ["-o", "json"]) == 0
    captured = capsys.readouterr()
    assert isinstance(json.loads(captured.out), dict) and captured.err.count("[y/N]") == 1


def test_bad_maintenance_id_is_a_usage_error(capsys):
    with pytest.raises(SystemExit):
        cli.main(["maintenance-end", "seven"])


def test_help_lists_the_hosting_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for command in ("maintenance-schedule", "machine-delete", "host-message-send", "host-messages", "payouts",
                    "machine-credentials", "host-install", "machine-history"):
        assert command in out


def test_machine_list_item_without_detail_still_prints(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "get_machine",
                        lambda self, *a, **kw: Machine.from_dict({**OLD_MACHINE, "unreadMessages": 1}))
    assert cli.main(["machine", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "name:      node-a" in out and "Clients" not in out
