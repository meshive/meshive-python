"""Reference assets (browse, preview and copy some files), assets registered from a connected bucket, what uses a saved
credential and deleting it, and the workspace's default output bucket."""
import asyncio
import inspect
import json

import httpx
import pytest

import meshive
from meshive import (
    AssetStorageSetting,
    AssetSubsetCopy,
    BucketAssetRegistered,
    BucketListing,
    CredentialUsage,
    MeshiveAPIError,
    NotFoundError,
    RegistryLoginUsage,
    ResourceAction,
    StorageCredentialDeleteCheck,
    SubsetPreview,
)
from tests.test_port_locks import Api
from tests.test_sdk import async_client, isolate_config_dir, slept, sync_client  # noqa: F401  (autouse fixtures)

SDK = "/v1/sdk"
OLD_SERVER = (404, {"detail": "Not Found"})

NEW_METHODS = ("browse_reference_objects", "register_bucket_objects", "register_bucket_prefix",
               "preview_reference_subset", "fetch_reference_subset", "get_hf_token_usage", "get_civitai_key_usage",
               "get_registry_login_usage", "delete_hf_token", "delete_civitai_key", "delete_registry_login",
               "check_storage_credential_delete", "delete_storage_credential", "set_asset_storage_setting")
NEW_NAMES = ("BucketAssetRegistered", "SubsetPreview", "AssetSubsetCopy", "CredentialUsage", "RegistryLoginTemplate",
             "RegistryLoginUsage", "StorageCredentialDeleteCheck")

LISTING = {"assetExternalId": "asset_ref", "bucket": "my-bucket", "prefix": "datasets/cats/", "subPrefix": "train",
           "commonPrefixes": ["train/a"],
           "objects": [{"key": "datasets/cats/train/1.jpg", "relativePath": "train/1.jpg", "sizeBytes": 2048,
                        "ext": "jpg"}],
           "nextContinuationToken": "tok-2", "isTruncated": True}
REGISTERED = {"assetExternalId": "asset_new", "name": "cats", "fileCount": 2, "totalSizeBytes": 4096,
              "status": "ready", "storageProvider": "user_s3"}
PREFIX_REGISTERED = {"assetExternalId": "asset_ref", "name": "cats", "status": "ready", "storageProvider": "user_s3",
                     "referenceMode": "prefix", "prefix": "datasets/cats/", "objectCount": 0, "totalSizeBytes": 0,
                     "sizeMeasured": False, "sizeNotice": "Too many objects to count."}
PREVIEW = {"assetExternalId": "asset_ref", "prefix": "datasets/cats/", "matchedCount": 3, "totalSizeBytes": 6144,
           "scannedCount": 10, "truncated": False, "sampleRelativePaths": ["train/1.jpg"], "fetchable": True,
           "limitNotice": None, "maxObjects": 10000, "maxTotalSizeBytes": 1 << 40}
FETCHED = {"assetExternalId": "asset_copy", "name": "cats-subset", "status": "uploading", "ingestSource": "s3_fetch",
           "fileCount": 3, "totalSizeBytes": 6144, "sourceAssetExternalId": "asset_ref"}
USAGE = [{"assetExternalId": "asset_a", "name": "llama", "activePodCount": 2}]
REGISTRY_USAGE = {"registryLoginId": 4, "templates": [{"templateId": 51, "name": "private-image"}],
                  "activePodCount": 1, "activeTaskCount": 0}
DELETE_CHECK = {"credentialId": 9, "label": "prod", "bucket": "my-bucket", "assetCount": 2, "fileCount": 40,
                "totalTrackedBytes": 1 << 30, "isWorkspaceDefault": True,
                "blockers": {"runningPods": [{"podName": "p-0"}], "uploadsInProgress": 0, "runningTasks": []},
                "stoppedPodsReferencing": []}


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


# --- Reference assets -----------------------------------------------------------------------------------

def test_browse_reference_objects():
    path = f"{SDK}/assets/asset_ref/reference/objects"
    api = Api({("GET", path): (200, LISTING)})
    listing = sync_client(api).browse_reference_objects("asset_ref", sub_prefix="train", continuation_token="tok-1")
    params = api.sent("GET", path)[0].url.params
    assert (params["subPrefix"], params["continuationToken"]) == ("train", "tok-1")
    assert isinstance(listing, BucketListing)
    assert (listing.asset_id, listing.prefix, listing.sub_prefix, listing.folders) == \
        ("asset_ref", "datasets/cats/", "train", ["train/a"])
    obj = listing.objects[0]
    assert (obj.key, obj.relative_path, obj.size_bytes, obj.ext) == ("datasets/cats/train/1.jpg", "train/1.jpg", 2048,
                                                                     "jpg")
    assert listing.next_continuation_token == "tok-2" and listing.is_truncated


