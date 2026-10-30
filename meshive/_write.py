"""Write request builders — (path, params, body) assembly + argument validation shared by the sync and async clients.

The server takes camelCase JSON. The shape of the input (required values, types, absolute paths) is checked here,
before the round trip, and raises ValueError (the same stance as the read-side _*_params helpers). Which values are
allowed — GPU counts, sizes, placements, storage types — is the server's rule, so it is not repeated here and a value
the server accepts is never refused first; the few fixed limits kept here (task duration, script size, log tail) are
the server's own. Prices are sent as strings whether given as Decimal, float or str.
"""
from __future__ import annotations

import warnings
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

RENTAL_TYPES = ("demand", "spot")
# The placements the server knows today, for reference only — start_pod sends any value and the server decides.
PLACEMENTS = ("same_node", "any_node", "wait_same_node", "wait_any_node")
# Known storage types, used only to send them in the server's spelling ("hostpath" → "hostPath").
STORAGE_TYPES = ("nfs", "hostPath")
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
    """[{"storage": pv_name, "mount_path": "/data"}] or [("pv_name", "/data")]."""
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
    """[8888] or [{"port": 8888, "name": "jupyter", "external": True}]."""
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
        entry: dict[str, Any] = {"port": _port_number(port, "ports[].port"), "external": bool(external)}
        if name:
            entry["name"] = str(name)
        out.append(entry)
    return out


def _template_ports(items: Any) -> list[dict[str, Any]]:
    """Changes to the ports the template opens — [{"port", "name", "new_port", "external"}], where port is the
    template's port (its current number) and the rest is what to change: rename it, give it another number, expose or
    hide it. Only the keys given are sent. Whether the port is the template's and whether the result clashes with
    another port is the server's check."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            raise ValueError("template_ports items must be mappings like {'port': 8080, 'name': 'ide'}")

        def pick(snake: str, camel: str) -> Any:
            return item.get(snake) if item.get(snake) is not None else item.get(camel)

        entry: dict[str, Any] = {"port": _port_number(item.get("port"), "template_ports[].port")}
        if item.get("name") is not None:
            entry["name"] = _require_str(item.get("name"), "template_ports[].name")
        new_port = pick("new_port", "newPort")
        if new_port is not None:
            entry["newPort"] = _port_number(new_port, "template_ports[].new_port")
        if item.get("external") is not None:
            entry["external"] = bool(item.get("external"))
        out.append(entry)
    return out


def _port_number(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{name} must be an integer between 1 and 65535")
    return value


def _usage_threshold(value: Any, name: str) -> float | None:
    """A usage warning threshold as a float (the server checks the range)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None


