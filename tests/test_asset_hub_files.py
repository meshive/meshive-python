"""Asset Hub files and catalog: buckets, registry logins, file views, download links by file id, exports, adding
files to a collection, the open-asset catalog and its import, the pod filter and the ComfyUI layout."""
import asyncio
import inspect
import json

import httpx
import pytest

import meshive
from meshive import (
    AssetStorageSetting,
    BucketListing,
    ComfyLayout,
    ConflictError,
    FileUploadBatch,
    FilesAdded,
    MeshiveAPIError,
    NotFoundError,
    OpenAssetCatalog,
    OpenAssetImport,
    RegistryLogin,
    StorageCredential,
    _client,
    _write,
)
from tests.test_exec_upload import Router
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)
from tests.test_sdk_write import Recorder


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content)


OLD_SERVER = (404, {"detail": "Not Found"})

CREDENTIAL = {"id": 7, "label": "training data", "provider": "r2", "bucket": "team-data", "region": "auto",
              "defaultPrefix": "meshive/", "verifiedAt": "2026-11-01T00:00:00Z", "deletedAt": None,
              "createdAt": "2026-10-01T00:00:00Z"}


# --- buckets -------------------------------------------------------------------------------------------

def test_list_storage_credentials_params_and_parse():
    rec = Recorder((200, [CREDENTIAL, {**CREDENTIAL, "id": 8, "verifiedAt": None, "region": None}]))
    creds = sync_client(rec).list_storage_credentials("team-ns")
    assert rec.last.method == "GET" and rec.last.url.path == "/v1/sdk/storage-credentials"
    assert dict(rec.last.url.params) == {"workspace": "team-ns"}           # removed ones are not asked for
    first, second = creds
    assert isinstance(first, StorageCredential)
    assert (first.credential_id, first.label, first.provider, first.bucket, first.region, first.default_prefix) == (
        7, "training data", "r2", "team-data", "auto", "meshive/")
    assert first.verified and not second.verified and second.region is None
    assert not hasattr(first, "endpoint_url")
    sync_client(rec).list_storage_credentials("team-ns", include_deleted=True)
    assert rec.last.url.params["includeDeleted"] == "true"
    with pytest.raises(ValueError):
        sync_client(rec).list_storage_credentials("team-ns", include_deleted="yes")
    with pytest.raises(NotFoundError, match="does not support bucket storage"):
        sync_client(Recorder(OLD_SERVER)).list_storage_credentials("team-ns")


def test_browse_bucket_params_and_parse():
    listing = {"credentialId": 7, "bucket": "team-data", "prefix": "datasets/", "delimiter": "/",
               "commonPrefixes": ["datasets/imagenet/"], "objects": [
                   {"key": "datasets/readme.md", "sizeBytes": 120, "lastModified": "2026-11-02T00:00:00Z", "ext": "md"}],
               "nextContinuationToken": "tok-2", "isTruncated": True}
    rec = Recorder((200, listing))
    page = sync_client(rec).browse_bucket(7, "team-ns", prefix="datasets/")
    assert rec.last.url.path == "/v1/sdk/storage-credentials/7/objects"
    assert dict(rec.last.url.params) == {"workspace": "team-ns", "prefix": "datasets/"}
    assert isinstance(page, BucketListing)
    assert (page.credential_id, page.bucket, page.prefix, page.folders) == (7, "team-data", "datasets/",
                                                                             ["datasets/imagenet/"])
    [obj] = page.objects
    assert (obj.key, obj.size_bytes, obj.ext) == ("datasets/readme.md", 120, "md") and obj.last_modified is not None
    assert page.next_continuation_token == "tok-2" and page.is_truncated
    sync_client(rec).browse_bucket("7", "team-ns", continuation_token="tok-2")
    assert dict(rec.last.url.params) == {"workspace": "team-ns", "prefix": "", "continuationToken": "tok-2"}
    for bad in ({"credential_id": "x"}, {"credential_id": 7, "prefix": None}):
        with pytest.raises(ValueError):
            sync_client(rec).browse_bucket(bad.pop("credential_id"), "team-ns", **bad)
    with pytest.raises(MeshiveAPIError) as err:
        sync_client(Recorder((400, {"detail": {"title": "Storage Credential Error", "message": "403 from bucket"}}))
                    ).browse_bucket(7, "team-ns")
    assert err.value.title == "Storage Credential Error"
    with pytest.raises(NotFoundError) as missing:          # a titled 404 is the server's answer, not an old server
        sync_client(Recorder((404, {"detail": {"title": "Storage Credential Not Found", "message": "no"}}))
                    ).browse_bucket(9, "team-ns")
    assert missing.value.title == "Storage Credential Not Found"