def test_browse_reference_objects_root_sends_an_empty_sub_prefix():
    path = f"{SDK}/assets/asset_ref/reference/objects"
    api = Api({("GET", path): (200, {**LISTING, "subPrefix": "", "nextContinuationToken": None})})
    sync_client(api).browse_reference_objects("asset_ref")
    params = api.sent("GET", path)[0].url.params
    assert params["subPrefix"] == "" and "continuationToken" not in params
    with pytest.raises(ValueError):
        sync_client(api).browse_reference_objects("asset_ref", sub_prefix=None)


def test_preview_reference_subset():
    path = f"{SDK}/assets/asset_ref/reference/preview"
    api = Api({("POST", path): (200, PREVIEW)})
    preview = sync_client(api).preview_reference_subset("asset_ref", include=["train/*.jpg"], exclude="*.tmp")
    assert api.body("POST", path) == {"include": ["train/*.jpg"], "exclude": ["*.tmp"]}
    assert isinstance(preview, SubsetPreview)
    assert (preview.asset_id, preview.matched_count, preview.total_bytes, preview.fetchable, preview.sample_paths) == \
        ("asset_ref", 3, 6144, True, ["train/1.jpg"])
    assert preview.max_objects == 10000 and preview.limit_notice is None
    # No patterns = every file.
    sync_client(api).preview_reference_subset("asset_ref")
    assert api.body("POST", path) == {"include": [], "exclude": []}


def test_fetch_reference_subset():
    path = f"{SDK}/assets/asset_ref/reference/fetch"
    api = Api({("POST", path): (202, FETCHED)})
    copy = sync_client(api).fetch_reference_subset("asset_ref", object_keys=["datasets/cats/a b.jpg "],
                                                   name="cats-subset", asset_type="Dataset", idempotency_key="f-1")
    request = api.sent("POST", path)[0]
    assert request.headers["Idempotency-Key"] == "f-1"
    # Object keys go as given (spaces are valid in keys); the type in the server's spelling.
    assert _json(request) == {"include": [], "exclude": [], "objectKeys": ["datasets/cats/a b.jpg "],
                              "name": "cats-subset", "assetType": "dataset"}
    assert isinstance(copy, AssetSubsetCopy)
    assert (copy.asset_id, copy.status, copy.source_asset_id, copy.file_count, copy.total_bytes) == \
        ("asset_copy", "uploading", "asset_ref", 3, 6144)
    sync_client(api).fetch_reference_subset("asset_ref", include="*.jpg")
    assert api.body("POST", path) == {"include": ["*.jpg"], "exclude": [], "objectKeys": []}


# --- Assets from a connected bucket ---------------------------------------------------------------------

def test_register_bucket_objects():
    path = f"{SDK}/storage-credentials/9/register"
    api = Api({("POST", path): (201, REGISTERED)})
    keys = ["datasets/cats/a.jpg", "datasets/cats/sub/b.jpg", "other/c.jpg", "datasets/cats2/d.jpg"]
    registered = sync_client(api).register_bucket_objects(9, keys, workspace="ws", name="cats",
                                                          base_prefix="datasets/cats", idempotency_key="r-1")
    request = api.sent("POST", path)[0]
    assert request.url.params["workspace"] == "ws" and request.headers["Idempotency-Key"] == "r-1"
    # Keys under the folder keep their path below it; a key outside it, or in a folder that only starts with the same
    # letters, gets the server's default (its last segment).
    assert _json(request) == {"objects": [{"objectKey": "datasets/cats/a.jpg", "relativePath": "a.jpg"},
                                          {"objectKey": "datasets/cats/sub/b.jpg", "relativePath": "sub/b.jpg"},
                                          {"objectKey": "other/c.jpg"}, {"objectKey": "datasets/cats2/d.jpg"}],
                              "name": "cats"}
    assert isinstance(registered, BucketAssetRegistered)
    assert (registered.asset_id, registered.file_count, registered.total_bytes, registered.storage_provider) == \
        ("asset_new", 2, 4096, "user_s3")
    assert registered.reference_mode is None and registered.size_measured


