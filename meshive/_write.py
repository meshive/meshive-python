"""Write request builders — (path, params, body) assembly + argument validation shared by the sync and async clients.

The server takes camelCase JSON. The shape of the input (required values, types, absolute paths) is checked here,
before the round trip, and raises ValueError (the same stance as the read-side _*_params helpers). Which values are
allowed — GPU counts, sizes, placements, storage types — is the server's rule, so it is not repeated here and a value
the server accepts is never refused first; the few fixed limits kept here (task duration, script size, log tail) are
the server's own. Prices are sent as strings whether given as Decimal, float or str.
"""
from __future__ import annotations

import math
import warnings
from collections.abc import Iterable, Mapping
from datetime import datetime
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


def _ports(ports: Iterable[int | Mapping[str, Any]] | None, lock: bool | None = None) -> list[dict[str, Any]]:
    """[8888] or [{"port": 8888, "name": "jupyter", "external": True, "locked": True}]. `locked` is sent when the item
    gives it; otherwise an exposed port gets `lock` (None = nothing is sent, and the server leaves the port open). An
    internal port never gets the default."""
    out: list[dict[str, Any]] = []
    for item in ports or []:
        if isinstance(item, bool):
            raise ValueError("ports items must be port numbers or mappings")
        if isinstance(item, int):
            port, name, external, locked = item, None, True, None
        elif isinstance(item, Mapping):
            port, name, external = item.get("port"), item.get("name"), item.get("external", True)
            locked = _flag(item.get("locked"), "ports[].locked")
        else:
            raise ValueError("ports items must be port numbers or mappings")
        entry: dict[str, Any] = {"port": _port_number(port, "ports[].port"), "external": bool(external)}
        if name:
            entry["name"] = str(name)
        if locked is None and entry["external"]:
            locked = lock
        if locked is not None:
            entry["locked"] = locked
        out.append(entry)
    return out


def _template_ports(items: Any) -> list[dict[str, Any]]:
    """Changes to the ports the template opens — [{"port", "name", "new_port", "external", "locked"}], where port is
    the template's port (its current number) and the rest is what to change: rename it, give it another number, expose
    or hide it, lock or unlock it. Only the keys given are sent. Whether the port is the template's and whether the
    result clashes with another port is the server's check."""
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
        locked = _flag(item.get("locked"), "template_ports[].locked")
        if locked is not None:
            entry["locked"] = locked
        out.append(entry)
    return out


def lock_template_ports(body: dict[str, Any], endpoints: Any, lock: bool, public_now: Any = ()) -> None:
    """Give every port of the template that the pod will newly expose a `locked` value, in body["templatePorts"].

    `endpoints` is the template's port list as the API returns it. A port counts as exposed when the template exposes
    it, or when the request's own templatePorts entry says external=True. An entry that already carries `locked` is
    left alone. With lock=True the value is the template author's default (isLocked true/false), and locked when the
    author set none; with lock=False every exposed port is sent as unlocked. The server never applies the template's
    default by itself, so a port left out here would be created open.

    `public_now` (an update that moves a pod to this template) holds the port numbers the pod exposes today. A port
    that ends up on one of those numbers keeps the address it has, and its lock: no value is added for it."""
    changes = list(body.get("templatePorts") or [])
    by_port = {entry["port"]: entry for entry in changes}
    public_now = set(public_now)
    for endpoint in endpoints or []:
        if not isinstance(endpoint, Mapping):
            continue
        port = endpoint.get("containerPort")
        if isinstance(port, bool) or not isinstance(port, int):
            continue
        entry = by_port.get(port)
        external = entry["external"] if entry is not None and "external" in entry else bool(endpoint.get("isExternal"))
        if not external or (entry is not None and "locked" in entry):
            continue
        if (entry.get("newPort", port) if entry is not None else port) in public_now:
            continue
        stored = endpoint.get("isLocked")
        if entry is None:
            entry = by_port[port] = {"port": port}
            changes.append(entry)
        entry["locked"] = (stored if isinstance(stored, bool) else True) if lock else False
    if changes:
        body["templatePorts"] = changes


def exposes_ports(body: Mapping[str, Any]) -> bool:
    """Whether a pod update body may make an existing port public without saying how it is locked: it moves the pod
    to another template, or a templatePorts entry says external=True and carries no `locked`. Deciding the lock then
    needs the pod's current ports (a port that is public already keeps its lock)."""
    return "templateId" in body or any(entry.get("external") is True and "locked" not in entry
                                       for entry in body.get("templatePorts") or [])


def lock_exposed_ports(body: dict[str, Any], public_now: Any, lock: bool) -> None:
    """Give `lock` to the ports a pod update turns from internal to exposed (a templatePorts entry with
    external=True and no `locked` of its own). A port in `public_now`, the numbers the pod exposes today, is exposed
    already: its lock is whatever was chosen for it, and it gets no value."""
    public_now = set(public_now)
    for entry in body.get("templatePorts") or []:
        if entry.get("external") is True and "locked" not in entry and entry["port"] not in public_now:
            entry["locked"] = lock


def port_locks(body: Mapping[str, Any]) -> dict[int, bool]:
    """{port: locked} for every port a pod create or update body sends a lock value for. A template port that is also
    renumbered is listed under its new number, the one the pod will have."""
    locks: dict[int, bool] = {}
    for entry in body.get("templatePorts") or []:
        if "locked" in entry:
            locks[entry.get("newPort", entry["port"])] = entry["locked"]
    for entry in body.get("ports") or []:
        if "locked" in entry:
            locks[entry["port"]] = entry["locked"]
    return locks


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


# --- Workspaces ---------------------------------------------------------------

def workspace_body(name: str | None) -> dict[str, Any]:
    """No name or a blank one → an empty body (the server generates one, as it does for a blank name). Which names are
    allowed (length, characters, already taken) is the server's rule, so a name is only checked for being text."""
    if name is None or (isinstance(name, str) and not name.strip()):
        return {}
    return {"name": _require_str(name, "name")}


def workspace_update_body(name: str) -> dict[str, Any]:
    """Renaming needs a name: unlike workspace_body, a blank one is refused rather than left for the server to generate."""
    return {"name": _require_str(name, "name")}


# --- Pods ---------------------------------------------------------------------

