import argparse
import importlib
from pathlib import Path
import json
from datetime import date, datetime, timedelta, timezone

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
    AssetFile,
    AssetUsage,
    ConnectCredential,
    CpuLine,
    Credit,
    CreditHistoryEntry,
    DailyCost,
    DailyEarning,
    DownloadFile,
    Earnings,
    GpuAvailability,
    GpuUsage,
    InitLog,
    Machine,
    MachineMetrics,
    Member,
    Pod,
    PodEndpoint,
    PodMetrics,
    ResourceCondition,
    Serving,
    Storage,
    Task,
    TaskInputAsset,
    TaskOutputs,
    TaskOutputsStatus,
    Template,
    Transaction,
    WhoAmI,
    Workspace,
    WorkspaceDetail,
    WorkspaceResources,
)

UTC = timezone.utc


class FakeClient:
    """Fake client injected in place of meshive.cli.main.Meshive."""

    last_kwargs = None

    def __init__(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        self.closed = False

    def me(self):
        return WhoAmI("a@b.com", "alice", "user", raw={"email": "a@b.com"})

    def list_workspaces(self):
        return [
            Workspace("ns", "Team", "d", 1, "active", "1.0",
                      WorkspaceResources(2, 0, 0), raw={"namespaceName": "ns"}, member_role="viewer"),
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
                   raw={"podName": pod_name},
                   endpoints=[PodEndpoint("ComfyUI", 8188, "connect", "https://c.meshive.ai", None, True, "ready")],
                   connect_credentials=[ConnectCredential("ACCESS_PASSWORD", "s3cret-pw", True, True),
                                        ConnectCredential("USERNAME", "admin")])

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

    # --- Read surface --------------------------------------------------
    # Methods whose argument passing is checked leave (name, args, kwargs) in last_call.
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
        # ram_size is bytes as the server sends it (256 GiB), network is bytes/sec (12.5 MB/s = 100 Mbps).
        return MachineMetrics(machine_id, 64.0, 0.1, 16.0, 256 * 1024 ** 3, 0.5, 65536.0,
                              [GpuUsage(0, 0.0, 0.1, 81920.0, 40.0)],
                              512000.0, 0.3, 4096000.0, 0.7, 12_500_000.0, 625_000.0,
                              raw={"machineId": machine_id})

    def list_gpus(self, *, rental_type="demand", min_vram=None):
        FakeClient.last_call = ("list_gpus", (), {"rental_type": rental_type, "min_vram": min_vram})
        return [GpuAvailability("NVIDIA H100", 80, rental_type, "2.50", 16, 128, 10, 8, 2,
                                raw={"gpuModel": "NVIDIA H100"}),
                GpuAvailability("NVIDIA RTX 4090", 24, rental_type, "0.60", 8, 32, 3, 2, 2,
                                raw={"gpuModel": "NVIDIA RTX 4090"})]

    def list_cpus(self, *, rental_type="demand"):
        FakeClient.last_call = ("list_cpus", (), {"rental_type": rental_type})
        return [CpuLine.from_dict({"rentalType": rental_type, "cpuModel": "amd-epyc-4-ddr5", "cpuVendor": "AMD",
                                   "cpuProductLine": "EPYC", "cpuGeneration": 4, "ddrType": 5, "cpuPricePerCore": "0.012",
                                   "ramPricePerGb": "0.0021", "combinations": [{"maxCpu": 64, "maxRam": 262144}]}),
                CpuLine.from_dict({"rentalType": rental_type, "cpuModel": "intel-xeon-0-ddr0", "cpuVendor": "Intel",
                                   "cpuProductLine": "Xeon", "cpuGeneration": 0, "ddrType": 0, "cpuPricePerCore": "0.01",
                                   "ramPricePerGb": "0.002", "combinations": []})]

    def get_transaction(self, transaction_id, workspace):
        FakeClient.last_call = ("get_transaction", (transaction_id, workspace), {})
        return Transaction.from_dict({
            "transactionId": transaction_id, "userAlias": "trainer", "resourceName": "5f0e1d2c3b4a6978",
            "action": "create", "status": "failed", "updatedAt": "2026-10-14T00:02:00Z",
            "transactionSteps": [
                {"step": "start", "status": "done", "detail": "Starting Pod creation process",
                 "updatedAt": "2026-10-14T00:00:00Z"},
                {"step": "fetch_assets", "status": "failed", "detail": "", "updatedAt": "2026-10-14T00:02:00Z",
                 "failureDiagnostic": {"failureReason": "source fetch failed: HTTP 403",
                                       "initLogs": [{"container": "source-fetch", "lines": ["GET x", "403"]}]}},
                {"step": "create_statefulset", "status": "done", "detail": "", "updatedAt": "2026-10-14T00:01:00Z"},
            ]})

    def list_api_keys(self):
        return [ApiKey(7, "laptop", "meshive_a1b2c3d4", ["read"], "active", raw={"id": 7})]

    def get_credit(self):
        return Credit(110.0, 100.0, 10.0, True, 10, 50, True, raw={"creditBalance": 110.0})

    def list_credit_history(self, *, start_date=None, end_date=None):
        FakeClient.last_call = ("list_credit_history", (), {"start_date": start_date, "end_date": end_date})
        if start_date == "bad-date":  # mimics the real SDK's date validation failure
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
        if limit < 1:  # mimics the real SDK's range validation
            raise ValueError("limit must be an integer between 1 and 200")
        return [Task("task_a", "train", workspace, "running", "task-a", "python:3.12", "NVIDIA RTX 4090",
                     1, 6, 24, "1.0", "0.5", "0", raw={"externalId": "task_a"}),
                Task("task_b", "eval", workspace, "failed", "task-b", "python:3.12", None,
                     0, 2, 8, "0.1", "0.05", "0.05", failure_reason="exit 1", exit_code=1,
                     raw={"externalId": "task_b"})]

    def get_task(self, task_id):
        return Task(task_id, "train", "ns", "succeeded", "task-a", "python:3.12", "NVIDIA RTX 4090",
                    1, 6, 24, "1.0", "0.5", "0.5", exit_code=0,
                    raw={"externalId": task_id, "scriptContent": "print(1)"},
                    output_upload_state="uploading", output_files_declared=4, output_files_completed=1,
                    output_bytes_declared=4096, output_bytes_completed=1024, output_upload_last_error="AccessDenied",
                    input_assets=[TaskInputAsset("asset_w", "weights", "/inputs/w")],
                    output_include=["*.safetensors", "samples/"])

    def list_assets(self, workspace, *, asset_type=None, status=None, page=1, page_size=20):
        FakeClient.last_call = ("list_assets", (workspace,),
                                {"asset_type": asset_type, "status": status, "page": page, "page_size": page_size})
        if page_size < 1:  # mimics the real SDK's range validation
            raise ValueError("page_size must be an integer between 1 and 100")
        items = [
            Asset("asset_data", "imagenet-mini", "dataset", "active", None, "meshive_r2", 2_147_483_648, 3,
                  True, namespace_name=workspace, raw={"assetExternalId": "asset_data"}),
            Asset("asset_lora", "style-lora", "adapter", "source_missing", "bucket unreachable", "user_s3",
                  150_000, 1, False, namespace_name=workspace, raw={"assetExternalId": "asset_lora"}),
            # A link asset not measured yet — its size is unknown (not 0)
            Asset("asset_link", "hf-link", "model", "active", None, "external", None, None, False,
                  namespace_name=workspace, raw={"assetExternalId": "asset_link"}),
        ]
        return AssetPage(items, total=45, page=page, page_size=page_size,
                         raw={"total": 45, "page": page, "pageSize": page_size, "items": [a.raw for a in items]})

    def get_asset(self, asset_id):
        return Asset(asset_id, "imagenet-mini", "dataset", "active", None, "meshive_r2", 1_500_000, 2, True,
                     namespace_name="ns", created_by="a@b.com", upload_status="failed", ingest_source="hf_import",
                     import_failure_reason="repo not found",
                     files=[AssetFile("a.bin", 1_000_000, "ready"), AssetFile("b.bin", 500_000, "ready")],
                     active_usage_contexts=[AssetUsage("pod", "pod-run", "trainer", "running")],
                     raw={"assetExternalId": asset_id})

    def download_asset(self, asset_id, dest, *, paths=None):
        FakeClient.last_call = ("download_asset", (asset_id, str(dest)), {"paths": paths})
        target = Path(dest) / "config" / "a.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"12345")
        return [target]

    def task_outputs(self, task_id):
        return TaskOutputs(task_id, [DownloadFile("result.csv", "https://r2/x", 2048)], False, "meshive_r2",
                           raw={"files": [{"filename": "result.csv"}]})

    def download_task_outputs(self, task_id, dest):
        target = Path(dest) / "result.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ok")
        return [target]

    def get_asset_storage(self, workspace):
        return AssetStorage(2_147_483_648, 0.015, 0.03, "grace", False, None,
                            datetime(2099, 1, 1, tzinfo=UTC), None, False, raw={"managedBytes": 2147483648})

    def list_transactions(self, workspace):
        FakeClient.last_call = ("list_transactions", (workspace,), {})
        return [
            Transaction(73213, "5f0e1d2c3b4a6978", "trainer", "create", "in_progress", "pull_image", "in_progress",
                        progress=0.42, updated_at=datetime(2026, 9, 10, 7, 58, tzinfo=UTC),
                        raw={"transactionId": 73213}),
            # Failure of a pod without an alias — a step with unknown progress (progress=None)
            Transaction(73214, "9eb176aae9309fb8", "", "restart", "failed", "pull_image", "failed",
                        detail="image pull stalled for 600s", raw={"transactionId": 73214}),
            Transaction(73215, "aa11", "comfy", "create", "failed", "fetch_assets", "failed",
                        detail="source fetch failed", raw={"transactionId": 73215}, live=False, phase="verifying",
                        init_logs=[InitLog("source-fetch", [f"line {i}" for i in range(15)] + ["\x1b[31mHTTP 403"],
                                           True)]),
        ]

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
    assert "ROLE" in out and "viewer" in out
    assert out.index("NAME") < out.index("ID")  # NAME first, then ID




