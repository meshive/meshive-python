import importlib
import json

import pytest

# `meshive.cli.__init__` does `from .main import main`, which shadows the
# `main` submodule attribute — import the module explicitly via importlib.
cli = importlib.import_module("meshive.cli.main")
from meshive.exceptions import AuthenticationError
from meshive.models import Machine, Pod, WhoAmI, Workspace, WorkspaceResources


class FakeClient:
    """meshive.cli.main.Meshive 자리에 주입되는 가짜 클라이언트."""

    last_kwargs = None

    def __init__(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        self.closed = False

    def me(self):
        return WhoAmI("a@b.com", "alice", "user", raw={"email": "a@b.com"})

    def list_workspaces(self):
        return [
            Workspace("ns", "Team", "d", 1, "active", "1.0",
                      WorkspaceResources(2, 0, 0), raw={"namespaceName": "ns"}),
            Workspace("ns2", "Team2", "d", 1, "active", "1.0",
                      WorkspaceResources(1, 0, 0), raw={"namespaceName": "ns2"}),
        ]

    def list_pods(self, workspace):
        if workspace == "ns2":
            return [Pod("pod-other", "ns2", "sidecar", "running", "demand", "0.5", False,
                        raw={"podName": "pod-other"})]
        return [
            Pod("pod-run", workspace, "trainer", "running", "demand", "0.9", False, raw={"podName": "pod-run"}),
            Pod("pod-stop", workspace, "notebook", "stopped", "spot", "0.4", False, raw={"podName": "pod-stop"}),
            Pod("pod-err", workspace, "trainer-2", "error", "spot", "0.4", False, raw={"podName": "pod-err"}),
        ]

    def get_pod(self, pod_name, workspace):
        return Pod(pod_name, workspace, "alias", "running", "on_demand", "0.9", False,
                   raw={"podName": pod_name})

    def list_machines(self):
        return [
            Machine("mac-gpu", "trainer-node", "gpu", "ONLINE", "NVIDIA H100", 8,
                    2.5, 0.999, "gold", raw={"id": "mac-gpu"}),
            Machine("mac-cpu", "builder-node", "cpu", "OFFLINE", "", 0,
                    0.0, 0.5, "silver", raw={"id": "mac-cpu"}),
        ]

    def get_machine(self, machine_id):
        return Machine(machine_id, "trainer-node", "gpu", "ONLINE", "NVIDIA H100", 8,
                       2.5, 0.999, "gold", raw={"id": machine_id})

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def patch_client(monkeypatch):
    monkeypatch.setattr(cli, "Meshive", FakeClient)


def test_me_human_output(capsys):
    assert cli.main(["me"]) == 0
    out = capsys.readouterr().out
    assert "a@b.com" in out
    assert "alice" in out


def test_whoami_alias(capsys):
    assert cli.main(["whoami"]) == 0
    assert "a@b.com" in capsys.readouterr().out


def test_me_json_output(capsys):
    assert cli.main(["me", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"email": "a@b.com"}


def test_workspaces_output(capsys):
    assert cli.main(["workspaces"]) == 0
    out = capsys.readouterr().out
    assert "ns" in out and "active" in out
    assert "Team" in out  # NAME(workspace_name) shown alongside ID(namespace_name)
    assert out.index("NAME") < out.index("ID")  # NAME first, then ID




def test_pods_output(capsys):
    assert cli.main(["pods", "team-ns"]) == 0
    out = capsys.readouterr().out
    assert "pod-run" in out and "pod-stop" in out and "pod-err" in out  # IDs
    assert "trainer" in out and "notebook" in out  # NAMEs (aliases)


def test_pods_name_filter(capsys):
    assert cli.main(["pods", "team-ns", "--name", "train"]) == 0
    out = capsys.readouterr().out
    assert "pod-run" in out and "pod-err" in out   # trainer, trainer-2
    assert "pod-stop" not in out                   # notebook


def test_pods_name_filter_combined_with_status(capsys):
    assert cli.main(["pods", "team-ns", "--name", "train", "--status", "error"]) == 0
    out = capsys.readouterr().out
    assert "pod-err" in out
    assert "pod-run" not in out and "pod-stop" not in out


def test_pods_status_filter(capsys):
    assert cli.main(["pods", "team-ns", "--status", "running"]) == 0
    out = capsys.readouterr().out
    assert "pod-run" in out
    assert "pod-stop" not in out and "pod-err" not in out


def test_pods_status_filter_comma(capsys):
    assert cli.main(["pods", "team-ns", "--status", "running,error"]) == 0
    out = capsys.readouterr().out
    assert "pod-run" in out and "pod-err" in out
    assert "pod-stop" not in out


def test_pods_status_filter_repeatable(capsys):
    assert cli.main(["pods", "team-ns", "--status", "running", "--status", "error"]) == 0
    out = capsys.readouterr().out
    assert "pod-run" in out and "pod-err" in out
    assert "pod-stop" not in out


def test_pods_rental_filter(capsys):
    assert cli.main(["pods", "team-ns", "--rental", "spot"]) == 0
    out = capsys.readouterr().out
    assert "pod-stop" in out and "pod-err" in out
    assert "pod-run" not in out


def test_pods_combined_filter(capsys):
    assert cli.main(["pods", "team-ns", "--status", "error", "--rental", "spot"]) == 0
    out = capsys.readouterr().out
    assert "pod-err" in out
    assert "pod-run" not in out and "pod-stop" not in out


def test_pods_unknown_status_errors(capsys):
    assert cli.main(["pods", "team-ns", "--status", "runnning"]) == 2  # typo
    err = capsys.readouterr().err
    assert "unknown status" in err and "runnning" in err


def test_pods_filter_no_match(capsys):
    assert cli.main(["pods", "team-ns", "--status", "terminated"]) == 0
    assert "No pods." in capsys.readouterr().out


def test_pods_all_aggregates_across_workspaces(capsys):
    assert cli.main(["pods", "--all"]) == 0
    out = capsys.readouterr().out
    assert "WORKSPACE" in out                          # extra column in --all mode
    assert "pod-run" in out and "pod-other" in out     # from ns and ns2
    assert "ns2" in out                                # workspace shown


def test_pods_all_respects_filters(capsys):
    assert cli.main(["pods", "--all", "--status", "running"]) == 0
    out = capsys.readouterr().out
    assert "pod-run" in out and "pod-other" in out     # both running
    assert "pod-stop" not in out and "pod-err" not in out


def test_pods_requires_workspace_or_all(capsys):
    assert cli.main(["pods"]) == 2
    assert "provide a workspace" in capsys.readouterr().err


def test_pods_workspace_and_all_conflict(capsys):
    assert cli.main(["pods", "team-ns", "--all"]) == 2
    assert "not both" in capsys.readouterr().err


def test_pods_single_workspace_has_no_workspace_column(capsys):
    assert cli.main(["pods", "team-ns"]) == 0
    assert "WORKSPACE" not in capsys.readouterr().out


def test_pod_json_output(capsys):
    assert cli.main(["pod", "team-ns", "pod-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"podName": "pod-1"}


def test_machines_output(capsys):
    assert cli.main(["machines"]) == 0
    out = capsys.readouterr().out
    assert "mac-gpu" in out and "mac-cpu" in out            # IDs
    assert "trainer-node" in out and "builder-node" in out  # NAMEs
    assert "8x NVIDIA H100" in out                          # GPU cell
    assert out.index("NAME") < out.index("ID")              # NAME first, then ID


def test_machines_alias_m(capsys):
    assert cli.main(["m"]) == 0
    assert "mac-gpu" in capsys.readouterr().out


def test_machines_type_filter(capsys):
    assert cli.main(["machines", "--type", "gpu"]) == 0
    out = capsys.readouterr().out
    assert "mac-gpu" in out
    assert "mac-cpu" not in out


def test_machines_status_filter_case_insensitive(capsys):
    # status enum 검증 없음 — 소문자 입력이 ONLINE 과 매칭돼야 한다.
    assert cli.main(["machines", "--status", "online"]) == 0
    out = capsys.readouterr().out
    assert "mac-gpu" in out
    assert "mac-cpu" not in out


def test_machines_status_filter_comma(capsys):
    assert cli.main(["machines", "--status", "online,offline"]) == 0
    out = capsys.readouterr().out
    assert "mac-gpu" in out and "mac-cpu" in out


def test_machines_name_filter(capsys):
    assert cli.main(["machines", "--name", "trainer"]) == 0
    out = capsys.readouterr().out
    assert "mac-gpu" in out
    assert "mac-cpu" not in out


def test_machines_filter_no_match(capsys):
    # FakeClient 머신은 gpu/cpu 뿐 → storage 필터는 매칭 0건.
    assert cli.main(["machines", "--type", "storage"]) == 0
    assert "No machines." in capsys.readouterr().out


def test_machine_single_output(capsys):
    assert cli.main(["machine", "mac-gpu"]) == 0
    out = capsys.readouterr().out
    assert "mac-gpu" in out and "trainer-node" in out
    assert "8x NVIDIA H100" in out
    assert "99.9%" in out  # uptime_rate 0.999 → percent


def test_machine_json_output(capsys):
    assert cli.main(["machine", "mac-gpu", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": "mac-gpu"}


def test_passes_credentials_to_client():
    cli.main(["me", "--api-key", "meshive_x", "--base-url", "https://api.dev"])
    assert FakeClient.last_kwargs == {"api_key": "meshive_x", "base_url": "https://api.dev"}


def test_api_error_returns_nonzero(monkeypatch, capsys):
    def raise_auth(self):
        raise AuthenticationError(401, "Invalid API key.", title="Unauthorized")

    monkeypatch.setattr(FakeClient, "me", raise_auth)
    assert cli.main(["me"]) == 1
    assert "Error:" in capsys.readouterr().err
