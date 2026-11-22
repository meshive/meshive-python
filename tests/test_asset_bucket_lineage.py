"""Asset reads: which asset a subset copy came from, and which bucket connection an asset reads through."""

import asyncio

import httpx
import pytest

from meshive import AsyncMeshive, Meshive
from meshive.models import Asset

ROW = {"assetExternalId": "asset_copy", "name": "copy", "assetType": "dataset", "status": "active",
       "storageProvider": "meshive_r2"}


def _from(ingest, ref):
    a = Asset.from_dict({**ROW, "ingestSource": ingest, "importSourceRef": ref})
    return a.source.from_asset_id if a.source else None


@pytest.mark.parametrize("ref, expected", [
    # what the server writes today: s3subset:<asset id>:<bucket>/<prefix>[?include=..&exclude=..]
    ("s3subset:asset_0123456789abcdef:my-bucket/data/train?include=*.png,*.jpg&exclude=tmp/*",
     "asset_0123456789abcdef"),
    ("s3subset:asset_0123456789abcdef:my-bucket/data/", "asset_0123456789abcdef"),
    ("s3subset:asset_0123456789abcdef:my-bucket/", "asset_0123456789abcdef"),
    ("s3subset:asset_src:b/p@v3", "asset_src"),       # an "@v" inside the prefix does not cut the id
    # the older format, still stored on assets copied before it changed: s3subset:<asset id>@v<n>:<bucket>/<prefix>
    ("s3subset:asset_ab12@v3:my-bucket/data/x?include=*.json", "asset_ab12"),
    ("s3subset:asset_src@v12:b/p?include=a&exclude=c", "asset_src"),
    ("s3subset:asset_src@v1:b/", "asset_src"),
])
def test_subset_source_asset_id_reads_both_formats(ref, expected):
    assert _from("s3_subset_fetch", ref) == expected


@pytest.mark.parametrize("ref", [
    "s3subset:", "s3subset:asset_src", "s3subset::bucket/prefix", "s3subset:@v1", "s3subset:asset_src:",
    "s3subset:asset_src@", "s3subset:asset_src@x1", "s3subset:asset_src@v", "s3subset:asset src:b/p",
    "s3subset:asset_src@v1", "s3subset:asset_src@v:b/p", "s3subset:asset_src@v1:",
    "other:asset_src:b/p", "other:asset_src@v1", "S3SUBSET:asset_src:b/p", "", None, 7,
])
def test_subset_source_asset_id_rejects_other_strings(ref):
    assert _from("s3_subset_fetch", ref) is None


def test_subset_source_only_for_a_subset_fetch():
    assert _from("web_upload", "s3subset:asset_src:b/p") is None


BUCKET_KEYS = {"storageCredentialId": 30, "storageCredentialLabel": "lab-bucket", "storageCredentialStatus": "ok"}


def test_registered_file_says_its_bucket_connection():
    a = Asset.from_dict({**ROW, "storageProvider": "user_s3", "ingestSource": "s3_register", **BUCKET_KEYS})
    assert (a.bucket_id, a.bucket_label, a.bucket_status) == (30, "lab-bucket", "ok")
    assert a.reference is None


def test_linked_folder_says_its_bucket_connection_also_on_reference():
    a = Asset.from_dict({**ROW, "storageProvider": "user_s3", "referenceMode": "prefix", "referencePrefix": "data/",
                         "credentialLabel": None, "credentialStatus": None, **BUCKET_KEYS})
    assert (a.bucket_id, a.bucket_label, a.bucket_status) == (30, "lab-bucket", "ok")
    assert (a.reference.credential_label, a.reference.credential_status) == ("lab-bucket", "ok")


def test_token_label_on_reference_wins_over_bucket():
    a = Asset.from_dict({**ROW, "referenceMode": "external", "credentialLabel": "hf token", "credentialStatus": "ok",
                         **BUCKET_KEYS})
    assert (a.reference.credential_label, a.reference.credential_status) == ("hf token", "ok")


def test_removed_connection_and_meshive_storage():
    gone = Asset.from_dict({**ROW, "storageProvider": "user_s3", "storageCredentialId": None,
                            "storageCredentialLabel": None, "storageCredentialStatus": "removed"})
    assert (gone.bucket_id, gone.bucket_label, gone.bucket_status) == (None, None, "removed")
    managed = Asset.from_dict({**ROW, "storageCredentialId": None, "storageCredentialLabel": None,
                               "storageCredentialStatus": None})
    assert (managed.bucket_id, managed.bucket_label, managed.bucket_status) == (None, None, None)
    old_server = Asset.from_dict(ROW)
    assert (old_server.bucket_id, old_server.bucket_label, old_server.bucket_status) == (None, None, None)


def test_snake_case_keys_are_accepted_too():
    a = Asset.from_dict({**ROW, "storage_credential_id": "31", "storage_credential_label": "x (removed #31)",
                         "storage_credential_status": "removed"})
    assert (a.bucket_id, a.bucket_label, a.bucket_status) == (31, "x (removed #31)", "removed")


DETAIL = {**ROW, "storageProvider": "user_s3", "referenceMode": "prefix", "referencePrefix": "d/",
          "ingestSource": "s3_subset_fetch", "importSourceRef": "s3subset:asset_src:b/d/?include=*.png",
          "files": [], **BUCKET_KEYS}


def _handler(request):
    return httpx.Response(200, json=DETAIL)


def test_sync_and_async_clients_read_the_same():
    sync = Meshive(api_key="meshive_test", base_url="https://api.test")
    sync._client = httpx.Client(transport=httpx.MockTransport(_handler))
    aclient = AsyncMeshive(api_key="meshive_test", base_url="https://api.test")
    aclient._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    for got in (sync.get_asset("asset_copy"), asyncio.run(aclient.get_asset("asset_copy"))):
        assert (got.bucket_id, got.bucket_label, got.bucket_status) == (30, "lab-bucket", "ok")
        assert got.source.from_asset_id == "asset_src"
        assert got.reference.credential_label == "lab-bucket"