def test_workspaces_show_all_billing_owners_and_unknown_fallback(capsys):
    workspaces = [
        Workspace.from_dict({"namespaceName": "ns-a", "workspaceName": "Team", "memberRole": "billing",
                             "ownerEmails": ["a@example.com", "b@example.com"]}),
        Workspace.from_dict({"namespaceName": "ns-b", "workspaceName": "Team", "memberRole": "viewer"}),
    ]
    cli._print_workspaces(workspaces, False)
    out = capsys.readouterr().out
    assert "OWNER" in out and "ROLE" in out
    assert "a@example.com, b@example.com" in out and "billing" in out
    assert "unknown" in out and "viewer" in out
    assert "ns-a" in out and "ns-b" in out


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
    assert "Team" in out and "Team2" in out            # workspace display names shown
    assert "ns2" not in out                             # internal workspace key stays out of this column


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


def test_pod_details_show_template_and_storage_names(capsys):
    pod = Pod.from_dict({
        "podName": "pod-1", "userAlias": "trainer", "workspaceName": "Team",
        "template": {"id": 42, "name": "Jupyter"},
        "linkedStorages": [{"pvName": "pv-internal", "userAlias": "datasets", "mountPath": "/data", "capacity": 10240, "storageType": "nfs"}],
    })
    cli._print_pod(pod, False)
    out = capsys.readouterr().out
    assert "Team" in out and "Jupyter" in out and "datasets" in out and "/data" in out and "10 GiB" in out
    assert "pv-internal" not in out