def _local_volumes(items: Any) -> list[dict[str, Any]]:
    """New local storage created with the pod — [{"name", "mount_path", "size_gb", "usage_warning",
    "usage_warning_threshold", "encrypted"}]. Only the shape is checked; size limits are the server's."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            raise ValueError("local_volumes items must be mappings with name, mount_path and size_gb")

        def pick(snake: str, camel: str) -> Any:
            return item.get(snake) if item.get(snake) is not None else item.get(camel)

        mount = _require_str(pick("mount_path", "mountPath"), "local_volumes[].mount_path")
        if not mount.startswith("/"):
            raise ValueError("local_volumes[].mount_path must be an absolute path")
        size = pick("size_gb", "sizeGb")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError("local_volumes[].size_gb must be a positive integer (GiB)")
        threshold = _usage_threshold(pick("usage_warning_threshold", "usageWarningThreshold"),
                                     "local_volumes[].usage_warning_threshold")
        warning, encrypted = pick("usage_warning", "usageWarning"), item.get("encrypted")
        out.append(_drop_none({
            "name": _require_str(item.get("name"), "local_volumes[].name"), "mountPath": mount, "sizeGb": size,
            "usageWarning": None if warning is None else bool(warning), "usageWarningThreshold": threshold,
            "encrypted": None if encrypted is None else bool(encrypted),
        }))
    return out


def _deprecated(message: str) -> None:
    # stacklevel 4 points at the code that called the client method (_deprecated → body builder → client method → caller).
    warnings.warn(message, DeprecationWarning, stacklevel=4)


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


# --- Pods ---------------------------------------------------------------------

def pod_body(name: str, template_id: int, *, gpu_model: str | None, gpu_count: int, gpu_vram_gb: int | None,
             rental_type: str, vcpu: int | None, ram_gb: int | None,
             volumes: Any, env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None, ports: Any,
             command: str | None, internet_premium: bool, uptime_premium: bool, cpu_premium: bool,
             region: str | None, max_price_per_hour: Any, input_assets: Any = None, watched_folders: Any = None,
             harvest_destination: Any = None, local_volumes: Any = None, cpu_line: str | None = None,
             template_ports: Any = None, model_set: str | None = None,
             watched_template_folders: Mapping[str, Any] | None = None,
             hf_token_id: int | None = None) -> dict[str, Any]:
    if isinstance(template_id, bool) or not isinstance(template_id, int) or template_id < 0:
        raise ValueError("template_id must be a non-negative integer")
    # How many GPUs one pod may take is the server's rule; only the type is checked here.
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ValueError("gpu_count must be a positive integer")
    if cpu_line is not None and not isinstance(cpu_line, str):
        raise ValueError("cpu_line must be a string (a cpu_line from list_cpus)")
    if model_set is not None and not isinstance(model_set, str):
        raise ValueError("model_set must be a string (a slug from list_model_sets)")
    if region:
        # The server places pods without a region (as the console does) and ignores the value, so it isn't sent.
        _deprecated("region is ignored by the server; the argument will be removed in meshive 2.0")
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
        # A CPU pod's line (list_cpus → cpu_line). Omitted = the server picks the cheapest line that fits.
        "cpuLine": cpu_line.strip() if isinstance(cpu_line, str) and cpu_line.strip() else None,
        # The system disk is fixed by a server-side formula (quote resources.disk_gb) — not sent.
        "volumes": _volumes(volumes),
        "localVolumes": _local_volumes(local_volumes) or None,
        "env": env_out,
        "secretKeys": secrets,
        "ports": _ports(ports),
        "templatePorts": _template_ports(template_ports) or None,
        "command": command if command else None,
        "internetPremium": bool(internet_premium),
        "uptimePremium": bool(uptime_premium),
        "cpuPremium": bool(cpu_premium),
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
        "inputAssets": _pod_input_assets(input_assets) or None,
        "watchedFolders": _watched_folders(watched_folders) or None,
        # The template's own watched folders, as the console's create screen sets them: {path: {"enabled", "include"}}.
        "watchedTemplateFolders": _template_folders(watched_template_folders, "watched_template_folders",
                                                    allow_path=True) or None,
        "harvestDestination": _harvest_destination(harvest_destination),
        # One model set per pod (list_model_sets → slug). Omitted = none.
        "modelSet": model_set.strip() if isinstance(model_set, str) and model_set.strip() else None,
        # Quick Deploy templates whose model set is gated on Hugging Face: the user's own workspace token
        # (list_hf_tokens → id), as the console's Quick Deploy panel sends it. Omitted = none.
        "hfTokenId": _optional_int(hf_token_id, "hf_token_id"),
    })


def _strings(values: str | Iterable[str] | None, name: str) -> list[str] | None:
    """A list of non-empty strings (a single string counts as one item). None or empty → None (not sent)."""
    if values is None:
        return None
    items = [values] if isinstance(values, str) else list(values)
    return [_require_str(v, f"{name}[]") for v in items] or None


def _flag(value: Any, name: str) -> bool | None:
    """A consent flag: None (not sent) or an explicit bool, so a string such as "false" can't turn into consent."""
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be an explicit boolean")


# Pod edit fields that are a change. The rest of the body (consents, the price cap) only qualifies a change.
_POD_UPDATE_CHANGES = ("name", "templateId", "rentalType", "gpuModel", "gpuVramGb", "gpuCount", "vcpu", "ramGb",
                       "cpuLine", "env", "secretKeys", "removeEnv", "ports", "templatePorts", "removePorts", "command",
                       "volumes", "localVolumes", "detachStorages", "enableFolderSaving")