def test_asset_storage_setting():
    rec = Recorder((200, {"storageCredentialId": 7, "provider": "r2", "label": "training data", "bucket": "team-data"}))
    setting = sync_client(rec).get_asset_storage_setting("team-ns")
    assert rec.last.url.path == "/v1/sdk/workspaces/team-ns/asset-storage" and not rec.last.url.params
    assert isinstance(setting, AssetStorageSetting)
    assert (setting.credential_id, setting.bucket, setting.managed) == (7, "team-data", False)
    managed = sync_client(Recorder((200, {"storageCredentialId": None}))).get_asset_storage_setting("team-ns")
    assert managed.managed and managed.label is None
    assert AssetStorageSetting.from_dict(None).managed
    with pytest.raises(NotFoundError, match="does not support bucket storage"):
        sync_client(Recorder(OLD_SERVER)).get_asset_storage_setting("team-ns")


# --- registry logins -----------------------------------------------------------------------------------

def test_list_registry_logins():
    rec = Recorder((200, [{"id": 3, "userAlias": "ghcr bot", "server": "ghcr.io", "username": "bot"},
                          {"registryLoginId": 4, "label": "hub", "server": "docker.io", "username": "me"}]))
    logins = sync_client(rec).list_registry_logins("team-ns")
    assert rec.last.url.path == "/v1/sdk/registry-logins" and dict(rec.last.url.params) == {"workspace": "team-ns"}
    assert all(isinstance(x, RegistryLogin) for x in logins)
    assert [(x.registry_login_id, x.label, x.server, x.username) for x in logins] == [
        (3, "ghcr bot", "ghcr.io", "bot"), (4, "hub", "docker.io", "me")]
    with pytest.raises(NotFoundError, match="does not support registry logins"):
        sync_client(Recorder(OLD_SERVER)).list_registry_logins("team-ns")


# --- file views ----------------------------------------------------------------------------------------

FILE_ROW = {"fileExternalId": "file_a", "assetExternalId": "asset_out", "assetName": "renders", "assetKind": "collection",
            "relativePath": "frames/0001.png", "sizeBytes": 2048, "fileType": "file", "mediaKind": "image",
            "sourceStatefulsetName": "pod-abc", "sourceUserAlias": "blender box", "sourceTaskRunId": 99,
            "sourceTaskExternalId": None, "sourceAvailable": True, "createdAt": "2026-11-20T00:00:00Z",
            "ingestSource": "harvest", "runFileCount": 1, "runTotalSizeBytes": 2048}