def test_storage_details_use_linked_pod_labels_and_keep_missing_name_fallback(capsys):
    storage = Storage.from_dict({
        "linkedPod": [{"podName": "pod-internal", "userAlias": "trainer"}, {"podName": "pod-legacy"}],
    })
    cli._print_storage(storage, False)
    out = capsys.readouterr().out
    assert "trainer, pod-legacy" in out
    assert "pod-internal" not in out


def test_pod_connect_info_hides_secrets_by_default(capsys):
    assert cli.main(["pod", "team-ns", "pod-1"]) == 0
    out = capsys.readouterr().out
    assert "ComfyUI" in out and "https://c.meshive.ai" in out and "ready" in out
    assert "ACCESS_PASSWORD" in out and "s3cret-pw" not in out and "--show-secrets" in out
    assert "admin" in out                                    # non-secret values stay as-is
    assert cli.main(["pod", "team-ns", "pod-1", "--show-secrets"]) == 0
    out = capsys.readouterr().out
    assert "s3cret-pw" in out and "hidden" not in out


def test_pod_show_secrets_explains_read_only_key_mask(capsys, monkeypatch):
    # The server sends "***" for secret values to read-only keys.
    pod = FakeClient().get_pod("pod-1", "team-ns")
    pod.connect_credentials[0].value = "***"
    monkeypatch.setattr(FakeClient, "get_pod", lambda self, pod_name, workspace: pod)
    assert cli.main(["pod", "team-ns", "pod-1", "--show-secrets"]) == 0
    assert "read-only API keys" in capsys.readouterr().out

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
    # No status enum validation — lowercase input must match ONLINE.
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
    # FakeClient machines are only gpu/cpu → the storage filter matches nothing.
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
# Read commands
# =============================================================================

