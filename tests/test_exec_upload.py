"""Commands in a pod (run_command, get_command_run) and uploads to Asset Hub (upload_asset and its steps), SDK and CLI."""
import asyncio
import importlib
import json
import os

import httpx
import pytest

from meshive import CommandResult, CommandRun, MeshiveAPIError, NotFoundError, UploadedAsset
from meshive import _client
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)

cli = importlib.import_module("meshive.cli.main")

RUN = {"podName": "p-0", "workspace": "ws", "mode": "run", "exitCode": 3, "stdout": "out\n", "stderr": "err\n",
       "truncated": False, "timedOut": False, "durationMs": 41}


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content)


class Router:
    """MockTransport handler: answers by (method, host, path) and records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for (method, host, path), answer in self.routes.items():
            if request.method == method and request.url.host == host and request.url.path.startswith(path):
                return answer(request) if callable(answer) else httpx.Response(answer[0], json=answer[1])
        raise AssertionError(f"unexpected {request.method} {request.url}")


# --- run_command / get_command_run --------------------------------------------------------------

def test_run_command_request_and_result():
    router = Router({("POST", "api.test", "/v1/sdk/pods/p-0/exec"): (200, RUN)})
    result = sync_client(router).run_command("p-0", "ws", "make test", timeout=7)
    request = router.requests[0]
    assert request.url.params["workspace"] == "ws"
    assert _json(request) == {"command": "make test", "timeout": 7, "background": False}
    assert isinstance(result, CommandResult)
    assert (result.exit_code, result.stdout, result.stderr, result.duration_ms) == (3, "out\n", "err\n", 41)


@pytest.mark.parametrize("status", [502, 504])
def test_run_command_is_not_resent_when_it_may_have_run(status):
    router = Router({("POST", "api.test", "/v1/sdk/pods/p-0/exec"): (status, {"detail": {"title": "x",
                                                                                       "message": "may still run"}})})
    with pytest.raises(MeshiveAPIError):
        sync_client(router).run_command("p-0", "ws", "rm -rf /tmp/x")
    assert len(router.requests) == 1


def test_run_command_is_resent_when_the_server_was_busy():
    answers = [httpx.Response(503, json={"detail": {"title": "Busy", "message": "busy"}}), httpx.Response(200, json=RUN)]
    router = Router({("POST", "api.test", "/v1/sdk/pods/p-0/exec"): lambda r: answers.pop(0)})
    assert sync_client(router).run_command("p-0", "ws", "id").exit_code == 3
    assert len(router.requests) == 2


@pytest.mark.parametrize("kwargs", [{"command": ""}, {"command": "id", "timeout": 26}, {"command": "id", "timeout": 0},
                                    {"command": "id", "timeout": True}])
def test_run_command_checks_arguments_before_sending(kwargs):
    router = Router({})
    with pytest.raises(ValueError):
        sync_client(router).run_command("p-0", "ws", **kwargs)
    assert router.requests == []


def test_background_and_status():
    router = Router({
        ("POST", "api.test", "/v1/sdk/pods/p-0/exec"): (200, {"podName": "p-0", "workspace": "ws", "mode": "background",
                                                          "runId": "run_0123456789ab",
                                                          "logPath": "/tmp/.meshive-runs/run_0123456789ab.log"}),
        ("GET", "api.test", "/v1/sdk/pods/p-0/exec/run_0123456789ab"): (200, {
            "podName": "p-0", "workspace": "ws", "runId": "run_0123456789ab", "state": "exited", "exitCode": 0,
            "output": "done", "truncated": False, "logBytes": 5, "logPath": "/tmp/.meshive-runs/run_0123456789ab.log"}),
    })
    client = sync_client(router)
    started = client.run_command("p-0", "ws", "blender -b s.blend -a", background=True)
    assert started.mode == "background" and started.run_id == "run_0123456789ab" and started.exit_code is None
    run = client.get_command_run("p-0", "ws", started.run_id, tail=20)
    assert isinstance(run, CommandRun) and run.finished and run.output == "done"
    assert router.requests[1].url.params["tail"] == "20"
    with pytest.raises(ValueError):
        client.get_command_run("p-0", "ws", "../../etc/passwd")


def test_older_server_without_exec_says_so():
    router = Router({("POST", "api.test", "/v1/sdk/pods/p-0/exec"): (404, {"detail": "Not Found"})})
    with pytest.raises(NotFoundError, match="does not support running commands"):
        sync_client(router).run_command("p-0", "ws", "id")


def test_async_run_command():
    router = Router({("POST", "api.test", "/v1/sdk/pods/p-0/exec"): (200, RUN)})

    async def go():
        client = async_client(router)
        return await client.run_command("p-0", "ws", "id")
    assert asyncio.run(go()).exit_code == 3


# --- upload_asset ---------------------------------------------------------------------------------

def _session(files):
    return {"uploadId": "asset_abc", "assetExternalId": "asset_abc", "name": "bundle", "presignExpiresIn": 900,
            "uploadExpiresAt": "2026-11-14T00:00:00Z", "files": files}


def _done(request):
    body = _json(request)
    return httpx.Response(200, json={"assetExternalId": "asset_abc", "name": "bundle", "status": "ready",
                                     "totalSizeBytes": 0, "fileCount": len(body["files"]), "contentHash": "h",
                                     "sent": body})


def test_upload_single_file_puts_to_storage_without_the_api_key(tmp_path):
    local = tmp_path / "film_bundle_v5.tgz"
    local.write_bytes(b"x" * 3000)
    puts = []

    def put(request):
        puts.append(request)
        return httpx.Response(200, headers={"ETag": '"e1"'})
    router = Router({
        ("POST", "api.test", "/v1/sdk/assets/uploads/asset_abc/complete"): _done,
        ("POST", "api.test", "/v1/sdk/assets/uploads"): (201, _session([
            {"fileId": 11, "relativePath": "film_bundle_v5.tgz", "mode": "single", "url": "https://r2.test/obj?sig=1"}])),
        ("PUT", "r2.test", "/obj"): put,
    })
    asset = sync_client(router).upload_asset(local, workspace="ws")
    assert isinstance(asset, UploadedAsset) and asset.asset_id == "asset_abc" and asset.status == "ready"
    start = _json(router.requests[0])
    assert router.requests[0].url.params["workspace"] == "ws"
    assert start["name"] == "film_bundle_v5.tgz" and start["kind"] == "file"
    assert start["files"][0]["relativePath"] == "film_bundle_v5.tgz" and start["files"][0]["sizeBytes"] == 3000
    [request] = puts
    assert request.content == b"x" * 3000 and request.headers["Content-Length"] == "3000"
    assert "authorization" not in {k.lower() for k in request.headers.keys()}
    sent = asset.raw["sent"]["files"][0]
    assert sent["fileId"] == 11 and sent["sha256"] == start["files"][0]["sha256"] and sent["parts"] == []


def test_upload_multipart_file_in_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(_client, "UPLOAD_PART_URL_BATCH", 2)
    local = tmp_path / "big.blend"
    local.write_bytes(bytes(range(256)) * 10)            # 2560 bytes, parts of 1000 → 3 parts
    bodies = {}

    def part_urls(request):
        numbers = _json(request)["files"][0]["partNumbers"]
        return httpx.Response(200, json={"presignExpiresIn": 900, "files": [
            {"fileId": 5, "mode": "multipart", "parts": [{"partNumber": n, "url": f"https://r2.test/p{n}"}
                                                         for n in numbers]}]})

    def put(request):
        number = int(request.url.path[2:])
        bodies[number] = request.content
        return httpx.Response(200, headers={"ETag": f'"etag-{number}"'})
    router = Router({
        ("POST", "api.test", "/v1/sdk/assets/uploads/asset_abc/part-urls"): part_urls,
        ("POST", "api.test", "/v1/sdk/assets/uploads/asset_abc/complete"): _done,
        ("POST", "api.test", "/v1/sdk/assets/uploads"): (201, _session([
            {"fileId": 5, "relativePath": "big.blend", "mode": "multipart", "partSizeBytes": 1000, "partCount": 3}])),
        ("PUT", "r2.test", "/p"): put,
    })
    asset = sync_client(router).upload_asset(local, workspace="ws")
    assert b"".join(bodies[n] for n in (1, 2, 3)) == local.read_bytes()
    assert [len(bodies[n]) for n in (1, 2, 3)] == [1000, 1000, 560]
    batches = [_json(r)["files"][0]["partNumbers"] for r in router.requests if r.url.path.endswith("/part-urls")]
    assert batches == [[1, 2], [3]]                      # URLs asked for a batch at a time
    assert asset.raw["sent"]["files"][0]["parts"] == [{"partNumber": n, "etag": f'"etag-{n}"'} for n in (1, 2, 3)]


def test_expired_single_link_is_replaced_once(tmp_path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    answers = [httpx.Response(403), httpx.Response(200, headers={"ETag": '"e"'})]
    router = Router({
        ("POST", "api.test", "/v1/sdk/assets/uploads/asset_abc/part-urls"): (200, {"presignExpiresIn": 900, "files": [
            {"fileId": 1, "mode": "single", "url": "https://r2.test/fresh", "parts": []}]}),
        ("POST", "api.test", "/v1/sdk/assets/uploads/asset_abc/complete"): _done,
        ("POST", "api.test", "/v1/sdk/assets/uploads"): (201, _session([
            {"fileId": 1, "relativePath": "a.txt", "mode": "single", "url": "https://r2.test/old"}])),
        ("PUT", "r2.test", "/"): lambda r: answers.pop(0),
    })
    assert sync_client(router).upload_asset(local, workspace="ws").status == "ready"
    puts = [r.url.path for r in router.requests if r.method == "PUT"]
    assert puts == ["/old", "/fresh"]
    assert _json([r for r in router.requests if r.url.path.endswith("/part-urls")][0])["files"][0]["partNumbers"] == []


def test_start_upload_is_not_resent_after_a_gateway_error(tmp_path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    router = Router({("POST", "api.test", "/v1/sdk/assets/uploads"): (502, {"detail": "bad gateway"})})
    with pytest.raises(MeshiveAPIError):
        sync_client(router).upload_asset(local, workspace="ws")
    assert len(router.requests) == 1


def test_failed_put_aborts_the_upload(tmp_path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    router = Router({
        ("POST", "api.test", "/v1/sdk/assets/uploads/asset_abc/abort"): (200, {"status": "failed"}),
        ("POST", "api.test", "/v1/sdk/assets/uploads"): (201, _session([
            {"fileId": 1, "relativePath": "a.txt", "mode": "single", "url": "https://r2.test/obj"}])),
        ("PUT", "r2.test", "/obj"): (403, {}),
    })
    with pytest.raises(MeshiveAPIError, match="Uploading a.txt failed"):
        sync_client(router).upload_asset(local, workspace="ws")
    assert router.requests[-1].url.path.endswith("/abort")


def test_folder_upload_keeps_relative_paths_and_skips_links(tmp_path):
    folder = tmp_path / "bundle"
    (folder / "shots").mkdir(parents=True)
    (folder / "render_seq.py").write_text("print(1)")
    (folder / "shots" / "s4.blend").write_bytes(b"blend")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    try:
        os.symlink(outside, folder / "link.txt")
    except (OSError, NotImplementedError):
        pass                                             # Windows without the privilege: nothing to skip
    (folder / "shots" / ".gitkeep").write_bytes(b"")
    plan, kind, name, empty = _client._upload_plan(folder)
    assert kind == "collection" and name == "bundle" and empty == 1
    assert [rel for rel, _, _ in plan] == ["render_seq.py", "shots/s4.blend"]


def test_upload_plan_refuses_missing_and_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        _client._upload_plan(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        _client._upload_plan(tmp_path / "empty")
    (tmp_path / "zero.bin").write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        _client._upload_plan(tmp_path / "zero.bin")


def test_start_asset_upload_low_level():
    router = Router({("POST", "api.test", "/v1/sdk/assets/uploads"): (201, _session([
        {"fileId": 1, "relativePath": "a.bin", "mode": "single", "url": "https://r2.test/a"}]))})

    async def go():
        return await async_client(router).start_asset_upload(
            "ws", [{"relative_path": "a.bin", "size_bytes": 4, "sha256": "f" * 64}], name="n", asset_type="file")
    session = asyncio.run(go())
    assert session.upload_id == "asset_abc" and session.files[0].url == "https://r2.test/a"
    assert "https://r2.test/a" not in repr(session.files[0])
    assert _json(router.requests[0]) == {"files": [{"relativePath": "a.bin", "sizeBytes": 4, "sha256": "f" * 64}],
                                         "name": "n", "assetType": "file"}


# --- CLI ----------------------------------------------------------------------------------------------

class FakeCli:
    last = None

    def __init__(self, *a, **kw):
        FakeCli.last = self
        self.calls = []

    def run_command(self, pod, ws, command, *, timeout, background):
        self.calls.append(("run_command", pod, ws, command, timeout, background))
        if background:
            return CommandResult.from_dict({"podName": pod, "workspace": ws, "mode": "background",
                                            "runId": "run_0123456789ab", "logPath": "/tmp/x.log"})
        return CommandResult.from_dict({**RUN, "timedOut": command == "sleep 99"})

    def get_command_run(self, pod, ws, run_id, *, tail):
        return CommandRun.from_dict({"runId": run_id, "state": "exited", "exitCode": 4, "output": "bye"})

    def upload_asset(self, path, *, workspace, name, asset_type):
        self.calls.append(("upload_asset", path, workspace, name, asset_type))
        return UploadedAsset.from_dict({"assetExternalId": "asset_abc", "name": "bundle", "status": "ready",
                                        "totalSizeBytes": 2048, "fileCount": 1})

    def close(self):
        pass


@pytest.fixture
def fake_cli(monkeypatch):
    monkeypatch.setattr(cli, "Meshive", FakeCli)
    return FakeCli


def test_cli_pod_exec_passes_output_through_and_exits_with_its_code(fake_cli, capsys):
    assert cli.main(["pod-exec", "ws", "p-0", "--max-seconds", "9", "--", "ls", "/work/my dir"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "out\n" and captured.err == "err\n"
    assert fake_cli.last.calls[0] == ("run_command", "p-0", "ws", "ls '/work/my dir'", 9, False)


@pytest.mark.parametrize("option", [["-o", "json"], ["--json"], ["--base-url", "https://example.invalid"],
                                    ["--timeout", "5"]])
def test_cli_pod_exec_refuses_global_options_after_the_pod(fake_cli, capsys, option):
    assert cli.main(["pod-exec", "ws", "p-0", *option, "--", "echo", "hi"]) == 2
    assert "before the workspace" in capsys.readouterr().err
    assert fake_cli.last.calls == []                     # nothing was sent


def test_cli_pod_exec_takes_global_options_before_the_workspace(fake_cli, capsys):
    assert cli.main(["pod-exec", "-o", "json", "ws", "p-0", "--", "echo", "hi"]) == 3
    assert json.loads(capsys.readouterr().out)["exitCode"] == 3
    assert fake_cli.last.calls[0][3] == "echo hi"


def test_cli_pod_exec_command_without_separator_may_start_with_a_dash(fake_cli, capsys):
    # No `--`: everything after the pod name is the command, as written.
    assert cli.main(["pod-exec", "ws", "p-0", "ls", "-la"]) == 3
    assert fake_cli.last.calls[0][3] == "ls -la"


def test_cli_pod_exec_timeout_exits_124(fake_cli, capsys):
    assert cli.main(["pod-exec", "ws", "p-0", "--", "sleep 99"]) == 124
    assert "--background" in capsys.readouterr().err


def test_cli_pod_exec_background_and_status(fake_cli, capsys):
    assert cli.main(["pod-exec", "ws", "p-0", "--background", "--", "blender -b s.blend -a"]) == 0
    assert "run_0123456789ab" in capsys.readouterr().out
    assert fake_cli.last.calls[0][-1] is True
    assert cli.main(["pod-exec-status", "ws", "p-0", "run_0123456789ab"]) == 4
    assert "bye" in capsys.readouterr().out


def test_cli_asset_upload(fake_cli, capsys, tmp_path):
    assert cli.main(["asset-upload", "ws", str(tmp_path), "--name", "bundle", "-o", "name"]) == 0
    assert capsys.readouterr().out.strip() == "asset_abc"
    assert fake_cli.last.calls[0] == ("upload_asset", str(tmp_path), "ws", "bundle", None)
