"""CLI write commands: pod-create/update/stop/start/restart/delete, storage-create/update/connect/delete,
serving-deploy/scale/pause/resume/delete, task-submit/stop, logs, task-logs.
Also models, hf-tokens, model-detect, model-register, model-delete (model registration for serving), asset-import, civitai-keys.

Rule: commands that cost money or can't be undone (create/start/deploy/submit/delete, plus serving-scale and
serving-resume, which can raise cost) first show an estimate or summary and ask for confirmation without `--yes` (exit 2 if not a TTY).
`--estimate` shows the estimate and stops. `pod-create --wait` exits 1 if the pod isn't ready in time or its creation fails
(the accepted transaction is printed, and a failure's reason).
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import warnings
from pathlib import Path
from typing import Any, Callable

import httpx

from . import _format as fmt
from .._client import LONG_RUNNING_READ_TIMEOUT, Meshive
from ..exceptions import ConflictError, MeshiveAPIError, MeshiveError, PodCreationFailedError, WaitTimeoutError
from ..models import (Logs, PodEstimate, PodUpdated, PodUpdateEstimate, ResourceAction, StorageConnection,
                      StorageEstimate, StorageUpdateEstimate, TaskEstimate)

Handler = Callable[[Meshive, argparse.Namespace, str, bool], int]


def _emit(output: str, raw: object, ids: list[str], show: Callable[[], None]) -> None:
    if output == "json":
        print(json.dumps(raw, indent=2, ensure_ascii=False))
    elif output == "name":
        for value in ids:
            print(fmt.clean(value))
    else:
        show()


def _emit_estimate(output: str, estimate: PodEstimate | StorageEstimate | TaskEstimate | PodUpdateEstimate | StorageUpdateEstimate,
                   show: Callable[[], None]) -> None:
    """`--estimate` output. json is the server payload only, table is the estimate table, name is one headline number
    like credit and asset-storage — the hourly estimate with the table's rounding (3 decimals), without `$` or thousands separators. Not printed when the unit price is unknown (CPU-preset tasks)."""
    ids: list[str] = []
    if output == "name":
        price = fmt.money_hourly(estimate.price_per_hour)
        if price != "-":
            ids.append(price.replace("$", "").replace(",", ""))
    _emit(output, estimate.raw, ids, show)


def _confirm(args: argparse.Namespace, question: str) -> bool:
    """Passes with --yes. Otherwise asks on a TTY; non-interactive runs print a hint and decline."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("Error: this command spends credit or deletes resources. Re-run with --yes to confirm.",
              file=sys.stderr)
        return False
    # Ask on stderr — when stdout isn't a terminal, input() writes the prompt to stdout, so with `-o json > out.json`, `| jq`
    # or `id=$(… -o name)` the prompt ends up in the result and the person never sees it. Flush stdout before asking, as input() does —
    # in a pipe it's block-buffered and the estimate table printed earlier would land after the prompt (flush errors are ignored like input() does). EOF without an answer declines.
    try:
        sys.stdout.flush()
    except OSError:
        pass
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _kv(pairs: list[tuple[str, str]]) -> None:
    width = max(len(k) for k, _ in pairs)
    for key, value in pairs:
        print(f"{key + ':':<{width + 2}}{fmt.clean(str(value))}")


def _parse_env(values: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--env expects KEY=VALUE, got {item!r}")
        env[key.strip()] = value
    return env


def _parse_volumes(values: list[str] | None) -> list[tuple[str, str]]:
    out = []
    for item in values or []:
        pv, sep, mount = item.partition(":")
        if not sep or not pv.strip() or not mount.startswith("/"):
            raise ValueError(f"--volume expects PV_NAME:/mount/path, got {item!r}")
        out.append((pv.strip(), mount))
    return out


def _parse_local_volumes(values: list[str] | None) -> list[dict[str, Any]]:
    """NAME:/PATH:SIZE_GB[:encrypted] — new local storage created with the pod. Limits (such as the minimum size) are the server's."""
    out = []
    for item in values or []:
        parts = item.split(":")
        flag = parts[3].strip().lower() if len(parts) == 4 else ""
        valid = len(parts) in (3, 4) and parts[0].strip() and parts[1].startswith("/") and flag in ("", "encrypted")
        try:
            size = int(parts[2]) if valid else None
        except ValueError:
            size = None
        if size is None:
            raise ValueError(f"--local-volume expects NAME:/PATH:SIZE_GB[:encrypted], got {item!r}")
        entry: dict[str, Any] = {"name": parts[0].strip(), "mount_path": parts[1], "size_gb": size}
        if flag:
            entry["encrypted"] = True
        out.append(entry)
    return out


def _deprecated_flag(flag: str, value: Any, why: str) -> None:
    """A flag that is still accepted but no longer does anything — say so instead of silently dropping it."""
    if value:
        print(f"Warning: {flag} is deprecated and ignored ({why}); it will be removed in 2.0.", file=sys.stderr)


def _parse_ports(values: list[str] | None) -> list[dict[str, Any]]:
    """8888 | 8888:jupyter | 8888:jupyter:internal"""
    out = []
    for item in values or []:
        parts = item.split(":")
        try:
            port = int(parts[0])
        except ValueError:
            raise ValueError(f"--port expects PORT[:NAME[:internal]], got {item!r}") from None
        entry: dict[str, Any] = {"port": port, "external": not (len(parts) > 2 and parts[2] == "internal")}
        if len(parts) > 1 and parts[1]:
            entry["name"] = parts[1]
        out.append(entry)
    return out


_TEMPLATE_PORT_USAGE = ("--template-port expects PORT[:NAME][:NEW_PORT][:internal|external], "
                        "e.g. 8080:ide, 8080::8443 or 8080:::internal")


def _parse_template_ports(values: list[str] | None) -> list[dict[str, Any]]:
    """PORT[:NAME][:NEW_PORT][:internal|external] — change a port the template opens. The fields are positional and an
    empty one keeps what the template has: 8080:ide renames, 8080::8443 renumbers, 8080:::internal hides,
    8080:ide:8443:external does all three. A NAME of only digits, or internal/external, is refused: it would read as
    another field, so the error shows where that field goes."""
    out = []
    for item in values or []:
        parts = [part.strip() for part in item.split(":")]
        if len(parts) > 4:
            raise ValueError(f"{_TEMPLATE_PORT_USAGE}; got {item!r}")
        port_text, name, new_port_text, visibility = parts + [""] * (4 - len(parts))
        try:
            entry: dict[str, Any] = {"port": int(port_text)}
            new_port = int(new_port_text) if new_port_text else None
        except ValueError:
            raise ValueError(f"{_TEMPLATE_PORT_USAGE}; got {item!r}") from None
        if name.isdigit():
            raise ValueError(f"--template-port {item!r} would rename the port to {name!r}; "
                             f"to give it the number {name}, write {port_text}::{name}")
        if name.lower() in ("internal", "external"):
            raise ValueError(f"--template-port {item!r} would rename the port to {name!r}; "
                             f"to make it {name.lower()}, write {port_text}:::{name.lower()}")
        if visibility.lower() not in ("", "internal", "external"):
            raise ValueError(f"{_TEMPLATE_PORT_USAGE}; got {item!r}")
        if name:
            entry["name"] = name
        if new_port is not None:
            entry["new_port"] = new_port
        if visibility:
            entry["external"] = visibility.lower() == "external"
        out.append(entry)
    return out


def _usage_warning(value: str | bool | None) -> dict[str, Any]:
    """storage-create --usage-warning [PERCENT] → create_storage arguments. The bare flag (True) turns the warning on
    at the server's default threshold (90%); PERCENT (1-100, as the console shows it) becomes the 0-1 threshold."""
    if value is None:
        return {}
    if value is True:
        return {"usage_warning": True}
    try:
        percent = int(value.strip().rstrip("%"))
    except ValueError:
        percent = 0
    if not 1 <= percent <= 100:
        raise ValueError(f"--usage-warning expects a whole percentage from 1 to 100 (e.g. 90), got {value!r}")
    return {"usage_warning": True, "usage_warning_threshold": percent / 100}


def _parse_input_assets(values: list[str] | None) -> list[dict[str, Any]]:
    out = []
    for item in values or []:
        asset, sep, _ = item.partition(":")
        if sep:
            # ASSET_ID:VERSION, accepted by earlier releases — assets have no versions and the server ignored it. Only warn so scripts don't break.
            print(f"Warning: {item!r}: assets no longer have versions; using {asset.strip()!r}.", file=sys.stderr)
        out.append({"asset": asset.strip()})
    return out


# =============================================================================
# Output
# =============================================================================

