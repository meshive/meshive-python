"""CLI 쓰기 커맨드 — 인자 파싱 → SDK 호출 인자, 확인(--yes/비대화형), --estimate, 출력 포맷."""
import copy
import importlib
import io
import json

import pytest


cli = importlib.import_module("meshive.cli.main")  # cli.__init__ 이 main 함수를 노출해 서브모듈을 가린다
from meshive.models import (Pod, Logs, PodCreated, PodEstimate, ResourceAction, Serving, StorageCreated, StorageEstimate,
                            TaskEstimate, TaskSubmitted, ModelDetection, ServingModel, AssetImported, WatchedFolders, SshAccess)

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
        self._rec("list_pods", *a, **kw); return []          # 생성 직후 파드가 아직 없다

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
        self.calls.append(("register_model", a, kw))   # kw 에 name= 이 있어 _rec 의 위치 인자와 겹친다
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
    # 콘솔과 같은 값이어야 한다 — $/hr 은 3자리, 견적 내역도 줄마다 3자리(Receipt).
    assert "$0.068/hr" in out and "gpu=$0.068" in out and "RTX 3060" in out
    # RAM·디스크는 쿠버네티스 Gi 로 잡히는 GiB, VRAM 만 업계 표기 GB.
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
    assert capsys.readouterr().out.strip() == "99"
    name, args, kw = _last("create_pod")
    assert args == ("agent-pod", 457) and kw["workspace"] == "ws"
    assert kw["gpu_model"] == "RTX 3060" and kw["gpu_count"] == 2 and kw["gpu_vram_gb"] == 12 and kw["rental_type"] == "spot"
    assert kw["vcpu"] == 8 and kw["ram_gb"] == 24 and "disk_gb" not in kw and kw["volumes"] == [("pv-a", "/data")]
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
    assert "$0.000/hr" in storage_out and "per GiB·month: $0.07" in storage_out
    assert "10 GiB" in storage_out and "305 GiB" in storage_out     # 크기 = capacity × 1024 MiB
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
    captured = capsys.readouterr()
    assert "task_1" in captured.out and "task-logs task_1" in captured.out
    assert "no longer have versions" in captured.err           # 옛 ASSET_ID:VERSION 은 경고하고 무시
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
    """파드가 시간 안에 안 나타나면 exit 1 — 자동화가 `--wait running` 성공으로 오인하면 안 된다. 수락된 transaction 은 출력."""
    assert cli.main(["pod-create", "ws", "late-pod", "--template", "457", "--yes", "--wait", "running", "--wait-timeout", "0"]) == 1
    captured = capsys.readouterr()
    assert "transaction" in captured.out and "99" in captured.out
    assert "did not appear" in captured.err and "transaction 99" in captured.err
    assert cli.main(["pod-create", "ws", "late-pod", "--template", "457", "--yes", "--wait", "running", "--wait-timeout", "0",
                     "-o", "name"]) == 1
    assert capsys.readouterr().out.strip() == "99"


def test_disk_flag_is_gone(capsys):
    """시스템 디스크는 서버 공식으로 고정된다 — 받는 척하던 --disk 는 없앤다(argparse 가 usage 오류 2 로 끝낸다)."""
    with pytest.raises(SystemExit) as exit_:
        cli.main(["pod-create", "ws", "p", "--template", "1", "--disk", "30", "--yes"])
    assert exit_.value.code == 2 and "--disk" in capsys.readouterr().err


def test_size_options_say_gib_and_vram_stays_gb(capsys):
    """--size·--ram 은 원래부터 GiB 였다(capacity × 1024 MiB, 쿠버네티스 Gi) — 도움말도 GiB 로. VRAM 티어만 GB."""
    for argv, expected in ((["--help"], ["--size GiB"]),
                           (["storage-create", "--help"], ["--size GiB"]),
                           (["pod-create", "--help"], ["--ram GiB", "RAM in GiB", "--vram GB"]),
                           (["task-submit", "--help"], ["--vram GB"])):
        with pytest.raises(SystemExit):
            cli.main(argv)
        out = capsys.readouterr().out
        assert all(text in out for text in expected), (argv, out)


