"""CLI write commands — argument parsing → SDK call arguments, confirmation (--yes/non-interactive), --estimate, output formats."""
import copy
import importlib
import io
import json

import pytest


cli = importlib.import_module("meshive.cli.main")  # cli.__init__ exposes the main function, which shadows the submodule
from meshive import Meshive, WaitTimeoutError
from meshive.models import (Pod, Logs, PodCreated, PodEstimate, ResourceAction, Serving, StorageCreated, StorageEstimate,
                            TaskEstimate, TaskSubmitted, ModelDetection, ServingModel, AssetImported, WatchedFolders, SshAccess,
                            Transaction)

ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"},
            "resources": {"gpu_model": "RTX 3060", "vram_gb": 12, "gpu_count": 1, "vcpu": 4, "ram_gb": 12, "disk_gb": 25,
                          "rental_type": "demand"},
            "availability": {"available_gpus": 2}, "template": {"id": 457, "name": "VSCode"}, "volumes": [], "note": "Estimated"}
STORAGE_ESTIMATE = {"pricePerHourUsd": "0.000097", "pricePerGbMonthUsd": "0.07", "storageType": "nfs", "maxSizeGb": 305}
TASK_ESTIMATE = {"pricePerHourUsd": None, "maxCostUsd": None, "maxDurationS": 3600, "resources": {}, "note": "CPU"}
TASK = {"externalId": "task_1", "name": "train", "namespaceName": "ws", "status": "queued", "podName": "task-1",
        "image": "img", "gpuModel": None, "gpuCount": 0, "cpuCores": 2, "ramGb": 8, "pricePerHour": "0",
        "costSoFar": "0", "totalCost": "0"}


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, *a, **kw):
        self.calls: list[tuple[str, tuple, dict]] = []
        FakeClient.instances.append(self)

    def _rec(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def get_pod(self, *a, **kw):
        return Pod.from_dict({"podName": a[0], "namespaceName": a[1], "hasUnpreservedWorkspace": True})

    def list_pods(self, *a, **kw):
        self._rec("list_pods", *a, **kw); return []          # right after creation the pod doesn't exist yet

    wait_for_new_pod = Meshive.wait_for_new_pod   # the real SDK method, on top of the fake list_pods
    _check_creation = Meshive._check_creation
    TRANSACTION = {"transactionId": 99, "status": "in_progress", "transactionSteps": []}

    def get_transaction(self, *a, **kw):
        self._rec("get_transaction", *a, **kw); return Transaction.from_dict(self.TRANSACTION)

    def get_serving(self, *a, **kw):
        self._rec("get_serving", *a, **kw)
        return Serving.from_dict({"id": int(a[0]), "namespaceName": "ws", "framework": "vllm", "status": "active",
                                  "minReplicas": 1, "maxReplicas": 3, "currentReplicas": 2, "autoScaleEnabled": False,
                                  "priceCapPerHour": "1.5"})

    def scale_serving(self, *a, **kw):
        self._rec("scale_serving", *a, **kw); return ResourceAction.from_dict({"id": str(a[0]), "action": "scale"}, resource="serving")

    def pause_serving(self, *a, **kw):
        self._rec("pause_serving", *a, **kw)
        return ResourceAction.from_dict({"id": str(a[0]), "action": "resume" if not kw.get("paused", True) else "pause"}, resource="serving")

    def estimate_pod(self, *a, **kw):
        self._rec("estimate_pod", *a, **kw); return PodEstimate.from_dict(ESTIMATE)

    def create_pod(self, *a, **kw):
        self._rec("create_pod", *a, **kw)
        return PodCreated.from_dict({"name": a[0], "workspace": kw["workspace"], "transactionId": 99, "estimate": ESTIMATE})

    def stop_pod(self, *a, **kw):
        self._rec("stop_pod", *a, **kw); return ResourceAction.from_dict({"podName": a[0], "action": "stop", "result": 1}, resource="pod")

    start_pod = restart_pod = delete_pod = lambda self, *a, **kw: (self._rec("pod_action", *a, **kw), ResourceAction.from_dict({"podName": a[0], "action": "x"}, resource="pod"))[1]

    def estimate_storage(self, *a, **kw):
        self._rec("estimate_storage", *a, **kw)
        return StorageEstimate.from_dict({**STORAGE_ESTIMATE, "sizeGb": a[1]})

    def create_storage(self, *a, **kw):
        self._rec("create_storage", *a, **kw)
        return StorageCreated.from_dict({"name": a[0], "workspace": kw["workspace"], "transactionId": 5,
                                         "estimate": {"pricePerHourUsd": "0", "pricePerGbMonthUsd": "0", "sizeGb": 1, "storageType": "nfs", "maxSizeGb": 1}})

    def estimate_task(self, *a, **kw):
        self._rec("estimate_task", *a, **kw)
        return TaskEstimate.from_dict(TASK_ESTIMATE)

    def submit_task(self, *a, **kw):
        self._rec("submit_task", *a, **kw)
        return TaskSubmitted.from_dict({"task": TASK, "estimate": {"maxDurationS": 3600, "resources": {}}})

    def get_pod_logs(self, *a, **kw):
        self._rec("get_pod_logs", *a, **kw)
        return Logs.from_dict({"podName": a[0], "workspace": a[1], "source": "live", "count": 2,
                               "lines": [{"line": "tick 1"}, {"line": "tick 2"}], "truncated": True})

    def list_models(self, *a, **kw):
        self._rec("list_models", *a, **kw)
        return [ServingModel.from_dict({"id": 12, "displayName": "qwen-small", "huggingfaceRepo": "Qwen/Qwen3-0.6B",
                                        "framework": "vllm", "apiModelId": "qwen-small-ab12", "modelType": "llm"})]

    def detect_model(self, *a, **kw):
        self._rec("detect_model", *a, **kw)
        return ModelDetection.from_dict({"status": "unsupported", "output": "text", "detail": "GGUF is not supported",
                                         "suggestedRepo": "Qwen/Qwen3-0.6B"})

    def register_model(self, *a, **kw):
        self.calls.append(("register_model", a, kw))   # kw has name=, which collides with _rec's positional argument
        return ResourceAction.from_dict({"id": "12", "action": "register", "workspace": kw["workspace"],
                                         "result": {"title": "Already Registered", "apiModelId": "qwen-small-ab12"}},
                                        resource="model")

    def delete_model(self, *a, **kw):
        self._rec("delete_model", *a, **kw)
        return ResourceAction.from_dict({"id": str(a[0]), "action": "delete"}, resource="model")

    WATCHED = {"version": "4:cd", "editable": True, "roots": [
        {"role": "output", "path": "/workspace/outputs", "origin": "template", "enabled": True, "include": ["*.png"],
         "includeOverride": None},
        {"role": "user", "path": "/workspace/logs", "origin": "user", "enabled": True, "include": [],
         "includeOverride": ["*.txt"]}]}

    def get_watched_folders(self, *a, **kw):
        self._rec("get_watched_folders", *a, **kw); return WatchedFolders.from_dict(self.WATCHED)

    def set_watched_folders(self, *a, **kw):
        self._rec("set_watched_folders", *a, **kw); return WatchedFolders.from_dict(self.WATCHED)

    def ssh_access(self, *a, **kw):
        self._rec("ssh_access", *a, **kw)
        return SshAccess.from_dict({"password": "pw-123", "sshUrl": "ssh -p 2222 root@m.example",
                                    "sshWebUrl": "https://m.example/?password=x", "expiredAt": 4_000_000_000})

    def import_asset(self, *a, **kw):
        self.calls.append(("import_asset", a, kw))
        return AssetImported.from_dict({"assetExternalId": "asset_abc", "name": "llama", "status": "active",
                                        "ingestSource": "hf_import", "fileCount": 4, "totalBytes": 2048, "isGated": True})

    def close(self):
        pass


@pytest.fixture(autouse=True)
def patch_client(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(cli, "Meshive", FakeClient)


@pytest.fixture
def non_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


def _last(name):
    for call in reversed(FakeClient.instances[-1].calls):
        if call[0] == name:
            return call
    raise AssertionError(f"{name} not called")


def test_pod_create_estimate_only(capsys):
    assert cli.main(["pod-create", "ws", "agent-pod", "--template", "457", "--gpu", "RTX 3060", "--estimate"]) == 0
    out = capsys.readouterr().out
    # Must match the console — $/hr with 3 decimals, and each estimate line with 3 decimals too (Receipt).
    assert "$0.068/hr" in out and "gpu=$0.068" in out and "RTX 3060" in out
    # RAM and disk are GiB (allocated as Kubernetes Gi); only VRAM uses the industry GB.
    assert "RTX 3060 (12 GB)" in out and "4 vCPU / 12 GiB" in out and "25 GiB" in out
    assert not any(c[0] == "create_pod" for c in FakeClient.instances[-1].calls)


def test_pod_create_requires_yes_when_not_tty(capsys, non_tty):
    assert cli.main(["pod-create", "ws", "p", "--template", "1"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not any(c[0] == "create_pod" for c in FakeClient.instances[-1].calls)


def test_pod_create_with_yes_passes_all_options(capsys):
    argv = ["pod-create", "ws", "agent-pod", "--template", "457", "--gpu", "RTX 3060", "--gpu-count", "2", "--vram", "12",
            "--spot", "--vcpu", "8", "--ram", "24", "--volume", "pv-a:/data", "--env", "A=1", "--env", "TOKEN=x",
            "--secret", "TOKEN", "--port", "8888:jupyter", "--port", "6006::internal", "--command", "sleep 1", "--region", "KR",
            "--uptime-premium", "--max-price", "0.5", "--yes", "-o", "name"]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "99"
    assert "--region is deprecated and ignored" in captured.err        # still accepted, no longer sent
    name, args, kw = _last("create_pod")
    assert args == ("agent-pod", 457) and kw["workspace"] == "ws"
    assert kw["gpu_model"] == "RTX 3060" and kw["gpu_count"] == 2 and kw["gpu_vram_gb"] == 12 and kw["rental_type"] == "spot"
    assert kw["vcpu"] == 8 and kw["ram_gb"] == 24 and "disk_gb" not in kw and kw["volumes"] == [("pv-a", "/data")]
    assert kw["env"] == {"A": "1", "TOKEN": "x"} and kw["secret_keys"] == ["TOKEN"]
    assert kw["ports"] == [{"port": 8888, "external": True, "name": "jupyter"}, {"port": 6006, "external": False}]
    assert kw["command"] == "sleep 1" and kw["uptime_premium"] is True and kw["max_price_per_hour"] == "0.5"
    assert "region" not in kw and kw["cpu_line"] is None and kw["local_volumes"] is None


def test_pod_create_bad_env_is_usage_error(capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "1", "--env", "NOEQUALS", "--yes"]) == 2
    assert "KEY=VALUE" in capsys.readouterr().err


def test_pod_stop_and_delete(capsys, non_tty):
    assert cli.main(["pod-stop", "ws", "p-0", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "stop"
    assert cli.main(["pod-delete", "ws", "p-0"]) == 2            # non-interactive + no --yes
    assert cli.main(["pod-delete", "ws", "p-0", "--yes", "--delete-local-storage", "pv-1"]) == 0
    name, args, kw = _last("pod_action")
    assert args == ("p-0", "ws") and kw["delete_local_storages"] == ["pv-1"]
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "--allow-data-loss", "-y"]) == 0
    assert _last("pod_action")[2]["placement"] == "any_node"


def test_storage_create_and_estimate(capsys, non_tty):
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--type", "nfs", "--encrypted", "--estimate"]) == 0
    # The hourly storage unit price has 3 decimals too — with 2 it would be "$0.00" and differ from the console ("$0.000").
    storage_out = capsys.readouterr().out
    assert "$0.000/hr" in storage_out and "per GiB·month: $0.07" in storage_out
    assert "10 GiB" in storage_out and "305 GiB" in storage_out     # size = capacity × 1024 MiB
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--yes", "-o", "name"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "5" and captured.err == ""
    name, args, kw = _last("create_storage")
    assert args == ("vol", 10) and kw["storage_type"] == "nfs" and "disk_type" not in kw and "region" not in kw


def test_task_submit_from_file(tmp_path, capsys):
    script = tmp_path / "train.py"
    script.write_text("print('hi', flush=True)\n")
    assert cli.main(["task-submit", "ws", "train", "--script", str(script), "--image", "python:3.12-slim",
                     "--cpu-preset", "micro-2c8g", "--arg=--epochs", "--arg", "3", "--input-asset", "asset_a:2",
                     "--max-duration", "7200", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "task_1" in captured.out and "task-logs task_1" in captured.out
    assert "no longer have versions" in captured.err           # the old ASSET_ID:VERSION warns and is ignored
    name, args, kw = _last("submit_task")
    assert args == ("train", "print('hi', flush=True)\n") and kw["cpu_preset"] == "micro-2c8g"
    assert kw["args"] == ["--epochs", "3"] and kw["input_assets"] == [{"asset": "asset_a"}] and kw["max_duration"] == 7200


def test_task_submit_missing_script_file(capsys):
    assert cli.main(["task-submit", "ws", "t", "--script", "/nope/x.py", "--image", "i", "--cpu-preset", "micro-2c8g", "--yes"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_logs_output_formats(capsys):
    assert cli.main(["logs", "ws", "p-0", "--tail", "50", "--wait", "3"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "tick 1\ntick 2\n" and "truncated" in captured.err
    assert _last("get_pod_logs")[2] == {"tail": 50, "container": None, "wait": 3.0}
    assert cli.main(["logs", "ws", "p-0", "-o", "name"]) == 0
    assert capsys.readouterr().out == "tick 1\ntick 2\n"


def test_any_node_yes_does_not_imply_data_loss_consent(capsys, non_tty):
    assert cli.main(['pod-start', 'ws', 'p-0', '--any-node', '--yes']) == 2
    assert all(c[0] != 'pod_action' for instance in FakeClient.instances for c in instance.calls)
    assert 'permanently deletes' in capsys.readouterr().err


def test_pod_create_wait_timeout_is_an_error_but_keeps_the_transaction(capsys):
    """If the pod doesn't show up in time, exit 1 — automation must not mistake it for `--wait running` success. The accepted transaction is printed."""
    assert cli.main(["pod-create", "ws", "late-pod", "--template", "457", "--yes", "--wait", "running", "--wait-timeout", "0"]) == 1
    captured = capsys.readouterr()
    assert "transaction" in captured.out and "99" in captured.out
    assert "did not appear" in captured.err and "transaction 99" in captured.err
    assert cli.main(["pod-create", "ws", "late-pod", "--template", "457", "--yes", "--wait", "running", "--wait-timeout", "0",
                     "-o", "name"]) == 1
    assert capsys.readouterr().out.strip() == "99"


def test_disk_flag_is_gone(capsys):
    """The system disk is fixed by a server-side formula — the --disk flag that pretended to take it is gone (argparse ends with usage error 2)."""
    with pytest.raises(SystemExit) as exit_:
        cli.main(["pod-create", "ws", "p", "--template", "1", "--disk", "30", "--yes"])
    assert exit_.value.code == 2 and "--disk" in capsys.readouterr().err


def test_size_options_say_gib_and_vram_stays_gb(capsys):
    """--size and --ram were always GiB (capacity × 1024 MiB, Kubernetes Gi) — so the help says GiB too. Only VRAM tiers are GB."""
    for argv, expected in ((["--help"], ["--size GiB"]),
                           (["storage-create", "--help"], ["--size GiB"]),
                           (["pod-create", "--help"], ["--ram GiB", "RAM in GiB", "--vram GB"]),
                           (["task-submit", "--help"], ["--vram GB"])):
        with pytest.raises(SystemExit):
            cli.main(argv)
        out = capsys.readouterr().out
        assert all(text in out for text in expected), (argv, out)


def test_serving_scale_and_resume_confirm_only_when_cost_can_rise(capsys, non_tty):
    # Wider range → needs confirmation (non-interactive + no --yes → 2, no call)
    assert cli.main(["serving-scale", "42", "--max-replicas", "4"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert all(c[0] != "scale_serving" for c in FakeClient.instances[-1].calls)
    assert cli.main(["serving-scale", "42", "--max-replicas", "4", "--yes"]) == 0
    assert _last("scale_serving")[2]["max_replicas"] == 4
    # Raising the cap and turning on autoscale need confirmation too
    assert cli.main(["serving-scale", "42", "--price-cap", "2"]) == 2
    assert cli.main(["serving-scale", "42", "--autoscale"]) == 2
    # Reductions apply right away without --yes
    assert cli.main(["serving-scale", "42", "--max-replicas", "2", "--price-cap", "1"]) == 0
    assert _last("scale_serving")[2] == {"min_replicas": None, "max_replicas": 2, "autoscale": None, "price_cap_per_hour": "1"}
    # resume restarts billing → confirmation; pause is immediate
    assert cli.main(["serving-resume", "42"]) == 2
    assert all(c[0] != "pause_serving" for c in FakeClient.instances[-1].calls)
    assert cli.main(["serving-resume", "42", "-y"]) == 0 and _last("pause_serving")[2]["paused"] is False
    assert cli.main(["serving-pause", "42"]) == 0 and _last("pause_serving")[2]["paused"] is True


def test_storage_disk_and_region_are_deprecated_and_not_sent(capsys):
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--disk", "SSD", "--region", "KR", "--estimate"]) == 0
    captured = capsys.readouterr()
    assert "nfs / NVMe" in captured.out       # the FakeClient response has no diskType, so the default NVMe
    assert "--disk is deprecated and ignored" in captured.err and "--region is deprecated and ignored" in captured.err
    assert not {"disk_type", "region"} & _last("estimate_storage")[2].keys()


def test_storage_type_is_left_to_the_server(capsys):
    """No argparse choices: the server decides which types it creates (it refuses hostPath with a pointer to pod-create)."""
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--type", "hostPath", "--estimate"]) == 0
    assert _last("estimate_storage")[2]["storage_type"] == "hostPath"
    with pytest.raises(SystemExit):
        cli.main(["storage-create", "--help"])
    assert "--local-volume" in capsys.readouterr().out


# --estimate is shared by pod-create, storage-create and task-submit — each output format's behavior is checked on all three.
ESTIMATE_ARGV = {
    "pod-create": ["pod-create", "ws", "p", "--template", "457", "--gpu", "RTX 3060", "--estimate"],
    "storage-create": ["storage-create", "ws", "vol", "--size", "10", "--estimate"],
    "task-submit": ["task-submit", "ws", "t", "--script-text", "print(1)", "--image", "img", "--cpu-preset", "micro-2c8g",
                    "--estimate"],
}


def _created_nothing():
    return all(c[0] not in ("create_pod", "create_storage", "submit_task") for c in FakeClient.instances[-1].calls)


@pytest.mark.parametrize("flag", [["-o", "json"], ["--json"]])
@pytest.mark.parametrize("command, payload", [("pod-create", ESTIMATE), ("storage-create", {**STORAGE_ESTIMATE, "sizeGb": 10}),
                                              ("task-submit", TASK_ESTIMATE)])
def test_estimate_json_is_only_the_server_payload(capsys, command, payload, flag):
    """`--estimate -o json` prints just the server's estimate payload — the human estimate table used to come first and broke jq/json.loads
    (observed: storage-create's first lines were `estimate:`, `per GiB·month:`, `size:`, then the JSON)."""
    expected = copy.deepcopy(payload)     # FakeClient passes the same dict as raw, so compare with a copy made before the call
    assert cli.main(ESTIMATE_ARGV[command] + flag) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert _created_nothing()


@pytest.mark.parametrize("command, price", [("pod-create", "0.068\n"), ("storage-create", "0.000\n"),
                                            ("task-submit", "")])    # CPU presets don't know the hourly price — no number to print
def test_estimate_name_is_just_the_hourly_price(capsys, command, price):
    """`-o name` prints one headline number like credit and asset-storage — the same number as the table's `$0.068/hr`, without `$`.
    It used to print the human estimate table as-is."""
    assert cli.main(ESTIMATE_ARGV[command] + ["-o", "name"]) == 0
    assert capsys.readouterr().out == price
    assert _created_nothing()


@pytest.mark.parametrize("price, shown", [("0.5", "0.500\n"), ("1.0005", "1.001\n"), ("1234.5678", "1234.568\n")])
def test_estimate_name_rounds_like_the_table(capsys, monkeypatch, price, shown):
    """A task with a unit price (GPU) prints that one number too. Rounding is the table's ROUND_HALF_UP (float formatting gives 1.0005 → 1.000),
    and thousands separators are dropped — the table shows `$1,234.568/hr`."""
    monkeypatch.setattr(FakeClient, "estimate_task",
                        lambda self, *a, **kw: TaskEstimate.from_dict({**TASK_ESTIMATE, "pricePerHourUsd": price}))
    argv = ["task-submit", "ws", "t", "--script-text", "print(1)", "--image", "img", "--gpu", "RTX 3060", "--estimate"]
    assert cli.main(argv + ["-o", "name"]) == 0
    assert capsys.readouterr().out == shown


@pytest.mark.parametrize("command", ESTIMATE_ARGV)
def test_estimate_table_is_the_default(capsys, command):
    assert cli.main(ESTIMATE_ARGV[command]) == 0
    assert capsys.readouterr().out.startswith("estimate:")
    assert _created_nothing()


@pytest.mark.parametrize("command, key, value", [("pod-create", "transactionId", 99), ("storage-create", "transactionId", 5),
                                                 ("task-submit", "task", TASK)])
def test_create_shows_the_estimate_first_only_in_table_mode(capsys, command, key, value):
    """Showing the estimate table before confirming a real create is table mode only — -o json prints just the accepted response."""
    argv = [arg for arg in ESTIMATE_ARGV[command] if arg != "--estimate"] + ["--yes"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("estimate:") and "accepted:" in out
    assert cli.main(argv + ["-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)[key] == value


class _Tty(io.StringIO):
    """A terminal a person answers in — isatty() is True, answers come line by line."""

    def isatty(self):
        return True


class _CtrlC(_Tty):
    def readline(self, *a):
        raise KeyboardInterrupt


@pytest.mark.parametrize("argv, answers", [
    (["pod-create", "ws", "p", "--template", "457"], "y\n"),
    (["storage-create", "ws", "vol", "--size", "10"], "y\n"),
    (["task-submit", "ws", "t", "--script-text", "print(1)", "--image", "img", "--cpu-preset", "micro-2c8g"], "y\n"),
    (["pod-delete", "ws", "p-0"], "y\n"),
    (["pod-start", "ws", "p-0", "--any-node"], "y\ny\n"),        # billing restarts + data-loss consent, two questions
    (["serving-scale", "42", "--max-replicas", "4"], "y\n"),
    (["serving-resume", "42"], "y\n"),
])
def test_confirm_question_goes_to_stderr(capsys, monkeypatch, argv, answers):
    """Confirmation prompts go to stderr — when answering in a terminal while stdout goes to a file or pipe (`-o json > out.json`, `| jq`),
    input() wrote the prompt to stdout, so `Create pod 'p' at $0.068/hr? [y/N] ` got prepended to the result and the person never saw it."""
    monkeypatch.setattr("sys.stdin", _Tty(answers))
    assert cli.main(argv + ["-o", "json"]) == 0
    captured = capsys.readouterr()
    assert isinstance(json.loads(captured.out), dict)
    assert captured.err.count("[y/N]") == answers.count("\n")


def test_confirm_keeps_name_output_to_the_id(capsys, monkeypatch):
    """`id=$(meshive pod-create … -o name)` — the prompt went into $id and the command looked hung."""
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "-o", "name"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "99\n" and "[y/N]" in captured.err


@pytest.mark.parametrize("answers, code", [("n\n", 2), ("", 2), (None, 130)])     # decline, EOF without an answer, Ctrl-C
def test_confirm_refused_or_interrupted_changes_nothing(capsys, monkeypatch, answers, code):
    monkeypatch.setattr("sys.stdin", _CtrlC() if answers is None else _Tty(answers))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "-o", "json"]) == code
    captured = capsys.readouterr()
    assert captured.out == "" and "[y/N]" in captured.err
    assert _created_nothing()


def test_any_node_data_loss_refused_at_the_second_question(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\nn\n"))              # yes to restarting billing, no to data loss
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "-o", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count("[y/N]") == 2
    assert all(c[0] != "pod_action" for c in FakeClient.instances[-1].calls)


def test_confirm_shows_the_estimate_before_asking_when_stdout_is_a_pipe(monkeypatch):
    """Flush stdout before asking, as input() does — stdout going to a pipe (`| tee log`, `2>&1 | tee log`) is block-buffered,
    so without flushing the estimate table shows up only after answering, or after the prompt."""
    pipe = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(pipe, encoding="utf-8"))
    shown = []

    class Human(_Tty):
        def readline(self, *a):
            shown.append(pipe.getvalue().decode())     # stdout sent to the pipe up to the moment of answering
            return "n\n"

    monkeypatch.setattr("sys.stdin", Human())
    assert cli.main(["pod-create", "ws", "p", "--template", "457"]) == 2
    assert shown and shown[0].startswith("estimate:")


def test_model_commands(capsys, non_tty):
    assert cli.main(["models", "ws"]) == 0
    out = capsys.readouterr().out
    assert "qwen-small" in out and "12" in out and "qwen-small-ab12" in out

    assert cli.main(["model-detect", "ws", "Qwen/Qwen3-0.6B-GGUF", "--hf-token", "4"]) == 1   # can't be served → exit 1
    out = capsys.readouterr().out
    assert "unsupported" in out and "GGUF is not supported" in out and "Qwen/Qwen3-0.6B" in out
    assert _last("detect_model")[2] == {"workspace": "ws", "hf_token_id": 4}

    # Registration costs nothing, so it proceeds without --yes (non-interactive)
    assert cli.main(["model-register", "ws", "Qwen/Qwen3-0.6B", "--name", "qwen-small", "--framework", "sglang"]) == 0
    out = capsys.readouterr().out
    assert "Already registered: model #12" in out and "serving-deploy ws 12" in out
    assert _last("register_model")[2] == {"workspace": "ws", "name": "qwen-small", "framework": "sglang",
                                          "hf_token_id": None, "context_length": None}

    assert cli.main(["model-delete", "12"]) == 2                 # deleting needs confirmation
    assert cli.main(["model-delete", "12", "--yes"]) == 0
    assert _last("delete_model")[1] == (12,)


def test_asset_import_needs_no_confirmation(capsys, non_tty):
    assert cli.main(["asset-import", "ws", "meta-llama/Llama-3.1-8B", "--hf-token", "2", "--path", "*.json",
                     "--type", "model"]) == 0
    out = capsys.readouterr().out
    assert "llama (asset_abc)" in out and "4 (2.0 KiB)" in out and "saved token or key" in out
    assert _last("import_asset")[2] == {"workspace": "ws", "name": None, "asset_type": "model", "revision": None,
                                        "paths": ["*.json"], "hf_token_id": 2, "civitai_key_id": None}


def test_pod_create_input_assets_and_watch(capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--gpu", "RTX 3060", "--estimate",
                     "--input-asset", "asset_a", "--input-asset", "asset_b=/workspace/models", "--watch", "/workspace/out"]) == 0
    kw = _last("estimate_pod")[2]
    assert kw["input_assets"] == ["asset_a", {"asset": "asset_b", "target_dir": "/workspace/models"}]
    assert kw["watched_folders"] == ["/workspace/out"]


def test_pod_create_watch_off_turns_template_folders_off():
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--estimate", "--watch-off", "/outputs"]) == 0
    assert _last("estimate_pod")[2]["watched_template_folders"] == {"/outputs": {"enabled": False}}


def test_pod_watch_shows_and_rewrites_with_the_version(capsys, non_tty):
    assert cli.main(["pod-watch", "ws", "p-0"]) == 0
    out = capsys.readouterr().out
    assert "/workspace/outputs" in out and "template" in out and "*.png" in out and "*.txt" in out
    assert not any(c[0] == "set_watched_folders" for c in FakeClient.instances[-1].calls)

    # Changes that add folders need confirmation (harvest storage is billed)
    assert cli.main(["pod-watch", "ws", "p-0", "--add", "/workspace/ckpt", "--include", "*.pt"]) == 2
    assert cli.main(["pod-watch", "ws", "p-0", "--add", "/workspace/ckpt", "--include", "*.pt", "--existing", "--yes"]) == 0
    kw = _last("set_watched_folders")[2]
    assert kw["expected_version"] == 4
    assert kw["template"] == {"/workspace/outputs": {"enabled": True, "include": None}}
    assert kw["user"] == [{"path": "/workspace/logs", "include": ["*.txt"], "enabled": True},
                          {"path": "/workspace/ckpt", "include": ["*.pt"], "include_existing": True}]

    # Reductions apply right away
    assert cli.main(["pod-watch", "ws", "p-0", "--off", "/workspace/outputs", "--remove", "/workspace/logs"]) == 0
    kw = _last("set_watched_folders")[2]
    assert kw["template"] == {"/workspace/outputs": {"enabled": False, "include": None}} and kw["user"] == []
    assert cli.main(["pod-watch", "ws", "p-0", "--off", "/nope"]) == 2     # unknown folder → usage error


def test_ssh_prints_the_command_and_password_only(capsys):
    assert cli.main(["ssh", "ws", "p-0"]) == 0
    out = capsys.readouterr().out
    assert "ssh -p 2222 root@m.example" in out and "pw-123" in out and "in " in out   # expiry is a time in the future
    assert _last("ssh_access")[1] == ("p-0", "ws")
    assert cli.main(["ssh", "ws", "p-0", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "ssh -p 2222 root@m.example"


# --- 1.1.0 ------------------------------------------------------------------------------------------------

def test_pod_create_local_volumes_and_cpu_line(capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--cpu-line", "amd-epyc-4-ddr5",
                     "--local-volume", "scratch:/scratch:50", "--local-volume", "keys:/keys:10:encrypted", "--estimate"]) == 0
    kw = _last("estimate_pod")[2]
    assert kw["cpu_line"] == "amd-epyc-4-ddr5"
    assert kw["local_volumes"] == [{"name": "scratch", "mount_path": "/scratch", "size_gb": 50},
                                   {"name": "keys", "mount_path": "/keys", "size_gb": 10, "encrypted": True}]


@pytest.mark.parametrize("value", ["scratch:/scratch", "scratch:scratch:50", ":/scratch:50", "scratch:/scratch:big",
                                   "scratch:/scratch:50:fast", "a:/b:1:encrypted:x"])
def test_pod_create_bad_local_volume_is_usage_error(capsys, value):
    assert cli.main(["pod-create", "ws", "p", "--template", "1", "--local-volume", value, "--yes"]) == 2
    assert "NAME:/PATH:SIZE_GB[:encrypted]" in capsys.readouterr().err
    assert not any(c[0] in ("estimate_pod", "create_pod") for c in FakeClient.instances[-1].calls)


def test_pod_estimate_lists_new_local_volumes(capsys, monkeypatch):
    payload = {**ESTIMATE, "breakdown": {"gpu": "0.068423", "local_storage": "0.0026"},
               "volumes": [{"storage": "pv-a", "mount_path": "/data"},
                           {"storage": None, "name": "scratch", "mount_path": "/scratch", "storage_type": "hostPath",
                            "size_gb": 50, "encrypted": True, "new": True}]}
    monkeypatch.setattr(FakeClient, "estimate_pod", lambda self, *a, **kw: PodEstimate.from_dict(payload))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--estimate"]) == 0
    out = capsys.readouterr().out
    assert "pv-a→/data" in out and "scratch (new local, 50 GiB, encrypted)→/scratch" in out and "None" not in out
    assert "local_storage=$0.003" in out


def test_pod_create_wait_passes_the_transaction_and_reports_a_failed_creation(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "TRANSACTION", {
        "transactionId": 99, "status": "failed", "transactionSteps": [
            {"step": "image_pull", "status": "failed", "detail": "",
             "failureDiagnostic": {"failureReason": "manifest unknown: \x1b[31mno such tag"}}]})
    argv = ["pod-create", "ws", "bad-image", "--template", "457", "--yes", "--wait", "running", "--wait-timeout", "600"]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert _last("get_transaction")[1] == (99, "ws")
    assert "(not created)" in captured.out and "99" in captured.out
    assert "failed to start and was cleaned up: creation failed at step image_pull: manifest unknown:" in captured.err
    assert "\x1b" not in captured.err and "meshive transaction ws 99" in captured.err   # server text is cleaned
    assert cli.main(argv + ["-o", "name"]) == 1
    assert capsys.readouterr().out.strip() == "99"


@pytest.mark.parametrize("placement", ["same_node", "wait_same_node", "Next_Week"])
def test_pod_start_placement_is_passed_through(capsys, non_tty, placement):
    """--placement is free text — the server knows which placements exist (and answers 422 for an unknown one)."""
    assert cli.main(["pod-start", "ws", "p-0", "--placement", placement, "--yes"]) == 0
    assert _last("pod_action")[2] == {"placement": placement, "allow_data_loss": False}


def test_pod_start_wait_any_node_needs_data_loss_consent(capsys, non_tty):
    assert cli.main(["pod-start", "ws", "p-0", "--placement", "wait_any_node", "--yes"]) == 2
    assert "permanently deletes" in capsys.readouterr().err
    assert all(c[0] != "pod_action" for c in FakeClient.instances[-1].calls)
    assert cli.main(["pod-start", "ws", "p-0", "--placement", "wait_any_node", "--allow-data-loss", "--yes"]) == 0
    assert _last("pod_action")[2] == {"placement": "wait_any_node", "allow_data_loss": True}


def test_pod_start_any_node_shorthand(capsys, non_tty):
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "--placement", "any_node", "--allow-data-loss", "-y"]) == 0
    assert _last("pod_action")[2]["placement"] == "any_node"
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "--placement", "wait_same_node", "-y"]) == 2
    assert "--any-node is short for --placement any_node" in capsys.readouterr().err
    assert all(c[0] != "pod_action" for c in FakeClient.instances[-1].calls)


def test_pod_start_wait_any_node_asks_twice_on_a_terminal(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\ny\n"))
    assert cli.main(["pod-start", "ws", "p-0", "--placement", "wait_any_node", "-o", "json"]) == 0
    assert capsys.readouterr().err.count("[y/N]") == 2
    assert _last("pod_action")[2] == {"placement": "wait_any_node", "allow_data_loss": True}


# --- 1.1.0: server-chosen start placement, template ports, storage usage warnings ------------------------

def _start_returns(monkeypatch, payload=None, error=None):
    def start_pod(self, *a, **kw):
        self._rec("pod_action", *a, **kw)
        if error:
            raise error
        return ResourceAction.from_dict({"podName": a[0], "action": "start", **(payload or {})}, resource="pod")
    monkeypatch.setattr(FakeClient, "start_pod", start_pod)


def test_pod_start_without_placement_lets_the_server_choose(capsys, non_tty, monkeypatch):
    _start_returns(monkeypatch, {"placement": "wait_same_node"})
    assert cli.main(["pod-start", "ws", "p-0", "--yes"]) == 0
    assert _last("pod_action")[2] == {"allow_data_loss": False}               # no placement sent
    assert all(c[0] != "get_pod" for c in FakeClient.instances[-1].calls)     # nothing to check in advance
    out = capsys.readouterr().out
    assert "placement:" in out and "wait_same_node" in out and "waits until a node has room" in out
    assert cli.main(["pod-start", "ws", "p-0", "--yes", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["placement"] == "wait_same_node"
    assert cli.main(["pod-start", "ws", "p-0", "--allow-data-loss", "--yes"]) == 0
    assert _last("pod_action")[2] == {"allow_data_loss": True}                # consent for whatever the server picks


def test_pod_start_output_without_a_reported_placement(capsys, non_tty):
    assert cli.main(["pod-start", "ws", "p-0", "--placement", "same_node", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "placement" not in out and "waits until" not in out


def test_pod_start_server_asks_for_data_loss_consent(capsys, non_tty, monkeypatch):
    """Without --placement the CLI can't know in advance that the pod may move — the server's 409 says which placement
    it chose, and the error tells how to consent to it."""
    from meshive.exceptions import ConflictError
    error = ConflictError(409, "consent required", title="Data Loss Consent Required",
                          raw={"detail": {"title": "Data Loss Consent Required", "message": "consent required",
                                          "placement": "wait_any_node"}})
    _start_returns(monkeypatch, error=error)
    assert cli.main(["pod-start", "ws", "p-0", "--yes", "-o", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "permanently deletes unpreserved workspace files" in captured.err
    assert "the server would start this pod with placement wait_any_node" in captured.err
    assert "re-run with --placement wait_any_node --allow-data-loss" in captured.err


def test_pod_start_consent_refusal_after_an_explicit_placement(capsys, non_tty, monkeypatch):
    """The pod gained unpreserved files after the CLI looked — same message as the CLI's own check."""
    from meshive.exceptions import ConflictError
    monkeypatch.setattr(FakeClient, "get_pod", lambda self, *a, **kw: Pod.from_dict(
        {"podName": a[0], "namespaceName": a[1], "hasUnpreservedWorkspace": False}))
    _start_returns(monkeypatch, error=ConflictError(409, "consent required", title="Data Loss Consent Required",
                                                    raw={"detail": {"placement": "any_node"}}))
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "--yes"]) == 2
    err = capsys.readouterr().err
    assert "separate data-loss consent is required: --allow-data-loss" in err


def test_pod_start_other_conflicts_stay_errors(capsys, non_tty, monkeypatch):
    from meshive.exceptions import ConflictError
    _start_returns(monkeypatch, error=ConflictError(409, "no room", title="No Capacity", raw={}))
    assert cli.main(["pod-start", "ws", "p-0", "--yes"]) == 1
    assert "[409] No Capacity: no room" in capsys.readouterr().err


def test_pod_create_template_ports(capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--port", "9000",
                     "--template-port", "8080:ide", "--template-port", "8888::9999",
                     "--template-port", "22:::internal", "--template-port", "7860:ui:7861:External",
                     "--template-port", "3000:", "--yes"]) == 0
    expected = [{"port": 8080, "name": "ide"}, {"port": 8888, "new_port": 9999}, {"port": 22, "external": False},
                {"port": 7860, "name": "ui", "new_port": 7861, "external": True}, {"port": 3000}]
    assert _last("estimate_pod")[2]["template_ports"] == expected
    assert _last("create_pod")[2]["template_ports"] == expected
    assert _last("create_pod")[2]["ports"] == [{"port": 9000, "external": True}]
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--estimate"]) == 0
    assert _last("estimate_pod")[2]["template_ports"] is None


@pytest.mark.parametrize("value, message", [
    ("ide", "PORT[:NAME][:NEW_PORT][:internal|external]"),
    ("8080:ide:fast", "PORT[:NAME][:NEW_PORT][:internal|external]"),
    ("8080:ide:8443:public", "PORT[:NAME][:NEW_PORT][:internal|external]"),
    ("8080:ide:8443:external:x", "PORT[:NAME][:NEW_PORT][:internal|external]"),
    ("8080:8443", "write 8080::8443"),
    ("8080:internal", "write 8080:::internal"),
    ("8080:External", "write 8080:::external"),
])
def test_pod_create_bad_template_port_is_usage_error(capsys, value, message):
    assert cli.main(["pod-create", "ws", "p", "--template", "1", "--template-port", value, "--yes"]) == 2
    assert message in capsys.readouterr().err
    assert not any(c[0] in ("estimate_pod", "create_pod") for c in FakeClient.instances[-1].calls)


@pytest.mark.parametrize("flags, expected", [
    ([], {}),
    (["--usage-warning"], {"usage_warning": True}),
    (["--usage-warning", "80"], {"usage_warning": True, "usage_warning_threshold": 0.8}),
    (["--usage-warning=85%"], {"usage_warning": True, "usage_warning_threshold": 0.85}),
    (["--usage-warning", "100"], {"usage_warning": True, "usage_warning_threshold": 1.0}),
])
def test_storage_create_usage_warning(capsys, flags, expected):
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", *flags, "--yes"]) == 0
    for method in ("estimate_storage", "create_storage"):
        kw = _last(method)[2]
        assert {k: v for k, v in kw.items() if k.startswith("usage_warning")} == expected


@pytest.mark.parametrize("value", ["0.9", "0", "101", "lots", ""])
def test_storage_create_bad_usage_warning_is_usage_error(capsys, value):
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", f"--usage-warning={value}", "--yes"]) == 2
    assert "--usage-warning expects a whole percentage from 1 to 100" in capsys.readouterr().err
    assert not any(c[0] in ("estimate_storage", "create_storage") for c in FakeClient.instances[-1].calls)


def test_storage_create_small_size_is_left_to_the_server(capsys):
    assert cli.main(["storage-create", "ws", "vol", "--size", "5", "--estimate"]) == 0
    assert _last("estimate_storage")[1] == ("vol", 5)


# --- 1.2.0: pod and storage edit, storage connect, model sets ----------------------------------------------

from meshive.models import (ModelSetList, PodUpdated, PodUpdateEstimate, StorageConnection,  # noqa: E402
                            StorageUpdated, StorageUpdateEstimate)

POD_UPDATE_ESTIMATE = {"podName": "p-0", "name": "agent-pod", "restarts": True,
                       "changes": ["GPU: 1x RTX 3060 -> 1x RTX 4090", "Environment: adds \x1b[31mHF_HOME"],
                       "pricePerHourUsd": "0.31", "currentPricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.31"},
                       "resources": {}, "volumes": [{"storage": "pv-a", "mount_path": "/data"}], "note": "Restarts."}
STORAGE_UPDATE_ESTIMATE = {"pvName": "pv-1", "name": "data", "sizeGb": 200, "currentSizeGb": 100,
                           "pricePerHourUsd": "0.0194", "currentPricePerHourUsd": "0.0097", "usageWarning": True,
                           "usageWarningThreshold": 0.8, "note": "Billed at the new size."}
MODEL_SETS = {"templateId": 457, "selectable": True, "modelSets": [
    {"slug": "sdxl-basics", "name": "SDXL basics", "task": "text-to-image", "totalSizeBytes": 7 * 1024 ** 3,
     "minVramGb": 12, "minVramEstimated": True,
     "licenses": [{"license": "openrail++"}, {"license": "openrail++"}, {"license": "mit"}], "licenseUnknownCount": 1,
     "items": [{"entrySlug": "a", "name": "a"}, {"entrySlug": "b", "name": "b"}]},
    {"slug": "flux-dev", "name": "FLUX dev", "task": "text-to-image", "totalSizeBytes": None, "minVramGb": None,
     "licenses": [], "items": []}]}


def _fake(name, value):
    """A FakeClient method that records its call and returns value (or raises it, when it is an exception)."""
    def method(self, *a, **kw):
        self.calls.append((name, a, kw))
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else value
    return method


@pytest.fixture
def edit_client(monkeypatch):
    monkeypatch.setattr(FakeClient, "estimate_pod_update", _fake(
        "estimate_pod_update", lambda: PodUpdateEstimate.from_dict(copy.deepcopy(POD_UPDATE_ESTIMATE))), raising=False)
    monkeypatch.setattr(FakeClient, "update_pod", _fake("update_pod", lambda: PodUpdated.from_dict(
        {"podName": "p-0", "workspace": "ws", "accepted": True, "transactionId": 900, "renamedOnly": False,
         "estimate": POD_UPDATE_ESTIMATE})), raising=False)
    monkeypatch.setattr(FakeClient, "estimate_storage_update", _fake(
        "estimate_storage_update", lambda: StorageUpdateEstimate.from_dict(dict(STORAGE_UPDATE_ESTIMATE))),
        raising=False)
    monkeypatch.setattr(FakeClient, "update_storage", _fake("update_storage", lambda: StorageUpdated.from_dict(
        {"pvName": "pv-1", "workspace": "ws", "accepted": True, "transactionId": 77,
         "estimate": STORAGE_UPDATE_ESTIMATE})), raising=False)
    monkeypatch.setattr(FakeClient, "list_model_sets", _fake(
        "list_model_sets", lambda: ModelSetList.from_dict(copy.deepcopy(MODEL_SETS))), raising=False)


def _connection(phase="ready", encrypted=True, ssh=True, **extra):
    conn = StorageConnection.from_dict({"pvName": "pv-1", "workspace": "ws", "phase": phase, "encrypted": encrypted,
                                        "podName": "pv-1-access-0" if phase == "ready" else None, **extra})
    if ssh:
        conn.ssh = SshAccess.from_dict({"password": "pw-123", "sshUrl": "ssh -p 2222 gateway@m.example",
                                        "expiredAt": 4_000_000_000})
    return conn


# storage-update

def test_storage_update_needs_something_to_change(capsys, edit_client):
    assert cli.main(["storage-update", "ws", "pv-1", "--yes"]) == 2
    assert "nothing to change" in capsys.readouterr().err
    assert FakeClient.instances[-1].calls == []


def test_storage_update_estimate(capsys, edit_client):
    assert cli.main(["storage-update", "ws", "pv-1", "--size", "200", "--estimate"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("estimate:") and "$0.019/hr (now $0.010/hr)" in out
    assert "200 GiB (now 100 GiB)" in out and "on, above 80%" in out and "data (pv-1)" in out
    assert _last("estimate_storage_update")[1:] == (("pv-1", "ws"), {"size_gb": 200})
    assert all(c[0] != "update_storage" for c in FakeClient.instances[-1].calls)
    assert cli.main(["storage-update", "ws", "pv-1", "--size", "200", "--estimate", "-o", "name"]) == 0
    assert capsys.readouterr().out == "0.019\n"


def test_storage_update_confirms_and_sends_only_the_flags_given(capsys, edit_client, non_tty):
    assert cli.main(["storage-update", "ws", "pv-1", "--size", "200"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert all(c[0] != "update_storage" for c in FakeClient.instances[-1].calls)
    assert cli.main(["storage-update", "ws", "pv-1", "--size", "200", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "accepted:" in out and "77" in out
    assert _last("update_storage")[1:] == (("pv-1", "ws"), {"size_gb": 200})
    assert cli.main(["storage-update", "ws", "pv-1", "--name", "archive", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out == "pv-1\n" and _last("update_storage")[2] == {"name": "archive"}


@pytest.mark.parametrize("flags, expected", [
    (["--usage-warning"], {"usage_warning": True}),
    (["--usage-warning", "80"], {"usage_warning": True, "usage_warning_threshold": 0.8}),
    (["--no-usage-warning"], {"usage_warning": False}),
    (["--name", "x", "--size", "20", "--no-usage-warning"], {"name": "x", "size_gb": 20, "usage_warning": False}),
])
def test_storage_update_usage_warning_flags(capsys, edit_client, flags, expected):
    assert cli.main(["storage-update", "ws", "pv-1", *flags, "--yes", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["transactionId"] == 77
    assert _last("estimate_storage_update")[2] == expected and _last("update_storage")[2] == expected


def test_storage_update_flag_errors(capsys, edit_client):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["storage-update", "ws", "pv-1", "--usage-warning", "--no-usage-warning"])
    assert exit_.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["storage-update", "pv-1", "--size", "20"])              # both the workspace and the storage
    capsys.readouterr()
    assert cli.main(["storage-update", "ws", "pv-1", "--usage-warning=150", "--yes"]) == 2
    assert "whole percentage" in capsys.readouterr().err


# storage-connect

def test_storage_connect_prints_the_ssh_command(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "connect_storage", _fake("connect_storage", lambda: _connection(
        remainingSeconds=1790, autoLockMinutes=30)), raising=False)
    assert cli.main(["storage-connect", "ws", "pv-1"]) == 0
    out = capsys.readouterr().out
    assert "ssh -p 2222 gateway@m.example" in out and "pw-123" in out and "pv-1-access-0" in out
    assert "locks again automatically in about 30 min" in out
    assert _last("connect_storage")[1:] == (("pv-1", "ws"), {"wait": True, "timeout": 300.0})
    assert cli.main(["storage-connect", "ws", "pv-1", "-o", "name"]) == 0
    assert capsys.readouterr().out == "ssh -p 2222 gateway@m.example\n"
    assert cli.main(["storage-connect", "ws", "pv-1", "-o", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["phase"] == "ready" and payload["ssh"]["password"] == "pw-123"


def test_storage_connect_plain_storage_has_no_lock_note(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "connect_storage", _fake("connect_storage", lambda: _connection(encrypted=False)),
                        raising=False)
    assert cli.main(["storage-connect", "ws", "pv-1"]) == 0
    assert "locks again" not in capsys.readouterr().out


def test_storage_connect_without_waiting(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "connect_storage", _fake("connect_storage", lambda: _connection(
        "preparing", ssh=False, autoLockMinutes=30)), raising=False)
    assert cli.main(["storage-connect", "ws", "pv-1", "--no-wait", "--wait-timeout", "60"]) == 0
    out = capsys.readouterr().out
    assert "preparing" in out and "Not ready yet" in out and "storage-connect ws pv-1" in out
    assert "after 30 min" in out and "password" not in out
    assert _last("connect_storage")[2] == {"wait": False, "timeout": 60.0}
    assert cli.main(["storage-connect", "ws", "pv-1", "--no-wait", "-o", "name"]) == 0
    assert capsys.readouterr().out == "preparing\n"


@pytest.mark.parametrize("error, message", [
    (WaitTimeoutError("Storage pv-1 was not ready for SSH access within 300s (last phase: preparing)."), "within 300s"),
    ("connection-error", "could not be opened for SSH access"),
])
def test_storage_connect_failures_exit_1(capsys, monkeypatch, error, message):
    from meshive.exceptions import StorageConnectionError
    if error == "connection-error":
        error = StorageConnectionError("pv-1")
    monkeypatch.setattr(FakeClient, "connect_storage", _fake("connect_storage", error), raising=False)
    assert cli.main(["storage-connect", "ws", "pv-1"]) == 1
    assert message in capsys.readouterr().err


# model-sets

def test_model_sets_table(capsys, edit_client):
    assert cli.main(["model-sets", "ws", "--template", "457"]) == 0
    out = capsys.readouterr().out
    assert "sdxl-basics" in out and "SDXL basics" in out and "7.00 GiB" in out and "~12 GB" in out
    assert "openrail++, mit + 1 unknown" in out and "pod-create --model-set" in out
    assert _last("list_model_sets")[1] == ("ws", 457)
    assert cli.main(["model-sets", "ws", "--template", "457", "-o", "name"]) == 0
    assert capsys.readouterr().out == "sdxl-basics\nflux-dev\n"
    assert cli.main(["model-sets", "ws", "--template", "457", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["modelSets"][0]["slug"] == "sdxl-basics"


def test_model_sets_not_selectable(capsys, monkeypatch):
    monkeypatch.setattr(FakeClient, "list_model_sets", _fake("list_model_sets", lambda: ModelSetList.from_dict(
        {"templateId": 12, "selectable": False, "reason": "This template adds its own model set."})), raising=False)
    assert cli.main(["model-sets", "ws", "--template", "12"]) == 0
    assert "doesn't take a model set: This template adds its own model set." in capsys.readouterr().out


# pod-create --model-set

def test_pod_create_with_a_model_set(capsys, monkeypatch):
    payload = {**ESTIMATE, "modelSet": {"slug": "sdxl-basics", "name": "SDXL basics", "file_count": 2,
                                        "total_size_bytes": 7 * 1024 ** 3, "min_vram_gb": 12,
                                        "min_vram_estimated": True}}
    monkeypatch.setattr(FakeClient, "estimate_pod", _fake("estimate_pod", lambda: PodEstimate.from_dict(payload)))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--model-set", "sdxl-basics", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "model set: SDXL basics (sdxl-basics): 2 files, 7.00 GiB, min VRAM ~12 GB" in captured.out
    assert "can take several minutes" in captured.err
    assert _last("estimate_pod")[2]["model_set"] == "sdxl-basics" and _last("create_pod")[2]["model_set"] == "sdxl-basics"
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--estimate"]) == 0
    captured = capsys.readouterr()
    assert _last("estimate_pod")[2]["model_set"] is None and "can take several minutes" not in captured.err


def test_pod_create_model_set_timeout(capsys, monkeypatch):
    import httpx
    request = httpx.Request("POST", "https://api.test/v1/sdk/pods")
    monkeypatch.setattr(FakeClient, "create_pod", _fake("create_pod", httpx.ReadTimeout("no answer", request=request)))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--model-set", "sdxl-basics", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "no answer within 900s" in err and "may still be created" in err and "meshive pods ws" in err
    # Without a model set a timeout is still the general "could not reach" error.
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--yes"]) == 1
    assert "could not reach the Meshive API" in capsys.readouterr().err


def test_pod_create_model_set_refusals(capsys, monkeypatch):
    from meshive.exceptions import MeshiveAPIError
    unavailable = MeshiveAPIError(400, "1 file could not be fetched.", title="Model Set Unavailable", raw={"detail": {
        "title": "Model Set Unavailable", "modelSet": "sdxl-basics",
        "failed": [{"slug": "sdxl-vae", "name": "sdxl_vae", "error": "403 from \x1b[31msource"}]}})
    monkeypatch.setattr(FakeClient, "create_pod", _fake("create_pod", unavailable))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--model-set", "sdxl-basics", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "[400] Model Set Unavailable" in err and "sdxl_vae: 403 from" in err and "\x1b" not in err
    assert "reused next time" in err

    wrong = MeshiveAPIError(400, "not for this template", title="Model Set Not For Template",
                            raw={"detail": {"available": ["sdxl-basics", {"slug": "flux-dev"}]}})
    monkeypatch.setattr(FakeClient, "estimate_pod", _fake("estimate_pod", wrong))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--model-set", "nope", "--estimate"]) == 1
    err = capsys.readouterr().err
    assert "model sets: sdxl-basics, flux-dev" in err and "meshive model-sets ws --template 457" in err

    other = MeshiveAPIError(400, "bad", title="Invalid Pod Settings", raw={})
    monkeypatch.setattr(FakeClient, "estimate_pod", _fake("estimate_pod", other))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "--estimate"]) == 1
    assert "[400] Invalid Pod Settings: bad" in capsys.readouterr().err


# pod-update

def test_pod_update_needs_something_to_change(capsys, edit_client):
    for extra in ([], ["--allow-node-migration", "--allow-data-loss", "--max-price", "1",
                       "--drop-watched-folder", "/x"]):
        assert cli.main(["pod-update", "ws", "p-0", "--yes", *extra]) == 2
        assert "nothing to change" in capsys.readouterr().err
        assert FakeClient.instances[-1].calls == []


def test_pod_update_maps_every_flag(capsys, edit_client):
    argv = ["pod-update", "ws", "p-0", "--name", "agent-pod", "--template", "458", "--rental", "spot",
            "--gpu", "RTX 4090", "--gpu-count", "2", "--vram", "24", "--vcpu", "16", "--ram", "64",
            "--cpu-line", "amd-epyc-4-ddr5", "--env", "HF_HOME=/data/hf", "--env", "TOKEN=x", "--secret", "TOKEN",
            "--remove-env", "OLD", "--port", "9000:api", "--template-port", "8080::8443", "--remove-port", "6006",
            "--command", "", "--volume", "pv-net:/data", "--local-volume", "scratch:/scratch:50", "--detach", "pv-old",
            "--allow-node-migration", "--allow-data-loss", "--drop-watched-folder", "/workspace/out",
            "--max-price", "0.5", "--yes"]
    assert cli.main(argv) == 0
    expected = {
        "name": "agent-pod", "template_id": 458, "rental_type": "spot", "gpu_model": "RTX 4090", "gpu_count": 2,
        "gpu_vram_gb": 24, "vcpu": 16, "ram_gb": 64, "cpu_line": "amd-epyc-4-ddr5",
        "env": {"HF_HOME": "/data/hf", "TOKEN": "x"}, "secret_keys": ["TOKEN"], "remove_env": ["OLD"],
        "ports": [{"port": 9000, "external": True, "name": "api"}], "template_ports": [{"port": 8080, "new_port": 8443}],
        "remove_ports": [6006], "command": "", "volumes": [("pv-net", "/data")],
        "local_volumes": [{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}], "detach_storages": ["pv-old"],
        "max_price_per_hour": "0.5", "allow_node_migration": True, "allow_data_loss": True,
        "drop_watched_folders": ["/workspace/out"],
    }
    assert _last("estimate_pod_update")[1:] == (("p-0", "ws"), expected)
    assert _last("update_pod")[1:] == (("p-0", "ws"), expected)
    out = capsys.readouterr().out
    assert "transaction" in out and "900" in out and "meshive transaction ws 900" in out


def test_pod_update_sends_only_the_flags_given(capsys, edit_client):
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "--yes"]) == 0
    assert _last("update_pod")[2] == {"gpu_model": "RTX 4090"}


def test_pod_update_estimate(capsys, edit_client):
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "--estimate"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("estimate:") and "$0.310/hr (now $0.068/hr)" in out
    assert "agent-pod (p-0)" in out and "yes, the running pod restarts" in out
    assert "  - GPU: 1x RTX 3060 -> 1x RTX 4090" in out and "Environment: adds" in out and "\x1b" not in out
    assert "pv-a→/data" in out and "gpu=$0.310" in out
    assert all(c[0] != "update_pod" for c in FakeClient.instances[-1].calls)
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "--estimate", "-o", "name"]) == 0
    assert capsys.readouterr().out == "0.310\n"
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "--estimate", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"][0].startswith("GPU")


def test_pod_update_confirms_first(capsys, edit_client, non_tty, monkeypatch):
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert all(c[0] != "update_pod" for c in FakeClient.instances[-1].calls)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "-o", "name"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "p-0\n" and "Update pod p-0 (then $0.310/hr)? It restarts" in captured.err


def test_pod_update_rename_only_output(capsys, edit_client, monkeypatch):
    monkeypatch.setattr(FakeClient, "update_pod", _fake("update_pod", lambda: PodUpdated.from_dict(
        {"podName": "p-0", "workspace": "ws", "accepted": True, "transactionId": None, "renamedOnly": True,
         "estimate": {}})))
    assert cli.main(["pod-update", "ws", "p-0", "--name", "new-name", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Renamed in place" in out and "meshive transaction" not in out


def _refusal(status, title, **detail):
    from meshive.exceptions import ConflictError, MeshiveAPIError
    cls = ConflictError if status == 409 else MeshiveAPIError
    return cls(status, f"{title} message", title=title, raw={"detail": {"title": title, **detail}})


@pytest.mark.parametrize("step", ["estimate_pod_update", "update_pod"])
@pytest.mark.parametrize("error, expected", [
    (_refusal(409, "Node Migration Required"), ["Node Migration Required message", "permanently deletes",
                                               "re-run with --allow-node-migration"]),
    (_refusal(409, "Data Loss Consent Required"), ["permanently deletes", "re-run with --allow-data-loss"]),
    (_refusal(400, "Watched folders would be removed", code="harvest_roots_orphaned",
              paths=["/workspace/out", "/workspace/my logs"]),
     ["removes watched folders of the pod: /workspace/out, /workspace/my logs",
      "--drop-watched-folder /workspace/out --drop-watched-folder '/workspace/my logs'"]),
    (_refusal(409, "Resource values need confirmation"), ["Re-run with --ram GiB"]),
])
def test_pod_update_consent_refusals_say_what_to_rerun_with(capsys, edit_client, monkeypatch, step, error, expected):
    monkeypatch.setattr(FakeClient, step, _fake(step, error))
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "--yes", "-o", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and all(text in captured.err for text in expected), captured.err


def test_pod_update_other_errors_exit_1(capsys, edit_client, monkeypatch):
    monkeypatch.setattr(FakeClient, "update_pod", _fake("update_pod", _refusal(400, "Pod Type Locked")))
    assert cli.main(["pod-update", "ws", "p-0", "--gpu", "RTX 4090", "--yes"]) == 1
    assert "[400] Pod Type Locked: Pod Type Locked message" in capsys.readouterr().err


def test_pod_update_flag_errors(capsys, edit_client):
    assert cli.main(["pod-update", "ws", "p-0", "--env", "NOEQUALS", "--yes"]) == 2
    assert "KEY=VALUE" in capsys.readouterr().err
    assert cli.main(["pod-update", "ws", "p-0", "--template-port", "8080:8443", "--yes"]) == 2
    assert "write 8080::8443" in capsys.readouterr().err
    for argv in (["--remove-port", "http"], ["--rental", "hourly"]):
        with pytest.raises(SystemExit) as exit_:
            cli.main(["pod-update", "ws", "p-0", *argv])
        assert exit_.value.code == 2
    assert FakeClient.instances == [] or all(not i.calls for i in FakeClient.instances)


def test_new_commands_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["pod-update", "--help"])
    out = capsys.readouterr().out
    assert all(flag in out for flag in ("--allow-node-migration", "--drop-watched-folder", "--remove-env",
                                        "--remove-port", "--detach", "--template-port", "--estimate"))
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "pod-update" in out and "storage-update" in out and "storage-connect" in out and "--model-set" in out