def test_register_bucket_objects_checks_the_shape_only():
    client = sync_client(Api())
    with pytest.raises(ValueError, match="at least one"):
        client.register_bucket_objects(9, [], workspace="ws", name="cats")
    with pytest.raises(ValueError, match="name"):
        client.register_bucket_objects(9, ["a.jpg"], workspace="ws", name="  ")
    with pytest.raises(ValueError, match="credential_id"):
        client.register_bucket_objects("../x", ["a.jpg"], workspace="ws", name="cats")
    # How many objects one call may register is the server's limit, not the SDK's.
    api = Api({("POST", f"{SDK}/storage-credentials/9/register"): (201, REGISTERED)})
    sync_client(api).register_bucket_objects(9, [f"k/{i}" for i in range(1200)], workspace="ws", name="many")
    assert len(api.body("POST", f"{SDK}/storage-credentials/9/register")["objects"]) == 1200


def test_register_bucket_prefix():
    path = f"{SDK}/storage-credentials/9/register-prefix"
    api = Api({("POST", path): (201, PREFIX_REGISTERED)})
    registered = sync_client(api).register_bucket_prefix(9, "datasets/cats/", workspace="ws", idempotency_key="p-1")
    request = api.sent("POST", path)[0]
    assert request.url.params["workspace"] == "ws" and _json(request) == {"prefix": "datasets/cats/"}
    assert (registered.asset_id, registered.reference_mode, registered.prefix) == ("asset_ref", "prefix",
                                                                                   "datasets/cats/")
    assert registered.file_count == 0 and not registered.size_measured and registered.size_notice
    sync_client(api).register_bucket_prefix(9, "datasets/cats", workspace="ws", name="cats", asset_type="dataset")
    assert api.body("POST", path) == {"prefix": "datasets/cats", "name": "cats", "assetType": "dataset"}
    for root in ("", "/", " "):
        with pytest.raises(ValueError):
            sync_client(api).register_bucket_prefix(9, root, workspace="ws")


@pytest.mark.parametrize("status, title", [(404, "Object Not Found"), (404, "Prefix Is Empty"),
                                           (400, "Storage Credential Error"), (409, "Concurrent Registration")])
def test_register_errors_keep_the_server_words(status, title):
    answer = (status, {"detail": {"title": title, "message": f"{title}."}})
    api = Api({("POST", f"{SDK}/storage-credentials/9/register"): answer,
               ("POST", f"{SDK}/storage-credentials/9/register-prefix"): answer})
    with pytest.raises(MeshiveAPIError, match=title) as raised:
        sync_client(api).register_bucket_objects(9, ["a"], workspace="ws", name="n")
    assert raised.value.title == title
    with pytest.raises(MeshiveAPIError, match=title):
        sync_client(api).register_bucket_prefix(9, "a/", workspace="ws")


# --- Saved credentials: usage and delete ----------------------------------------------------------------

def test_credential_usage():
    api = Api({("GET", f"{SDK}/hf-tokens/7/usage"): (200, USAGE),
               ("GET", f"{SDK}/civitai-keys/8/usage"): (200, {"assets": USAGE}),
               ("GET", f"{SDK}/registry-logins/4/usage"): (200, REGISTRY_USAGE)})
    client = sync_client(api)
    hf = client.get_hf_token_usage(7, "ws")
    civitai = client.get_civitai_key_usage("8", "ws")
    registry = client.get_registry_login_usage(4, "ws")
    for method, path in (("GET", f"{SDK}/hf-tokens/7/usage"), ("GET", f"{SDK}/civitai-keys/8/usage"),
                         ("GET", f"{SDK}/registry-logins/4/usage")):
        assert api.sent(method, path)[0].url.params["workspace"] == "ws"
    assert hf == civitai == [CredentialUsage(asset_id="asset_a", name="llama", active_pod_count=2)]
    assert isinstance(registry, RegistryLoginUsage)
    assert (registry.registry_login_id, registry.active_pod_count, registry.active_task_count) == (4, 1, 0)
    assert [(t.template_id, t.name) for t in registry.templates] == [(51, "private-image")]


@pytest.mark.parametrize("method_name, path, credential_id", [
    ("delete_hf_token", "/hf-tokens/7", 7), ("delete_civitai_key", "/civitai-keys/8", 8),
    ("delete_registry_login", "/registry-logins/4", 4), ("delete_storage_credential", "/storage-credentials/9", 9)])
def test_delete_credential(method_name, path, credential_id):
    answer = {"resource": "credential", "id": str(credential_id), "workspace": "ws", "action": "delete",
              "accepted": True, "result": {"kind": "hf_token", "label": "main"}}
    api = Api({("DELETE", SDK + path): (200, answer)})
    action = getattr(sync_client(api), method_name)(credential_id, "ws", idempotency_key="d-1")
    request = api.sent("DELETE", SDK + path)[0]
    assert request.url.params["workspace"] == "ws" and request.headers["Idempotency-Key"] == "d-1"
    assert isinstance(action, ResourceAction)
    assert (action.resource, action.id, action.action, action.workspace) == ("credential", str(credential_id),
                                                                             "delete", "ws")
    assert action.result == {"kind": "hf_token", "label": "main"}
    assert action.raw["operationPath"] == path