def pod_body(name: str, template_id: int, *, gpu_model: str | None, gpu_count: int, gpu_vram_gb: int | None,
             rental_type: str, vcpu: int | None, ram_gb: int | None,
             volumes: Any, env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None, ports: Any,
             command: str | None, internet_premium: bool, uptime_premium: bool, cpu_premium: bool,
             region: str | None, max_price_per_hour: Any, input_assets: Any = None, watched_folders: Any = None,
             harvest_destination: Any = None, local_volumes: Any = None, cpu_line: str | None = None,
             template_ports: Any = None, model_set: str | None = None,
             watched_template_folders: Mapping[str, Any] | None = None,
             hf_token_id: int | None = None, lock_public_ports: bool = True) -> dict[str, Any]:
    """POST /pods (and its estimate) body. Every exposed port in `ports` carries `locked`: its own value, or
    lock_public_ports. The ports the template opens get theirs from lock_template_ports, once the template is read."""
    if isinstance(template_id, bool) or not isinstance(template_id, int) or template_id < 0:
        raise ValueError("template_id must be a non-negative integer")
    if not isinstance(lock_public_ports, bool):
        raise ValueError("lock_public_ports must be an explicit boolean")
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
        "ports": _ports(ports, lock_public_ports),
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
                       "volumes", "localVolumes", "detachStorages", "enableFolderSaving", "addInputAssets",
                       "removeInputAssets")


def pod_update_body(*, name: str | None = None, template_id: int | None = None, rental_type: str | None = None,
                    gpu_model: str | None = None, gpu_vram_gb: int | None = None, gpu_count: int | None = None,
                    vcpu: int | None = None, ram_gb: int | None = None, cpu_line: str | None = None,
                    env: Mapping[str, Any] | None = None, secret_keys: Iterable[str] | None = None,
                    remove_env: str | Iterable[str] | None = None, ports: Any = None, template_ports: Any = None,
                    remove_ports: Iterable[int] | None = None, command: str | None = None, volumes: Any = None,
                    local_volumes: Any = None, detach_storages: str | Iterable[str] | None = None,
                    allow_node_migration: bool | None = None, allow_data_loss: bool | None = None,
                    drop_watched_folders: str | Iterable[str] | None = None,
                    max_price_per_hour: Any = None, enable_folder_saving: bool | None = None,
                    add_input_assets: Any = None, remove_input_assets: int | Iterable[int] | None = None,
                    lock_public_ports: bool = True) -> dict[str, Any]:
    """PATCH /pods/{pod} (and its estimate) body — only what is given is sent, so the rest of the pod stays as it is.
    command="" is sent (it means "back to the template's command"). Which changes the pod allows is the server's call.
    add_input_assets takes create_pod's input_assets items; remove_input_assets the connection_ids of input assets
    (get_pod_assets).
    A new exposed port in `ports` carries `locked`: its own value, or lock_public_ports. Ports that the change
    exposes or takes over from a new template get theirs once the pod is read (lock_exposed_ports,
    lock_template_ports); a port that is public already never gets a value it wasn't given. lock_public_ports by
    itself is not a change."""
    if not isinstance(lock_public_ports, bool):
        raise ValueError("lock_public_ports must be an explicit boolean")
    lock = lock_public_ports
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
        "ports": _ports(ports, lock) or None,
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
        "addInputAssets": _pod_input_assets(add_input_assets) or None,
        "removeInputAssets": _connection_ids(remove_input_assets, "remove_input_assets"),
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


# --- Port locks and access keys -----------------------------------------------

def port_segment(port: Any) -> str:
    """A container port as a URL path segment."""
    return str(_port_number(port, "port"))


def port_lock_body(locked: Any) -> dict[str, Any]:
    """PATCH …/ports/{port}/lock body. Only a real bool is taken, so a string such as "false" can't lock or unlock a
    port by accident."""
    if not isinstance(locked, bool):
        raise ValueError("locked must be an explicit boolean (True to lock the port, False to unlock it)")
    return {"locked": locked}


def port_preflight_body(allow: Any) -> dict[str, Any]:
    """PATCH …/ports/{port}/preflight body. Only a real bool is taken, so a string such as "false" can't let
    preflight requests through by accident."""
    if not isinstance(allow, bool):
        raise ValueError("allow must be an explicit boolean (True to let preflight requests through, False to refuse "
                         "them)")
    return {"allowPreflight": allow}


def access_key_body(name: Any, expires_in_days: Any) -> dict[str, Any]:
    """POST …/ports/{port}/keys body. Which lifetimes exist is the server's rule (30, 90 or 365 days for an API key
    caller; a key that never expires is made only in the web console), so only the type is checked. None is refused
    rather than read as "never expires"."""
    if isinstance(expires_in_days, bool) or not isinstance(expires_in_days, int) or expires_in_days < 0:
        raise ValueError("expires_in_days must be a whole number of days: 30, 90 or 365")
    return {"name": _require_str(name, "name"), "expiresInDays": expires_in_days}