def _print_pod_estimate(est: PodEstimate, color: bool) -> None:
    res = est.resources
    hw = (f"{res.get('gpu_count')}× {res.get('gpu_model')} ({res.get('vram_gb')} GB)" if res.get("gpu_model")
          else f"CPU ({res.get('cpu_model', '-')})")
    _kv([("estimate", fmt.paint(f"{fmt.money_hourly(est.price_per_hour)}/hr", "green", color)),
         ("hardware", hw),
         ("vcpu / ram", f"{res.get('vcpu')} vCPU / {res.get('ram_gb')} GiB"),
         ("disk", f"{res.get('disk_gb')} GiB"),
         ("rental", str(res.get("rental_type", "-"))),
         ("template", f"{est.template.get('name', '-')} (#{est.template.get('id', '-')})"),
         ("breakdown", ", ".join(f"{k}={fmt.money_hourly(v)}" for k, v in est.breakdown.items()) or "-"),
         ("available", str(est.availability.get("available_gpus", est.availability.get("machine_count", "-"))))])
    if est.volumes:
        print(fmt.clean("volumes:   " + ", ".join(_volume_cell(v) for v in est.volumes if isinstance(v, dict))))
    if est.model_set:
        print(fmt.clean(f"model set: {_model_set_cell(est.model_set)}"))
    print(fmt.paint(est.note, "dim", color))


def _model_set_cell(model_set: dict[str, Any]) -> str:
    """'SDXL basics (sdxl-basics): 3 files, 9.50 GiB, min VRAM ~12 GB' from the estimate's modelSet."""
    label = str(model_set.get("name") or model_set.get("slug") or "-")
    if model_set.get("slug") and model_set.get("name"):
        label += f" ({model_set['slug']})"
    parts = []
    if model_set.get("file_count") is not None:
        parts.append(f"{model_set['file_count']} files")
    if model_set.get("total_size_bytes") is not None:
        parts.append(fmt.bytes_human(model_set["total_size_bytes"]))
    if model_set.get("min_vram_gb") is not None:
        parts.append(f"min VRAM {_vram_gb(model_set['min_vram_gb'], model_set.get('min_vram_estimated'))}")
    return f"{label}: {', '.join(parts)}" if parts else label


def _vram_gb(value: Any, estimated: Any = None) -> str:
    """A minimum VRAM in GB, '~' in front when it is an estimate."""
    try:
        number = f"{float(value):g}"
    except (TypeError, ValueError):
        return "-"
    return f"{'~' if estimated else ''}{number} GB"


def _volume_cell(volume: dict[str, Any]) -> str:
    """An attached storage as 'pv→/mount'; a local volume created with the pod as 'name (new local, 20 GiB)→/mount'."""
    if volume.get("new"):
        extra = ", encrypted" if volume.get("encrypted") else ""
        return f"{volume.get('name')} (new local, {volume.get('size_gb')} GiB{extra})→{volume.get('mount_path')}"
    return f"{volume.get('storage')}→{volume.get('mount_path')}"


def _print_action(action: ResourceAction, color: bool) -> None:
    pairs = [(action.resource, action.id), ("action", action.action)]
    if action.placement:       # pod-start: the placement used, which the server picks when none was given
        pairs.append(("placement", action.placement))
    _kv(pairs + [("accepted", fmt.yes_no(action.accepted)),
                 ("result", json.dumps(action.result) if action.result not in (None, "") else "-")])
    if action.placement and action.placement.lower().startswith("wait_"):
        print(fmt.paint("The pod waits until a node has room for it, then starts.", "dim", color))


def _print_logs(logs: Logs, color: bool) -> None:
    if not logs.lines:
        print(fmt.paint(logs.note or "No logs.", "dim", color))
        return
    for item in logs.lines:
        print(fmt.clean(item.line))
    footer = f"— {logs.count} line(s), source={logs.source}"
    if logs.truncated:
        footer += ", truncated"
    if logs.finished:
        footer += ", finished"
    print(fmt.paint(footer, "dim", color), file=sys.stderr)


# =============================================================================
# Pods
# =============================================================================

def _pod_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    # --region is accepted but not passed on: the server ignores it (cmd_pod_create prints the deprecation note).
    return dict(
        gpu_model=args.gpu, gpu_count=args.gpu_count, gpu_vram_gb=args.vram,
        rental_type="spot" if args.spot else "demand", vcpu=args.vcpu, ram_gb=args.ram, cpu_line=args.cpu_line,
        volumes=_parse_volumes(args.volume), local_volumes=_parse_local_volumes(args.local_volume) or None,
        env=_parse_env(args.env), secret_keys=args.secret or [],
        ports=_parse_ports(args.port), template_ports=_parse_template_ports(args.template_port) or None,
        command=args.overwrite_command, internet_premium=args.internet_premium,
        uptime_premium=args.uptime_premium, cpu_premium=args.cpu_premium,
        max_price_per_hour=args.max_price,
        input_assets=[_pod_input_asset(v) for v in args.pod_input_asset or []] or None,
        watched_folders=args.watch or None, model_set=args.model_set,
        watched_template_folders=_template_folder_settings(args.watch_off, args.watch_move) or None,
        hf_token_id=args.hf_token,
    )


def _template_folder_settings(watch_off: list[str] | None, watch_move: list[str] | None) -> dict[str, Any]:
    """--watch-off PATH and --watch-move PATH=NEW_PATH → watched_template_folders."""
    settings: dict[str, Any] = {path: {"enabled": False} for path in watch_off or []}
    for value in watch_move or []:
        old, sep, new = value.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise ValueError(f"--watch-move expects TEMPLATE_PATH=NEW_PATH, got {value!r}")
        settings.setdefault(old.strip(), {})["path"] = new.strip()
    return settings


def _pod_input_asset(value: str) -> Any:
    """ASSET_ID or ASSET_ID=/target/dir."""
    asset, sep, target = value.partition("=")
    return {"asset": asset.strip(), "target_dir": target.strip()} if sep and target.strip() else asset.strip()