def pod_update_body(*, name: str | None = None, template_id: int | None = None, rental_type: str | None = None,
                    gpu_model: str | None = None, gpu_vram_gb: int | None = None, gpu_count: int | None = None,
                    vcpu: int | None = None, ram_gb: int | None = None, cpu_line: str | None = None,
                    env: Mapping[str, Any] | None = None, secret_keys: Iterable[str] | None = None,
                    remove_env: str | Iterable[str] | None = None, ports: Any = None, template_ports: Any = None,
                    remove_ports: Iterable[int] | None = None, command: str | None = None, volumes: Any = None,
                    local_volumes: Any = None, detach_storages: str | Iterable[str] | None = None,
                    allow_node_migration: bool | None = None, allow_data_loss: bool | None = None,
                    drop_watched_folders: str | Iterable[str] | None = None,
                    max_price_per_hour: Any = None, enable_folder_saving: bool | None = None) -> dict[str, Any]:
    """PATCH /pods/{pod} (and its estimate) body — only what is given is sent, so the rest of the pod stays as it is.
    command="" is sent (it means "back to the template's command"). Which changes the pod allows is the server's call."""
    if template_id is not None and (isinstance(template_id, bool) or not isinstance(template_id, int) or template_id < 0):
        raise ValueError("template_id must be a non-negative integer")
    if command is not None and not isinstance(command, str):
        raise ValueError("command must be a string ('' goes back to the template's command)")
    env_out, secrets = _env(env, secret_keys)
    removed_ports = None
    if remove_ports is not None:
        items = [remove_ports] if isinstance(remove_ports, int) else list(remove_ports)
        removed_ports = [_port_number(p, "remove_ports[]") for p in items] or None
    body = _drop_none({
        "name": None if name is None else _require_str(name, "name"),
        "templateId": template_id,
        "rentalType": None if rental_type is None else _rental(rental_type),
        "gpuModel": None if gpu_model is None else _require_str(gpu_model, "gpu_model"),
        "gpuVramGb": _optional_int(gpu_vram_gb, "gpu_vram_gb"),
        "gpuCount": _optional_int(gpu_count, "gpu_count"),
        "vcpu": _optional_int(vcpu, "vcpu"),
        "ramGb": _optional_int(ram_gb, "ram_gb"),
        "cpuLine": None if cpu_line is None else _require_str(cpu_line, "cpu_line"),
        "env": env_out or None,
        "secretKeys": secrets or None,
        "removeEnv": _strings(remove_env, "remove_env"),
        "ports": _ports(ports) or None,
        "templatePorts": _template_ports(template_ports) or None,
        "removePorts": removed_ports,
        "command": command,
        "volumes": _volumes(volumes) or None,
        "localVolumes": _local_volumes(local_volumes) or None,
        "detachStorages": _strings(detach_storages, "detach_storages"),
        "allowNodeMigration": _flag(allow_node_migration, "allow_node_migration"),
        "allowDataLoss": _flag(allow_data_loss, "allow_data_loss"),
        "dropWatchedFolders": _strings(drop_watched_folders, "drop_watched_folders"),
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
        # Only True asks for something; False is the same as not passing it.
        "enableFolderSaving": True if _flag(enable_folder_saving, "enable_folder_saving") else None,
    })
    if not any(key in body for key in _POD_UPDATE_CHANGES):
        raise ValueError("pass at least one change, such as name, gpu_model, env, ports or volumes")
    return body


_POD_INPUT_ASSET_KEYS = ("asset", "asset_id", "target_dir", "role", "paths", "strip_prefix")


