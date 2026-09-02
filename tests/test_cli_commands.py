import argparse
import importlib
import json
from datetime import date, datetime, timezone

import httpx
import pytest

# `meshive.cli.__init__` does `from .main import main`, which shadows the
# `main` submodule attribute — import the module explicitly via importlib.
cli = importlib.import_module("meshive.cli.main")
from meshive.exceptions import AuthenticationError, WaitTimeoutError
from meshive.models import (
    ApiKey,
    Asset,
    AssetPage,
    AssetStorage,
    AssetVersion,
    Credit,
    CreditHistoryEntry,
    DailyCost,
    DailyEarning,
    Earnings,
    GpuAvailability,
    GpuUsage,
    Machine,
    MachineMetrics,
    Member,
    Pod,
    PodMetrics,
    ResourceCondition,
    Serving,
    Storage,
    Task,
    Template,
    WhoAmI,
    Workspace,
    WorkspaceDetail,
    WorkspaceResources,
)

UTC = timezone.utc


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

    def wait_for_pod(self, pod_name, workspace, *, until, timeout):
        FakeClient.last_wait = {"pod_name": pod_name, "workspace": workspace,
                                "until": until, "timeout": timeout}
        return self.get_pod(pod_name, workspace)

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

    # --- 0.0.7 확장 read 표면 --------------------------------------------------
    # 인자 전달을 검증하는 메서드는 last_call 에 (name, args, kwargs) 를 남긴다.
    last_call = None

    def get_workspace(self, workspace):
        FakeClient.last_call = ("get_workspace", (workspace,), {})
        return WorkspaceDetail(
            workspace, "Team", "2.10", "40.5", 4, 32, 131072, 512000.0,
            resources=[ResourceCondition("pod", 2, 1, 0), ResourceCondition("storage", 1, 0, 0)],
            costs=[DailyCost(date(2026, 8, 30), 1.0, 0.5, 0.0, 0.25, 0.0)],
            raw={"namespaceName": workspace})

    def list_members(self, workspace):
        return [Member("a@b.com", "admin", None, raw={"user": "a@b.com"}),
                Member("c@d.com", "viewer", None, raw={"user": "c@d.com"})]

    def list_storages(self, workspace):
        return [
            Storage("pv-data", workspace, "datasets", "nfs", "running", 102400.0, 40960.0, 0.6,
                    "0.01", ["pod-run"], False, True, None, raw={"pvName": "pv-data"}),
            Storage("pv-local", workspace, "scratch", "hostPath", "creating", 51200.0, 51200.0, 0.0,
                    "0.005", [], False, False, None, raw={"pvName": "pv-local"}),
        ]

    def get_storage(self, storage_name, workspace):
        return Storage(storage_name, workspace, "datasets", "nfs", "running", 102400.0, 40960.0, 0.6,
                       "0.01", ["pod-run"], True, True, None, raw={"pvName": storage_name})

    def get_pod_metrics(self, pod_name, workspace):
        return PodMetrics(pod_name, 8.0, 0.25, 32768.0, None, [GpuUsage(0, 0.9, 0.5, 24576.0, 61.0)],
                          51200, 12288, raw={"podName": pod_name})

    def get_machine_metrics(self, machine_id):
        return MachineMetrics(machine_id, 64.0, 0.1, 16.0, 262144.0, 0.5, 65536.0,
                              [GpuUsage(0, 0.0, 0.1, 81920.0, 40.0)],
                              512000.0, 0.3, 4096000.0, 0.7, 1310720.0, 655360.0,
                              raw={"machineId": machine_id})

    def list_gpus(self, *, rental_type="demand", min_vram=None):
        FakeClient.last_call = ("list_gpus", (), {"rental_type": rental_type, "min_vram": min_vram})
        return [GpuAvailability("NVIDIA H100", 80, rental_type, "2.50", 16, 128, 10, 8, 2,
                                raw={"gpuModel": "NVIDIA H100"}),
                GpuAvailability("NVIDIA RTX 4090", 24, rental_type, "0.60", 8, 32, 3, 2, 2,
                                raw={"gpuModel": "NVIDIA RTX 4090"})]

    def list_api_keys(self):
        return [ApiKey(7, "laptop", "meshive_a1b2c3d4", ["read"], "active", raw={"id": 7})]

    def get_credit(self):
        return Credit(110.0, 100.0, 10.0, True, 10, 50, True, raw={"creditBalance": 110.0})

    def list_credit_history(self, *, start_date=None, end_date=None):
        FakeClient.last_call = ("list_credit_history", (), {"start_date": start_date, "end_date": end_date})
        if start_date == "bad-date":  # 실제 SDK 의 날짜 검증 실패를 흉내
            raise ValueError("start_date must be a date, datetime, or 'YYYY-MM-DD' string")
        return [CreditHistoryEntry(3, 25.0, True, "credit_card", datetime(2026, 7, 1, tzinfo=UTC), raw={"id": 3}),
                CreditHistoryEntry(4, -12.5, True, "refund", datetime(2026, 7, 2, tzinfo=UTC), raw={"id": 4})]

    def get_earnings(self, *, start_date=None, end_date=None):
        FakeClient.last_call = ("get_earnings", (), {"start_date": start_date, "end_date": end_date})
        return Earnings(2.5, 12.0, 340.25,
                        [DailyEarning(date(2026, 8, 30), 1.0, 10.0, 0.5, 11.5),
                         DailyEarning(date(2026, 8, 29), 1.0, 9.0, 0.5, 10.5)],
                        raw={"currentEarning": "2.5"})

    def list_templates(self, workspace=None, *, app_type=None):
        FakeClient.last_call = ("list_templates", (workspace,), {"app_type": app_type})
        templates = [Template(12, "PyTorch", "d", True, "pod", "framework", "", "meshive/pytorch:2.4", "gpu",
                              raw={"id": 12})]
        if workspace:
            templates.append(Template(99, "mine", "d", False, "pod", "custom", "", "me/img:1", "any",
                                      raw={"id": 99}))
        return templates

    def get_template(self, template_id, workspace=None):
        FakeClient.last_call = ("get_template", (template_id,), {"workspace": workspace})
        return Template(template_id, "PyTorch", "d", True, "pod", "framework", "", "meshive/pytorch:2.4",
                        "gpu", "12.4", "pytorch", "2.4", raw={"id": template_id})

    def list_servings(self, workspace):
        return [Serving(5, workspace, "Llama 3 70B", "llama-3-70b", "vllm", "active", False, 1, 3, 2, 2,
                        "https://e", "5.0", True, raw={"id": 5}),
                Serving(6, workspace, None, "sd-xl", "diffusers", "error", True, 0, 1, 0, None,
                        None, "0", False, raw={"id": 6})]

    def get_serving(self, serving_id):
        return Serving(serving_id, "ns", "Llama 3 70B", "llama-3-70b", "vllm", "active", False, 1, 3, 2, 2,
                       "https://e", "5.0", True, raw={"id": serving_id})

    def list_tasks(self, workspace, *, status=None, limit=50, offset=0):
        FakeClient.last_call = ("list_tasks", (workspace,), {"status": status, "limit": limit, "offset": offset})
        if limit < 1:  # 실제 SDK 의 범위 검증을 흉내
            raise ValueError("limit must be an integer between 1 and 200")
        return [Task("task_a", "train", workspace, "running", "task-a", "python:3.12", "NVIDIA RTX 4090",
                     1, 6, 24, "1.0", "0.5", "0", raw={"externalId": "task_a"}),
                Task("task_b", "eval", workspace, "failed", "task-b", "python:3.12", None,
                     0, 2, 8, "0.1", "0.05", "0.05", failure_reason="exit 1", exit_code=1,
                     raw={"externalId": "task_b"})]

    def get_task(self, task_id):
        return Task(task_id, "train", "ns", "succeeded", "task-a", "python:3.12", "NVIDIA RTX 4090",
                    1, 6, 24, "1.0", "0.5", "0.5", exit_code=0,
                    raw={"externalId": task_id, "scriptContent": "print(1)"})

    def list_assets(self, workspace, *, asset_type=None, status=None, page=1, page_size=20):
        FakeClient.last_call = ("list_assets", (workspace,),
                                {"asset_type": asset_type, "status": status, "page": page, "page_size": page_size})
        if page_size < 1:  # 실제 SDK 의 범위 검증을 흉내
            raise ValueError("page_size must be an integer between 1 and 100")
        items = [
            Asset("asset_data", "imagenet-mini", "dataset", "active", None, "meshive_r2", 2, 2_147_483_648, 3,
                  True, namespace_name=workspace, raw={"assetExternalId": "asset_data"}),
            Asset("asset_lora", "style-lora", "adapter", "source_missing", "bucket unreachable", "user_s3",
                  1, 150_000, 1, False, namespace_name=workspace, raw={"assetExternalId": "asset_lora"}),
        ]
        return AssetPage(items, total=45, page=page, page_size=page_size,
                         raw={"total": 45, "page": page, "pageSize": page_size, "items": [a.raw for a in items]})

    def get_asset(self, asset_id):
        versions = [AssetVersion(2, "uploading", 0, 0, "hf_import", "meshive_r2", None, False, "repo not found"),
                    AssetVersion(1, "ready", 1_500_000, 2, "web_upload", "meshive_r2", None, False, None)]
        return Asset(asset_id, "imagenet-mini", "dataset", "active", None, "meshive_r2", 1, 1_500_000, 2, True,
                     namespace_name="ns", created_by="a@b.com", latest_version=versions[1], versions=versions,
                     raw={"assetExternalId": asset_id,
                          "activeUsageContexts": [{"kind": "pod", "name": "trainer", "identifier": "pod-run"}]})

    def get_asset_storage(self, workspace):
        return AssetStorage(2_147_483_648, 0.015, 0.03, "grace", False, None,
                            datetime(2099, 1, 1, tzinfo=UTC), None, False, raw={"managedBytes": 2147483648})

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
    assert FakeClient.last_kwargs == {"api_key": "meshive_x", "base_url": "https://api.dev",
                                      "timeout": 30.0}