def test_every_command_has_a_handler():
    """If subcommands/aliases and _HANDLERS drift apart, argparse passes and only execution fails — pinned here."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) - {"login", "logout"} == set(cli._HANDLERS)


def test_workspace_detail_output(capsys):
    assert cli.main(["workspace", "ns"]) == 0
    out = capsys.readouterr().out
    assert "Team" in out and "ns" in out
    assert "$2.100" in out and "$40.50" in out          # price/hr has 3 decimals, avg/day 2
    assert "128 GiB" in out and "500 GiB" in out         # ram/storage: MiB → GiB (1024-based label like the console)
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
    assert "100 GiB" in out and "60.0%" in out
    assert out.index("NAME") < out.index("ID")

    assert cli.main(["storages", "ns", "--type", "hostPath"]) == 0   # case-insensitive
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
    assert "encrypted: yes" in out and "host maintenance: not billed while the window is open" in out
    assert "100 GiB, 60.0% used (40 GiB free)" in out
    assert cli.main(["storage", "ns", "pv-data", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"pvName": "pv-data"}


def test_pod_metrics_output(capsys):
    assert cli.main(["pod-metrics", "ns", "pod-1"]) == 0
    out = capsys.readouterr().out
    assert "8 cores, 25.0% used" in out
    assert "32 GiB, n/a used" in out                      # not measurable → n/a (distinct from 0%)
    assert "gpu 0:" in out and "24 GB vram" in out and "61°C" in out   # VRAM alone keeps the industry GB
    assert "12 GiB used of 50 GiB" in out
    assert cli.main(["pod-metrics", "ns", "pod-1", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["pod-1"]


def test_machine_metrics_output(capsys):
    assert cli.main(["machine-metrics", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "64 cores, 10.0% used, 16 allocated" in out
    assert "ram:       256 GiB, 50.0% used, 64 GiB allocated" in out   # ram_size is bytes, allocation is MiB
    assert "10.0% of 80 GB vram" in out
    assert "root disk: 500 GiB, 30.0% used" in out
    assert "pv disk:   4,000 GiB, 70.0% used" in out
    assert "rx 100.0 Mbps, tx 5.0 Mbps" in out            # bytes/sec × 8 ÷ 10^6 (with 1024² it would be 95.4 / 4.8)
    assert cli.main(["machine-metrics", "mac-1", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["mac-1"]


def test_gpu_line_shows_na_when_vram_size_is_unknown(monkeypatch, capsys):
    # When GPU memory can't be read the server sends vramSize as null (missing DCGM FB metrics, pod GPU lookup failure); when both FB values are 0
    # it sends 0. Neither is the card size — n/a instead of '0 GB vram'. Goes through server JSON → SDK parsing.
    gpus = [{"gpuNumber": 0, "coreUsageRate": None, "vramUsageRate": None, "vramSize": None, "temp": 40.0},
            {"gpuNumber": 1, "coreUsageRate": None, "vramUsageRate": None, "vramSize": 0.0, "temp": 41.0}]
    monkeypatch.setattr(FakeClient, "get_machine_metrics", lambda self, machine_id: MachineMetrics.from_dict(
        {"machineId": machine_id, "cpu": {"core": 8}, "ram": {"size": 16 * 1024 ** 3}, "gpu": gpus}))
    monkeypatch.setattr(FakeClient, "get_pod_metrics", lambda self, pod_name, workspace: PodMetrics.from_dict(
        {"podName": pod_name, "cpu": {"core": 8}, "ram": {"size": 16384}, "gpu": gpus[:1]}))

    assert cli.main(["machine-metrics", "mac-1"]) == 0
    out = capsys.readouterr().out
    assert "gpu 0:     n/a core, n/a of n/a vram, 40°C" in out
    assert "gpu 1:     n/a core, n/a of n/a vram, 41°C" in out
    assert "GB vram" not in out
    assert cli.main(["pod-metrics", "ns", "pod-1"]) == 0
    out = capsys.readouterr().out
    assert "gpu 0:     n/a core, n/a of n/a vram, 40°C" in out and "GB vram" not in out


def test_gpus_output_and_passthrough(capsys):
    assert cli.main(["gpus"]) == 0
    out = capsys.readouterr().out
    assert "NVIDIA H100" in out and "80 GB" in out and "$2.500" in out   # $/hr has 3 decimals
    assert FakeClient.last_call == ("list_gpus", (), {"rental_type": "demand", "min_vram": None})

    assert cli.main(["gpus", "--rental", "spot", "--vram", "40", "--model", "h100"]) == 0
    out = capsys.readouterr().out
    assert "NVIDIA H100" in out and "4090" not in out     # --model is a client-side filter
    assert FakeClient.last_call == ("list_gpus", (), {"rental_type": "spot", "min_vram": 40})

    assert cli.main(["gpus", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["NVIDIA H100", "NVIDIA RTX 4090"]


def test_cpus_output_and_passthrough(capsys):
    assert cli.main(["cpus"]) == 0
    out = capsys.readouterr().out
    assert FakeClient.last_call == ("list_cpus", (), {"rental_type": "demand"})
    assert "amd-epyc-4-ddr5" in out and "AMD EPYC gen 4" in out and "DDR5" in out
    assert "$0.012" in out and "$0.002" in out and "256 GiB" in out and "--cpu-line" in out
    assert "Intel Xeon " in out and "DDR0" not in out and "gen 0" not in out      # unknown generation/DDR aren't shown
    assert cli.main(["cpus", "--rental", "spot", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["amd-epyc-4-ddr5", "intel-xeon-0-ddr0"]
    assert FakeClient.last_call == ("list_cpus", (), {"rental_type": "spot"})
    assert cli.main(["cpus", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["cpuModel"] == "amd-epyc-4-ddr5"


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
    assert "balance:         $110.00" in out
    # Free credits retired (2026-10) — no paid/bonus split lines (the fields stay on the model for backward compatibility)
    assert "paid:" not in out and "bonus" not in out
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
        cli.main(["template", "twelve"])   # argparse: usage error if not an int


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

    assert cli.main(["servings", "ns", "--status", "stopped"]) == 2    # a status servings don't have
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
    assert "exit code:   0" in out and "6 cores / 24 GiB" in out
    assert "outputs:     uploading, 1 of 4 files, 1.0 KiB of 4.0 KiB" in out
    assert "upload error: AccessDenied" in out
    assert "output filter: *.safetensors, samples/" in out
    assert "weights" in out and "/inputs/w" in out          # input asset table
    assert cli.main(["task", "task_a", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scriptContent"] == "print(1)"


# --- task outputs status (Task.outputs) -----------------------------------------------------------

OUTPUTS_NOW = datetime(2026, 11, 5, 7, 0, tzinfo=UTC)
_MIB = 1024 * 1024
_KNOWN = {"filesSoFar": 48, "filesTotal": 126, "filesOverLimit": 0, "filesLimit": 2000, "bytesSaved": 43_201_331,
          "bytesSoFar": 43_201_331, "bytesTotal": 113_874_534}
_UNKNOWN = {"filesSoFar": 48, "filesTotal": None, "filesOverLimit": 0, "filesLimit": 200, "bytesSoFar": 43_201_331,
            "bytesTotal": None}
_OVER = {"filesSoFar": 1200, "filesTotal": 2431, "filesOverLimit": 431, "filesLimit": 2000, "bytesSaved": 900 * _MIB,
         "bytesSoFar": 900 * _MIB, "bytesTotal": 1536 * _MIB}
_PARTIAL = {"phase": "partially_saved", "assetExternalId": "asset_9c22", "assetName": "run outputs",
            "filesSaved": 2000, "filesNotSaved": 431, "filesOverLimit": 431, "filesTotal": 2431, "filesLimit": 2000,
            "bytesSaved": 1536 * _MIB}


def _iso(seconds: int) -> str:
    return (OUTPUTS_NOW + timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize("outputs, expected", [
    ({"phase": "running", "filesLimit": 2000}, None),
    ({"phase": "preparing", "filesLimit": 2000}, "preparing to save · scanning /outputs"),
    ({"phase": "uploading", **_KNOWN}, "uploading · 48/126 files · 41.2 of 108.6 MiB"),
    # The files over the limit aren't uploaded, so progress is out of the 2,000 that are.
    ({"phase": "uploading", **_OVER},
     "uploading · 1,200/2,000 files · 900.0 MiB of 1.50 GiB · 431 over the 2,000-file limit won't be saved"),
    ({"phase": "uploading", **_UNKNOWN}, "uploading · 48 files so far · 41.2 MiB · total not known yet"),
    ({"phase": "uploading", **_UNKNOWN, "filesSoFar": 0, "bytesSoFar": 0}, "uploading · total not known yet"),
    ({"phase": "retrying", **_KNOWN, "attempts": 2, "nextAttemptAt": _iso(190)},
     "retrying the upload · 48/126 files · 41.2 of 108.6 MiB · next try in 3 minutes"),
    ({"phase": "retrying", **_UNKNOWN, "attempts": 2, "nextAttemptAt": _iso(-5)},
     "retrying the upload · 48 files so far · 41.2 MiB · retrying now"),
    ({"phase": "registering", **_KNOWN, "filesSoFar": 126, "bytesSaved": 113_874_534, "bytesSoFar": 113_874_534,
      "expectedInAssetsAt": _iso(25)},
     "adding to Assets · 126/126 files · 108.6 MiB · expected in a few seconds"),
    ({"phase": "registering", **_UNKNOWN, "expectedInAssetsAt": _iso(150)},
     "adding to Assets · 48 files · 41.2 MiB · expected in 2 minutes"),
    ({"phase": "saved", "assetExternalId": "asset_9c21", "assetName": "run outputs", "filesSaved": 126,
      "bytesSaved": 113_874_534, "filesNotSaved": 0},
     "saved to Assets as run outputs (asset_9c21) · 126 files · 108.6 MiB"),
    ({"phase": "saved", "assetExternalId": None, "assetName": None, "assetDeleted": True, "filesSaved": 126},
     "saved to Assets, but the asset has since been deleted"),
    (_PARTIAL, "partly saved to Assets as run outputs (asset_9c22) · 2,000 files · 1.50 GiB"),
    ({"phase": "not_saved", "filesSaved": 0, "filesNotSaved": 126}, "not saved"),
    ({"phase": "discarded", "assetExternalId": "asset_d", "assetName": "run", "filesSaved": 48},
     "upload discarded · the files uploaded before that are in Assets as run (asset_d)"),
    ({"phase": "discarded", "filesSaved": 0}, "upload discarded"),
    ({"phase": "empty", "filesLimit": 2000}, "no output files"),
    ({"phase": "archiving"}, "archiving"),           # a phase from a newer server, as the server wrote it
])
def test_task_outputs_line(outputs, expected):
    assert cli._task_outputs_line(TaskOutputsStatus.from_dict(outputs), now=OUTPUTS_NOW) == expected


@pytest.mark.parametrize("outputs, expected", [
    (_PARTIAL, "Only 2,000 of 2,431 output files were saved — 431 were over the 2,000-file limit."),
    # Some files also failed to upload: the limit isn't the whole reason, so it isn't named.
    ({**_PARTIAL, "filesSaved": 1931, "filesNotSaved": 500}, "Only 1,931 of 2,431 output files were saved."),
    ({"phase": "partially_saved", "filesSaved": 40, "filesNotSaved": 86, "filesOverLimit": 0},
     "Only 40 of 126 output files were saved."),
    # The server counted no missing file: nothing to say.
    ({"phase": "partially_saved", "filesSaved": 126, "filesNotSaved": 0, "filesOverLimit": 0}, None),
])
def test_task_outputs_partial_note(outputs, expected):
    assert cli._task_outputs_partial_note(TaskOutputsStatus.from_dict(outputs)) == expected


def _task_with_outputs(task_id, outputs, **fields):
    return Task.from_dict({"externalId": task_id, "name": "sdxl", "namespaceName": "ns", "status": "succeeded",
                           "podName": "task-o", "image": "python:3.12", "gpuModel": "NVIDIA RTX 4090",
                           "gpuCount": 1, "cpuCores": 6, "ramGb": 24, "pricePerHour": "1.0", "costSoFar": "0.5",
                           "totalCost": "0.5", "provider": "partner-cloud", "outputUploadState": "uploading",
                           "outputFilesDeclared": 126, "outputFilesCompleted": 48, "outputBytesDeclared": 4096,
                           "outputBytesCompleted": 1024, **fields, "outputs": outputs})


def test_task_shows_the_outputs_status(capsys, monkeypatch):
    task = _task_with_outputs("task_o", {**_PARTIAL, "assetName": "run\x1b[31m outputs"})
    monkeypatch.setattr(FakeClient, "get_task", lambda self, task_id: task)
    assert cli.main(["task", "task_o"]) == 0
    out = capsys.readouterr().out
    assert "outputs:     partly saved to Assets as run[31m outputs (asset_9c22) · 2,000 files · 1.50 GiB\n" in out
    assert "             Only 2,000 of 2,431 output files were saved — 431 were over the 2,000-file limit.\n" in out
    assert "\x1b" not in out                                  # server text is cleaned
    assert "48 of 126 files" not in out                       # the raw counters give way to the status
    assert "partner" not in out and "provider" not in out   # where a task runs is never printed

    cli._print_task(task, True)                               # with color: the partial note is yellow
    assert "\033[33m             Only 2,000 of 2,431" in capsys.readouterr().out


def test_task_outputs_status_for_unknown_total_and_failures(capsys, monkeypatch):
    tasks = {
        "task_u": _task_with_outputs("task_u", {"phase": "uploading", **_UNKNOWN}),
        "task_n": _task_with_outputs("task_n", {"phase": "not_saved", "filesSaved": 0, "filesNotSaved": 126,
                                                "lastError": "AccessDenied"}, outputUploadLastError="AccessDenied"),
        "task_r": _task_with_outputs("task_r", {"phase": "running", "filesLimit": 2000}, status="running",
                                     outputUploadState=None),
    }
    monkeypatch.setattr(FakeClient, "get_task", lambda self, task_id: tasks[task_id])
    assert cli.main(["task", "task_u"]) == 0
    assert "outputs:     uploading · 48 files so far · 41.2 MiB · total not known yet\n" in capsys.readouterr().out
    assert cli.main(["task", "task_n"]) == 0
    out = capsys.readouterr().out
    assert "outputs:     not saved\n" in out and "upload error: AccessDenied" in out
    assert cli.main(["task", "task_r"]) == 0
    assert "outputs:" not in capsys.readouterr().out          # nothing to say while the task runs
    cli._print_task(tasks["task_n"], True)
    assert "\033[31moutputs:     not saved" in capsys.readouterr().out


def test_task_with_a_deleted_log_keeps_its_saved_outputs(capsys, monkeypatch):
    # outputsPurgedAt marks the task's log as deleted after its retention period; the outputs stay in Assets.
    purged_at = (datetime.now(UTC) - timedelta(days=3, hours=1)).isoformat()
    task = _task_with_outputs("task_p", {"phase": "saved", "assetExternalId": "asset_9c21", "assetName": "run outputs",
                                         "filesSaved": 126, "bytesSaved": 113_874_534, "filesNotSaved": 0},
                              outputUploadState="completed", outputsPurgedAt=purged_at)
    monkeypatch.setattr(FakeClient, "get_task", lambda self, task_id: task)
    assert cli.main(["task", "task_p"]) == 0
    out = capsys.readouterr().out
    assert "outputs:     saved to Assets as run outputs (asset_9c21) · 126 files · 108.6 MiB\n" in out
    assert "log:         deleted 3 days ago\n" in out
    assert out.count("outputs:") == 1                          # no "outputs: deleted" next to the saved line

    old = Task.from_dict({**task.raw, "outputs": None})        # a server that predates the outputs status
    monkeypatch.setattr(FakeClient, "get_task", lambda self, task_id: old)
    assert cli.main(["task", "task_p"]) == 0
    out = capsys.readouterr().out
    assert "log:         deleted 3 days ago\n" in out and "outputs:     deleted" not in out


def test_tasks_table_has_an_outputs_column_when_the_server_sends_it(capsys, monkeypatch):
    assert cli.main(["tasks", "ns"]) == 0                     # FakeClient's tasks come from an older server
    assert "OUTPUTS" not in capsys.readouterr().out

    tasks = [_task_with_outputs("task_up", {"phase": "uploading", **_KNOWN}),
             _task_with_outputs("task_unk", {"phase": "uploading", **_UNKNOWN}),
             _task_with_outputs("task_reg", {"phase": "registering", **_KNOWN}),
             _task_with_outputs("task_part", _PARTIAL),
             _task_with_outputs("task_run", {"phase": "running"}, status="running"),
             _task_with_outputs("task_old", None)]
    monkeypatch.setattr(FakeClient, "list_tasks", lambda self, workspace, **kwargs: tasks)
    assert cli.main(["tasks", "ns"]) == 0
    header, *rows = capsys.readouterr().out.splitlines()
    assert header.split()[:4] == ["NAME", "ID", "STATUS", "OUTPUTS"]
    start = header.index("OUTPUTS")
    end = header.index("GPU")
    cells = {row.split()[1]: row[start:end].strip() for row in rows}
    assert cells == {"task_up": "uploading 48/126", "task_unk": "uploading", "task_reg": "adding to Assets",
                     "task_part": "partly saved 2,000/2,431", "task_run": "-", "task_old": "-"}


# --- assets (Asset Hub) -------------------------------------------------------

def test_assets_output_filters_and_paging(capsys):
    assert cli.main(["assets", "ns"]) == 0
    out = capsys.readouterr().out
    assert "imagenet-mini" in out and "asset_data" in out and "style-lora" in out
    assert "2.00 GiB" in out and "146.5 KiB" in out           # bytes → human (1024 basis, 1024-based labels)
    assert "managed" in out and "s3" in out                    # storage provider labels
    assert "Page 1 of 3 (45 assets)" in out                    # page hint based on the server's total
    assert "VERSIONS" not in out                               # assets have no versions
    link = next(line for line in out.splitlines() if "hf-link" in line)
    assert " 0 B" not in link and "-" in link                  # an unknown size is '-', not 0
    assert FakeClient.last_call == ("list_assets", ("ns",),
                                    {"asset_type": None, "status": None, "page": 1, "page_size": 20})

    assert cli.main(["assets", "ns", "--type", "Dataset", "--status", "active",
                     "--page", "2", "--page-size", "50"]) == 0
    out = capsys.readouterr().out
    assert "Page 2 is past the end: 45 assets on 1 page." in out   # 45 < 50 → only one page
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
    assert capsys.readouterr().out.splitlines() == ["asset_data", "asset_lora", "asset_link"]


def test_assets_page_past_the_end_is_explained(capsys):
    assert cli.main(["assets", "ns", "--page", "9", "--page-size", "20"]) == 0   # 45 assets → only 3 pages
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
    assert "1.4 MiB in 2 files" in out
    assert "upload:     failed" in out and "source:     hf_import" in out
    assert "in use:     yes (pod trainer)" in out
    assert "a.bin" in out and "976.6 KiB" in out and "b.bin" in out    # file table
    assert "import failed: repo not found" in out
    assert "VERSION" not in out
    assert cli.main(["asset", "asset_data", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["asset_data"]
    assert cli.main(["asset", "asset_data", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["assetExternalId"] == "asset_data"


def test_asset_storage_output_and_name(capsys):
    assert cli.main(["asset-storage", "ns"]) == 0
    out = capsys.readouterr().out
    assert "managed:       2.00 GiB" in out
    assert "$0.02 per GiB-month" in out and "$0.03" in out   # asset storage is billed per GiB
    assert "grace (uploads block in" in out
    assert "billing credit: none (pods and tasks cannot start)" in out
    assert "paid balance:" not in out
    assert cli.main(["asset-storage", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "0.03"


# --- transactions (in-flight pod operations) ----------------------------------

def test_transactions_output_alias_and_name(capsys):
    """The table is drawn only when there is at least one in-progress operation — a typo in render_table's arguments (TypeError) only shows up here."""
    assert cli.main(["transactions", "ns"]) == 0
    out = capsys.readouterr().out
    assert FakeClient.last_call == ("list_transactions", ("ns",), {})
    assert "trainer" in out and "9eb176aae9309fb8" in out    # without an alias, the statefulset name
    assert "73213" in out and "pull_image" in out and "image pull stalled for 600s" in out
    assert "42%" in out and "0%" not in out                  # unknown progress (None) is '-' — not printed as 0%
    assert "verifying" in out                                # name of the phase where bytes stall
    assert "txn 73215 source-fetch log (before restart):" in out
    assert "line 6" in out and "line 5\n" not in out          # only the last 10 lines
    assert "HTTP 403" in out and "\x1b[31m" not in out         # control characters in log lines are stripped
    assert "\033[" not in out                                # no color when not a tty

    assert cli.main(["txn", "ns", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["73213", "73214", "73215"]
    assert cli.main(["transactions", "ns", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"transactionId": 73213}, {"transactionId": 73214},
                                                   {"transactionId": 73215}]


def test_transaction_shows_steps_in_order_and_the_reason(capsys):
    assert cli.main(["transaction", "ns", "73216"]) == 0
    out = capsys.readouterr().out
    assert FakeClient.last_call == ("get_transaction", (73216, "ns"), {})
    assert "trainer (5f0e1d2c3b4a6978)" in out and "73216" in out and "failed" in out
    assert "reason:      failed at step fetch_assets: source fetch failed: HTTP 403" in out
    # Steps in the order they happened, not the server's alphabetical order.
    assert out.index("Starting Pod creation process") < out.index("create_statefulset") < out.index("fetch_assets  ")
    assert "txn 73216 source-fetch log:" in out and "  403" in out
    assert cli.main(["transaction", "ns", "73216", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "73216"
    assert cli.main(["transaction", "ns", "73216", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["transactionId"] == 73216


def test_transaction_needs_a_workspace_and_a_numeric_id(capsys):
    for argv in (["transaction", "73216"], ["transaction", "ns", "abc"],
                 ["transaction", "73216", "--workspace", "ns"]):      # the workspace is positional, as in `pod-start`
        with pytest.raises(SystemExit) as exit_:
            cli.main(argv)
        assert exit_.value.code == 2
    capsys.readouterr()


def test_transactions_table_paints_status_on_a_tty(monkeypatch, capsys):
    """Color is turned on with render_table's `enabled=` — drop the argument and the table still prints but color silently turns off."""
    monkeypatch.setattr(cli.fmt, "color_enabled", lambda stream=None: True)
    assert cli.main(["transactions", "ns"]) == 0
    assert "\033[31m● failed" in capsys.readouterr().out    # the failed status cell is red


def test_asset_download_saves_under_asset_id_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["asset-download", "asset_data", "--path", "config/*"]) == 0
    out = capsys.readouterr().out
    assert FakeClient.last_call == ("download_asset", ("asset_data", "asset_data"), {"paths": ["config/*"]})
    assert "asset_data/config/a.json" in out and "5 B" in out and "1 file downloaded." in out
    assert cli.main(["asset-download", "asset_data", "-d", "out", "-o", "name"]) == 0
    assert capsys.readouterr().out.splitlines() == ["out/config/a.json"]


def test_task_outputs_lists_and_downloads(tmp_path, capsys):
    assert cli.main(["task-outputs", "task_a"]) == 0
    out = capsys.readouterr().out
    assert "result.csv" in out and "2.0 KiB" in out
    assert cli.main(["task-outputs", "task_a", "--download", str(tmp_path)]) == 0
    assert "result.csv" in capsys.readouterr().out and (tmp_path / "result.csv").read_bytes() == b"ok"
