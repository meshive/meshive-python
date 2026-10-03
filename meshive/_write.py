"""쓰기 요청 빌더 — 동기/비동기 클라이언트가 공유하는 (path, params, body) 조립 + 인자 검증.

서버(WebServerBackend routers/sdk/write.py, write_resources.py)는 camelCase JSON 을 받는다. 값 검증은 서버 왕복 전에
여기서 끝내 ValueError 로 알린다(읽기 쪽 _*_params 와 같은 자세). 가격은 Decimal/float/str 어느 것이든 문자열로 보낸다.
"""
from __future__ import annotations

import warnings
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

RENTAL_TYPES = ("demand", "spot")
PLACEMENTS = ("same_node", "any_node")
STORAGE_TYPES = ("nfs", "hostPath")
DISK_TYPES = ("HDD", "SSD", "NVMe")
MAX_SCRIPT_BYTES = 256 * 1024
TASK_DURATION_RANGE = (3600, 86400)


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_int(value: Any, name: str, *, minimum: int = 1) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _price(value: Any, name: str) -> str | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a number (USD per hour)") from None
    if amount <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return format(amount.normalize(), "f")


def _rental(value: str) -> str:
    rental = (value or "").strip().lower()
    if rental not in RENTAL_TYPES:
        raise ValueError(f"rental_type must be one of {', '.join(RENTAL_TYPES)}")
    return rental


def _env(env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None) -> tuple[dict[str, str], list[str]]:
    env_out = {str(k): str(v) for k, v in (env or {}).items()}
    secrets = [str(k) for k in (secret_keys or [])]
    unknown = [k for k in secrets if k not in env_out]
    if unknown:
        raise ValueError(f"secret_keys must be keys of env: {', '.join(unknown)}")
    return env_out, secrets


def _volumes(volumes: Iterable[Mapping[str, Any] | tuple[str, str]] | None) -> list[dict[str, str]]:
    """[{"storage": pv_name, "mount_path": "/data"}] 또는 [("pv_name", "/data")]."""
    out: list[dict[str, str]] = []
    for item in volumes or []:
        if isinstance(item, tuple) and len(item) == 2:
            storage, mount = item
        elif isinstance(item, Mapping):
            storage, mount = item.get("storage") or item.get("pv_name"), item.get("mount_path") or item.get("mountPath")
        else:
            raise ValueError("volumes items must be (pv_name, mount_path) or {'storage': ..., 'mount_path': ...}")
        storage = _require_str(storage, "volumes[].storage")
        mount = _require_str(mount, "volumes[].mount_path")
        if not mount.startswith("/"):
            raise ValueError("volumes[].mount_path must be an absolute path")
        out.append({"storage": storage, "mountPath": mount})
    return out