def _pod_input_assets(items: Any) -> list[dict[str, Any]]:
    """Pod input asset — "asset_id" or {"asset", "target_dir", "role", "paths", "strip_prefix"} (paths = some
    files/folders inside the asset; strip_prefix "" keeps their folder structure, a prefix strips that prefix, and
    leaving it out strips the folder they have in common, as the console's Flatten/Keep structure toggle)."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"asset": _require_str(item, "input_assets[]")})
        elif isinstance(item, Mapping):
            unknown = sorted(str(key) for key in item if key not in _POD_INPUT_ASSET_KEYS)
            if unknown:
                # A misspelled key (say include_paths) would otherwise be dropped and the whole asset attached.
                raise ValueError(f"input_assets[] has unknown key(s) {', '.join(unknown)}; "
                                 f"use {', '.join(k for k in _POD_INPUT_ASSET_KEYS if k != 'asset_id')}")
            paths, strip = item.get("paths"), item.get("strip_prefix")
            if strip is not None and not isinstance(strip, str):
                raise ValueError("input_assets[].strip_prefix must be a string ('' keeps the folder structure)")
            out.append(_drop_none({
                "asset": _require_str(item.get("asset") or item.get("asset_id"), "input_assets[].asset"),
                "targetDir": item.get("target_dir") or None, "role": item.get("role") or None,
                "includePaths": ([paths] if isinstance(paths, str) else list(paths)) if paths else None,
                "stripPrefix": strip,
            }))
        else:
            raise ValueError("input_assets items must be asset ids or mappings")
    return out


def _watched_folders(items: Any) -> list[dict[str, Any]]:
    """Watched folder — "path" or {"path", "include", "include_existing", "enabled"}. include_existing=True also uploads
    files already there; enabled=False adds it turned off."""
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
                # A folder kept turned off (when set_watched_folders sends back what it read). Omitted = server default = on.
                "enabled": None if item.get("enabled") is None else bool(item["enabled"]),
            }))
        else:
            raise ValueError("watched_folders items must be paths or mappings")
    return out


def _harvest_destination(value: Any) -> dict[str, Any] | None:
    """Where harvested files go — None/"managed" (default) or {"mode": "user_s3", "credential_id": N}."""
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
    """PUT …/harvest body — full replacement. template = {path: {"enabled", "include"}}, user = list of folders."""
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer (WatchedFolders.revision)")
    return {"expectedVersion": expected_version, "template": _template_folders(template, "template"),
            "user": _watched_folders(user)}


def _template_folders(template: Mapping[str, Any] | None, name: str, *, allow_path: bool = False) -> dict[str, Any]:
    """The template's watched folders by path — {path: {"enabled": bool, "include": [globs] or None}}. include None
    keeps the template's patterns. When creating a pod (allow_path), "path" moves an output folder to another path;
    a running pod's template folders can't be moved."""
    if template is None:
        return {}
    if not isinstance(template, Mapping):
        raise ValueError(f"{name} must be a mapping of folder path to {{'enabled', 'include'}}")
    toggles = {}
    for path, toggle in template.items():
        toggle = {} if toggle is None else toggle
        if not isinstance(toggle, Mapping):     # {path: False} would otherwise read as "on"
            raise ValueError(f"{name}[{path!r}] must be a mapping like {{'enabled': False}}")
        moved = toggle.get("path")
        if moved is not None and not allow_path:
            raise ValueError(f"{name}[{path!r}]: a running pod's template folder can't be moved — turn it off and "
                             "add a folder of yours instead")
        toggles[_require_str(path, f"{name} path")] = _drop_none({
            "enabled": bool(toggle.get("enabled", True)),
            "include": list(toggle["include"]) if toggle.get("include") is not None else None,
            "path": None if moved is None else _require_str(moved, f"{name}[{path!r}].path")})
    return toggles


def placement(value: str | None) -> str | None:
    """start_pod placement, normalized (" Any_Node " → "any_node"). None stays None: the server then picks what the
    console would. Which placements exist is the server's call (an unknown one comes back as a 422), so only an empty
    value is refused here."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("placement must be a non-empty string, e.g. same_node or wait_any_node")
    return value.strip().lower()


def start_pod_params(workspace: str, placement_value: str | None, allow_data_loss: bool) -> dict[str, Any]:
    """Query of POST /pods/{pod}/start. Without a placement the parameter is left out (not sent empty), so the server
    applies the console's choice."""
    if not isinstance(allow_data_loss, bool):
        raise ValueError("allow_data_loss must be an explicit boolean")
    params: dict[str, Any] = {"workspace": workspace}
    chosen = placement(placement_value)
    if chosen is not None:
        params["placement"] = chosen
    params["allow_data_loss"] = allow_data_loss
    return params


def delete_pod_params(workspace: str, delete_local_storages: Iterable[str] | None) -> dict[str, str]:
    params = {"workspace": workspace}
    names = [s.strip() for s in (delete_local_storages or []) if isinstance(s, str) and s.strip()]
    if names:
        params["deleteLocalStorages"] = ",".join(names)
    return params


# --- Storage ------------------------------------------------------------------