def test_serving_scale_and_resume_confirm_only_when_cost_can_rise(capsys, non_tty):
    # 범위 확대 → 확인 필요(비대화형 + --yes 없음 → 2, 호출 없음)
    assert cli.main(["serving-scale", "42", "--max-replicas", "4"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert all(c[0] != "scale_serving" for c in FakeClient.instances[-1].calls)
    assert cli.main(["serving-scale", "42", "--max-replicas", "4", "--yes"]) == 0
    assert _last("scale_serving")[2]["max_replicas"] == 4
    # 상한 인상·autoscale 켜기도 확인 대상
    assert cli.main(["serving-scale", "42", "--price-cap", "2"]) == 2
    assert cli.main(["serving-scale", "42", "--autoscale"]) == 2
    # 줄이는 변경은 --yes 없이 바로 적용
    assert cli.main(["serving-scale", "42", "--max-replicas", "2", "--price-cap", "1"]) == 0
    assert _last("scale_serving")[2] == {"min_replicas": None, "max_replicas": 2, "autoscale": None, "price_cap_per_hour": "1"}
    # resume 은 과금 재개 → 확인, pause 는 즉시
    assert cli.main(["serving-resume", "42"]) == 2
    assert all(c[0] != "pause_serving" for c in FakeClient.instances[-1].calls)
    assert cli.main(["serving-resume", "42", "-y"]) == 0 and _last("pause_serving")[2]["paused"] is False
    assert cli.main(["serving-pause", "42"]) == 0 and _last("pause_serving")[2]["paused"] is True


def test_storage_estimate_shows_disk_type(capsys):
    assert cli.main(["storage-create", "ws", "vol", "--size", "10", "--disk", "SSD", "--estimate"]) == 0
    assert "nfs / NVMe" in capsys.readouterr().out       # FakeClient 응답에 diskType 이 없어 기본 NVMe
    assert _last("estimate_storage")[2]["disk_type"] == "SSD"


# --estimate 는 pod-create·storage-create·task-submit 공통 — 출력 포맷별 동작을 세 커맨드에 같이 건다.
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
    """`--estimate -o json` 은 서버 견적 payload 하나만 — 사람용 견적 표가 앞에 붙어 jq·json.loads 가 깨졌다
    (dev 실측: storage-create 의 첫 줄들이 `estimate:`·`per GiB·month:`·`size:`, 그 뒤에 JSON)."""
    expected = copy.deepcopy(payload)     # FakeClient 가 같은 dict 를 raw 로 넘기므로 호출 전 사본과 비교한다
    assert cli.main(ESTIMATE_ARGV[command] + flag) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert _created_nothing()


@pytest.mark.parametrize("command, price", [("pod-create", "0.068\n"), ("storage-create", "0.000\n"),
                                            ("task-submit", "")])    # CPU 프리셋은 시간당 단가를 모른다 — 찍을 숫자가 없다
def test_estimate_name_is_just_the_hourly_price(capsys, command, price):
    """`-o name` 은 credit·asset-storage 처럼 헤드라인 숫자 하나 — 표의 `$0.068/hr` 과 같은 숫자를 `$` 없이.
    전에는 사람용 견적 표를 그대로 찍었다."""
    assert cli.main(ESTIMATE_ARGV[command] + ["-o", "name"]) == 0
    assert capsys.readouterr().out == price
    assert _created_nothing()


@pytest.mark.parametrize("price, shown", [("0.5", "0.500\n"), ("1.0005", "1.001\n"), ("1234.5678", "1234.568\n")])
def test_estimate_name_rounds_like_the_table(capsys, monkeypatch, price, shown):
    """단가가 있는 태스크(GPU)도 그 숫자 하나. 반올림은 표와 같은 ROUND_HALF_UP(float 포맷이면 1.0005 → 1.000),
    천 단위 쉼표는 뺀다 — 표는 `$1,234.568/hr`."""
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
    """실제로 만들 때 확인 전에 견적 표를 보여 주는 건 table 모드뿐 — -o json 은 수락 응답 하나만 찍는다."""
    argv = [arg for arg in ESTIMATE_ARGV[command] if arg != "--estimate"] + ["--yes"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("estimate:") and "accepted:" in out
    assert cli.main(argv + ["-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)[key] == value


class _Tty(io.StringIO):
    """사람이 답하는 터미널 — isatty() 가 True, 답은 한 줄씩."""

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
    (["pod-start", "ws", "p-0", "--any-node"], "y\ny\n"),        # 과금 재개 + 데이터 유실 동의, 질문 두 번
    (["serving-scale", "42", "--max-replicas", "4"], "y\n"),
    (["serving-resume", "42"], "y\n"),
])
def test_confirm_question_goes_to_stderr(capsys, monkeypatch, argv, answers):
    """확인 질문은 stderr 로 — 터미널에서 답하면서 stdout 을 파일·파이프로 돌리면(`-o json > out.json`, `| jq`)
    input() 이 질문을 stdout 에 써서 결과 앞에 `Create pod 'p' at $0.068/hr? [y/N] ` 가 붙었고 사람은 질문을 못 봤다."""
    monkeypatch.setattr("sys.stdin", _Tty(answers))
    assert cli.main(argv + ["-o", "json"]) == 0
    captured = capsys.readouterr()
    assert isinstance(json.loads(captured.out), dict)
    assert captured.err.count("[y/N]") == answers.count("\n")


def test_confirm_keeps_name_output_to_the_id(capsys, monkeypatch):
    """`id=$(meshive pod-create … -o name)` — 질문이 $id 로 들어가 명령이 멈춘 것처럼 보였다."""
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "-o", "name"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "99\n" and "[y/N]" in captured.err


@pytest.mark.parametrize("answers, code", [("n\n", 2), ("", 2), (None, 130)])     # 거절, 답 없이 EOF, Ctrl-C
def test_confirm_refused_or_interrupted_changes_nothing(capsys, monkeypatch, answers, code):
    monkeypatch.setattr("sys.stdin", _CtrlC() if answers is None else _Tty(answers))
    assert cli.main(["pod-create", "ws", "p", "--template", "457", "-o", "json"]) == code
    captured = capsys.readouterr()
    assert captured.out == "" and "[y/N]" in captured.err
    assert _created_nothing()


def test_any_node_data_loss_refused_at_the_second_question(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Tty("y\nn\n"))              # 과금 재개엔 예, 데이터 유실엔 아니오
    assert cli.main(["pod-start", "ws", "p-0", "--any-node", "-o", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count("[y/N]") == 2
    assert all(c[0] != "pod_action" for c in FakeClient.instances[-1].calls)


def test_confirm_shows_the_estimate_before_asking_when_stdout_is_a_pipe(monkeypatch):
    """묻기 전에 input() 처럼 stdout 을 비운다 — 파이프(`| tee log`, `2>&1 | tee log`)로 나가는 stdout 은 블록 버퍼라,
    안 비우면 견적 표가 답한 뒤에야 나오거나 질문보다 뒤에 찍힌다."""
    pipe = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(pipe, encoding="utf-8"))
    shown = []

    class Human(_Tty):
        def readline(self, *a):
            shown.append(pipe.getvalue().decode())     # 답하는 순간까지 파이프로 나간 stdout
            return "n\n"

    monkeypatch.setattr("sys.stdin", Human())
    assert cli.main(["pod-create", "ws", "p", "--template", "457"]) == 2
    assert shown and shown[0].startswith("estimate:")


def test_model_commands(capsys, non_tty):
    assert cli.main(["models", "ws"]) == 0
    out = capsys.readouterr().out
    assert "qwen-small" in out and "12" in out and "qwen-small-ab12" in out

    assert cli.main(["model-detect", "ws", "Qwen/Qwen3-0.6B-GGUF", "--hf-token", "4"]) == 1   # 서빙 불가 → exit 1
    out = capsys.readouterr().out
    assert "unsupported" in out and "GGUF is not supported" in out and "Qwen/Qwen3-0.6B" in out
    assert _last("detect_model")[2] == {"workspace": "ws", "hf_token_id": 4}

    # 등록은 비용이 없어 --yes 없이도(비대화형) 진행한다
    assert cli.main(["model-register", "ws", "Qwen/Qwen3-0.6B", "--name", "qwen-small", "--framework", "sglang"]) == 0
    out = capsys.readouterr().out
    assert "Already registered: model #12" in out and "serving-deploy ws 12" in out
    assert _last("register_model")[2] == {"workspace": "ws", "name": "qwen-small", "framework": "sglang",
                                          "hf_token_id": None, "context_length": None}

    assert cli.main(["model-delete", "12"]) == 2                 # 지우기는 확인이 필요하다
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


def test_pod_watch_shows_and_rewrites_with_the_version(capsys, non_tty):
    assert cli.main(["pod-watch", "ws", "p-0"]) == 0
    out = capsys.readouterr().out
    assert "/workspace/outputs" in out and "template" in out and "*.png" in out and "*.txt" in out
    assert not any(c[0] == "set_watched_folders" for c in FakeClient.instances[-1].calls)

    # 늘리는 변경은 확인이 필요하다(수확 저장 과금)
    assert cli.main(["pod-watch", "ws", "p-0", "--add", "/workspace/ckpt", "--include", "*.pt"]) == 2
    assert cli.main(["pod-watch", "ws", "p-0", "--add", "/workspace/ckpt", "--include", "*.pt", "--existing", "--yes"]) == 0
    kw = _last("set_watched_folders")[2]
    assert kw["expected_version"] == 4
    assert kw["template"] == {"/workspace/outputs": {"enabled": True, "include": None}}
    assert kw["user"] == [{"path": "/workspace/logs", "include": ["*.txt"], "enabled": True},
                          {"path": "/workspace/ckpt", "include": ["*.pt"], "include_existing": True}]

    # 줄이는 변경은 바로
    assert cli.main(["pod-watch", "ws", "p-0", "--off", "/workspace/outputs", "--remove", "/workspace/logs"]) == 0
    kw = _last("set_watched_folders")[2]
    assert kw["template"] == {"/workspace/outputs": {"enabled": False, "include": None}} and kw["user"] == []
    assert cli.main(["pod-watch", "ws", "p-0", "--off", "/nope"]) == 2     # 모르는 폴더 → 사용 오류


def test_ssh_prints_the_command_and_password_only(capsys):
    assert cli.main(["ssh", "ws", "p-0"]) == 0
    out = capsys.readouterr().out
    assert "ssh -p 2222 root@m.example" in out and "pw-123" in out and "in " in out   # 만료는 앞으로의 시각
    assert _last("ssh_access")[1] == ("p-0", "ws")
    assert cli.main(["ssh", "ws", "p-0", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "ssh -p 2222 root@m.example"