def test_list_asset_files_params_and_parse():
    rec = Recorder((200, {"data": [FILE_ROW], "total": 41, "hasMore": True, "nextCursor": "c2",
                          "catalogAddedTotal": 3}))
    page = sync_client(rec).list_asset_files("team-ns", view="Output", search="frames", pod="pod-abc-0",
                                             task_id="task_1", include_catalog=True, limit=50)
    assert rec.last.url.path == "/v1/sdk/assets/files"
    assert dict(rec.last.url.params) == {"workspace": "team-ns", "view": "output", "search": "frames",
                                         "pod": "pod-abc-0", "taskId": "task_1", "includeCatalogAdded": "true",
                                         "limit": "50"}
    assert (page.total, page.has_more, page.next_cursor, page.catalog_hidden, len(page)) == (41, True, "c2", 3, 1)
    [f] = page
    assert (f.file_id, f.asset_id, f.asset_name, f.asset_kind, f.path, f.size_bytes, f.media_kind) == (
        "file_a", "asset_out", "renders", "collection", "frames/0001.png", 2048, "image")
    assert (f.source_pod, f.source_statefulset, f.source_task_id, f.source_available) == (
        "blender box", "pod-abc", None, True)
    assert f.detected_format is None and f.pickle_flag is None             # sent only by servers that have them
    flagged = sync_client(Recorder((200, {"data": [{**FILE_ROW, "pickleFlag": "suspected", "detectedFormat": "pickle"},
                                                   {**FILE_ROW, "pickleFlag": None, "detectedFormat": None}],
                                          "total": 2, "hasMore": False}))).list_asset_files("team-ns", view="model")
    assert [(r.pickle_flag, r.detected_format) for r in flagged] == [("suspected", "pickle"), (None, None)]
    sync_client(rec).list_asset_files("team-ns", view="model", cursor="c2")
    assert dict(rec.last.url.params) == {"workspace": "team-ns", "view": "model", "cursor": "c2"}
    for bad in ({"view": ""}, {"view": "model", "limit": 0}, {"view": "model", "include_catalog": "1"},
                {"view": "model", "search": " "}):
        with pytest.raises(ValueError):
            sync_client(rec).list_asset_files("team-ns", **bad)
    with pytest.raises(NotFoundError, match="does not support asset file views"):
        sync_client(Recorder(OLD_SERVER)).list_asset_files("team-ns", view="model")
    # An older server routes /assets/files to /assets/{asset_id} with asset_id="files" and answers 'Asset Not Found'.
    old_asset_route = (404, {"detail": {"title": "Asset Not Found", "message": "The asset does not exist."}})
    with pytest.raises(NotFoundError, match="does not support asset file views"):
        sync_client(Recorder(old_asset_route)).list_asset_files("team-ns", view="model")
    with pytest.raises(NotFoundError, match="does not support asset file views"):
        async def run():
            async with async_client(Recorder(old_asset_route)) as client:
                await client.list_asset_files("team-ns", view="model")
        asyncio.run(run())
    with pytest.raises(NotFoundError) as err:          # the new route's own 404 for an unknown pod is kept
        sync_client(Recorder((404, {"detail": {"title": "Pod Not Found", "message": "no"}}))
                    ).list_asset_files("team-ns", view="output", pod="gone-0")
    assert err.value.title == "Pod Not Found"


def test_list_asset_detail_files_params_and_parse():
    rec = Recorder((200, {"items": [{"relativePath": "sub/a.safetensors", "sizeBytes": 10, "status": "ready",
                                     "fileExternalId": "file_1", "fileType": "lora", "mediaKind": "weights"}],
                          "total": 7}))
    files = sync_client(rec).list_asset_detail_files("asset_abc", paths=["sub/*", "*.png"], media_kind="Weights",
                                                     file_type="lora", offset=200, limit=100)
    assert rec.last.url.path == "/v1/sdk/assets/asset_abc/files"
    assert rec.last.url.params.get_list("paths") == ["sub/*", "*.png"]
    assert {k: rec.last.url.params[k] for k in ("mediaKind", "fileType", "offset", "limit")} == {
        "mediaKind": "weights", "fileType": "lora", "offset": "200", "limit": "100"}
    assert files.total == 7 and [(f.path, f.file_id, f.file_type) for f in files] == [
        ("sub/a.safetensors", "file_1", "lora")]
    sync_client(rec).list_asset_detail_files("asset_abc")
    assert dict(rec.last.url.params) == {"offset": "0", "limit": "200"}
    for bad in ({"offset": -1}, {"limit": 0}, {"paths": [""]}):
        with pytest.raises(ValueError):
            sync_client(rec).list_asset_detail_files("asset_abc", **bad)


# --- download links by file id --------------------------------------------------------------------------

def test_asset_file_download_urls():
    rec = Recorder((200, {"expiresIn": 600, "skippedFileCount": 2, "files": [
        {"fileExternalId": "file_a", "assetExternalId": "asset_out", "assetName": "renders", "assetKind": "collection",
         "relativePath": "frames/0001.png", "sizeBytes": 2048, "url": "https://r2.test/a?sig=1",
         "storageProvider": "meshive_r2"}]}))
    links = sync_client(rec).asset_file_download_urls("team-ns", ["file_a", "file_b", "file_c"])
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/assets/files/download-urls"
    assert dict(rec.last.url.params) == {"workspace": "team-ns"} and rec.body() == {"fileIds": ["file_a", "file_b",
                                                                                                 "file_c"]}
    assert (links.expires_in, links.skipped_file_count) == (600, 2)
    [f] = links.files
    assert isinstance(f, meshive.DownloadFile)               # usable wherever a download file is
    assert (f.file_id, f.asset_id, f.asset_name, f.path, f.size_bytes, f.url, f.storage_provider) == (
        "file_a", "asset_out", "renders", "frames/0001.png", 2048, "https://r2.test/a?sig=1", "meshive_r2")
    assert len(_write.file_download_body([f"file_{i}" for i in range(500)])["fileIds"]) == 500
    for bad in ([], [""], [f"file_{i}" for i in range(501)]):
        with pytest.raises(ValueError):
            sync_client(rec).asset_file_download_urls("team-ns", bad)
    with pytest.raises(meshive.RateLimitError) as err:
        sync_client(Recorder((429, {"detail": {"title": "Daily Export Limit Reached", "message": "cap"}}),
                             )).asset_file_download_urls("team-ns", "file_a")
    assert err.value.title == "Daily Export Limit Reached"


