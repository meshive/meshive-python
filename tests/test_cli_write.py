"""CLI 쓰기 커맨드 — 인자 파싱 → SDK 호출 인자, 확인(--yes/비대화형), --estimate, 출력 포맷."""
import importlib
import json

import pytest


cli = importlib.import_module("meshive.cli.main")  # cli.__init__ 이 main 함수를 노출해 서브모듈을 가린다
from meshive.models import (Pod, Logs, PodCreated, PodEstimate, ResourceAction, StorageCreated, StorageEstimate,
                            TaskEstimate, TaskSubmitted)

ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"},
            "resources": {"gpu_model": "RTX 3060", "vram_gb": 12, "gpu_count": 1, "vcpu": 4, "ram_gb": 12, "disk_gb": 25,
                          "rental_type": "demand"},
            "availability": {"available_gpus": 2}, "template": {"id": 457, "name": "VSCode"}, "volumes": [], "note": "Estimated"}
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
        return StorageEstimate.from_dict({"pricePerHourUsd": "0.000097", "pricePerGbMonthUsd": "0.07", "sizeGb": a[1],
                                          "storageType": "nfs", "maxSizeGb": 305})

    def create_storage(self, *a, **kw):
        self._rec("create_storage", *a, **kw)
        return StorageCreated.from_dict({"name": a[0], "workspace": kw["workspace"], "transactionId": 5,
                                         "estimate": {"pricePerHourUsd": "0", "pricePerGbMonthUsd": "0", "sizeGb": 1, "storageType": "nfs", "maxSizeGb": 1}})

    def estimate_task(self, *a, **kw):
        self._rec("estimate_task", *a, **kw)
        return TaskEstimate.from_dict({"pricePerHourUsd": None, "maxCostUsd": None, "maxDurationS": 3600, "resources": {}, "note": "CPU"})

    def submit_task(self, *a, **kw):
        self._rec("submit_task", *a, **kw)
        return TaskSubmitted.from_dict({"task": TASK, "estimate": {"maxDurationS": 3600, "resources": {}}})

    def get_pod_logs(self, *a, **kw):
        self._rec("get_pod_logs", *a, **kw)
        return Logs.from_dict({"podName": a[0], "workspace": a[1], "source": "live", "count": 2,
                               "lines": [{"line": "tick 1"}, {"line": "tick 2"}], "truncated": True})

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
    # 콘솔과 같은 값이어야 한다 — $/hr 은 3자리, 견적 내역도 줄마다 3자리(Receipt).
    assert "$0.068/hr" in out and "gpu=$0.068" in out and "RTX 3060" in out
    assert not any(c[0] == "create_pod" for c in FakeClient.instances[-1].calls)


def test_pod_create_requires_yes_when_not_tty(capsys, non_tty):
    assert cli.main(["pod-create", "ws", "p", "--template", "1"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not any(c[0] == "create_pod" for c in FakeClient.instances[-1].calls)


def test_pod_create_with_yes_passes_all_options(capsys):
    argv = ["pod-create", "ws", "agent-pod", "--template", "457", "--gpu", "RTX 3060", "--gpu-count", "2", "--vram", "12",
            "--spot", "--vcpu", "8", "--ram", "24", "--disk", "30", "--volume", "pv-a:/data", "--env", "A=1", "--env", "TOKEN=x",
            "--secret", "TOKEN", "--port", "8888:jupyter", "--port", "6006::internal", "--command", "sleep 1", "--region", "KR",
            "--uptime-premium", "--max-price", "0.5", "--yes", "-o", "name"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == "99"
    name, args, kw = _last("create_pod")
    assert args == ("agent-pod", 457) and kw["workspace"] == "ws"
    assert kw["gpu_model"] == "RTX 3060" and kw["gpu_count"] == 2 and kw["gpu_vram_gb"] == 12 and kw["rental_type"] == "spot"
    assert kw["vcpu"] == 8 and kw["ram_gb"] == 24 and kw["disk_gb"] == 30 and kw["volumes"] == [("pv-a", "/data")]
    assert kw["env"] == {"A": "1", "TOKEN": "x"} and kw["secret_keys"] == ["TOKEN"]
    assert kw["ports"] == [{"port": 8888, "external": True, "name": "jupyter"}, {"port": 6006, "external": False}]
    assert kw["command"] == "sleep 1" and kw["region"] == "KR" and kw["uptime_premium"] is True and kw["max_price_per_hour"] == "0.5"


def test_pod_create_bad_env_is_usage_error(capsys):
    assert cli.main(["pod-create", "ws", "p", "--template", "1", "--env", "NOEQUALS", "--yes"]) == 2
    assert "KEY=VALUE" in capsys.readouterr().err


def test_pod_stop_and_delete(capsys, non_tty):
    assert cli.main(["pod-stop", "ws", "p-0", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "stop"
    assert cli.main(["pod-delete", "ws", "p-0"]) == 2            # 비대화형 + --yes 없음
    assert cli.main(["pod-delete", "ws", "p-0", "--yes", "--delete-local-storage", "pv-1"]) == 0
    name, args, kw = _last("pod_action")
    assert args == ("p-0", "ws") and kw["delete_local_storages"] == ["pv-1"]
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "--allow-data-loss", "-y"]) == 0
    assert _last("pod_action")[2]["placement"] == "any_node"


def test_storage_create_and_estimate(capsys, non_tty):
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--type", "nfs", "--encrypted", "--estimate"]) == 0
    # 스토리지 시간당 단가도 3자리 — 2자리면 "$0.00" 이 돼 콘솔("$0.000")과 갈린다.
    storage_out = capsys.readouterr().out
    assert "$0.000/hr" in storage_out and "per GB·month: $0.07" in storage_out
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--yes", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "5"
    name, args, kw = _last("create_storage")
    assert args == ("vol", 10) and kw["storage_type"] == "nfs" and kw["disk_type"] == "NVMe"


def test_task_submit_from_file(tmp_path, capsys):
    script = tmp_path / "train.py"
    script.write_text("print('hi', flush=True)\n")
    assert cli.main(["task-submit", "ws", "train", "--script", str(script), "--image", "python:3.12-slim",
                     "--cpu-preset", "micro-2c8g", "--arg=--epochs", "--arg", "3", "--input-asset", "asset_a:2",
                     "--max-duration", "7200", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "task_1" in out and "task-logs task_1" in out
    name, args, kw = _last("submit_task")
    assert args == ("train", "print('hi', flush=True)\n") and kw["cpu_preset"] == "micro-2c8g"
    assert kw["args"] == ["--epochs", "3"] and kw["input_assets"] == [{"asset": "asset_a", "version": 2}] and kw["max_duration"] == 7200


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