def _ports(ports: Iterable[int | Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """[8888] 또는 [{"port": 8888, "name": "jupyter", "external": True}]."""
    out: list[dict[str, Any]] = []
    for item in ports or []:
        if isinstance(item, bool):
            raise ValueError("ports items must be port numbers or mappings")
        if isinstance(item, int):
            port, name, external = item, None, True
        elif isinstance(item, Mapping):
            port, name, external = item.get("port"), item.get("name"), item.get("external", True)
        else:
            raise ValueError("ports items must be port numbers or mappings")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("ports[].port must be an integer between 1 and 65535")
        entry: dict[str, Any] = {"port": port, "external": bool(external)}
        if name:
            entry["name"] = str(name)
        out.append(entry)
    return out


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


# --- 파드 --------------------------------------------------------------------

def pod_body(name: str, template_id: int, *, gpu_model: str | None, gpu_count: int, gpu_vram_gb: int | None,
             rental_type: str, vcpu: int | None, ram_gb: int | None,
             volumes: Any, env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None, ports: Any,
             command: str | None, internet_premium: bool, uptime_premium: bool, cpu_premium: bool,
             region: str | None, max_price_per_hour: Any, input_assets: Any = None, watched_folders: Any = None,
             harvest_destination: Any = None) -> dict[str, Any]:
    if isinstance(template_id, bool) or not isinstance(template_id, int) or template_id < 0:
        raise ValueError("template_id must be a non-negative integer")
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or not 1 <= gpu_count <= 8:
        raise ValueError("gpu_count must be an integer between 1 and 8")
    env_out, secrets = _env(env, secret_keys)
    return _drop_none({
        "name": _require_str(name, "name"),
        "templateId": template_id,
        "gpuModel": gpu_model.strip() if isinstance(gpu_model, str) and gpu_model.strip() else None,
        "gpuCount": gpu_count,
        "gpuVramGb": _optional_int(gpu_vram_gb, "gpu_vram_gb"),
        "rentalType": _rental(rental_type),
        "vcpu": _optional_int(vcpu, "vcpu"),
        "ramGb": _optional_int(ram_gb, "ram_gb"),
        # 시스템 디스크는 서버 공식으로 고정된다(견적 resources.disk_gb) — 보내지 않는다.
        "volumes": _volumes(volumes),
        "env": env_out,
        "secretKeys": secrets,
        "ports": _ports(ports),
        "command": command if command else None,
        "internetPremium": bool(internet_premium),
        "uptimePremium": bool(uptime_premium),
        "cpuPremium": bool(cpu_premium),
        "region": region.strip() if isinstance(region, str) and region.strip() else None,
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
        "inputAssets": _pod_input_assets(input_assets) or None,
        "watchedFolders": _watched_folders(watched_folders) or None,
        "harvestDestination": _harvest_destination(harvest_destination),
    })


def _pod_input_assets(items: Any) -> list[dict[str, Any]]:
    """Pod 입력 자산 — "asset_id" 또는 {"asset", "target_dir", "role", "paths"} (paths = 자산 안 일부 파일·폴더)."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"asset": _require_str(item, "input_assets[]")})
        elif isinstance(item, Mapping):
            paths = item.get("paths")
            out.append(_drop_none({
                "asset": _require_str(item.get("asset") or item.get("asset_id"), "input_assets[].asset"),
                "targetDir": item.get("target_dir") or None, "role": item.get("role") or None,
                "includePaths": ([paths] if isinstance(paths, str) else list(paths)) if paths else None,
            }))
        else:
            raise ValueError("input_assets items must be asset ids or mappings")
    return out


def _watched_folders(items: Any) -> list[dict[str, Any]]:
    """수확 폴더 — "path" 또는 {"path", "include", "include_existing", "enabled"}. include_existing=True 면 지금 있는 파일도 올린다."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"path": _require_str(item, "watched_folders[]")})
        elif isinstance(item, Mapping):
            include = item.get("include")
            out.append(_drop_none({
                "path": _require_str(item.get("path"), "watched_folders[].path"),
                "include": ([include] if isinstance(include, str) else list(include)) if include else None,
                "includeExisting": bool(item.get("include_existing", False)),
                # 끈 채로 두는 폴더(set_watched_folders 가 읽은 값을 돌려보낼 때). 없으면 서버 기본 = 켜짐.
                "enabled": None if item.get("enabled") is None else bool(item["enabled"]),
            }))
        else:
            raise ValueError("watched_folders items must be paths or mappings")
    return out


def _harvest_destination(value: Any) -> dict[str, Any] | None:
    """수확 파일이 갈 곳 — None/"managed"(기본) 또는 {"mode": "user_s3", "credential_id": N}."""
    if value is None or value == "managed":
        return None
    if isinstance(value, Mapping):
        mode = value.get("mode", "managed")
        if mode not in ("managed", "user_s3"):
            raise ValueError("harvest_destination.mode must be 'managed' or 'user_s3'")
        return _drop_none({"mode": mode, "credentialId": _optional_int(value.get("credential_id"),
                                                                       "harvest_destination.credential_id", minimum=0)})
    raise ValueError("harvest_destination must be 'managed' or a mapping")


def watched_folders_body(expected_version: int, template: Mapping[str, Any] | None,
                         user: Any) -> dict[str, Any]:
    """PUT …/harvest 본문 — 전체 교체. template = {경로: {"enabled", "include"}}, user = 폴더 목록."""
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer (WatchedFolders.revision)")
    toggles = {}
    for path, toggle in (template or {}).items():
        toggle = toggle or {}
        toggles[_require_str(path, "template path")] = _drop_none({
            "enabled": bool(toggle.get("enabled", True)),
            "include": list(toggle["include"]) if toggle.get("include") is not None else None})
    return {"expectedVersion": expected_version, "template": toggles, "user": _watched_folders(user)}


def placement(value: str) -> str:
    choice = (value or "").strip().lower()
    if choice not in PLACEMENTS:
        raise ValueError(f"placement must be one of {', '.join(PLACEMENTS)}")
    return choice


def delete_pod_params(workspace: str, delete_local_storages: Iterable[str] | None) -> dict[str, str]:
    params = {"workspace": workspace}
    names = [s.strip() for s in (delete_local_storages or []) if isinstance(s, str) and s.strip()]
    if names:
        params["deleteLocalStorages"] = ",".join(names)
    return params


# --- 스토리지 -------------------------------------------------------------------

def storage_body(name: str, size_gb: int, *, storage_type: str, disk_type: str, encrypted: bool,
                 region: str | None, max_price_per_hour: Any) -> dict[str, Any]:
    if isinstance(size_gb, bool) or not isinstance(size_gb, int) or size_gb < 1:
        raise ValueError("size_gb must be a positive integer")
    kind = next((t for t in STORAGE_TYPES if t.lower() == (storage_type or "").strip().lower()), None)
    if kind is None:
        raise ValueError(f"storage_type must be one of {', '.join(STORAGE_TYPES)}")
    disk = next((t for t in DISK_TYPES if t.lower() == (disk_type or "").strip().lower()), None)
    if disk is None:
        raise ValueError(f"disk_type must be one of {', '.join(DISK_TYPES)}")
    # at-rest 암호화는 네트워크(nfs) 스토리지만 — 서버(WSB write_resources)가 hostPath+encrypted 를 422 로 거절한다.
    if encrypted and kind != "nfs":
        raise ValueError("encrypted is only available for nfs (network) storage; hostPath volumes cannot be encrypted")
    return _drop_none({
        "name": _require_str(name, "name"), "sizeGb": size_gb, "storageType": kind, "diskType": disk,
        "encrypted": bool(encrypted), "region": region.strip() if isinstance(region, str) and region.strip() else None,
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
    })


# --- 서빙 ---------------------------------------------------------------------

def model_body(huggingface_repo: str, *, hf_token_id: int | None = None, name: str | None = None,
               framework: str | None = None, context_length: int | None = None) -> dict[str, Any]:
    """서빙 모델 감지·등록 본문. HF 토큰은 워크스페이스에 등록된 것의 id 로만 받는다(값은 보내지 않는다)."""
    if framework is not None and framework not in ("vllm", "sglang"):
        raise ValueError("framework must be 'vllm' or 'sglang'")
    return _drop_none({
        "huggingfaceRepo": _require_str(huggingface_repo, "huggingface_repo").strip(),
        "hfTokenId": _optional_int(hf_token_id, "hf_token_id", minimum=0), "modelName": name or None,
        "framework": framework, "contextLength": _optional_int(context_length, "context_length"),
    })


def asset_import_body(target: str, *, name: str | None = None, asset_type: str | None = None,
                      revision: str | None = None, paths: Iterable[str] | None = None,
                      hf_token_id: int | None = None, civitai_key_id: int | None = None) -> dict[str, Any]:
    """`target` 하나로 source 를 고른다 — `owner/name` 또는 huggingface.co URL = huggingface, civitai.com URL =
    civitai, 그 밖의 http(s) URL = 직링크 1파일(url). 토큰·키는 워크스페이스에 등록된 것의 id 로만."""
    target = _require_str(target, "target").strip()
    parsed = urlparse(target)
    body: dict[str, Any] = {}
    if parsed.scheme in ("http", "https"):
        host = (parsed.hostname or "").lower()
        if host in ("huggingface.co", "www.huggingface.co"):
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 2:
                raise ValueError(f"not a Hugging Face repo URL: {target!r}")
            body.update(source="huggingface", hfRepo="/".join(parts[:2]))
        elif host.endswith("civitai.com"):
            body.update(source="civitai", sourceUrl=target)
        else:
            body.update(source="url", sourceUrl=target)
    elif target.count("/") == 1 and " " not in target:
        body.update(source="huggingface", hfRepo=target)
    else:
        raise ValueError("target must be a Hugging Face repo (owner/name) or an http(s) URL")
    return _drop_none({
        **body, "hfRevision": revision or None, "hfTokenId": _optional_int(hf_token_id, "hf_token_id", minimum=0),
        "civitaiKeyId": _optional_int(civitai_key_id, "civitai_key_id", minimum=0),
        "pathFilters": [p for p in ([paths] if isinstance(paths, str) else paths or []) if p] or None,
        "name": name or None, "assetType": asset_type.strip().lower() if asset_type else None,
    })


def serving_deploy_body(model_registration_id: int, *, price_cap_per_hour: Any, min_replicas: int,
                        max_replicas: int, autoscale: bool, max_context_tokens: int | None,
                        share_idle_capacity: bool) -> dict[str, Any]:
    if isinstance(model_registration_id, bool) or not isinstance(model_registration_id, int) or model_registration_id < 0:
        raise ValueError("model_registration_id must be a non-negative integer")
    if isinstance(min_replicas, bool) or not isinstance(min_replicas, int) or min_replicas < 0:
        raise ValueError("min_replicas must be an integer >= 0")
    if isinstance(max_replicas, bool) or not isinstance(max_replicas, int) or max_replicas < max(1, min_replicas):
        raise ValueError("max_replicas must be an integer >= max(1, min_replicas)")
    cap = _price(price_cap_per_hour, "price_cap_per_hour")
    if cap is None:
        raise ValueError("price_cap_per_hour is required (per-replica hourly cap in USD)")
    return _drop_none({
        "modelRegistrationId": model_registration_id, "minReplicas": min_replicas, "maxReplicas": max_replicas,
        "autoscale": bool(autoscale), "priceCapPerHourUsd": cap,
        "maxContextTokens": _optional_int(max_context_tokens, "max_context_tokens"),
        "shareIdleCapacity": bool(share_idle_capacity),
    })


def serving_scale_body(*, min_replicas: int | None, max_replicas: int | None, autoscale: bool | None,
                       price_cap_per_hour: Any) -> dict[str, Any]:
    body = _drop_none({
        "minReplicas": _optional_int(min_replicas, "min_replicas", minimum=0),
        "maxReplicas": _optional_int(max_replicas, "max_replicas"),
        "autoscale": None if autoscale is None else bool(autoscale),
        "priceCapPerHourUsd": _price(price_cap_per_hour, "price_cap_per_hour"),
    })
    if not body:
        raise ValueError("pass at least one of min_replicas, max_replicas, autoscale, price_cap_per_hour")
    return body


# --- 태스크 --------------------------------------------------------------------

def task_body(name: str, script: str, *, image: str | None, template_id: int | None, requirements: str | None,
              env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None, args: Iterable[str] | None,
              gpu_model: str | None, gpu_count: int | None, gpu_vram_gb: int | None, cpu_preset: str | None,
              max_duration: int, webhook_url: str | None, input_assets: Any, max_price_per_hour: Any) -> dict[str, Any]:
    script_text = script if isinstance(script, str) else ""
    if not script_text.strip():
        raise ValueError("script must be a non-empty string (Python source)")
    if len(script_text.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ValueError(f"script exceeds {MAX_SCRIPT_BYTES // 1024} KiB; upload large code as an asset instead")
    if not image and template_id is None:
        raise ValueError("pass image or template_id")
    if isinstance(max_duration, bool) or not isinstance(max_duration, int) or not TASK_DURATION_RANGE[0] <= max_duration <= TASK_DURATION_RANGE[1]:
        raise ValueError("max_duration must be an integer between 3600 and 86400 seconds (1h..24h)")
    if bool(gpu_model) == bool(cpu_preset):
        raise ValueError("pass exactly one of gpu_model (GPU task) or cpu_preset (CPU task)")
    env_out, secrets = _env(env, secret_keys)
    assets: list[dict[str, Any]] = []
    for item in input_assets or []:
        if isinstance(item, str):
            assets.append({"asset": item})
        elif isinstance(item, Mapping):
            asset = _require_str(item.get("asset") or item.get("asset_id"), "input_assets[].asset")
            entry: dict[str, Any] = {"asset": asset}
            if item.get("version") is not None:
                # 자산에 버전이 없다 — 서버도 무시하던 값이라 보내지 않는다. 0.2 에서 받는 것 자체를 없앤다.
                warnings.warn("input_assets[].version is ignored: assets no longer have versions",
                              DeprecationWarning, stacklevel=3)
            if item.get("target_dir"):
                entry["targetDir"] = str(item["target_dir"])
            assets.append(entry)
        else:
            raise ValueError("input_assets items must be asset ids or mappings")
    return _drop_none({
        "name": _require_str(name, "name"), "script": script_text, "requirements": requirements or None,
        "templateId": _optional_int(template_id, "template_id", minimum=0), "image": image or None,
        "env": env_out, "secretKeys": secrets, "args": [str(a) for a in (args or [])],
        "gpuModel": gpu_model or None, "gpuCount": _optional_int(gpu_count, "gpu_count"),
        "gpuVramGb": _optional_int(gpu_vram_gb, "gpu_vram_gb"), "cpuPreset": cpu_preset or None,
        "maxDurationS": max_duration, "webhookUrl": webhook_url or None, "inputAssets": assets,
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
    })


# --- 로그 --------------------------------------------------------------------

def logs_params(*, tail: int, wait: float | None, container: str | None = None,
                cursor: int | None = None) -> dict[str, Any]:
    """cursor 는 외부 provider 태스크 전용: None/0 = 마지막 tail 줄, 이전 응답의 next_cursor = 그 뒤 증분."""
    if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= 1000:
        raise ValueError("tail must be an integer between 1 and 1000")
    params: dict[str, Any] = {"tail": tail}
    if wait is not None:
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 15:
            raise ValueError("wait must be between 0 and 15 seconds")
        params["wait"] = wait
    if container:
        params["container"] = container
    if cursor:
        params["cursor"] = _optional_int(cursor, "cursor", minimum=0)
    return params