def cmd_ssh(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    """Prints only the connect command and one-time password — does not run ssh for you."""
    access = client.ssh_access(args.pod_name, args.workspace)

    def show() -> None:
        _kv([("command", access.command), ("password", access.password),
             ("expires", fmt.relative_time(access.expires_at) if access.expires_at else "-")])
        if access.web_url:
            print(fmt.paint(f"Browser terminal: {fmt.clean(access.web_url)}", "dim", color))
        print(fmt.paint("One-time password — run the command and paste it when asked.", "dim", color))

    _emit(output, access.raw, [access.command], show)
    return 0


def cmd_pod_watch(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    """Show or change watched folders — read the current settings, edit them, then replace everything at that version (409 if changed elsewhere)."""
    current = client.get_watched_folders(args.pod_name, args.workspace)
    changing = args.add or args.remove or args.on or args.off
    if changing:
        if current.folders is None or current.revision is None:
            print(f"Error: this pod's watched folders can't be changed now ({current.editable_reason or 'unknown'}).",
                  file=sys.stderr)
            return 1
        if current.editable_reason == "no_sidecar" and (args.add or args.on):
            hint = (f"run `meshive pod-update {args.workspace} {args.pod_name} --enable-folder-saving` first "
                    "(the pod restarts once)" if current.can_enable_saving
                    else "attach storage with `meshive pod-update --volume` or `--local-volume` first")
            print(f"Error: this pod has no uploader to watch folders — {hint}.", file=sys.stderr)
            return 1
        template = {f.path: {"enabled": f.enabled, "include": f.include_override}
                    for f in current.folders if f.origin == "template"}
        user = [{"path": f.path, "include": f.include_override, "enabled": f.enabled}
                for f in current.folders if f.origin == "user"]
        for path in args.on or []:
            _toggle(template, user, path, True)
        for path in args.off or []:
            _toggle(template, user, path, False)
        user = [u for u in user if u["path"] not in set(args.remove or [])]
        for path in args.add or []:
            user.append({"path": path, "include": args.include or None, "include_existing": args.existing})
        if (args.add or args.on) and not _confirm(args, f"Watch more folders on {args.pod_name}? "
                                                        "New files there are uploaded as assets (managed storage is billed)."):
            return 2
        current = client.set_watched_folders(args.pod_name, args.workspace, expected_version=current.revision,
                                             template=template, user=user)

    def show() -> None:
        if current.folders is None:
            print(f"Watched folders can't be read now ({current.editable_reason or 'unknown'}).")
            return
        if not current.folders:
            print("No watched folders.")
        else:
            fmt.render_table(
                ["FOLDER", "FROM", "STATE", "FILES"],
                [[f.path, f.origin, "blocked (network storage)" if f.blocked_reason else ("on" if f.enabled else "off"),
                  ", ".join(f.include_override if f.include_override is not None else f.include) or "all"]
                 for f in current.folders], enabled=color)
        if current.restart_hint:
            print(fmt.paint("Restart the pod to apply the change.", "yellow", color))
        elif not current.editable and current.editable_reason:
            print(fmt.paint(f"Can't be changed now: {current.editable_reason.replace('_', ' ')}.", "dim", color))

    _emit(output, current.raw, [f.path for f in current.folders or []], show)
    return 0


def _toggle(template: dict[str, Any], user: list[dict[str, Any]], path: str, enabled: bool) -> None:
    if path in template:
        template[path]["enabled"] = enabled
        return
    for folder in user:
        if folder["path"] == path:
            folder["enabled"] = enabled
            return
    raise ValueError(f"{path} is not a watched folder of this pod (use --add for a new one)")


_MODEL_SET_ERRORS = ("Model Set Not For Template", "Model Set Unavailable")
# A gated model set (Hugging Face license) refused at create: the token is missing, its account hasn't accepted the
# license (or isn't approved yet), or the token itself was rejected.
_HF_TOKEN_ERRORS = ("Hugging Face token required", "License not accepted", "Access not approved",
                    "Hugging Face token rejected", "Token Not Found", "Model Set Needs a Token")


def _error_detail(err: MeshiveAPIError) -> dict[str, Any]:
    detail = err.raw.get("detail") if isinstance(err.raw, dict) else None
    return detail if isinstance(detail, dict) else {}


def _explain_model_set_error(err: MeshiveAPIError, args: argparse.Namespace) -> bool:
    """A refused model set: the server's error plus what it names — the sets the template offers, or the files that
    couldn't be fetched. False for other errors."""
    if err.title in _HF_TOKEN_ERRORS:
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        print(f"Accept the model's license on Hugging Face with the account the token belongs to, then pass "
              f"`--hf-token ID` (see `meshive hf-tokens {args.workspace}`).", file=sys.stderr)
        return True
    if err.title not in _MODEL_SET_ERRORS:
        return False
    detail = _error_detail(err)
    print(fmt.clean(f"Error: {err}"), file=sys.stderr)
    if err.title == "Model Set Not For Template":
        slugs = [str(a.get("slug") if isinstance(a, dict) else a) for a in detail.get("available") or [] if a]
        if slugs:
            print(fmt.clean(f"This template's model sets: {', '.join(slugs)}."), file=sys.stderr)
        print(f"See `meshive model-sets {args.workspace} --template {args.template}`.", file=sys.stderr)
    else:
        for item in detail.get("failed") or []:
            if isinstance(item, dict):
                print(fmt.clean(f"  {item.get('name') or item.get('slug') or '?'}: {item.get('error') or 'failed'}"),
                      file=sys.stderr)
        print("The pod was not created. Files already fetched stay in the workspace and are reused next time.",
              file=sys.stderr)
    return True


def cmd_pod_create(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    try:
        return _pod_create(client, args, output, color)
    except MeshiveAPIError as err:
        if not _explain_model_set_error(err, args):
            raise
        return 1


def _pod_create(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    kwargs = _pod_kwargs(args)
    _deprecated_flag("--region", args.region, "pods are placed without a region")
    estimate = client.estimate_pod(args.name, args.template, workspace=args.workspace, **kwargs)
    if args.estimate:
        _emit_estimate(output, estimate, lambda: _print_pod_estimate(estimate, color))
        return 0
    if output == "table":      # show the estimate before asking — not mixed into json/name output
        _print_pod_estimate(estimate, color)
    if not _confirm(args, f"Create pod '{args.name}' at {fmt.money_hourly(estimate.price_per_hour)}/hr?"):
        return 2
    fetches_set = bool(args.model_set or args.hf_token is not None)
    if fetches_set:
        print("Fetching the model set's files before the pod is created; this can take several minutes.",
              file=sys.stderr)
    try:
        created = client.create_pod(args.name, args.template, workspace=args.workspace, **kwargs)
    except httpx.TimeoutException:
        if not fetches_set:
            raise
        # The SDK doesn't resend this create after a timeout: the server may still be fetching files for it.
        waited = max(args.timeout, LONG_RUNNING_READ_TIMEOUT)
        print(fmt.clean(f"Error: no answer within {waited:g}s while the server fetched the model set's files. The pod "
                        f"may still be created: check `meshive pods {args.workspace}` and "
                        f"`meshive transactions {args.workspace}` before trying again."), file=sys.stderr)
        return 1
    pod_name, timed_out, failed = None, None, None
    if args.wait:
        try:
            # With the transaction id a failed creation ends the wait at once, with the server's reason. An id that
            # isn't a number can't be looked up, so the wait then watches the pod only (rather than failing after the create).
            txn = created.transaction_id if str(created.transaction_id).isdigit() else None
            pod_name = client.wait_for_new_pod(args.name, args.workspace, until=args.wait, timeout=args.wait_timeout,
                                               transaction_id=txn).pod_name
        except WaitTimeoutError as exc:
            timed_out = exc
        except PodCreationFailedError as exc:
            failed = exc
    _emit(output, created.raw, [pod_name or str(created.transaction_id)], lambda: _kv([
        ("accepted", fmt.yes_no(created.accepted)), ("name", created.name),
        ("pod", pod_name or ("(not created)" if failed else "(assigned shortly — see `meshive pods`)")),
        ("transaction", str(created.transaction_id))]))
    if failed:
        print(fmt.clean(f"Error: {str(failed).rstrip('.')}. "
                        f"See `meshive transaction {args.workspace} {created.transaction_id}`."),
              file=sys.stderr)
        return 1
    if timed_out:
        # Accepted, but the pod didn't show up or get ready in time — automation must not mistake this for `--wait running` success.
        print(fmt.clean(f"Error: {timed_out} The create was accepted (transaction {created.transaction_id}); "
                        f"check `meshive pods {args.workspace}`."), file=sys.stderr)
        return 1
    return 0


# Placements that may start the pod on another node, which deletes its unpreserved workspace files.
_MOVING_PLACEMENTS = ("any_node", "wait_any_node")
_MOVE_WARNING = ("Moving this pod to another node permanently deletes unpreserved workspace files. "
                 "Attached local hostPath storage stays on the old node.")
_CONSENT_REQUIRED = "Data Loss Consent Required"    # the server's 409 title for a move without consent


def _start_placement(args: argparse.Namespace) -> str | None:
    """--placement as given (the server decides which exist), any_node for --any-node, or "" when neither is given
    (the server then picks what the console would). None after a usage error."""
    placement = (args.placement or "").strip()
    if args.any_node and placement and placement.lower() != "any_node":
        print("Error: --any-node is short for --placement any_node; pass one or the other.", file=sys.stderr)
        return None
    return placement or ("any_node" if args.any_node else "")


def _start_refused_for_consent(err: ConflictError, given: str) -> int:
    """The server refused a start that may move the pod because it has unpreserved workspace files. Without
    --placement the CLI couldn't know in advance, so name the placement the server chose and how to consent to it."""
    detail = err.raw.get("detail") if isinstance(err.raw, dict) else None
    chosen = str((detail.get("placement") if isinstance(detail, dict) else None) or given or "PLACEMENT")
    print(_MOVE_WARNING, file=sys.stderr)
    if given:
        print("Error: separate data-loss consent is required: --allow-data-loss (in addition to --yes).", file=sys.stderr)
    else:
        print(fmt.clean(f"Error: the server would start this pod with placement {chosen}, which may move it to another "
                        f"node. To consent, re-run with --placement {chosen} --allow-data-loss (in addition to --yes)."),
              file=sys.stderr)
    return 2


def _pod_action(method: str, needs_confirm: bool, question: str):
    def handler(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
        placement = _start_placement(args) if method == "start_pod" else ""
        if placement is None:
            return 2
        if needs_confirm and not _confirm(args, question.format(pod=args.pod_name)):
            return 2
        extra: dict[str, Any] = {}
        if method == "start_pod":
            if placement:          # otherwise no placement is sent and the server picks what the console would
                extra["placement"] = placement
            extra["allow_data_loss"] = bool(getattr(args, "allow_data_loss", False))
            if placement.lower() in _MOVING_PLACEMENTS:
                current = client.get_pod(args.pod_name, args.workspace)
                if current.has_unpreserved_workspace is not False:
                    print(_MOVE_WARNING, file=sys.stderr)
                    if not extra["allow_data_loss"]:
                        if not sys.stdin.isatty():
                            print("Error: separate data-loss consent is required: --allow-data-loss (in addition to --yes).",
                                  file=sys.stderr)
                            return 2
                        if not _confirm(argparse.Namespace(yes=False),
                                        f"Permanently lose unpreserved files for {args.workspace}/{args.pod_name} if it moves?"):
                            return 2
                        extra["allow_data_loss"] = True
        if method == "delete_pod":
            extra["delete_local_storages"] = args.delete_local_storage or []
        try:
            action: ResourceAction = getattr(client, method)(args.pod_name, args.workspace, **extra)
        except ConflictError as err:
            if method != "start_pod" or err.title != _CONSENT_REQUIRED:
                raise
            return _start_refused_for_consent(err, placement)
        _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
        return 0
    return handler


# =============================================================================
# Storage / serving / tasks
# =============================================================================

def _print_storage_estimate(est: StorageEstimate, color: bool) -> None:
    _kv([("estimate", fmt.paint(f"{fmt.money_hourly(est.price_per_hour)}/hr", "green", color)),
         ("per GiB·month", fmt.money(est.price_per_gb_month)), ("size", f"{est.size_gb} GiB"),
         ("type", f"{est.storage_type} / {est.disk_type}"), ("max size", f"{est.max_size_gb} GiB")])
    print(fmt.paint(est.note, "dim", color))


def cmd_storage_create(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    # --disk and --region are accepted but not passed on: the server ignores both (storage is always NVMe).
    _deprecated_flag("--disk", args.disk, "storage is always NVMe")
    _deprecated_flag("--region", args.region, "storage is created without a region")
    kwargs = dict(storage_type=args.type, encrypted=args.encrypted, max_price_per_hour=args.max_price,
                  **_usage_warning(args.usage_warning))
    estimate = client.estimate_storage(args.name, args.size, workspace=args.workspace, **kwargs)
    if args.estimate:
        _emit_estimate(output, estimate, lambda: _print_storage_estimate(estimate, color))
        return 0
    if output == "table":
        _print_storage_estimate(estimate, color)
    if not _confirm(args, f"Create {args.size} GiB {estimate.storage_type} storage '{args.name}' "
                          f"at {fmt.money_hourly(estimate.price_per_hour)}/hr?"):
        return 2
    created = client.create_storage(args.name, args.size, workspace=args.workspace, **kwargs)
    _emit(output, created.raw, [str(created.transaction_id)], lambda: _kv([
        ("accepted", "yes"), ("name", created.name), ("transaction", str(created.transaction_id)),
        ("storage id", "(assigned shortly — see `meshive storages`)")]))
    return 0


def cmd_storage_delete(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if not _confirm(args, f"Delete storage '{args.storage_name}' and its data?"):
        return 2
    action = client.delete_storage(args.storage_name, args.workspace)
    _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
    return 0


def _usage_warning_cell(on: bool | None, threshold: float | None) -> str:
    if on is None:
        return "-"
    if not on:
        return "off"
    return f"on, above {threshold * 100:.0f}%" if threshold is not None else "on"


def _price_change(new: str | None, current: str | None) -> str:
    """'$0.120/hr (now $0.068/hr)', or just the new price when the current one is unknown or the same."""
    price = f"{fmt.money_hourly(new)}/hr"
    if current is not None and fmt.money_hourly(current) != fmt.money_hourly(new):
        price += f" (now {fmt.money_hourly(current)}/hr)"
    return price


def _print_storage_update_estimate(est: StorageUpdateEstimate, color: bool) -> None:
    size = f"{est.size_gb} GiB" if est.size_gb is not None else "-"
    if est.current_size_gb is not None and est.current_size_gb != est.size_gb:
        size += f" (now {est.current_size_gb} GiB)"
    _kv([("estimate", fmt.paint(_price_change(est.price_per_hour, est.current_price_per_hour), "green", color)),
         ("storage", f"{est.name} ({est.pv_name})" if est.name else est.pv_name or "-"), ("size", size),
         ("usage warning", _usage_warning_cell(est.usage_warning, est.usage_warning_threshold))])
    if est.note:
        print(fmt.paint(est.note, "dim", color))


def _storage_update_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if args.name is not None:
        kwargs["name"] = args.name
    if args.size is not None:
        kwargs["size_gb"] = args.size
    if args.no_usage_warning:
        kwargs["usage_warning"] = False
    else:
        kwargs.update(_usage_warning(args.usage_warning))
    return kwargs


def cmd_storage_update(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    kwargs = _storage_update_kwargs(args)
    if not kwargs:
        print("Error: nothing to change. Pass --name, --size, --usage-warning or --no-usage-warning.", file=sys.stderr)
        return 2
    estimate = client.estimate_storage_update(args.storage_name, args.workspace, **kwargs)
    if args.estimate:
        _emit_estimate(output, estimate, lambda: _print_storage_update_estimate(estimate, color))
        return 0
    if output == "table":
        _print_storage_update_estimate(estimate, color)
    if "size_gb" in kwargs:
        question = (f"Grow storage '{args.storage_name}' to {args.size} GiB at "
                    f"{fmt.money_hourly(estimate.price_per_hour)}/hr? Storage can't shrink later.")
    else:
        question = f"Update storage '{args.storage_name}'?"
    if not _confirm(args, question):
        return 2
    updated = client.update_storage(args.storage_name, args.workspace, **kwargs)
    pv_name = updated.pv_name or args.storage_name
    transaction = "-" if updated.transaction_id is None else str(updated.transaction_id)
    _emit(output, updated.raw, [pv_name], lambda: _kv([
        ("accepted", fmt.yes_no(updated.accepted)), ("storage", pv_name), ("transaction", transaction)]))
    return 0


def _lock_note(connection: StorageConnection) -> str:
    if connection.remaining_seconds is not None:
        when = f"in about {max(1, round(connection.remaining_seconds / 60))} min"
    elif connection.auto_lock_minutes is not None:
        when = f"after {connection.auto_lock_minutes} min"
    else:
        when = "after a while"
    return f"Encrypted storage: it is unlocked for this access and locks again automatically {when}."


def cmd_storage_connect(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    """Prints only the connect command and one-time password, like `ssh` — does not run ssh for you."""
    connection = client.connect_storage(args.storage_name, args.workspace, wait=not args.no_wait,
                                        timeout=args.wait_timeout)
    access = connection.ssh
    raw = {**connection.raw, "ssh": access.raw} if access else connection.raw

    def show() -> None:
        pairs = [("storage", connection.pv_name or args.storage_name), ("phase", connection.phase or "-")]
        if connection.pod_name:
            pairs.append(("access pod", connection.pod_name))
        if access:
            pairs += [("command", access.command), ("password", access.password),
                      ("expires", fmt.relative_time(access.expires_at) if access.expires_at else "-")]
        _kv(pairs)
        if access and access.web_url:
            print(fmt.paint(f"Browser terminal: {fmt.clean(access.web_url)}", "dim", color))
        if access:
            print(fmt.paint("One-time password — run the command and paste it when asked.", "dim", color))
        else:
            print(fmt.paint(f"Not ready yet. Run `meshive storage-connect {args.workspace} {args.storage_name}` "
                            "again for the SSH command.", "dim", color))
        if connection.encrypted:
            print(fmt.paint(_lock_note(connection), "yellow", color))

    _emit(output, raw, [access.command if access else connection.phase], show)
    return 0


# Pod edit arguments that only qualify a change (consents and the price cap) — on their own they change nothing.
_POD_UPDATE_QUALIFIERS = frozenset({"allow_node_migration", "allow_data_loss", "drop_watched_folders",
                                    "max_price_per_hour"})


def _pod_update_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """update_pod arguments for the flags given — nothing else is sent, so the rest of the pod stays as it is."""
    kwargs = dict(
        name=args.name, template_id=args.template, rental_type=args.rental, gpu_model=args.gpu,
        gpu_count=args.gpu_count, gpu_vram_gb=args.vram, vcpu=args.vcpu, ram_gb=args.ram, cpu_line=args.cpu_line,
        env=_parse_env(args.env) or None, secret_keys=args.secret or None, remove_env=args.remove_env or None,
        ports=_parse_ports(args.port) or None, template_ports=_parse_template_ports(args.template_port) or None,
        remove_ports=args.remove_port or None, command=args.overwrite_command,
        volumes=_parse_volumes(args.volume) or None, local_volumes=_parse_local_volumes(args.local_volume) or None,
        detach_storages=args.detach or None, max_price_per_hour=args.max_price,
        allow_node_migration=True if args.allow_node_migration else None,
        allow_data_loss=True if args.allow_data_loss else None,
        drop_watched_folders=args.drop_watched_folder or None,
        enable_folder_saving=True if args.enable_folder_saving else None,
    )
    return {k: v for k, v in kwargs.items() if v is not None}


def _print_pod_update_estimate(est: PodUpdateEstimate, color: bool) -> None:
    restarts = {True: "yes, the running pod restarts to apply the change", False: "no"}.get(est.restarts, "-")
    pairs = [("estimate", fmt.paint(_price_change(est.price_per_hour, est.current_price_per_hour), "green", color)),
             ("pod", f"{est.name} ({est.pod_name})" if est.name else est.pod_name or "-"),
             ("restarts", restarts)]
    if est.breakdown:
        pairs.append(("breakdown", ", ".join(f"{k}={fmt.money_hourly(v)}" for k, v in est.breakdown.items())))
    _kv(pairs)
    print("changes:" if est.changes else "changes:   none")
    for change in est.changes:
        print(fmt.clean(f"  - {change}"))
    if est.volumes:
        print(fmt.clean("volumes:   " + ", ".join(_volume_cell(v) for v in est.volumes if isinstance(v, dict))))
    if est.note:
        print(fmt.paint(est.note, "dim", color))


def _pod_update_refused(err: MeshiveAPIError) -> int | None:
    """A change the server holds back until it gets consent: say what to rerun with (exit 2, like pod-start's
    consent paths). None for any other error."""
    detail = _error_detail(err)
    if detail.get("code") == "harvest_roots_orphaned":
        paths = [str(p) for p in detail.get("paths") or []]
        print(fmt.clean(f"Error: this change removes watched folders of the pod: {', '.join(paths) or '-'}. New files "
                        "there would no longer be uploaded as assets."), file=sys.stderr)
        flags = " ".join(f"--drop-watched-folder {shlex.quote(p)}" for p in paths) or "--drop-watched-folder PATH"
        print(fmt.clean(f"To go ahead, re-run with {flags} (in addition to --yes)."), file=sys.stderr)
        return 2
    if err.title == "Node Migration Required":
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        print(_MOVE_WARNING, file=sys.stderr)
        print("To let the pod move to another machine, re-run with --allow-node-migration (in addition to --yes); "
              "add --allow-data-loss to also consent to losing its unpreserved workspace files.", file=sys.stderr)
        return 2
    if err.title == _CONSENT_REQUIRED:
        print(_MOVE_WARNING, file=sys.stderr)
        print("Error: separate data-loss consent is required: re-run with --allow-data-loss "
              "(in addition to --allow-node-migration and --yes).", file=sys.stderr)
        return 2
    if err.title == "Resource values need confirmation":
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        print("Re-run with --ram GiB (and --vcpu N) to confirm the values this pod should have.", file=sys.stderr)
        return 2
    return None


def cmd_pod_update(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    kwargs = _pod_update_kwargs(args)
    if not set(kwargs) - _POD_UPDATE_QUALIFIERS:
        print("Error: nothing to change. Pass what to change, such as --name, --gpu, --env or --port "
              "(see `meshive pod-update --help`).", file=sys.stderr)
        return 2
    try:
        estimate = client.estimate_pod_update(args.pod_name, args.workspace, **kwargs)
        if args.estimate:
            _emit_estimate(output, estimate, lambda: _print_pod_update_estimate(estimate, color))
            return 0
        if output == "table":
            _print_pod_update_estimate(estimate, color)
        price = f"{fmt.money_hourly(estimate.price_per_hour)}/hr"
        restart = " It restarts to apply the change." if estimate.restarts else ""
        if not _confirm(args, f"Update pod {args.pod_name} (then {price})?{restart}"):
            return 2
        updated = client.update_pod(args.pod_name, args.workspace, **kwargs)
    except MeshiveAPIError as err:
        code = _pod_update_refused(err)
        if code is None:
            raise
        return code
    _emit(output, updated.raw, [updated.pod_name or args.pod_name], lambda: _print_pod_updated(updated, args, color))
    return 0


def _print_pod_updated(updated: PodUpdated, args: argparse.Namespace, color: bool) -> None:
    transaction = updated.transaction_id
    _kv([("accepted", fmt.yes_no(updated.accepted)), ("pod", updated.pod_name or args.pod_name),
         ("transaction", "-" if transaction is None else str(transaction))])
    if updated.renamed_only:
        print(fmt.paint("Renamed in place; the pod did not restart.", "dim", color))
    elif transaction is not None:
        print(fmt.paint(f"Follow it with `meshive transaction {args.workspace} {transaction}`.", "dim", color))


def cmd_serving_deploy(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    cap = fmt.money_hourly(args.price_cap)
    if not _confirm(args, f"Deploy model registration #{args.model_id} with up to {args.max_replicas} replica(s) "
                          f"at ≤{cap}/hr each?"):
        return 2
    action = client.deploy_serving(args.model_id, workspace=args.workspace, price_cap_per_hour=args.price_cap,
                                   min_replicas=args.min_replicas, max_replicas=args.max_replicas,
                                   autoscale=not args.no_autoscale, max_context_tokens=args.max_context,
                                   share_idle_capacity=args.share_idle)
    _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
    return 0


def cmd_serving_scale(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    autoscale = None
    if args.autoscale:
        autoscale = True
    elif args.no_autoscale:
        autoscale = False
    # Confirm only changes that can raise cost (wider range, turning on autoscale, higher cap) — reductions apply immediately.
    current = client.get_serving(args.serving_id)
    if current.scale_raises_cost(min_replicas=args.min_replicas, max_replicas=args.max_replicas, autoscale=autoscale,
                                 price_cap_per_hour=args.price_cap):
        if not _confirm(args, f"Scale serving #{args.serving_id} ({current.min_replicas}-{current.max_replicas} replicas, "
                              f"cap {fmt.money_hourly(current.price_cap_per_hour) if current.price_cap_per_hour else 'none'}/hr)? "
                              "Its possible hourly cost goes up."):
            return 2
    action = client.scale_serving(args.serving_id, min_replicas=args.min_replicas, max_replicas=args.max_replicas,
                                  autoscale=autoscale, price_cap_per_hour=args.price_cap)
    _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
    return 0


def _serving_simple(method: str, question: str | None = None, **fixed: Any):
    def handler(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
        if question and not _confirm(args, question.format(id=args.serving_id)):
            return 2
        action = getattr(client, method)(args.serving_id, **fixed)
        _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
        return 0
    return handler


def cmd_models(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    items = client.list_models(args.workspace)

    def show() -> None:
        if not items:
            print("No registered models. Register one with `meshive model-register`.")
            return
        fmt.render_table(["NAME", "ID", "REPO", "FRAMEWORK", "API MODEL"],
                         [[m.name or "-", str(m.registration_id), m.huggingface_repo or "-", m.framework or "-",
                           m.api_model_id or "-"] for m in items], aligns=["l", "r", "l", "l", "l"], enabled=color)

    _emit(output, [m.raw for m in items], [str(m.registration_id) for m in items], show)
    return 0


def cmd_hf_tokens(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    tokens = client.list_hf_tokens(args.workspace)

    def show() -> None:
        if not tokens:
            print("No Hugging Face tokens. Add one in the console to register private models.")
            return
        fmt.render_table(["NAME", "ID", "ASSETS", "CREATED"],
                         [[t.label or "-", str(t.token_id), str(t.used_by_asset_count), fmt.relative_time(t.created_at)]
                          for t in tokens], aligns=["l", "r", "r", "l"], enabled=color)

    _emit(output, [{"id": t.token_id, "label": t.label} for t in tokens], [str(t.token_id) for t in tokens], show)
    return 0


def cmd_model_detect(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    d = client.detect_model(args.repo, workspace=args.workspace, hf_token_id=args.hf_token)

    def show() -> None:
        pairs = [("status", fmt.paint(d.status, "green" if d.ok else "red", color))]
        if d.output:
            pairs.append(("output", d.output + (" (takes images)" if d.takes_image else "")))
        for key, value in (("framework", d.framework), ("architecture", d.architecture)):
            if value:
                pairs.append((key, value))
        if d.context_length:
            pairs.append(("context", f"{d.context_length:,} tokens"))
        if d.file_size_bytes:
            pairs.append(("size", fmt.bytes_human(d.file_size_bytes)))
        if d.detail:
            pairs.append(("detail", d.detail))
        if d.suggested_repo:
            pairs.append(("try instead", d.suggested_repo))
        _kv(pairs)

    _emit(output, d.raw, [d.status], show)
    return 0 if d.ok else 1


def cmd_model_register(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    # No cost (just a registration; the download happens at deploy) → no confirmation.
    action = client.register_model(args.repo, workspace=args.workspace, name=args.name, framework=args.framework,
                                   hf_token_id=args.hf_token, context_length=args.context)
    result = action.result if isinstance(action.result, dict) else {}

    def show() -> None:
        verb = "Already registered" if result.get("title") == "Already Registered" else "Registered"
        print(f"{verb}: model #{fmt.clean(action.id)} (API model {fmt.clean(str(result.get('apiModelId') or '-'))})")
        print(fmt.paint(f"Deploy it: meshive serving-deploy {args.workspace} {action.id} --price-cap USD", "dim", color))

    _emit(output, action.raw, [action.id], show)
    return 0


def _print_task_estimate(est: TaskEstimate, color: bool) -> None:
    price = f"{fmt.money_hourly(est.price_per_hour)}/hr" if est.price_per_hour is not None else "depends on machine"
    cost = fmt.money(est.max_cost) if est.max_cost is not None else "-"
    _kv([("estimate", fmt.paint(price, "green", color)), ("max cost", f"{cost} (for {est.max_duration}s)"),
         ("resources", json.dumps(est.resources))])
    print(fmt.paint(est.note, "dim", color))


def _read_text(path: str | None, label: str) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise ValueError(f"cannot read {label} file {path!r}: {err}") from None


def cmd_asset_import(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    # Registering a link costs nothing (no storage charge; bytes come from the source when used) → no confirmation.
    a = client.import_asset(args.target, workspace=args.workspace, name=args.name, asset_type=args.type,
                            revision=args.revision, paths=args.path, hf_token_id=args.hf_token,
                            civitai_key_id=args.civitai_key)

    def show() -> None:
        _kv([("asset", f"{a.name} ({a.asset_id})"), ("status", a.status), ("source", a.ingest_source),
             ("files", f"{a.file_count} ({fmt.bytes_human(a.total_bytes)})")] +
            ([("commit", a.resolved_commit)] if a.resolved_commit else []))
        note = "Linked, not copied: pods and tasks download it from the source when they start."
        if a.is_gated:
            note += " The source needs its saved token or key at that time too."
        print(fmt.paint(note, "dim", color))

    _emit(output, a.raw, [a.asset_id], show)
    return 0


def cmd_civitai_keys(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    keys = client.list_civitai_keys(args.workspace)

    def show() -> None:
        if not keys:
            print("No CivitAI keys. Add one in the console for models that need it.")
            return
        fmt.render_table(["NAME", "ID", "ASSETS", "CREATED"],
                         [[k.label or "-", str(k.token_id), str(k.used_by_asset_count), fmt.relative_time(k.created_at)]
                          for k in keys], aligns=["l", "r", "r", "l"], enabled=color)

    _emit(output, [{"id": k.token_id, "label": k.label} for k in keys], [str(k.token_id) for k in keys], show)
    return 0


def cmd_model_delete(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if not _confirm(args, f"Delete model registration #{args.model_id}?"):
        return 2
    action = client.delete_model(args.model_id)
    _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
    return 0


def cmd_task_submit(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    script = args.script_text if args.script_text is not None else _read_text(args.script, "--script")
    kwargs = dict(image=args.image, template_id=args.template, requirements=_read_text(args.requirements, "--requirements"),
                  env=_parse_env(args.env), secret_keys=args.secret or [], args=args.arg or [], gpu_model=args.gpu,
                  gpu_count=args.gpu_count, gpu_vram_gb=args.vram, cpu_preset=args.cpu_preset,
                  max_duration=args.max_duration, webhook_url=args.webhook, input_assets=_parse_input_assets(args.input_asset),
                  max_price_per_hour=args.max_price, output_include=args.output_include)
    estimate = client.estimate_task(args.name, script or "", workspace=args.workspace, **kwargs)
    if args.estimate:
        _emit_estimate(output, estimate, lambda: _print_task_estimate(estimate, color))
        return 0
    if output == "table":
        _print_task_estimate(estimate, color)
    cost = fmt.money(estimate.max_cost) if estimate.max_cost is not None else "an unknown amount"
    if not _confirm(args, f"Submit task '{args.name}' (up to {cost})?"):
        return 2
    with warnings.catch_warnings(record=True) as caught:
        # The SDK warns when the server ignored --output-include; print it like the CLI's other warnings.
        warnings.simplefilter("always", RuntimeWarning)
        submitted = client.submit_task(args.name, script or "", workspace=args.workspace, **kwargs)
    for warning in caught:
        print(f"Warning: {warning.message}", file=sys.stderr)
    task = submitted.task
    _emit(output, submitted.raw, [task.task_id], lambda: _kv([
        ("accepted", "yes"), ("task", task.task_id), ("name", task.name), ("status", task.status),
        ("pod", task.pod_name or "-"),
        *([("output filter", ", ".join(task.output_include))] if task.output_include else []),
        ("logs", f"meshive task-logs {task.task_id}")]))
    return 0


def cmd_task_stop(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    action = client.stop_task(args.task_id)
    _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
    return 0


def cmd_logs(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    logs = client.get_pod_logs(args.pod_name, args.workspace, tail=args.tail, container=args.container, wait=args.wait)
    _emit(output, logs.raw, [item.line for item in logs.lines], lambda: _print_logs(logs, color))
    return 0


def cmd_task_logs(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    logs = client.get_task_logs(args.task_id, tail=args.tail, wait=args.wait, cursor=args.cursor)
    _emit(output, logs.raw, [item.line for item in logs.lines], lambda: _print_logs(logs, color))
    return 0


# =============================================================================
# Parser
# =============================================================================

def _yes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation.")


def add_parsers(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    # --- pods ---
    p = sub.add_parser("pod-create", parents=[common], help="Create a pod (shows the estimate first).")
    p.add_argument("workspace", help="Workspace ID (namespace name).")
    p.add_argument("name", help="Pod name (label; unique per workspace).")
    p.add_argument("--template", type=int, required=True, metavar="ID", help="Template ID (see `meshive templates`).")
    p.add_argument("--gpu", default=None, metavar="MODEL", help="GPU model from `meshive gpus`. Omit for a CPU pod.")
    p.add_argument("--gpu-count", type=int, default=1, metavar="N")
    p.add_argument("--vram", type=int, default=None, metavar="GB", help="VRAM tier when a model comes in several.")
    p.add_argument("--spot", action="store_true", help="Spot (preemptible) rental instead of on-demand.")
    p.add_argument("--vcpu", type=int, default=None, metavar="N",
                   help="vCPU (default: recommended for the GPU; 8 for a CPU pod). Values the console offers.")
    p.add_argument("--ram", type=int, default=None, metavar="GiB",
                   help="RAM in GiB (default: recommended; 4 GiB per vCPU for a CPU pod). Values the console offers.")
    p.add_argument("--cpu-line", default=None, metavar="LINE",
                   help="CPU pods only: the CPU line to run on, from `meshive cpus` (default: the cheapest that fits).")
    p.add_argument("--volume", action="append", metavar="PV:/mount",
                   help="Attach an existing network storage (repeatable).")
    p.add_argument("--local-volume", action="append", metavar="NAME:/PATH:SIZE_GB[:encrypted]",
                   help="Create local storage on the pod's machine together with the pod (repeatable); "
                        "not available for spot pods.")
    p.add_argument("--env", action="append", metavar="KEY=VALUE", help="Environment variable (repeatable).")
    p.add_argument("--secret", action="append", metavar="KEY", help="Mark an --env key as secret (repeatable).")
    p.add_argument("--port", action="append", metavar="PORT[:NAME[:internal]]",
                   help="Open a port the template doesn't (repeatable). To change one it opens, use --template-port.")
    p.add_argument("--template-port", action="append", metavar="PORT[:NAME][:NEW_PORT][:internal|external]",
                   help="Change a port the template opens (repeatable), as the console's deploy screen does. PORT is "
                        "its current number; the other fields are positional and an empty one keeps the template's "
                        "value: 8080:ide renames it, 8080::8443 gives it the number 8443, 8080:::internal hides it, "
                        "8080:ide:8443:external does all three. Template ports can't be removed.")
    # Without changing dest it overwrites args.command, which holds the subcommand name, and the command disappears.
    p.add_argument("--command", dest="overwrite_command", default=None, help="Override the template command.")
    p.add_argument("--region", default=None, metavar="CODE", help="Deprecated and ignored; pods are placed without a region.")
    p.add_argument("--internet-premium", action="store_true")
    p.add_argument("--uptime-premium", action="store_true")
    p.add_argument("--cpu-premium", action="store_true")
    p.add_argument("--max-price", default=None, metavar="USD", help="Cap the final compute USD/hour rate; storage/Asset Hub charges are separate.")
    p.add_argument("--input-asset", action="append", dest="pod_input_asset", metavar="ASSET_ID[=DIR]",
                   help="Attach an Asset Hub asset (repeatable); default place /inputs/<name> or the template's path.")
    p.add_argument("--watch", action="append", metavar="PATH",
                   help="Upload new files in this folder as assets (repeatable); see `meshive pod-watch`.")
    p.add_argument("--watch-off", action="append", metavar="PATH",
                   help="Turn off one of the template's watched folders from the start (repeatable), as the console's "
                        "create screen does; `meshive pod-watch --on` turns it back on.")
    p.add_argument("--watch-move", action="append", metavar="PATH=NEW_PATH",
                   help="Move one of the template's output folders to another path (repeatable); $MESHIVE_OUTPUT_DIR "
                        "and its storage follow.")
    p.add_argument("--model-set", default=None, metavar="SLUG",
                   help="Start the pod with this model set (see `meshive model-sets`). Its files are fetched before "
                        "the pod is created, which can take several minutes.")
    p.add_argument("--hf-token", type=int, default=None, metavar="ID",
                   help="Quick Deploy templates whose model set is gated on Hugging Face: a token ID from "
                        "`meshive hf-tokens` whose account accepted the model's license. The files download with it.")
    p.add_argument("--estimate", action="store_true", help="Only show the estimate; create nothing.")
    p.add_argument("--wait", default=None, metavar="STATUS",
                   help="After creating, wait until the pod reaches STATUS (e.g. running); a failed creation ends the "
                        "wait with the reason.")
    p.add_argument("--wait-timeout", type=float, default=600.0, metavar="SECONDS")
    _yes(p)

    p = sub.add_parser("pod-update", parents=[common],
                       help="Change a pod: name, template, hardware, environment, ports, command or storage "
                            "(shows the changes and the new price first).")
    p.add_argument("workspace", help="Workspace ID (namespace name).")
    p.add_argument("pod_name", help="Pod ID (pod name).")
    p.add_argument("--name", default=None, help="New name (label).")
    p.add_argument("--template", type=int, default=None, metavar="ID", help="Move the pod to another template; its variables, ports and command start from it.")
    p.add_argument("--rental", choices=["demand", "spot"], default=None, help="Change the rental type.")
    p.add_argument("--gpu", default=None, metavar="MODEL", help="GPU model from `meshive gpus`.")
    p.add_argument("--gpu-count", type=int, default=None, metavar="N")
    p.add_argument("--vram", type=int, default=None, metavar="GB", help="VRAM tier when a model comes in several.")
    p.add_argument("--vcpu", type=int, default=None, metavar="N", help="vCPU (values the console offers).")
    p.add_argument("--ram", type=int, default=None, metavar="GiB", help="RAM in GiB (values the console offers).")
    p.add_argument("--cpu-line", default=None, metavar="LINE", help="CPU pods only: the CPU line, from `meshive cpus`.")
    p.add_argument("--env", action="append", metavar="KEY=VALUE", help="Set or add an environment variable (repeatable).")
    p.add_argument("--secret", action="append", metavar="KEY", help="Mark an --env key as secret (repeatable).")
    p.add_argument("--remove-env", action="append", metavar="KEY",
                   help="Remove an environment variable you added (repeatable); the template's can't be removed.")
    p.add_argument("--port", action="append", metavar="PORT[:NAME[:internal]]", help="Open a new port (repeatable).")
    p.add_argument("--template-port", action="append", metavar="PORT[:NAME][:NEW_PORT][:internal|external]",
                   help="Rename, renumber, expose or hide a current port (repeatable), as in `pod-create`.")
    p.add_argument("--remove-port", action="append", type=int, metavar="PORT",
                   help="Close a port you opened (repeatable); the template's can't be removed.")
    p.add_argument("--command", dest="overwrite_command", default=None,
                   help='Replace the command; --command "" goes back to the template\'s command.')
    p.add_argument("--volume", action="append", metavar="PV:/mount",
                   help="Attach existing storage (repeatable): network storage, or local storage on the pod's machine.")
    p.add_argument("--local-volume", action="append", metavar="NAME:/PATH:SIZE_GB[:encrypted]",
                   help="Create local storage on the pod's machine and attach it (repeatable); not for spot pods.")
    p.add_argument("--detach", action="append", metavar="PV", help="Detach a storage (repeatable); its data stays.")
    p.add_argument("--allow-node-migration", action="store_true",
                   help="Let the pod move to another machine when its machine can't fit the change.")
    p.add_argument("--allow-data-loss", action="store_true",
                   help="Separately consent to permanent loss of unpreserved workspace files if the pod moves.")
    p.add_argument("--drop-watched-folder", action="append", metavar="PATH",
                   help="Agree that the change removes this watched folder (repeatable); the error names them.")
    p.add_argument("--enable-folder-saving", action="store_true",
                   help="Add the uploader to an older pod so its folders can be watched (needs storage; a running "
                        "pod restarts). `meshive pod-watch` shows whether a pod needs it.")
    p.add_argument("--max-price", default=None, metavar="USD", help="Cap the new compute USD/hour rate.")
    p.add_argument("--estimate", action="store_true", help="Only show the changes and the new price; change nothing.")
    _yes(p)

    for name, help_text in (("pod-stop", "Stop a pod (pod billing stops; storage billing continues)."),
                            ("pod-start", "Start a stopped pod (billing resumes)."),
                            ("pod-restart", "Restart a pod."),
                            ("pod-delete", "Delete a pod.")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("workspace", help="Workspace ID (namespace name).")
        p.add_argument("pod_name", help="Pod ID (pod name).")
        if name == "pod-start":
            p.add_argument("--placement", default=None, metavar="PLACEMENT",
                           help="same_node, any_node, wait_same_node or wait_any_node. wait_* keeps the pod waiting "
                                "until capacity frees up; any_node and wait_any_node may move it to another node, which "
                                "permanently deletes unpreserved workspace files. Without it the server picks what the "
                                "console would (same_node, or wait_same_node when its node has no room now; any_node or "
                                "wait_any_node when its node can no longer run it) and the output shows which.")
            p.add_argument("--any-node", action="store_true", help="Short for --placement any_node.")
            p.add_argument("--allow-data-loss", action="store_true",
                           help="Separately consent to permanent loss of unpreserved workspace files for this pod's migration.")
            _yes(p)
        if name == "pod-delete":
            p.add_argument("--delete-local-storage", action="append", metavar="PV",
                           help="Also delete this attached local (hostPath) storage (repeatable).")
            _yes(p)

    # --- storages ---
    p = sub.add_parser("ssh", parents=[common], help="Print a one-time SSH command and password for a pod (read & write key).")
    p.add_argument("workspace"); p.add_argument("pod_name")
    p = sub.add_parser("pod-watch", parents=[common],
                       help="Show or change a running pod's watched folders (new files become assets; no restart).")
    p.add_argument("workspace"); p.add_argument("pod_name")
    p.add_argument("--add", action="append", metavar="PATH", help="Watch a new folder (repeatable).")
    p.add_argument("--include", action="append", metavar="GLOB", help="With --add: only matching files (repeatable).")
    p.add_argument("--existing", action="store_true", help="With --add: also upload files already there.")
    p.add_argument("--remove", action="append", metavar="PATH", help="Stop watching a folder you added.")
    p.add_argument("--on", action="append", metavar="PATH", help="Turn a folder back on.")
    p.add_argument("--off", action="append", metavar="PATH", help="Turn a folder off (template folders too).")
    _yes(p)
    p = sub.add_parser("storage-create", parents=[common], help="Create a storage volume (shows the estimate first).")
    p.add_argument("workspace"); p.add_argument("name", help="Storage name (label).")
    p.add_argument("--size", type=int, required=True, metavar="GiB", help="Size in GiB (at least 10).")
    p.add_argument("--type", default="nfs", metavar="TYPE",
                   help="Storage type (default: nfs, network storage). Local storage is created together with a pod: "
                        "`meshive pod-create --local-volume`.")
    p.add_argument("--disk", default=None, metavar="TYPE", help="Deprecated and ignored; storage is always NVMe.")
    p.add_argument("--encrypted", action="store_true", help="At-rest encryption.")
    p.add_argument("--usage-warning", nargs="?", const=True, default=None, metavar="PERCENT",
                   help="Email a warning when the storage is more than PERCENT full (1-100; default 90), like the "
                        "console's usage warning. E.g. --usage-warning, or --usage-warning 80.")
    p.add_argument("--region", default=None, metavar="CODE", help="Deprecated and ignored.")
    p.add_argument("--max-price", default=None, metavar="USD", help="Cap this volume's final initial USD/hour rate.")
    p.add_argument("--estimate", action="store_true", help="Only show the estimate; create nothing.")
    _yes(p)
    p = sub.add_parser("storage-delete", parents=[common], help="Delete a storage volume.")
    p.add_argument("workspace"); p.add_argument("storage_name", help="Storage ID (pv name).")
    _yes(p)
    p = sub.add_parser("storage-update", parents=[common],
                       help="Rename a storage, grow it, or change its usage warning (shows the new price first).")
    p.add_argument("workspace", help="Workspace ID (namespace name).")
    p.add_argument("storage_name", help="Storage ID (pv name).")
    p.add_argument("--name", default=None, help="New name (label).")
    p.add_argument("--size", type=int, default=None, metavar="GiB", help="New size in GiB (storage can only grow).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--usage-warning", nargs="?", const=True, default=None, metavar="PERCENT",
                   help="Email a warning when the storage is more than PERCENT full (1-100; default 90).")
    g.add_argument("--no-usage-warning", action="store_true", help="Turn the usage warning off.")
    p.add_argument("--estimate", action="store_true", help="Only show the new price; change nothing.")
    _yes(p)
    p = sub.add_parser("storage-connect", parents=[common],
                       help="Open a storage for SSH and SFTP and print a one-time SSH command and password.")
    p.add_argument("workspace", help="Workspace ID (namespace name).")
    p.add_argument("storage_name", help="Storage ID (pv name).")
    p.add_argument("--no-wait", action="store_true", help="Print the status without waiting until it is ready.")
    p.add_argument("--wait-timeout", type=float, default=300.0, metavar="SECONDS",
                   help="Give up waiting after this long (default: 300).")

    # --- servings ---
    p = sub.add_parser("serving-deploy", parents=[common], help="Deploy a registered model as a serving.")
    p.add_argument("workspace"); p.add_argument("model_id", type=int, help="Model registration ID (see `meshive models`).")
    p.add_argument("--price-cap", required=True, metavar="USD", help="Per-replica hourly price cap.")
    p.add_argument("--min-replicas", type=int, default=1); p.add_argument("--max-replicas", type=int, default=3)
    p.add_argument("--no-autoscale", action="store_true")
    p.add_argument("--max-context", type=int, default=None, metavar="TOKENS")
    p.add_argument("--share-idle", action="store_true", help="Share idle capacity (official text models only).")
    _yes(p)
    p = sub.add_parser("serving-scale", parents=[common], help="Change a serving's replica range, autoscale, or price cap.")
    p.add_argument("serving_id", type=int)
    p.add_argument("--min-replicas", type=int, default=None); p.add_argument("--max-replicas", type=int, default=None)
    g = p.add_mutually_exclusive_group(); g.add_argument("--autoscale", action="store_true"); g.add_argument("--no-autoscale", action="store_true")
    p.add_argument("--price-cap", default=None, metavar="USD")
    _yes(p)
    p = sub.add_parser("serving-pause", parents=[common], help="Pause a serving."); p.add_argument("serving_id", type=int)
    p = sub.add_parser("serving-resume", parents=[common], help="Resume a paused serving (billing resumes)."); p.add_argument("serving_id", type=int); _yes(p)
    p = sub.add_parser("serving-delete", parents=[common], help="Delete a serving."); p.add_argument("serving_id", type=int); _yes(p)

    # --- asset import ---
    p = sub.add_parser("asset-import", parents=[common],
                       help="Link a Hugging Face repo, CivitAI model or file URL as an asset (no copy, no storage charge).")
    p.add_argument("workspace")
    p.add_argument("target", help="owner/name or a huggingface.co, civitai.com or direct file URL.")
    p.add_argument("--name", default=None, help="Asset name (default: from the source).")
    p.add_argument("--type", default=None, choices=["dataset", "model", "adapter", "checkpoint", "config", "file"])
    p.add_argument("--revision", default=None, help="Hugging Face branch, tag or commit (default: main).")
    p.add_argument("--path", action="append", metavar="GLOB", help="Only link matching files (repeatable).")
    p.add_argument("--hf-token", type=int, default=None, metavar="ID", help="Token ID from `meshive hf-tokens`.")
    p.add_argument("--civitai-key", type=int, default=None, metavar="ID", help="Key ID from `meshive civitai-keys`.")
    p = sub.add_parser("civitai-keys", parents=[common], help="List the workspace's CivitAI keys.")
    p.add_argument("workspace")

    # --- serving models ---
    p = sub.add_parser("models", parents=[common], help="List the models registered for serving (their ID goes to serving-deploy).")
    p.add_argument("workspace")
    p = sub.add_parser("hf-tokens", parents=[common],
                       help="List the workspace's Hugging Face tokens (for private and gated repos).")
    p.add_argument("workspace")
    p = sub.add_parser("model-detect", parents=[common], help="Check whether a Hugging Face repo can be served (exit 1 if not).")
    p.add_argument("workspace"); p.add_argument("repo", help="Hugging Face repo, e.g. Qwen/Qwen3-0.6B.")
    p.add_argument("--hf-token", type=int, default=None, metavar="ID", help="Token ID from `meshive hf-tokens` (private repos).")
    p = sub.add_parser("model-register", parents=[common], help="Register a Hugging Face model for serving (free; it downloads when deployed).")
    p.add_argument("workspace"); p.add_argument("repo", help="Hugging Face repo, e.g. Qwen/Qwen3-0.6B.")
    p.add_argument("--name", default=None, help="Display name (default: the repo name).")
    p.add_argument("--framework", default=None, choices=["vllm", "sglang"])
    p.add_argument("--hf-token", type=int, default=None, metavar="ID", help="Token ID from `meshive hf-tokens` (private repos).")
    p.add_argument("--context", type=int, default=None, metavar="TOKENS", help="Max context (default: detected).")
    p = sub.add_parser("model-delete", parents=[common], help="Delete a model registration (not while it is deployed).")
    p.add_argument("model_id", type=int, help="Model registration ID."); _yes(p)

    # --- tasks ---
    p = sub.add_parser("task-submit", parents=[common], help="Submit a one-off task (shows the estimate first).")
    p.add_argument("workspace"); p.add_argument("name", help="Task name.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--script", default=None, metavar="FILE", help="Python script file.")
    g.add_argument("--script-text", default=None, metavar="CODE", help="Python source inline.")
    p.add_argument("--image", default=None, help="Container image (or use --template).")
    p.add_argument("--template", type=int, default=None, metavar="ID")
    p.add_argument("--requirements", default=None, metavar="FILE", help="requirements.txt file.")
    p.add_argument("--env", action="append", metavar="KEY=VALUE"); p.add_argument("--secret", action="append", metavar="KEY")
    p.add_argument("--arg", action="append", metavar="ARG", help="Command-line argument for the script (repeatable).")
    p.add_argument("--gpu", default=None, metavar="MODEL"); p.add_argument("--gpu-count", type=int, default=None, metavar="N")
    p.add_argument("--vram", type=int, default=None, metavar="GB")
    p.add_argument("--cpu-preset", default=None, metavar="PRESET", help="CPU task preset, e.g. micro-2c8g (instead of --gpu).")
    p.add_argument("--max-duration", type=int, default=3600, metavar="SECONDS", help="Hard stop (3600..86400).")
    p.add_argument("--webhook", default=None, metavar="URL")
    p.add_argument("--input-asset", action="append", metavar="ASSET_ID")
    p.add_argument("--max-price", default=None, metavar="USD", help="Final compute USD/hour cap; storage and Asset Hub charges are separate.")
    p.add_argument("--output-include", action="append", metavar="GLOB",
                   help="Only upload files in /outputs matching GLOB, relative to /outputs: '*.safetensors' (any depth), "
                        "'samples/*.png' (that folder), 'checkpoints/' (every folder of that name, with its contents); "
                        "repeatable. Default: every file. "
                        "Files left out still use /outputs disk while the task runs.")
    p.add_argument("--estimate", action="store_true", help="Only show the estimate; submit nothing.")
    _yes(p)
    p = sub.add_parser("task-stop", parents=[common], help="Stop a running task."); p.add_argument("task_id")

    # --- logs ---
    p = sub.add_parser("logs", parents=[common], help="Show the last lines of a pod's logs.")
    p.add_argument("workspace"); p.add_argument("pod_name")
    p.add_argument("--tail", type=int, default=200, metavar="N"); p.add_argument("--container", default=None)
    p.add_argument("--wait", type=float, default=None, metavar="SECONDS", help="Wait for the log watcher if the buffer is empty (0..15).")
    p = sub.add_parser("task-logs", parents=[common], help="Show the last lines of a task's logs.")
    p.add_argument("task_id"); p.add_argument("--tail", type=int, default=200, metavar="N")
    p.add_argument("--wait", type=float, default=None, metavar="SECONDS"); p.add_argument("--cursor", type=int, default=None)


HANDLERS: dict[str, Handler] = {
    "pod-create": cmd_pod_create,
    "pod-update": cmd_pod_update,
    "pod-stop": _pod_action("stop_pod", False, ""),
    "pod-start": _pod_action("start_pod", True, "Start pod {pod} (billing resumes)?"),
    "pod-restart": _pod_action("restart_pod", False, ""),
    "pod-delete": _pod_action("delete_pod", True, "Delete pod {pod}?"),
    "pod-watch": cmd_pod_watch,
    "ssh": cmd_ssh,
    "storage-create": cmd_storage_create,
    "storage-delete": cmd_storage_delete,
    "storage-update": cmd_storage_update,
    "storage-connect": cmd_storage_connect,
    "serving-deploy": cmd_serving_deploy,
    "serving-scale": cmd_serving_scale,
    "serving-pause": _serving_simple("pause_serving", paused=True),
    "serving-resume": _serving_simple("pause_serving", "Resume serving #{id}? Billing resumes.", paused=False),
    "serving-delete": _serving_simple("delete_serving", "Delete serving #{id}?"),
    "models": cmd_models,
    "asset-import": cmd_asset_import,
    "civitai-keys": cmd_civitai_keys,
    "hf-tokens": cmd_hf_tokens,
    "model-detect": cmd_model_detect,
    "model-register": cmd_model_register,
    "model-delete": cmd_model_delete,
    "task-submit": cmd_task_submit,
    "task-stop": cmd_task_stop,
    "logs": cmd_logs,
    "task-logs": cmd_task_logs,
}