def test_delete_credential_with_a_bare_answer_and_a_replay():
    path = f"{SDK}/hf-tokens/7"

    def replay(request):
        return httpx.Response(200, json={"title": "Success", "message": "Deleted"},
                              headers={"Idempotent-Replayed": "true"})
    api = Api({("DELETE", path): replay})
    action = sync_client(api).delete_hf_token("7", "ws")
    assert (action.resource, action.id, action.action, action.workspace) == ("credential", "7", "delete", "ws")
    assert action.replayed


def test_credential_not_found_keeps_the_server_words():
    missing = {"detail": {"title": "Credential Not Found", "message": "No such token in this workspace."}}
    api = Api({("DELETE", f"{SDK}/hf-tokens/7"): (404, missing), ("GET", f"{SDK}/hf-tokens/7/usage"): (404, missing)})
    with pytest.raises(NotFoundError, match="No such token"):
        sync_client(api).delete_hf_token(7, "ws")
    with pytest.raises(NotFoundError, match="No such token"):
        sync_client(api).get_hf_token_usage(7, "ws")


def test_check_storage_credential_delete():
    path = f"{SDK}/storage-credentials/9/delete-check"
    api = Api({("GET", path): (200, DELETE_CHECK)})
    check = sync_client(api).check_storage_credential_delete(9, "ws")
    assert api.sent("GET", path)[0].url.params["workspace"] == "ws"
    assert isinstance(check, StorageCredentialDeleteCheck)
    assert (check.credential_id, check.label, check.bucket, check.asset_count, check.file_count) == \
        (9, "prod", "my-bucket", 2, 40)
    assert check.total_tracked_bytes == 1 << 30 and check.is_workspace_default
    assert (check.running_pods, check.running_tasks) == (1, 0)


# --- The default output bucket --------------------------------------------------------------------------

def test_set_asset_storage_setting():
    path = f"{SDK}/workspaces/ws/asset-storage"
    api = Api({("PUT", path): [(200, {"storageCredentialId": 9, "provider": "r2", "label": "prod",
                                      "bucket": "my-bucket"}),
                               (200, {"storageCredentialId": None})]})
    client = sync_client(api)
    bucket = client.set_asset_storage_setting("ws", 9, idempotency_key="s-1")
    assert api.body("PUT", path) == {"bucketId": 9}
    assert api.sent("PUT", path)[0].headers["Idempotency-Key"] == "s-1"
    assert isinstance(bucket, AssetStorageSetting) and bucket.credential_id == 9 and not bucket.managed
    managed = client.set_asset_storage_setting("ws", None)
    # Meshive storage is an explicit null, so the server can't read it as "no change".
    assert api.body("PUT", path) == {"bucketId": None} and managed.managed
    for bad in (True, "9", 1.5, -1):
        with pytest.raises(ValueError):
            client.set_asset_storage_setting("ws", bad)


# --- Older servers --------------------------------------------------------------------------------------

def test_an_older_server_says_it_does_not_support_these():
    routes = {("GET", f"{SDK}/assets/asset_ref/reference/objects"): OLD_SERVER,
              ("POST", f"{SDK}/assets/asset_ref/reference/preview"): OLD_SERVER,
              ("POST", f"{SDK}/assets/asset_ref/reference/fetch"): OLD_SERVER,
              ("POST", f"{SDK}/storage-credentials/9/register"): OLD_SERVER,
              ("POST", f"{SDK}/storage-credentials/9/register-prefix"): OLD_SERVER,
              ("GET", f"{SDK}/hf-tokens/7/usage"): OLD_SERVER, ("GET", f"{SDK}/civitai-keys/8/usage"): OLD_SERVER,
              ("GET", f"{SDK}/registry-logins/4/usage"): OLD_SERVER, ("DELETE", f"{SDK}/hf-tokens/7"): OLD_SERVER,
              ("DELETE", f"{SDK}/civitai-keys/8"): OLD_SERVER, ("DELETE", f"{SDK}/registry-logins/4"): OLD_SERVER,
              ("GET", f"{SDK}/storage-credentials/9/delete-check"): OLD_SERVER,
              ("DELETE", f"{SDK}/storage-credentials/9"): OLD_SERVER,
              # The read route exists there, so the write is a 405.
              ("PUT", f"{SDK}/workspaces/ws/asset-storage"): (405, {"detail": "Method Not Allowed"})}
    client = sync_client(Api(routes))
    calls = (lambda: client.browse_reference_objects("asset_ref"),
             lambda: client.preview_reference_subset("asset_ref"),
             lambda: client.fetch_reference_subset("asset_ref"),
             lambda: client.register_bucket_objects(9, ["a"], workspace="ws", name="n"),
             lambda: client.register_bucket_prefix(9, "a/", workspace="ws"),
             lambda: client.get_hf_token_usage(7, "ws"), lambda: client.get_civitai_key_usage(8, "ws"),
             lambda: client.get_registry_login_usage(4, "ws"), lambda: client.delete_hf_token(7, "ws"),
             lambda: client.delete_civitai_key(8, "ws"), lambda: client.delete_registry_login(4, "ws"),
             lambda: client.check_storage_credential_delete(9, "ws"),
             lambda: client.delete_storage_credential(9, "ws"),
             lambda: client.set_asset_storage_setting("ws", None))
    for call in calls:
        with pytest.raises(NotFoundError, match="does not support"):
            call()