def test_timeout_flag_passed_to_client():
    cli.main(["me", "--timeout", "5"])
    assert FakeClient.last_kwargs["timeout"] == 5.0


def test_non_positive_timeout_rejected(capsys):
    assert cli.main(["me", "--timeout", "0"]) == 2
    assert "--timeout" in capsys.readouterr().err


def test_api_error_returns_nonzero(monkeypatch, capsys):
    def raise_auth(self):
        raise AuthenticationError(401, "Invalid API key.", title="Unauthorized")

    monkeypatch.setattr(FakeClient, "me", raise_auth)
    assert cli.main(["me"]) == 1
    assert "Error:" in capsys.readouterr().err


# --- output formats ---------------------------------------------------------

def test_output_name_lists_ids_only(capsys):
    assert cli.main(["pods", "team-ns", "-o", "name"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["pod-run", "pod-stop", "pod-err"]  # IDs only, no header/alias


def test_output_name_respects_filters(capsys):
    assert cli.main(["machines", "-o", "name", "--type", "gpu"]) == 0
    assert capsys.readouterr().out.splitlines() == ["mac-gpu"]


def test_output_name_single_resource(capsys):
    assert cli.main(["machine", "mac-gpu", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["mac-gpu"]


def test_output_json_matches_legacy_json_flag(capsys):
    assert cli.main(["workspaces", "-o", "json"]) == 0
    from_o = capsys.readouterr().out
    assert cli.main(["workspaces", "--json"]) == 0
    assert capsys.readouterr().out == from_o


# --- pod --wait -------------------------------------------------------------

def test_pod_wait_calls_wait_for_pod(capsys):
    FakeClient.last_wait = None
    assert cli.main(["pod", "team-ns", "pod-1", "--wait", "running", "--wait-timeout", "12"]) == 0
    assert FakeClient.last_wait == {"pod_name": "pod-1", "workspace": "team-ns",
                                    "until": "running", "timeout": 12.0}
    assert "pod-1" in capsys.readouterr().out


def test_pod_without_wait_does_not_poll():
    FakeClient.last_wait = None
    assert cli.main(["pod", "team-ns", "pod-1"]) == 0
    assert FakeClient.last_wait is None


def test_pod_wait_unknown_status_errors(capsys):
    assert cli.main(["pod", "team-ns", "pod-1", "--wait", "runnning"]) == 2
    assert "unknown status" in capsys.readouterr().err


def test_pod_wait_timeout_returns_nonzero(monkeypatch, capsys):
    def raise_timeout(self, pod_name, workspace, *, until, timeout):
        raise WaitTimeoutError("Timed out waiting for pod pod-1 to reach running.")

    monkeypatch.setattr(FakeClient, "wait_for_pod", raise_timeout)
    assert cli.main(["pod", "team-ns", "pod-1", "--wait", "running"]) == 1
    assert "Timed out" in capsys.readouterr().err


# --- transport / interrupt failures -----------------------------------------

def test_network_error_is_reported_not_raised(monkeypatch, capsys):
    def raise_connect(self):
        raise httpx.ConnectError("Connection refused",
                                 request=httpx.Request("GET", "https://api.test/v1/sdk/me"))

    monkeypatch.setattr(FakeClient, "me", raise_connect)
    assert cli.main(["me"]) == 1
    err = capsys.readouterr().err
    assert "could not reach" in err and "https://api.test/v1/sdk/me" in err


def test_keyboard_interrupt_exits_130(monkeypatch):
    def raise_interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeClient, "me", raise_interrupt)
    assert cli.main(["me"]) == 130


# =============================================================================
# 0.0.7 commands
# =============================================================================

def test_every_command_has_a_handler():
    """서브커맨드/별칭과 _HANDLERS 가 어긋나면 argparse 는 통과하고 실행만 실패한다 — 여기서 고정."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) - {"login", "logout"} == set(cli._HANDLERS)


def test_workspace_detail_output(capsys):
    assert cli.main(["workspace", "ns"]) == 0
    out = capsys.readouterr().out
    assert "Team" in out and "ns" in out
    assert "$2.10" in out and "$40.50" in out           # price/hr, avg/day
    assert "128 GB" in out and "500 GB" in out           # ram/storage: MiB → GB
    assert "RESOURCE" in out and "pod" in out            # resource table
    assert "2026-08-30" in out and "$1.75" in out        # daily cost + total


def test_workspace_json_and_name(capsys):
    assert cli.main(["workspace", "ns", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"namespaceName": "ns"}
    assert cli.main(["workspace", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["ns"]


def test_members_output_and_name(capsys):
    assert cli.main(["members", "ns"]) == 0
    out = capsys.readouterr().out
    assert "a@b.com" in out and "admin" in out and "viewer" in out
    assert cli.main(["members", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["a@b.com", "c@d.com"]


def test_storages_output_and_filters(capsys):
    assert cli.main(["storages", "ns"]) == 0
    out = capsys.readouterr().out
    assert "pv-data" in out and "pv-local" in out and "datasets" in out
    assert "100 GB" in out and "60.0%" in out
    assert out.index("NAME") < out.index("ID")

    assert cli.main(["storages", "ns", "--type", "hostPath"]) == 0   # 대소문자 무시
    out = capsys.readouterr().out
    assert "pv-local" in out and "pv-data" not in out

    assert cli.main(["storages", "ns", "--status", "running", "--name", "data"]) == 0
    out = capsys.readouterr().out
    assert "pv-data" in out and "pv-local" not in out

    assert cli.main(["storages", "ns", "--status", "runing"]) == 2
    assert "unknown status" in capsys.readouterr().err

    assert cli.main(["storages", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["pv-data", "pv-local"]


def test_storage_single_output(capsys):
    assert cli.main(["storage", "ns", "pv-data"]) == 0
    out = capsys.readouterr().out
    assert "pv-data" in out and "pod-run" in out
    assert "encrypted: yes" in out and "under maintenance" in out
    assert "100 GB, 60.0% used (40 GB free)" in out
    assert cli.main(["storage", "ns", "pv-data", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"pvName": "pv-data"}


def test_pod_metrics_output(capsys):
    assert cli.main(["pod-metrics", "ns", "pod-1"]) == 0
    out = capsys.readouterr().out
    assert "8 cores, 25.0% used" in out
    assert "32 GB, n/a used" in out                       # 측정 불가 → n/a (0% 와 구분)
    assert "gpu 0:" in out and "24 GB vram" in out and "61°C" in out
    assert "12 GB used of 50 GB" in out
    assert cli.main(["pod-metrics", "ns", "pod-1", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["pod-1"]


def test_machine_metrics_output(capsys):
    assert cli.main(["machine-metrics", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "64 cores, 10.0% used, 16 allocated" in out
    assert "256 GB, 50.0% used, 64 GB allocated" in out
    assert "root disk: 500 GB, 30.0% used" in out
    assert "pv disk:   4,000 GB, 70.0% used" in out
    assert "rx 10.0 Mbps, tx 5.0 Mbps" in out             # bytes/s → Mbps (웹과 동일 환산)
    assert cli.main(["machine-metrics", "mac-1", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["mac-1"]


def test_gpus_output_and_passthrough(capsys):
    assert cli.main(["gpus"]) == 0
    out = capsys.readouterr().out
    assert "NVIDIA H100" in out and "80 GB" in out and "$2.50" in out
    assert FakeClient.last_call == ("list_gpus", (), {"rental_type": "demand", "min_vram": None})

    assert cli.main(["gpus", "--rental", "spot", "--vram", "40", "--model", "h100"]) == 0
    out = capsys.readouterr().out
    assert "NVIDIA H100" in out and "4090" not in out     # --model 은 클라이언트 필터
    assert FakeClient.last_call == ("list_gpus", (), {"rental_type": "spot", "min_vram": 40})

    assert cli.main(["gpus", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["NVIDIA H100", "NVIDIA RTX 4090"]


def test_api_keys_output_and_alias(capsys):
    assert cli.main(["api-keys"]) == 0
    out = capsys.readouterr().out
    assert "laptop" in out and "meshive_a1b2c3d4" in out and "read" in out
    assert "never" in out                                  # last used / expires
    assert cli.main(["keys", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["7"]


def test_credit_output_and_name(capsys):
    assert cli.main(["credit"]) == 0
    out = capsys.readouterr().out
    assert "$110.00" in out and "$100.00" in out and "$10.00" in out
    assert "on (add $50.00 when below $10.00)" in out and "on file" in out
    assert cli.main(["credit", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "110.00"


def test_credit_history_output_dates_and_errors(capsys):
    assert cli.main(["credit-history", "--since", "2026-07-01", "--until", "2026-07-31"]) == 0
    out = capsys.readouterr().out
    assert "2026-07-01" in out and "$25.00" in out and "-$12.50" in out and "refund" in out
    assert FakeClient.last_call == ("list_credit_history", (),
                                    {"start_date": "2026-07-01", "end_date": "2026-07-31"})

    assert cli.main(["credit-history", "--since", "bad-date"]) == 2   # SDK ValueError → usage error
    assert "start_date" in capsys.readouterr().err

    assert cli.main(["credit-history", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["3", "4"]


def test_earnings_output_days_and_name(capsys):
    assert cli.main(["earnings"]) == 0
    out = capsys.readouterr().out
    assert "$2.50" in out and "$12.00" in out and "$340.25" in out
    assert "2026-08-30" in out and "2026-08-29" in out

    assert cli.main(["earnings", "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "2026-08-30" in out and "2026-08-29" not in out

    assert cli.main(["earnings", "--days", "-1"]) == 2

    assert cli.main(["earnings", "--since", "2026-08-01", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "340.25"
    assert FakeClient.last_call[2]["start_date"] == "2026-08-01"


def test_templates_output_and_passthrough(capsys):
    assert cli.main(["templates"]) == 0
    out = capsys.readouterr().out
    assert "PyTorch" in out and "official" in out and "mine" not in out
    assert FakeClient.last_call == ("list_templates", (None,), {"app_type": None})

    assert cli.main(["templates", "--workspace", "ns", "--type", "IDE"]) == 0
    out = capsys.readouterr().out
    assert "mine" in out and "custom" in out
    assert FakeClient.last_call == ("list_templates", ("ns",), {"app_type": "ide"})

    assert cli.main(["templates", "--workspace", "ns", "--name", "torch"]) == 0
    out = capsys.readouterr().out
    assert "PyTorch" in out and "mine" not in out

    assert cli.main(["templates", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["12"]


def test_template_single_output(capsys):
    assert cli.main(["template", "12"]) == 0
    out = capsys.readouterr().out
    assert "PyTorch" in out and "pytorch 2.4" in out and "12.4" in out
    assert FakeClient.last_call == ("get_template", (12,), {"workspace": None})

    assert cli.main(["template", "99", "--workspace", "ns", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": 99}
    assert FakeClient.last_call == ("get_template", (99,), {"workspace": "ns"})

    with pytest.raises(SystemExit):
        cli.main(["template", "twelve"])   # argparse: int 가 아니면 usage error


def test_servings_output_and_filters(capsys):
    assert cli.main(["servings", "ns"]) == 0
    out = capsys.readouterr().out
    assert "Llama 3 70B" in out and "sd-xl" in out
    assert "2 (1-3)" in out and "$5.00" in out and "(paused)" in out

    assert cli.main(["servings", "ns", "--status", "active"]) == 0
    out = capsys.readouterr().out
    assert "Llama" in out and "sd-xl" not in out

    assert cli.main(["servings", "ns", "--name", "sd"]) == 0
    out = capsys.readouterr().out
    assert "sd-xl" in out and "Llama" not in out

    assert cli.main(["servings", "ns", "--status", "stopped"]) == 2    # serving 에는 없는 상태
    assert "unknown status" in capsys.readouterr().err

    assert cli.main(["servings", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["5", "6"]


def test_serving_single_output(capsys):
    assert cli.main(["serving", "5"]) == 0
    out = capsys.readouterr().out
    assert "Llama 3 70B" in out and "https://e" in out
    assert "2 running, 1-3 configured, 2 healthy" in out
    assert cli.main(["serving", "5", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["5"]


def test_tasks_output_filters_and_paging(capsys):
    assert cli.main(["tasks", "ns"]) == 0
    out = capsys.readouterr().out
    assert "task_a" in out and "task_b" in out
    assert "1x NVIDIA RTX 4090" in out and "cpu" in out
    assert FakeClient.last_call == ("list_tasks", ("ns",), {"status": None, "limit": 50, "offset": 0})

    assert cli.main(["tasks", "ns", "--status", "running,failed", "--limit", "10", "--offset", "5"]) == 0
    capsys.readouterr()
    assert FakeClient.last_call == ("list_tasks", ("ns",),
                                    {"status": ["failed", "running"], "limit": 10, "offset": 5})

    assert cli.main(["tasks", "ns", "--name", "eval"]) == 0
    out = capsys.readouterr().out
    assert "task_b" in out and "task_a" not in out

    assert cli.main(["tasks", "ns", "--status", "runing"]) == 2
    assert "unknown status" in capsys.readouterr().err

    assert cli.main(["tasks", "ns", "--limit", "0"]) == 2              # SDK ValueError → 2
    assert "limit" in capsys.readouterr().err

    assert cli.main(["tasks", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["task_a", "task_b"]


def test_task_single_output(capsys):
    assert cli.main(["task", "task_a"]) == 0
    out = capsys.readouterr().out
    assert "task_a" in out and "succeeded" in out
    assert "exit code:   0" in out and "6 cores / 24 GB" in out
    assert cli.main(["task", "task_a", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scriptContent"] == "print(1)"


# --- assets (Asset Hub) -------------------------------------------------------

def test_assets_output_filters_and_paging(capsys):
    assert cli.main(["assets", "ns"]) == 0
    out = capsys.readouterr().out
    assert "imagenet-mini" in out and "asset_data" in out and "style-lora" in out
    assert "2.00 GB" in out and "146.5 KB" in out             # bytes → human
    assert "managed" in out and "s3" in out                    # storage provider labels
    assert "Page 1 of 3 (45 assets)" in out                    # 서버 total 기준 페이지 힌트
    assert FakeClient.last_call == ("list_assets", ("ns",),
                                    {"asset_type": None, "status": None, "page": 1, "page_size": 20})

    assert cli.main(["assets", "ns", "--type", "Dataset", "--status", "active",
                     "--page", "2", "--page-size", "50"]) == 0
    out = capsys.readouterr().out
    assert "Page 2 is past the end: 45 assets on 1 page." in out   # 45 < 50 → 한 페이지뿐
    assert FakeClient.last_call == ("list_assets", ("ns",),
                                    {"asset_type": "dataset", "status": "active", "page": 2, "page_size": 50})

    assert cli.main(["assets", "ns", "--name", "lora"]) == 0
    out = capsys.readouterr().out
    assert "style-lora" in out and "imagenet-mini" not in out

    assert cli.main(["assets", "ns", "--page-size", "0"]) == 2   # SDK ValueError → usage error
    assert "page_size" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["assets", "ns", "--type", "video"])          # argparse choices

    assert cli.main(["assets", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["asset_data", "asset_lora"]


def test_assets_page_past_the_end_is_explained(capsys):
    assert cli.main(["assets", "ns", "--page", "9", "--page-size", "20"]) == 0   # 45개 → 3페이지뿐
    out = capsys.readouterr().out
    assert "Page 9 is past the end: 45 assets on 3 pages." in out
    assert "Use --page" not in out


def test_assets_json_keeps_page_metadata_with_filtered_items(capsys):
    assert cli.main(["assets", "ns", "--name", "lora", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["total"] == 45 and body["page"] == 1
    assert [a["assetExternalId"] for a in body["items"]] == ["asset_lora"]


def test_asset_single_output(capsys):
    assert cli.main(["asset", "asset_data"]) == 0
    out = capsys.readouterr().out
    assert "imagenet-mini" in out and "asset_data" in out and "a@b.com" in out
    assert "1.4 MB in 2 files" in out                          # 최신 READY 버전 기준
    assert "in use:     yes (pod trainer)" in out
    assert "v2" in out and "uploading" in out and "v1" in out and "ready" in out
    assert "v2 import failed: repo not found" in out
    assert cli.main(["asset", "asset_data", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["asset_data"]
    assert cli.main(["asset", "asset_data", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["assetExternalId"] == "asset_data"


def test_asset_storage_output_and_name(capsys):
    assert cli.main(["asset-storage", "ns"]) == 0
    out = capsys.readouterr().out
    assert "managed:       2.00 GB" in out
    assert "$0.015 per GB-month" in out and "$0.03" in out
    assert "grace (uploads block in" in out
    assert "none (pods and tasks cannot start)" in out
    assert cli.main(["asset-storage", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "0.03"