def access_key_roll_body(overlap_seconds: Any) -> dict[str, Any]:
    """POST …/keys/{id}/roll body. Which overlaps exist is the server's rule (0, 3600, 86400 or 604800 seconds)."""
    if isinstance(overlap_seconds, bool) or not isinstance(overlap_seconds, int) or overlap_seconds < 0:
        raise ValueError("overlap_seconds must be a whole number of seconds: 0 (the old value stops now), 3600, 86400 "
                         "or 604800")
    return {"overlapSeconds": overlap_seconds}


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
               framework: str | None = None, context_length: int | None = None,
               input_modalities: str | Iterable[str] | None = None) -> dict[str, Any]:
    """Body for detecting/registering a serving model. HF tokens are accepted only as the id of one saved in the workspace (the value is never sent).

    Text models are served with vLLM. The server decides which engines it accepts and answers any other `framework`
    with a 400 that says why, so only the type is checked here. `input_modalities` (["text"] or ["text", "image"])
    says what a model takes when the Hugging Face repo doesn't; which values are allowed is the server's rule."""
    if framework is not None:
        framework = _require_str(framework, "framework")
    if input_modalities is not None and (isinstance(input_modalities, Mapping)
                                         or not isinstance(input_modalities, (str, Iterable))):
        raise ValueError('input_modalities must be a list such as ["text"] or ["text", "image"]')
    modalities = _strings(input_modalities, "input_modalities")
    if input_modalities is not None and not modalities:
        raise ValueError('input_modalities must not be empty; leave it out to let the server tell')
    return _drop_none({
        "huggingfaceRepo": _require_str(huggingface_repo, "huggingface_repo").strip(),
        "hfTokenId": _optional_int(hf_token_id, "hf_token_id", minimum=0), "modelName": name or None,
        "framework": framework, "contextLength": _optional_int(context_length, "context_length"),
        "inputModalities": modalities,
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


MAX_ASSET_FILE_DELETE = 1000   # the server's per-call limit on files to delete


def asset_update_body(*, name: str | None = None, asset_type: str | None = None) -> dict[str, Any]:
    """Body for renaming or retyping an asset: only what changes, at least one of the two. No asset can be made an
    output (outputs only come from a task or a pod's watched folder), so asset_type="output" is refused here."""
    if name is None and asset_type is None:
        raise ValueError("pass name or asset_type (or both)")
    kind = _require_str(asset_type, "asset_type").lower() if asset_type is not None else None
    if kind == "output":
        raise ValueError("asset_type can't be 'output': output assets only come from tasks and pods")
    return _drop_none({"name": _require_str(name, "name") if name is not None else None, "assetType": kind})


def asset_file_delete_body(*, file_ids: str | Iterable[str] | None = None, asset_id: str | None = None,
                           paths: str | Iterable[str] | None = None,
                           expected_file_count: int | None = None) -> dict[str, Any]:
    """Body for previewing or deleting asset files. Exactly one selection: `file_ids` ("file_..." ids, any assets of
    one workspace, up to 1000), or `asset_id` with `paths` (globs over that asset's file paths)."""
    ids = _strings(file_ids, "file_ids")
    globs = _strings(paths, "paths")
    if ids is not None and (asset_id is not None or globs is not None):
        raise ValueError("pass either file_ids or asset_id with paths, not both")
    if ids is None:
        if asset_id is None or globs is None:
            raise ValueError("pass file_ids, or asset_id together with paths")
        asset_id = _require_str(asset_id, "asset_id")
    elif len(ids) > MAX_ASSET_FILE_DELETE:
        raise ValueError(f"at most {MAX_ASSET_FILE_DELETE} file_ids per call")
    return _drop_none({"fileIds": ids, "assetId": asset_id, "paths": globs,
                       "expectedFileCount": _optional_int(expected_file_count, "expected_file_count", minimum=0)})


MAX_FILE_DOWNLOAD_LINKS = 500   # the server's per-call limit on files to sign


def file_download_body(file_ids: str | Iterable[str]) -> dict[str, Any]:
    """Body for download links to files picked by id ("file_..." ids from any assets of one workspace, up to 500)."""
    ids = _strings(file_ids, "file_ids")
    if ids is None:
        raise ValueError("file_ids needs at least one file id")
    if len(ids) > MAX_FILE_DOWNLOAD_LINKS:
        raise ValueError(f"at most {MAX_FILE_DOWNLOAD_LINKS} file_ids per call")
    return {"fileIds": ids}


def asset_export_body(*, bucket_id: int, prefix: str | None = None) -> dict[str, Any]:
    """Body for copying an asset to a connected bucket. prefix = the folder in the bucket (server default if omitted)."""
    if isinstance(bucket_id, bool) or not isinstance(bucket_id, int) or bucket_id < 0:
        raise ValueError("bucket_id must be a non-negative integer (a credential_id from list_storage_credentials)")
    return _drop_none({"bucketId": bucket_id, "prefix": _require_str(prefix, "prefix") if prefix is not None else None})


def asset_hf_upload_body(*, hf_repo: str, hf_token_id: int, private: bool = True,
                         path_in_repo: str | None = None) -> dict[str, Any]:
    """Body for uploading an asset to a Hugging Face repo ("owner/name") with a saved token that can write to it."""
    if isinstance(hf_token_id, bool) or not isinstance(hf_token_id, int) or hf_token_id < 0:
        raise ValueError("hf_token_id must be a non-negative integer (a token id from list_hf_tokens)")
    if not isinstance(private, bool):
        raise ValueError("private must be True or False")
    return _drop_none({"hfRepo": _require_str(hf_repo, "hf_repo"), "hfTokenId": hf_token_id, "private": private,
                       "pathInRepo": _require_str(path_in_repo, "path_in_repo") if path_in_repo is not None else None})


def open_asset_import_body(*, entries: str | Iterable[str] | None = None, model_set: str | None = None,
                           hf_token_id: int | None = None) -> dict[str, Any]:
    """Body for importing catalog entries or every file of one model set as linked assets. Exactly one of the two."""
    slugs = _strings(entries, "entries")
    if (slugs is None) == (model_set is None):
        raise ValueError("pass either entries (catalog entry slugs) or model_set (a set slug), not both")
    return _drop_none({"entrySlugs": slugs,
                       "modelSet": _require_str(model_set, "model_set") if model_set is not None else None,
                       "hfTokenId": _optional_int(hf_token_id, "hf_token_id", minimum=0)})


def file_batch_part_urls_body(file_id: str, part_numbers: Iterable[int] = ()) -> dict[str, Any]:
    """Body for new upload links of one file of an add-files upload: parts of a multipart file, or with no part
    numbers a new single-PUT link."""
    numbers = []
    for n in part_numbers:
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError("part_numbers must be integers >= 1")
        numbers.append(n)
    return {"files": [{"fileExternalId": _require_str(file_id, "file_id"), "partNumbers": numbers}]}


def file_batch_complete_body(files: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Body for finishing an add-files upload: [{"file_id", "sha256"?, "parts": [{"part_number", "etag"}] for
    multipart}]. No files = finish every file of the upload (the server lists them)."""
    out = []
    for f in files:
        file_id = f.get("file_id", f.get("fileExternalId"))
        item: dict[str, Any] = {"fileExternalId": _require_str(file_id, "file_id"),
                                "parts": [{"partNumber": int(p.get("part_number", p.get("partNumber"))),
                                           "etag": str(p["etag"])} for p in f.get("parts") or []]}
        if f.get("sha256"):
            item["sha256"] = str(f["sha256"])
        out.append(item)
    return {"files": out}


def _connection_ids(values: int | Iterable[int] | None, name: str) -> list[int] | None:
    """Pod input-asset connection ids (a single id counts as one). None or empty → None (not sent)."""
    if values is None:
        return None
    items = [values] if isinstance(values, int) else list(values)
    for value in items:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be connection_id integers from get_pod_assets")
    return items or None


def _selection(paths: str | Iterable[str] | None, strip_prefix: str | None, name: str) -> dict[str, Any]:
    """Some files of an asset: paths inside it, and strip_prefix ("" keeps their folders; None lets the server strip
    the folder they share)."""
    if strip_prefix is not None and not isinstance(strip_prefix, str):
        raise ValueError(f"{name}strip_prefix must be a string ('' keeps the folder structure)")
    return {"paths": _strings(paths, f"{name}paths"), "stripPrefix": strip_prefix}


def pod_asset_add_body(asset: str, *, role: str, target_dir: str | None = None,
                       paths: str | Iterable[str] | None = None, strip_prefix: str | None = None) -> dict[str, Any]:
    """Body for adding an asset to an existing pod: the asset, the input slot `role`, and optionally a target folder
    and some of its files (as an input_assets item of create_pod). strip_prefix "" is sent (keep the folders)."""
    selection = _selection(paths, strip_prefix, "")
    return _drop_none({
        "asset": _require_str(asset, "asset"), "role": _require_str(role, "role"),
        "targetDir": None if target_dir is None else _require_str(target_dir, "target_dir"),
        "includePaths": selection["paths"], "stripPrefix": selection["stripPrefix"],
    })


def attach_plan_body(selections: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Body for counting what file selections of an asset would place: [{"paths", "strip_prefix"?}], in order."""
    if isinstance(selections, Mapping):
        selections = [selections]
    out = []
    for item in selections:
        if not isinstance(item, Mapping):
            raise ValueError("selections items must be mappings with 'paths' (and optionally 'strip_prefix')")
        unknown = sorted(str(key) for key in item if key not in ("paths", "strip_prefix"))
        if unknown:
            raise ValueError(f"selections[] has unknown key(s) {', '.join(unknown)}; use paths, strip_prefix")
        selection = _selection(item.get("paths"), item.get("strip_prefix"), "selections[].")
        out.append(_drop_none({"paths": selection["paths"] or [], "stripPrefix": selection["stripPrefix"]}))
    if not out:
        raise ValueError("selections needs at least one selection")
    return {"selections": out}


def serving_deploy_body(model_registration_id: int, *, price_cap_per_hour: Any, min_replicas: int,
                        max_replicas: int, autoscale: bool, max_context_tokens: int | None,
                        share_idle_capacity: bool, output_bucket_id: int | None = None) -> dict[str, Any]:
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
        "outputBucketId": _optional_int(output_bucket_id, "output_bucket_id"),
    })


def serving_scale_body(*, min_replicas: int | None, max_replicas: int | None, autoscale: bool | None,
                       price_cap_per_hour: Any, output_bucket_id: int | None = None, managed_output: bool = False,
                       share_idle_capacity: bool | None = None) -> dict[str, Any]:
    if managed_output and output_bucket_id is not None:
        raise ValueError("pass output_bucket_id or managed_output=True, not both")
    body = _drop_none({
        "minReplicas": _optional_int(min_replicas, "min_replicas", minimum=0),
        "maxReplicas": _optional_int(max_replicas, "max_replicas"),
        "autoscale": None if autoscale is None else bool(autoscale),
        "priceCapPerHourUsd": _price(price_cap_per_hour, "price_cap_per_hour"),
        "outputBucketId": _optional_int(output_bucket_id, "output_bucket_id"),
        # Only sent when true: it moves the serving's outputs back to Meshive storage.
        "managedOutput": True if managed_output else None,
        "shareIdleCapacity": _flag(share_idle_capacity, "share_idle_capacity"),
    })
    if not body:
        raise ValueError("pass at least one of min_replicas, max_replicas, autoscale, price_cap_per_hour, "
                         "output_bucket_id, managed_output, share_idle_capacity")
    return body


# --- Tasks --------------------------------------------------------------------

def _output_include(values: Any) -> list[str] | None:
    """Output filter — one glob or a list of globs, relative to /outputs. None or empty → None (not sent: every file is
    uploaded). Only the shape is checked; which patterns are valid is the server's rule."""
    if values is not None and (isinstance(values, Mapping) or not isinstance(values, (str, Iterable))):
        raise ValueError("output_include must be a glob or a list of globs, e.g. ['*.safetensors', 'samples/*.png']")
    return _strings(values, "output_include")


_TASK_INPUT_ASSET_KEYS = ("asset", "asset_id", "target_dir", "paths", "strip_prefix", "version")
_TASK_PROJECT_KEYS = ("asset", "entrypoint", "requirements_path")
TASK_DELETE_MAX_IDS = 100   # the server's per-call limit on tasks to delete


def _task_input_assets(items: Any) -> list[dict[str, Any]]:
    """Task input asset — "asset_id" or {"asset", "target_dir", "paths", "strip_prefix"}, as create_pod's items
    without "role". `paths`/`strip_prefix` pick some files of the asset the same way."""
    assets: list[dict[str, Any]] = []
    for item in _input_asset_items(items):
        if isinstance(item, str):
            assets.append({"asset": item})
            continue
        unknown = sorted(str(key) for key in item if key not in _TASK_INPUT_ASSET_KEYS)
        if unknown:
            # A misspelled key (say include_paths) would otherwise be dropped and the whole asset attached.
            raise ValueError(f"input_assets[] has unknown key(s) {', '.join(unknown)}; "
                             "use asset, target_dir, paths, strip_prefix")
        if item.get("version") is not None:
            # Assets have no versions — the server already ignored this value, so it isn't sent. 2.0 stops accepting it at all.
            warnings.warn("input_assets[].version is ignored: assets no longer have versions",
                          DeprecationWarning, stacklevel=4)
        paths, strip = item.get("paths"), item.get("strip_prefix")
        if strip is not None and not isinstance(strip, str):
            raise ValueError("input_assets[].strip_prefix must be a string ('' keeps the folder structure)")
        assets.append(_drop_none({
            "asset": _require_str(item.get("asset") or item.get("asset_id"), "input_assets[].asset"),
            "targetDir": str(item["target_dir"]) if item.get("target_dir") else None,
            "includePaths": _strings(paths, "input_assets[].paths"),
            "stripPrefix": strip,
        }))
    return assets


def _input_asset_items(items: Any) -> list[Any]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append(_require_str(item, "input_assets[]"))
        elif isinstance(item, Mapping):
            out.append(item)
        else:
            raise ValueError("input_assets items must be asset ids or mappings")
    return out


def _task_project(project: Any) -> dict[str, Any] | None:
    """{"asset", "entrypoint", "requirements_path"?} → the request's project. Which paths are valid is the server's rule."""
    if project is None:
        return None
    if not isinstance(project, Mapping):
        raise ValueError('project must be a mapping such as {"asset": "asset_...", "entrypoint": "train.py"}')
    unknown = sorted(str(key) for key in project if key not in _TASK_PROJECT_KEYS)
    if unknown:
        raise ValueError(f"project has unknown key(s) {', '.join(unknown)}; use {', '.join(_TASK_PROJECT_KEYS)}")
    requirements_path = project.get("requirements_path")
    return _drop_none({
        "asset": _require_str(project.get("asset"), "project.asset"),
        "entrypoint": _require_str(project.get("entrypoint"), "project.entrypoint"),
        "requirementsPath": None if requirements_path is None else _require_str(requirements_path,
                                                                                "project.requirements_path"),
    })


def _task_output_bucket(value: Any) -> tuple[bool, int | None]:
    """(send it, value): None → not sent (the workspace's default), "managed" → an explicit null (Meshive storage),
    a bucket id → that id."""
    if value is None:
        return False, None
    if value == "managed":
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('output_bucket must be a bucket id (list_storage_credentials) or "managed"')
    return True, value


def task_body(name: str, script: str | None, *, image: str | None, template_id: int | None, requirements: str | None,
              env: Mapping[str, Any] | None, secret_keys: Iterable[str] | None, args: Iterable[str] | None,
              gpu_model: str | None, gpu_count: int | None, gpu_vram_gb: int | None, cpu_preset: str | None,
              max_duration: int, webhook_url: str | None, input_assets: Any, max_price_per_hour: Any,
              output_include: str | Iterable[str] | None = None, project: Any = None,
              registry_login_id: int | None = None, output_bucket: Any = None) -> dict[str, Any]:
    script_text = script if isinstance(script, str) else ""
    if script is not None and not isinstance(script, str):
        raise ValueError("script must be a string (Python source)")
    project_body = _task_project(project)
    if bool(script_text.strip()) == (project_body is not None):
        raise ValueError("pass exactly one of script (Python source) or project (an uploaded folder asset)")
    if len(script_text.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ValueError(f"script exceeds {MAX_SCRIPT_BYTES // 1024} KiB; upload large code as an asset instead")
    if not image and template_id is None:
        raise ValueError("pass image or template_id")
    if registry_login_id is not None and template_id is not None:
        raise ValueError("registry_login_id is for a custom image; a template_id brings its own registry login")
    send_bucket, bucket = _task_output_bucket(output_bucket)
    if isinstance(max_duration, bool) or not isinstance(max_duration, int) or not TASK_DURATION_RANGE[0] <= max_duration <= TASK_DURATION_RANGE[1]:
        raise ValueError("max_duration must be an integer between 3600 and 86400 seconds (1h..24h)")
    if bool(gpu_model) == bool(cpu_preset):
        raise ValueError("pass exactly one of gpu_model (GPU task) or cpu_preset (CPU task)")
    env_out, secrets = _env(env, secret_keys)
    body = _drop_none({
        "name": _require_str(name, "name"), "script": script_text if project_body is None else None, "requirements": requirements or None,
        "project": project_body,
        "templateId": _optional_int(template_id, "template_id", minimum=0), "image": image or None,
        "registryLoginId": _optional_int(registry_login_id, "registry_login_id"),
        "env": env_out, "secretKeys": secrets, "args": [str(a) for a in (args or [])],
        "gpuModel": gpu_model or None, "gpuCount": _optional_int(gpu_count, "gpu_count"),
        "gpuVramGb": _optional_int(gpu_vram_gb, "gpu_vram_gb"), "cpuPreset": cpu_preset or None,
        "maxDurationS": max_duration, "webhookUrl": webhook_url or None,
        "inputAssets": _task_input_assets(input_assets),
        "maxPricePerHourUsd": _price(max_price_per_hour, "max_price_per_hour"),
        # Only files in /outputs matching these globs are uploaded. Omitted = every file (the body is unchanged).
        "outputInclude": _output_include(output_include),
    })
    if send_bucket:
        # After _drop_none: an explicit null means Meshive storage, while a missing key means the workspace default.
        body["outputBucketId"] = bucket
    return body


def task_delete_body(task_ids: str | Iterable[str]) -> dict[str, Any]:
    """{"taskIds": [...]}, duplicates dropped (order kept), 1 to TASK_DELETE_MAX_IDS ids."""
    if task_ids is None or isinstance(task_ids, Mapping) or not isinstance(task_ids, (str, Iterable)):
        raise ValueError("task_ids must be a task id or a list of task ids")
    ids = list(dict.fromkeys(_strings(task_ids, "task_ids") or []))
    if not ids:
        raise ValueError("task_ids needs at least one task id")
    if len(ids) > TASK_DELETE_MAX_IDS:
        raise ValueError(f"task_ids takes at most {TASK_DELETE_MAX_IDS} task ids per call")
    return {"taskIds": ids}


# --- Webhooks and inference keys ----------------------------------------------------

def _event_types(values: Any) -> list[str]:
    """A list of event type names. Which names exist, and whether an empty list is allowed, is the server's rule."""
    if isinstance(values, str):
        values = [values]
    if values is None or isinstance(values, Mapping) or not isinstance(values, Iterable):
        raise ValueError("event_types must be a list of event type names")
    return [_require_str(v, "event_types[]") for v in values]


def webhook_create_body(url: Any, event_types: Any, *, label: Any = None) -> dict[str, Any]:
    """POST /webhooks body {url, eventTypes, label?}."""
    body: dict[str, Any] = {"url": _require_str(url, "url"), "eventTypes": _event_types(event_types)}
    if label is not None:
        body["label"] = _require_str(label, "label")
    return body


def webhook_update_body(*, url: Any = None, label: Any = None, event_types: Any = None,
                        enabled: Any = None) -> dict[str, Any]:
    """PATCH /webhooks/{id} body: only what is given, at least one of them."""
    body: dict[str, Any] = {}
    if url is not None:
        body["url"] = _require_str(url, "url")
    if label is not None:
        body["label"] = _require_str(label, "label")
    if event_types is not None:
        body["eventTypes"] = _event_types(event_types)
    flag = _flag(enabled, "enabled")
    if flag is not None:
        body["enabled"] = flag
    if not body:
        raise ValueError("update_webhook needs at least one of url, label, event_types or enabled")
    return body


def inference_key_body(name: Any = None, expires_in_days: Any = None) -> dict[str, Any]:
    """POST /inference-keys body {name?, expiresInDays?}. Left out, the key gets the server's default name and never
    expires; 0 is refused rather than read as either."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = _require_str(name, "name")
    if expires_in_days is not None:
        if isinstance(expires_in_days, bool) or not isinstance(expires_in_days, int) or expires_in_days < 1:
            raise ValueError("expires_in_days must be a whole number of days >= 1; leave it out for a key that does "
                             "not expire")
        body["expiresInDays"] = expires_in_days
    return body


# --- Assets from a connected bucket, reference subsets, the default output bucket -------------

def _asset_type(value: Any) -> str | None:
    return _require_str(value, "asset_type").lower() if value is not None else None


def _object_key(value: Any, name: str) -> str:
    """A bucket object key or folder, sent exactly as given: spaces are valid in S3 keys, so nothing is stripped."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _object_keys(values: str | Iterable[str] | None, name: str) -> list[str]:
    if values is None:
        return []
    items = [values] if isinstance(values, str) else list(values)
    return [_object_key(v, f"{name}[]") for v in items]


def bucket_register_body(object_keys: str | Iterable[str], *, name: str, asset_type: str | None = None,
                         base_prefix: str | None = None) -> dict[str, Any]:
    """POST /storage-credentials/{id}/register body {objects: [{objectKey, relativePath?}], name, assetType?}.
    With base_prefix, a key that starts with it is stored under the rest of the key (keeps the folder layout);
    other keys, and every key without base_prefix, get the server's default (the key's last segment). How many
    objects one call may register is the server's limit."""
    keys = _object_keys(object_keys, "object_keys")
    if not keys:
        raise ValueError("object_keys needs at least one object key")
    folder = _object_key(base_prefix, "base_prefix") if base_prefix is not None else None
    if folder is not None and not folder.endswith("/"):
        folder += "/"   # a folder: "models" must not match the key "models2/a.bin"
    objects: list[dict[str, Any]] = []
    for key in keys:
        item: dict[str, Any] = {"objectKey": key}
        if folder and key.startswith(folder) and key[len(folder):]:
            item["relativePath"] = key[len(folder):]
        objects.append(item)
    return _drop_none({"objects": objects, "name": _require_str(name, "name"),
                       "assetType": _asset_type(asset_type)})


def bucket_register_prefix_body(prefix: str, *, name: str | None = None,
                                asset_type: str | None = None) -> dict[str, Any]:
    """POST /storage-credentials/{id}/register-prefix body {prefix, name?, assetType?}. The bucket's root can't be
    registered, so an empty prefix is refused."""
    folder = _object_key(prefix, "prefix")
    if not folder.strip("/ "):
        raise ValueError("prefix must name a folder in the bucket (the bucket's root can't be registered)")
    return _drop_none({"prefix": folder, "name": _require_str(name, "name") if name is not None else None,
                       "assetType": _asset_type(asset_type)})


def _patterns(values: str | Iterable[str] | None, name: str) -> list[str]:
    return _strings(values, name) or []


def reference_preview_body(include: str | Iterable[str] | None = (),
                           exclude: str | Iterable[str] | None = ()) -> dict[str, Any]:
    """POST /assets/{id}/reference/preview body {include, exclude} — globs over paths inside the reference asset;
    no include = every file."""
    return {"include": _patterns(include, "include"), "exclude": _patterns(exclude, "exclude")}


def reference_fetch_body(include: str | Iterable[str] | None = (), exclude: str | Iterable[str] | None = (),
                         object_keys: str | Iterable[str] | None = (), name: str | None = None,
                         asset_type: str | None = None) -> dict[str, Any]:
    """POST /assets/{id}/reference/fetch body {include, exclude, objectKeys, name?, assetType?}. Object keys, when
    given, pick the files and the globs are ignored (server rule)."""
    return _drop_none({**reference_preview_body(include, exclude), "objectKeys": _object_keys(object_keys, "object_keys"),
                       "name": _require_str(name, "name") if name is not None else None,
                       "assetType": _asset_type(asset_type)})


def asset_storage_setting_body(credential_id: Any) -> dict[str, Any]:
    """PUT /workspaces/{ws}/asset-storage body {bucketId}: a connected bucket's credential_id, or None for Meshive
    storage (sent as an explicit null)."""
    if credential_id is None:
        return {"bucketId": None}
    if isinstance(credential_id, bool) or not isinstance(credential_id, int) or credential_id < 0:
        raise ValueError("credential_id must be a non-negative integer (a credential_id from "
                         "list_storage_credentials), or None for Meshive storage")
    return {"bucketId": credential_id}


# --- Account ------------------------------------------------------------------------

def profile_body(username: Any) -> dict[str, Any]:
    """PATCH /me body {username}: the name is stripped and must not be blank. Its length and characters are the
    server's rule (its 422 'Invalid Name' says what is allowed)."""
    return {"username": _require_str(username, "username")}


# --- Host machines ----------------------------------------------------------------

# The kinds of machine login reveal_machine_credentials can reveal, and the agents issue_install_command installs.
MACHINE_CREDENTIAL_KINDS = ("ssh", "ipmi", "grafana")
AGENT_TYPES = ("compute", "storage")


def listing_method(listed: Any) -> str:
    """set_machine_listing: POST …/listing lists the machine, DELETE unlists it. Only a real bool is taken, so a string
    such as "false" can't list a machine by accident."""
    if not isinstance(listed, bool):
        raise ValueError("listed must be an explicit boolean (True to list the machine, False to unlist it)")
    return "POST" if listed else "DELETE"


def _when(value: Any, name: str) -> str:
    """A maintenance time — a datetime or an ISO 8601 string. Whether it is valid and far enough ahead is the server's
    call; a time without an offset is read as UTC there."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{name} must be a datetime or an ISO 8601 string, e.g. 2026-11-20T09:00:00Z")


def _text(value: Any, name: str) -> str:
    """Optional free text (a description, a message body): any string, "" included."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def maintenance_body(*, reason: str, start: Any, end: Any, description: str = "",
                     emergency: bool = False) -> dict[str, Any]:
    """POST …/maintenance (and its estimate) body. A reason is required, as in the console. The 48-hour notice for a
    regular window, overlaps and the machine's state are the server's checks."""
    if not isinstance(emergency, bool):
        raise ValueError("emergency must be an explicit boolean")   # an emergency window counts as downtime
    return {"reason": _require_str(reason, "reason"), "description": _text(description, "description"),
            "startTime": _when(start, "start"), "endTime": _when(end, "end"), "isEmergency": emergency}


def maintenance_update_body(*, reason: str | None = None, description: str | None = None, start: Any = None,
                            end: Any = None) -> dict[str, Any]:
    """PATCH /maintenance/{id} body — only what is given is sent; the rest of the window stays as it is."""
    body = _drop_none({
        "reason": None if reason is None else _require_str(reason, "reason"),
        "description": None if description is None else _text(description, "description"),
        "startTime": None if start is None else _when(start, "start"),
        "endTime": None if end is None else _when(end, "end"),
    })
    if not body:
        raise ValueError("pass at least one of reason, description, start, end")
    return body


def maintenance_extend_body(*, hours: Any, reason: str, description: str = "") -> dict[str, Any]:
    """POST /maintenance/{id}/extend body. The upper limit on hours is the server's."""
    if isinstance(hours, bool) or not isinstance(hours, (int, float, Decimal)):
        raise ValueError("hours must be a number of hours greater than 0")
    amount = float(hours)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("hours must be a number of hours greater than 0")
    return {"hours": amount, "reason": _require_str(reason, "reason"), "description": _text(description, "description")}


def host_message_body(*, to: str, title: str, content: str = "") -> dict[str, Any]:
    """POST …/messages body. to is a workspace ID or "all"; who may be sent to and the length limits are the server's."""
    return {"to": _require_str(to, "to"), "title": _require_str(title, "title"), "content": _text(content, "content")}


FEEDBACK_KINDS = ("bug", "idea", "complaint")


def feedback_body(*, summary: str, kind: str | None = None, details: str | None = None, tool: str | None = None,
                  error_code: str | None = None) -> dict[str, Any]:
    """POST /feedback body. The length limits (a one-line summary under 200 characters, 4000 with the details) are the
    server's; so is which `tool` / `error_code` values it keeps."""
    body: dict[str, Any] = {"summary": _require_str(summary, "summary")}
    if kind is not None:
        value = kind.strip().lower() if isinstance(kind, str) else ""
        if value not in FEEDBACK_KINDS:
            raise ValueError(f"kind must be one of {', '.join(FEEDBACK_KINDS)}")
        body["kind"] = value
    for key, value, name in (("details", details, "details"), ("tool", tool, "tool"),
                             ("errorCode", error_code, "error_code")):
        if value is not None:
            text = _text(value, name)
            if text.strip():
                body[key] = text
    return body


def message_ids_body(message_ids: Any) -> dict[str, Any]:
    """POST …/messages/read body. How many ids one request takes is the server's rule."""
    items = [message_ids] if isinstance(message_ids, (int, str)) else list(message_ids or [])
    ids = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("message_ids must be message ids (non-negative integers)")
        ids.append(item)
    if not ids:
        raise ValueError("pass at least one message id")
    return {"messageIds": ids}


def credentials_body(kind: str) -> dict[str, Any]:
    value = kind.strip().lower() if isinstance(kind, str) else ""
    if value not in MACHINE_CREDENTIAL_KINDS:
        raise ValueError(f"kind must be one of {', '.join(MACHINE_CREDENTIAL_KINDS)}")
    return {"kind": value}


def install_command_body(agent_type: str) -> dict[str, Any]:
    value = agent_type.strip().lower() if isinstance(agent_type, str) else ""
    if value not in AGENT_TYPES:
        raise ValueError(f"agent_type must be one of {', '.join(AGENT_TYPES)}")
    return {"agentType": value}


def host_messages_params(machine_id: str | None, unread_only: bool) -> dict[str, Any]:
    if not isinstance(unread_only, bool):
        raise ValueError("unread_only must be an explicit boolean")
    params: dict[str, Any] = {"unreadOnly": unread_only}
    if machine_id is not None:
        params["machineId"] = _require_str(machine_id, "machine_id")
    return params


def message_to_host_body(*, title: str, content: str = "", pod: str | None = None,
                         machine_id: str | None = None) -> dict[str, Any]:
    """POST /workspaces/{workspace}/host-messages body — a renter writing to the host of one machine, named by the
    pod that runs on it or by the machine's id. Exactly one of them. Whether the workspace may write to that host and
    the length limits are the server's rules."""
    if (pod is None) == (machine_id is None):
        raise ValueError("pass exactly one of pod and machine_id")
    body: dict[str, Any] = {"title": _require_str(title, "title"), "content": _text(content, "content")}
    if pod is not None:
        body["podName"] = _require_str(pod, "pod")
    else:
        body["machineId"] = _require_str(machine_id, "machine_id")
    return body


# How a GPU machine's fans are driven: Meshive's default curve, a custom curve, or left to the NVIDIA driver.
FAN_CURVE_MODES = ("default", "custom", "driver")


def _fan_value(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _fan_point(point: Any) -> dict[str, int]:
    """One curve point: a (temperature, fan_percent) pair or a dict with temperature and fan_percent (or fanPercent)."""
    if isinstance(point, Mapping):
        percent = point.get("fan_percent", point.get("fanPercent"))
        temperature = point.get("temperature")
    elif isinstance(point, (list, tuple)) and len(point) == 2:
        temperature, percent = point
    else:
        raise ValueError("each point must be a (temperature, fan_percent) pair or a dict with temperature and "
                         "fan_percent")
    return {"temperature": _fan_value(temperature, "point temperature"),
            "fanPercent": _fan_value(percent, "point fan_percent")}


def fan_curve_body(mode: Any, points: Any = None) -> dict[str, Any]:
    """PUT …/fan-curve (and its estimate) body {mode, points}. A custom curve needs its points; with another mode the
    points given are kept as the saved custom curve. The number of points, the temperature and fan ranges and the
    curve's shape are the server's rules (its 400 'Invalid fan curve' says what is wrong)."""
    value = mode.strip().lower() if isinstance(mode, str) else ""
    if value not in FAN_CURVE_MODES:
        raise ValueError(f"mode must be one of {', '.join(FAN_CURVE_MODES)}")
    if points is None:
        if value == "custom":
            raise ValueError("a custom fan curve needs its points")
        return {"mode": value, "points": None}
    if isinstance(points, (str, bytes, Mapping)) or not isinstance(points, Iterable):
        raise ValueError("points must be a list of (temperature, fan_percent) pairs")
    return {"mode": value, "points": [_fan_point(point) for point in points]}


# --- Image inspection and custom templates -------------------------------------

class _Unset:
    """The default of an argument whose None has a meaning of its own (update_template's registry_login_id: None
    detaches the login, leaving it out keeps it)."""

    _instance: "_Unset | None" = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()


def image_inspect_body(image: str, *, registry_login_id: int | None = None,
                       platform: str = "linux/amd64") -> dict[str, Any]:
    """POST /images/inspect body. platform is "os/arch" ("linux/amd64"); which ones exist is the server's list."""
    if not isinstance(platform, str) or platform.count("/") != 1:
        raise ValueError('platform must be "os/arch", e.g. "linux/amd64"')
    os_name, arch = (part.strip() for part in platform.split("/"))
    if not os_name or not arch:
        raise ValueError('platform must be "os/arch", e.g. "linux/amd64"')
    body: dict[str, Any] = {"image": _require_str(image, "image"), "platformOs": os_name, "platformArch": arch}
    login = _optional_int(registry_login_id, "registry_login_id")
    if login is not None:
        body["registryLoginId"] = login
    return body


def _item_value(item: Any, *names: str) -> Any:
    """The first of `names` a mapping has (or an object has as an attribute) that isn't None."""
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if value is not None:
            return value
    return None


def _item_flag(item: Any, name: str, *names: str) -> bool | None:
    value = _item_value(item, *names)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _template_envs(items: Any) -> list[dict[str, Any]]:
    """[{"key", "value"?, "secret"?, "auto_generate"?, "show_on_connect"?, "from_image"?}] or TemplateEnv objects (as
    get_template returns them). A secret without a value keeps the value the template stores for that key; with
    auto_generate each pod gets a random one."""
    if isinstance(items, (str, Mapping)) or not isinstance(items, Iterable):
        raise ValueError("envs must be a list of {'key': ..., 'value': ...} items")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) and not hasattr(item, "key"):
            raise ValueError("envs items must be mappings like {'key': 'HF_HOME', 'value': '/data/hf'}")
        key = _require_str(_item_value(item, "key"), "envs[].key")
        if key in seen:
            raise ValueError(f"envs has the key {key!r} twice")
        seen.add(key)
        entry: dict[str, Any] = {"key": key}
        value = _item_value(item, "value")
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                raise ValueError("envs[].value must be a string")
            entry["value"] = str(value)
        entry["isSecret"] = bool(_item_flag(item, "envs[].secret", "secret", "is_secret"))
        entry["isAutoGenerated"] = bool(_item_flag(item, "envs[].auto_generate", "auto_generate", "auto_generated"))
        entry["showOnConnect"] = bool(_item_flag(item, "envs[].show_on_connect", "show_on_connect"))
        from_image = _item_flag(item, "envs[].from_image", "from_image")
        if from_image is not None:
            entry["isBase"] = from_image
        out.append(entry)
    return out


def _template_port_list(items: Any, lock: bool) -> list[dict[str, Any]]:
    """[{"port", "name", "external"?, "locked"?}] or TemplatePort objects. A public port without its own `locked`
    gets `lock`; an internal port never gets a lock value (it has no public URL)."""
    if isinstance(items, (str, Mapping)) or not isinstance(items, Iterable):
        raise ValueError("ports must be a list of {'port': ..., 'name': ...} items")
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, Mapping) and not hasattr(item, "port"):
            raise ValueError("ports items must be mappings like {'port': 8888, 'name': 'jupyter', 'external': True}")
        port = _port_number(_item_value(item, "port"), "ports[].port")
        if port in seen:
            raise ValueError(f"ports has the port {port} twice")
        seen.add(port)
        external = bool(_item_flag(item, "ports[].external", "external"))
        entry: dict[str, Any] = {"port": port, "name": _require_str(_item_value(item, "name"), "ports[].name"),
                                 "external": external}
        if external:
            locked = _item_flag(item, "ports[].locked", "locked")
            entry["locked"] = lock if locked is None else locked
        out.append(entry)
    return out