# --- exports --------------------------------------------------------------------------------------------

def test_export_asset_and_upload_to_hf():
    rec = Recorder((202, {"assetExternalId": "asset_abc", "name": "imagenet", "status": "exporting",
                          "targetBucket": "team-data", "targetPrefix": "meshive/exports/asset_abc",
                          "message": "Started."}))
    done = sync_client(rec).export_asset("asset_abc", workspace="team-ns", bucket_id=7, idempotency_key="exp-1")
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/assets/asset_abc/export"
    assert dict(rec.last.url.params) == {"workspace": "team-ns"} and rec.body() == {"bucketId": 7}
    assert rec.last.headers["Idempotency-Key"] == "exp-1"
    assert (done.asset_id, done.status, done.target_bucket, done.target_prefix, done.replayed) == (
        "asset_abc", "exporting", "team-data", "meshive/exports/asset_abc", False)
    sync_client(rec).export_asset("asset_abc", workspace="team-ns", bucket_id=7, prefix="backups/")
    assert rec.body() == {"bucketId": 7, "prefix": "backups/"}

    hf = Recorder((202, {"assetExternalId": "asset_abc", "name": "imagenet", "status": "uploading",
                         "hfRepo": "me/model", "repoUrl": "https://huggingface.co/me/model", "pathInRepo": "",
                         "message": "Started.", "licenseNotice": "You are responsible."}))
    up = sync_client(hf).upload_asset_to_hf("asset_abc", workspace="team-ns", hf_repo="me/model", hf_token_id=2)
    assert hf.last.url.path == "/v1/sdk/assets/asset_abc/upload-hf"
    assert hf.body() == {"hfRepo": "me/model", "hfTokenId": 2, "private": True}
    assert (up.status, up.repo_url, up.license_notice) == ("uploading", "https://huggingface.co/me/model",
                                                           "You are responsible.")
    sync_client(hf).upload_asset_to_hf("asset_abc", workspace="team-ns", hf_repo="me/model", hf_token_id=2,
                                       private=False, path_in_repo="weights")
    assert hf.body() == {"hfRepo": "me/model", "hfTokenId": 2, "private": False, "pathInRepo": "weights"}

    for bad in ({"bucket_id": None}, {"bucket_id": True}, {"bucket_id": 7, "prefix": " "}):
        with pytest.raises(ValueError):
            sync_client(rec).export_asset("asset_abc", workspace="team-ns", **bad)
    for bad in ({"hf_repo": "", "hf_token_id": 2}, {"hf_repo": "me/m", "hf_token_id": None},
                {"hf_repo": "me/m", "hf_token_id": 2, "private": "no"}):
        with pytest.raises(ValueError):
            sync_client(hf).upload_asset_to_hf("asset_abc", workspace="team-ns", **bad)
    with pytest.raises(NotFoundError, match="does not support asset export"):
        sync_client(Recorder(OLD_SERVER)).export_asset("asset_abc", workspace="team-ns", bucket_id=7)
    with pytest.raises(NotFoundError, match="does not support Hugging Face upload"):
        sync_client(Recorder(OLD_SERVER)).upload_asset_to_hf("asset_abc", workspace="team-ns", hf_repo="me/m",
                                                             hf_token_id=2)


# --- adding files to a collection ----------------------------------------------------------------------

def _batch(files):
    return {"uploadBatchId": "upb_1", "uploadToken": "tok-secret", "assetExternalId": "asset_col",
            "manifestSha256": "m", "uploadExpiresAt": "2026-11-22T00:00:00Z", "presignExpiresIn": 900, "files": files}