# --- Both clients ---------------------------------------------------------------------------------------

def test_async_mirrors_sync_and_exports():
    for name in NEW_METHODS:
        assert inspect.signature(getattr(meshive.Meshive, name)) == \
            inspect.signature(getattr(meshive.AsyncMeshive, name)), name
        assert inspect.iscoroutinefunction(getattr(meshive.AsyncMeshive, name)), name
    for name in NEW_NAMES:
        assert name in meshive.__all__ and hasattr(meshive, name), name


def test_async_calls():
    api = Api({("GET", f"{SDK}/assets/asset_ref/reference/objects"): (200, LISTING),
               ("POST", f"{SDK}/assets/asset_ref/reference/preview"): (200, PREVIEW),
               ("POST", f"{SDK}/assets/asset_ref/reference/fetch"): (202, FETCHED),
               ("POST", f"{SDK}/storage-credentials/9/register"): (201, REGISTERED),
               ("POST", f"{SDK}/storage-credentials/9/register-prefix"): (201, PREFIX_REGISTERED),
               ("GET", f"{SDK}/hf-tokens/7/usage"): (200, USAGE),
               ("GET", f"{SDK}/registry-logins/4/usage"): (200, REGISTRY_USAGE),
               ("DELETE", f"{SDK}/civitai-keys/8"): (200, {"title": "Success"}),
               ("GET", f"{SDK}/storage-credentials/9/delete-check"): (200, DELETE_CHECK),
               ("DELETE", f"{SDK}/storage-credentials/9"): (200, {"resource": "credential", "id": "9",
                                                                  "workspace": "ws", "action": "delete"}),
               ("PUT", f"{SDK}/workspaces/ws/asset-storage"): (200, {"storageCredentialId": None})})

    async def run():
        async with async_client(api) as client:
            return (await client.browse_reference_objects("asset_ref"),
                    await client.preview_reference_subset("asset_ref", include="*.jpg"),
                    await client.fetch_reference_subset("asset_ref", include="*.jpg"),
                    await client.register_bucket_objects(9, ["datasets/cats/a.jpg"], workspace="ws", name="cats",
                                                         base_prefix="datasets/cats/"),
                    await client.register_bucket_prefix(9, "datasets/cats/", workspace="ws"),
                    await client.get_hf_token_usage(7, "ws"),
                    await client.get_registry_login_usage(4, "ws"),
                    await client.delete_civitai_key(8, "ws"),
                    await client.check_storage_credential_delete(9, "ws"),
                    await client.delete_storage_credential(9, "ws"),
                    await client.set_asset_storage_setting("ws", None))
    (listing, preview, copy, registered, prefix, usage, registry, civitai, check, bucket,
     setting) = asyncio.run(run())
    assert listing.objects[0].relative_path == "train/1.jpg" and preview.fetchable and copy.asset_id == "asset_copy"
    assert api.body("POST", f"{SDK}/storage-credentials/9/register")["objects"] == \
        [{"objectKey": "datasets/cats/a.jpg", "relativePath": "a.jpg"}]
    assert registered.asset_id == "asset_new" and prefix.reference_mode == "prefix" and usage[0].active_pod_count == 2
    assert registry.templates[0].template_id == 51 and (civitai.id, civitai.workspace) == ("8", "ws")
    assert check.running_pods == 1 and bucket.id == "9" and setting.managed
    assert api.body("PUT", f"{SDK}/workspaces/ws/asset-storage") == {"bucketId": None}