def storage_body(name: str, size_gb: int, *, storage_type: str, disk_type: str, encrypted: bool,
                 region: str | None, max_price_per_hour: Any, usage_warning: bool | None = None,
                 usage_warning_threshold: Any = None) -> dict[str, Any]:
    """Standalone storage. Which types the server creates this way is its rule (today network storage only — local
    storage is created with a pod, see create_pod(local_volumes=...)), so storage_type is passed through, and so is
    the minimum size. The server ignores disk_type (always NVMe) and region, so neither is sent. The usage warning
    settings are sent only when given (the server's default threshold is 0.9)."""
    if isinstance(size_gb, bool) or not isinstance(size_gb, int) or size_gb < 1:
        raise ValueError("size_gb must be a positive integer")
    kind = _require_str(storage_type, "storage_type")
    kind = next((t for t in STORAGE_TYPES if t.lower() == kind.lower()), kind)
    if disk_type is not None and str(disk_type).strip().lower() != "nvme":
        _deprecated("disk_type is ignored by the server (storage is always NVMe); "
                    "the argument will be removed in meshive 2.0")
    if region:
        _deprecated("region is ignored by the server; the argument will be removed in meshive 2.0")
    return _drop_none({
        "name": _require_str(name, "name"), "sizeGb": size_gb, "storageType": kind, "encrypted": bool(encrypted),
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
        "usageWarning": None if usage_warning is None else bool(usage_warning),
        "usageWarningThreshold": _usage_threshold(usage_warning_threshold, "usage_warning_threshold"),
    })


def storage_update_body(*, name: str | None = None, size_gb: int | None = None, usage_warning: bool | None = None,
                        usage_warning_threshold: Any = None) -> dict[str, Any]:
    """PATCH /storages/{pv} (and its estimate) body — only what is given is sent; the rest keeps its current value.
    The minimum size and that storage can only grow are the server's rules."""
    if size_gb is not None and (isinstance(size_gb, bool) or not isinstance(size_gb, int) or size_gb < 1):
        raise ValueError("size_gb must be a positive integer (GiB)")
    body = _drop_none({
        "name": None if name is None else _require_str(name, "name"),
        "sizeGb": size_gb,
        "usageWarning": None if usage_warning is None else bool(usage_warning),
        "usageWarningThreshold": _usage_threshold(usage_warning_threshold, "usage_warning_threshold"),
    })
    if not body:
        raise ValueError("pass at least one of name, size_gb, usage_warning, usage_warning_threshold")
    return body


# --- Serving ------------------------------------------------------------------

def model_body(huggingface_repo: str, *, hf_token_id: int | None = None, name: str | None = None,
               framework: str | None = None, context_length: int | None = None) -> dict[str, Any]:
    """Body for detecting/registering a serving model. HF tokens are accepted only as the id of one saved in the workspace (the value is never sent)."""
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
    """Pick the source from `target` alone — `owner/name` or a huggingface.co URL = huggingface, a civitai.com URL =
    civitai, any other http(s) URL = a single direct-link file (url). Tokens and keys only as the id of one saved in the workspace."""
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


# --- Tasks --------------------------------------------------------------------

def _output_include(values: Any) -> list[str] | None:
    """Output filter — one glob or a list of globs, relative to /outputs. None or empty → None (not sent: every file is
    uploaded). Only the shape is checked; which patterns are valid is the server's rule."""
    if values is not None and (isinstance(values, Mapping) or not isinstance(values, (str, Iterable))):
        raise ValueError("output_include must be a glob or a list of globs, e.g. ['*.safetensors', 'samples/*.png']")
    return _strings(values, "output_include")


def task_body(name: str, script: str, *, image: str | None, template_id: int | None, requirements: str | None,
              env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None, args: Iterable[str] | None,
              gpu_model: str | None, gpu_count: int | None, gpu_vram_gb: int | None, cpu_preset: str | None,
              max_duration: int, webhook_url: str | None, input_assets: Any, max_price_per_hour: Any,
              output_include: str | Iterable[str] | None = None) -> dict[str, Any]:
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
                # Assets have no versions — the server already ignored this value, so it isn't sent. 2.0 stops accepting it at all.
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
        # Only files in /outputs matching these globs are uploaded. Omitted = every file (the body is unchanged).
        "outputInclude": _output_include(output_include),
    })


# --- Logs ---------------------------------------------------------------------

def logs_params(*, tail: int, wait: float | None, container: str | None = None,
                cursor: int | None = None) -> dict[str, Any]:
    """cursor is for external-provider tasks only: None/0 = last tail lines, a previous response's next_cursor = the increment after it."""
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