def _added(request):
    body = _json(request)
    return httpx.Response(200, json={"uploadBatchId": "upb_1", "status": "completed", "assetExternalId": "asset_col",
                                     "uploadStatus": "ready", "addedFileCount": len(body["files"]),
                                     "addedSizeBytes": 5, "totalFileCount": 12, "totalSizeBytes": 999,
                                     "contentHash": None, "sent": body,
                                     "files": [{"fileExternalId": f["fileExternalId"], "relativePath": "x"}
                                               for f in body["files"]]})


def test_start_asset_file_upload_hides_the_token_and_links():
    rec = Recorder((201, _batch([{"fileExternalId": "file_n", "relativePath": "new.png", "sizeBytes": 5,
                                  "mode": "single", "url": "https://r2.test/put?sig=secret", "parts": []}])))
    batch = sync_client(rec).start_asset_file_upload("asset_col", [{"relative_path": "new.png", "size_bytes": 5}])
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/assets/asset_col/uploads"
    assert not rec.last.url.params and rec.body() == {"files": [{"relativePath": "new.png", "sizeBytes": 5}]}
    assert isinstance(batch, FileUploadBatch)
    assert (batch.upload_batch_id, batch.upload_token, batch.asset_id, batch.presign_expires_in) == (
        "upb_1", "tok-secret", "asset_col", 900)
    [target] = batch.files
    assert (target.file_id, target.relative_path, target.mode, target.url) == (
        "file_n", "new.png", "single", "https://r2.test/put?sig=secret")
    shown = repr(batch)
    assert "tok-secret" not in shown and "sig=secret" not in shown
    assert "tok-secret" not in json.dumps(batch.raw) and "sig=secret" not in json.dumps(batch.raw)
    with pytest.raises(ValueError):
        sync_client(rec).start_asset_file_upload("asset_col", [])


def test_start_asset_file_upload_errors_and_no_resend():
    collisions = (409, {"detail": {"title": "File Path Already Exists", "message": "exists",
                                   "collisions": ["new.png"]}})
    with pytest.raises(ConflictError) as err:
        sync_client(Recorder(collisions)).start_asset_file_upload("asset_col", [{"path": "new.png", "size_bytes": 5}])
    assert err.value.title == "File Path Already Exists"
    gateway = Recorder((502, {"detail": "bad gateway"}))
    with pytest.raises(MeshiveAPIError):
        sync_client(gateway).start_asset_file_upload("asset_col", [{"path": "new.png", "size_bytes": 5}])
    assert len(gateway.requests) == 1                       # the answer holds a token: never resent blind
    with pytest.raises(NotFoundError, match="does not support adding files"):
        sync_client(Recorder(OLD_SERVER)).start_asset_file_upload("asset_col", [{"path": "a", "size_bytes": 1}])


def test_batch_steps_send_the_token_header():
    rec = Recorder((200, {"presignExpiresIn": 900, "files": []}))
    client = sync_client(rec)
    client.asset_file_upload_part_urls("asset_col", "upb_1", "tok-secret", "file_n", [1, 2])
    assert rec.last.url.path == "/v1/sdk/assets/asset_col/uploads/upb_1/part-urls"
    assert rec.last.headers["X-Asset-Upload-Token"] == "tok-secret"
    assert rec.last.headers["Authorization"] == "Bearer meshive_test"
    assert rec.body() == {"files": [{"fileExternalId": "file_n", "partNumbers": [1, 2]}]}
    client.complete_asset_file_upload("asset_col", "upb_1", "tok-secret")
    assert rec.last.url.path.endswith("/upb_1/complete") and rec.body() == {"files": []}
    client.complete_asset_file_upload("asset_col", "upb_1", "tok-secret",
                                      [{"file_id": "file_n", "sha256": "h", "parts": [{"part_number": 1, "etag": "e"}]}])
    assert rec.body() == {"files": [{"fileExternalId": "file_n", "parts": [{"partNumber": 1, "etag": "e"}],
                                     "sha256": "h"}]}
    client.abort_asset_file_upload("asset_col", "upb_1", "tok-secret")
    assert rec.last.url.path.endswith("/upb_1/abort") and rec.last.headers["X-Asset-Upload-Token"] == "tok-secret"
    for bad in (("..", "upb_1", "t"), ("asset_col", "", "t"), ("asset_col", "upb_1", " ")):
        with pytest.raises(ValueError):
            client.abort_asset_file_upload(*bad)
    with pytest.raises(ValueError):
        client.asset_file_upload_part_urls("asset_col", "upb_1", "t", "file_n", [0])
    with pytest.raises(ConflictError):
        sync_client(Recorder((409, {"detail": {"title": "Upload Already Completed", "message": "done"}}))
                    ).abort_asset_file_upload("asset_col", "upb_1", "t")