def _template_mounts(items: Any) -> list[dict[str, str]]:
    """[{"path", "name"}] (or "mount_path") or TemplateVolumeMount objects: folders that should be on a volume."""
    if isinstance(items, (str, Mapping)) or not isinstance(items, Iterable):
        raise ValueError("volume_mounts must be a list of {'path': ..., 'name': ...} items")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) and not hasattr(item, "mount_path"):
            raise ValueError("volume_mounts items must be mappings like {'path': '/workspace', 'name': 'work'}")
        path = _require_str(_item_value(item, "path", "mount_path"), "volume_mounts[].path")
        if path in seen:
            raise ValueError(f"volume_mounts has the path {path!r} twice")
        seen.add(path)
        out.append({"name": _require_str(_item_value(item, "name"), "volume_mounts[].name"), "mountPath": path})
    return out


_SEMANTIC_PATH_KEYS = ("role", "path", "expect", "include", "required", "cardinality", "placement")


def _template_semantic_paths(items: Any) -> list[dict[str, Any]]:
    """[{"role", "path", "expect"?, "include"?, "required"?, "cardinality"?, "placement"?}] or TemplateSemanticPath
    objects; [] clears them. Only the keys given are sent; the allowed roles and values are the server's."""
    if isinstance(items, (str, Mapping)) or not isinstance(items, Iterable):
        raise ValueError("semantic_paths must be a list of {'role': ..., 'path': ...} items")
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) and not hasattr(item, "role"):
            raise ValueError("semantic_paths items must be mappings like {'role': 'model', 'path': '/models'}")
        entry: dict[str, Any] = {"role": _require_str(_item_value(item, "role"), "semantic_paths[].role"),
                                 "path": _require_str(_item_value(item, "path"), "semantic_paths[].path")}
        for key in ("expect", "cardinality", "placement"):
            value = _item_value(item, key)
            if value is not None:
                entry[key] = _require_str(value, f"semantic_paths[].{key}")
        include = _item_value(item, "include")
        if include is not None:
            entry["include"] = _strings(include, "semantic_paths[].include") or []
        required = _item_flag(item, "semantic_paths[].required", "required")
        if required is not None:
            entry["required"] = required
        out.append(entry)
    return out