def test_add_asset_files_single_and_multipart(tmp_path, monkeypatch):
    monkeypatch.setattr(_client, "UPLOAD_PART_URL_BATCH", 2)
    folder = tmp_path / "more"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"hello")
    (folder / "big.bin").write_bytes(bytes(range(256)) * 10)      # 2560 bytes in parts of 1000
    bodies, puts = {}, []

    def part_urls(request):
        assert request.headers["X-Asset-Upload-Token"] == "tok-secret"
        numbers = _json(request)["files"][0]["partNumbers"]
        return httpx.Response(200, json={"presignExpiresIn": 900, "files": [
            {"fileExternalId": "file_big", "mode": "multipart",
             "parts": [{"partNumber": n, "url": f"https://r2.test/p{n}"} for n in numbers]}]})

    def put(request):
        puts.append(request)
        if request.url.path.startswith("/p"):
            bodies[int(request.url.path[2:])] = request.content
        return httpx.Response(200, headers={"ETag": f'"etag-{request.url.path}"'})
    router = Router({
        ("POST", "api.test", "/v1/sdk/assets/asset_col/uploads/upb_1/part-urls"): part_urls,
        ("POST", "api.test", "/v1/sdk/assets/asset_col/uploads/upb_1/complete"): _added,
        ("POST", "api.test", "/v1/sdk/assets/asset_col/uploads"): (201, _batch([
            {"fileExternalId": "file_a", "relativePath": "a.txt", "sizeBytes": 5, "mode": "single",
             "url": "https://r2.test/single"},
            {"fileExternalId": "file_big", "relativePath": "big.bin", "sizeBytes": 2560, "mode": "multipart",
             "partSizeBytes": 1000, "partCount": 3}])),
        ("PUT", "r2.test", "/"): put,
    })
    added = sync_client(router).add_asset_files("asset_col", folder)
    assert isinstance(added, FilesAdded)
    assert (added.asset_id, added.added_file_count, added.total_file_count, added.total_size_bytes) == (
        "asset_col", 2, 12, 999)
    start = _json(router.requests[0])
    assert [f["relativePath"] for f in start["files"]] == ["a.txt", "big.bin"]
    assert b"".join(bodies[n] for n in (1, 2, 3)) == (folder / "big.bin").read_bytes()
    assert all("authorization" not in {k.lower() for k in r.headers.keys()} for r in puts)
    assert all("x-asset-upload-token" not in {k.lower() for k in r.headers.keys()} for r in puts)
    sent = added.raw["sent"]["files"]
    assert sent[0]["fileExternalId"] == "file_a" and sent[0]["parts"] == []
    assert [p["partNumber"] for p in sent[1]["parts"]] == [1, 2, 3]
    batches = [_json(r)["files"][0]["partNumbers"] for r in router.requests if r.url.path.endswith("/part-urls")]
    assert batches == [[1, 2], [3]]