def template_body(*, name: Any = None, image: Any = None, description: Any = None, command: Any = None,
                  envs: Any = None, ports: Any = None, volume_mounts: Any = None, semantic_paths: Any = None,
                  registry_login_id: Any = UNSET, lock_public_ports: bool = True) -> dict[str, Any]:
    """POST /templates (with image) or PATCH /templates/{id} (without) body — only what is given is sent. Each list
    replaces the stored one. registry_login_id None sends null (detach), UNSET leaves it out. Limits (port count,
    name length, reserved env names) are the server's."""
    if not isinstance(lock_public_ports, bool):
        raise ValueError("lock_public_ports must be an explicit boolean")
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = _require_str(name, "name")
    if image is not None:
        body["image"] = _require_str(image, "image")
    if description is not None:
        body["description"] = _text(description, "description")
    if command is not None:
        body["command"] = _text(command, "command")
    if envs is not None:
        body["envs"] = _template_envs(envs)
    if ports is not None:
        body["ports"] = _template_port_list(ports, lock_public_ports)
    if volume_mounts is not None:
        body["volumeMounts"] = _template_mounts(volume_mounts)
    if semantic_paths is not None:
        body["semanticPaths"] = _template_semantic_paths(semantic_paths)
    if registry_login_id is not UNSET:
        body["registryLoginId"] = _optional_int(registry_login_id, "registry_login_id")
    return body


def template_port_locks(body: Mapping[str, Any]) -> dict[int, bool]:
    """{port: locked} of the public ports a template body sends."""
    return {p["port"]: p["locked"] for p in body.get("ports") or [] if "locked" in p}


# --- Logs ---------------------------------------------------------------------

def logs_params(*, tail: int, wait: float | None, container: str | None = None,
                cursor: int | None = None) -> dict[str, Any]:
    """cursor applies when a response gave next_cursor: None/0 = last tail lines, a previous response's next_cursor = the increment after it."""
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