def test_add_asset_files_aborts_on_failure(tmp_path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    router = Router({
        ("POST", "api.test", "/v1/sdk/assets/asset_col/uploads/upb_1/abort"): (200, {"uploadBatchId": "upb_1",
                                                                                     "status": "aborted"}),
        ("POST", "api.test", "/v1/sdk/assets/asset_col/uploads"): (201, _batch([
            {"fileExternalId": "file_a", "relativePath": "a.txt", "sizeBytes": 5, "mode": "single",
             "url": "https://r2.test/obj"}])),
        ("PUT", "r2.test", "/obj"): (500, {}),
    })
    with pytest.raises(MeshiveAPIError, match="Uploading a.txt failed"):
        sync_client(router).add_asset_files("asset_col", local)
    assert router.requests[-1].url.path.endswith("/upb_1/abort")
    assert router.requests[-1].headers["X-Asset-Upload-Token"] == "tok-secret"


# --- the open-asset catalog -----------------------------------------------------------------------------

CATALOG = {
    "categories": ["checkpoints", "loras"],
    "entries": [{"slug": "sdxl-base", "name": "SDXL base", "category": "checkpoints", "family": "sdxl",
                 "description": "base", "source": "hf", "sizeBytes": 6_900_000_000, "fileCount": 1,
                 "defaultRole": "checkpoints", "license": "openrail++", "licenseUrl": "https://x", "hfGated": None,
                 "existingAssetId": "asset_sdxl"},
                {"slug": "detail-lora", "name": "Detail", "category": "loras", "sourceMark": "civitai",
                 "existingAssetExternalId": None}],
    "modelSets": [{"slug": "flux-dev", "name": "FLUX dev", "hfTokenRequired": True,
                   "items": [{"entrySlug": "flux1-dev", "name": "FLUX.1 dev", "category": "diffusion_models"}]}],
}


def test_list_open_assets():
    rec = Recorder((200, CATALOG))
    catalog = sync_client(rec).list_open_assets("team-ns")
    assert rec.last.url.path == "/v1/sdk/open-assets" and dict(rec.last.url.params) == {"workspace": "team-ns"}
    assert isinstance(catalog, OpenAssetCatalog) and catalog.categories == ["checkpoints", "loras"]
    sdxl, lora = catalog.entries
    assert (sdxl.slug, sdxl.source, sdxl.size_bytes, sdxl.default_role, sdxl.existing_asset_id) == (
        "sdxl-base", "hf", 6_900_000_000, "checkpoints", "asset_sdxl")
    assert (lora.source, lora.existing_asset_id, lora.size_bytes) == ("civitai", None, 0)
    [flux] = catalog.model_sets
    assert isinstance(flux, meshive.ModelSet) and flux.hf_token_required and flux.items[0].entry_slug == "flux1-dev"
    with pytest.raises(NotFoundError, match="does not support the open-asset catalog"):
        sync_client(Recorder(OLD_SERVER)).list_open_assets("team-ns")


def test_import_open_assets():
    rec = Recorder((200, {"results": [
        {"slug": "sdxl-base", "status": "reused", "defaultRole": "checkpoints", "assetExternalId": "asset_sdxl",
         "name": "SDXL base", "assetType": "checkpoint"},
        {"slug": "flux1-dev", "status": "failed", "error": "gated", "errorTitle": "Hugging Face Token Required",
         "defaultRole": "diffusion_models"}]}))
    done = sync_client(rec).import_open_assets("team-ns", entries=["sdxl-base", "flux1-dev"], hf_token_id=2,
                                               idempotency_key="cat-1")
    assert rec.last.method == "POST" and rec.last.url.path == "/v1/sdk/open-assets/materialize"
    assert dict(rec.last.url.params) == {"workspace": "team-ns"} and rec.last.headers["Idempotency-Key"] == "cat-1"
    assert rec.body() == {"entrySlugs": ["sdxl-base", "flux1-dev"], "hfTokenId": 2}
    assert isinstance(done, OpenAssetImport)
    assert [(r.slug, r.asset_id) for r in done.imported] == [("sdxl-base", "asset_sdxl")]
    [failed] = done.failed
    assert (failed.error_title, failed.default_role, failed.asset_id) == ("Hugging Face Token Required",
                                                                          "diffusion_models", None)
    sync_client(rec).import_open_assets("team-ns", model_set="flux-dev")
    assert rec.body() == {"modelSet": "flux-dev"}
    for bad in ({}, {"entries": ["a"], "model_set": "s"}, {"entries": []}, {"model_set": " "},
                {"entries": "a", "hf_token_id": "2"}):
        with pytest.raises(ValueError):
            sync_client(rec).import_open_assets("team-ns", **bad)
    with pytest.raises(NotFoundError, match="does not support catalog import"):
        sync_client(Recorder(OLD_SERVER)).import_open_assets("team-ns", model_set="flux-dev")


# --- pod filter and ComfyUI layout ---------------------------------------------------------------------

def test_list_assets_pod_filter():
    rec = Recorder((200, {"items": [], "total": 0, "page": 1, "pageSize": 20}))
    sync_client(rec).list_assets("team-ns", pod="blender-0")
    assert rec.last.url.params["pod"] == "blender-0"
    sync_client(rec).list_assets("team-ns")
    assert "pod" not in rec.last.url.params
    with pytest.raises(ValueError):
        sync_client(rec).list_assets("team-ns", pod=" ")
    with pytest.raises(NotFoundError) as err:
        sync_client(Recorder((404, {"detail": {"title": "Pod Not Found", "message": "no"}}))
                    ).list_assets("team-ns", pod="gone-0")
    assert err.value.title == "Pod Not Found"


def test_get_comfy_layout():
    rec = Recorder((200, {"enabled": True, "reason": None, "groups": [
        {"path": "checkpoints/", "role": "checkpoints", "fileCount": 2, "sizeBytes": 100}]}))
    layout = sync_client(rec).get_comfy_layout("asset_abc")
    assert rec.last.url.path == "/v1/sdk/assets/asset_abc/comfy-layout" and not rec.last.url.params
    assert isinstance(layout, ComfyLayout) and layout.enabled
    assert [(g.path, g.role, g.file_count, g.size_bytes) for g in layout.groups] == [("checkpoints/", "checkpoints",
                                                                                       2, 100)]
    off = sync_client(Recorder((200, {"enabled": False, "reason": "no_layout"}))).get_comfy_layout("asset_abc")
    assert (off.enabled, off.reason, off.groups) == (False, "no_layout", [])
    with pytest.raises(NotFoundError, match="does not support ComfyUI layouts"):
        sync_client(Recorder(OLD_SERVER)).get_comfy_layout("asset_abc")


# --- parity and exports --------------------------------------------------------------------------------

NEW_METHODS = ("list_storage_credentials", "browse_bucket", "get_asset_storage_setting", "list_registry_logins",
               "list_asset_files", "list_asset_detail_files", "asset_file_download_urls", "export_asset",
               "upload_asset_to_hf", "start_asset_file_upload", "asset_file_upload_part_urls",
               "complete_asset_file_upload", "abort_asset_file_upload", "list_open_assets", "import_open_assets",
               "get_comfy_layout", "list_assets")
NEW_MODELS = ("StorageCredential", "AssetStorageSetting", "BucketObject", "BucketListing", "WorkspaceFile",
              "AssetFilePage", "AssetFileList", "FileDownloadItem", "FileDownload", "AssetExport", "AssetHfUpload",
              "BatchUploadTarget", "FileUploadBatch", "AddedFile", "FilesAdded", "OpenAssetEntry", "OpenAssetCatalog",
              "OpenAssetImportRow", "OpenAssetImport", "ComfyLayoutGroup", "ComfyLayout")


def test_async_mirrors_sync_and_exports():
    for name in NEW_METHODS:
        assert inspect.signature(getattr(meshive.Meshive, name)) == \
            inspect.signature(getattr(meshive.AsyncMeshive, name)), name
        assert inspect.iscoroutinefunction(getattr(meshive.AsyncMeshive, name)), name
    for name in NEW_MODELS:
        assert name in meshive.__all__ and getattr(meshive, name).__name__ == name


def test_async_calls():
    rec = Recorder((200, [CREDENTIAL]), (200, CATALOG),
                   (201, _batch([{"fileExternalId": "file_n", "relativePath": "n.png", "sizeBytes": 1,
                                  "mode": "single", "url": "https://r2.test/u"}])),
                   (200, {"uploadBatchId": "upb_1", "assetExternalId": "asset_col", "addedFileCount": 1,
                          "addedSizeBytes": 1, "totalFileCount": 2, "totalSizeBytes": 3, "files": []}))

    async def run():
        async with async_client(rec) as client:
            creds = await client.list_storage_credentials("team-ns")
            catalog = await client.list_open_assets("team-ns")
            batch = await client.start_asset_file_upload("asset_col", [{"path": "n.png", "size_bytes": 1}])
            added = await client.complete_asset_file_upload("asset_col", batch.upload_batch_id, batch.upload_token)
            return creds, catalog, batch, added
    creds, catalog, batch, added = asyncio.run(run())
    assert creds[0].credential_id == 7 and catalog.entries[0].slug == "sdxl-base"
    assert batch.upload_token == "tok-secret" and added.total_file_count == 2
    assert rec.last.headers["X-Asset-Upload-Token"] == "tok-secret"
