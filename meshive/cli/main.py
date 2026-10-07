import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable

import httpx

from . import _format as fmt
from . import _write as write_cli
from .. import _config, _credentials
from .._version import __version__
from .._client import Meshive
from ..exceptions import MeshiveError
from ..models import (
    ApiKey,
    Asset,
    AssetPage,
    AssetStorage,
    Credit,
    CreditHistoryEntry,
    Earnings,
    GpuAvailability,
    Machine,
    MachineMetrics,
    Member,
    Pod,
    PodMetrics,
    Serving,
    Storage,
    Task,
    Template,
    Transaction,
    WhoAmI,
    Workspace,
    WorkspaceDetail,
)

# Pod lifecycle statuses (server enum). For --status validation — the server doesn't take this filter, so
# typos are caught on the client and valid values are shown instead of a silent empty result. Storage (PV) uses the same enum.
POD_STATUSES = (
    "pending", "creating", "running", "waiting", "stopping",
    "stopped", "error", "unreachable", "terminating", "terminated",
)
# Task statuses (server enum). tasks --status goes to the server filter, but typos are caught here.
TASK_STATUSES = (
    "queued", "scheduling", "pulling", "fetching", "running",
    "succeeded", "failed", "timed_out", "stopped",
)
# Serving statuses (server enum) — validation for the servings --status client-side filter.
# terminated is left out because the server excludes it from the list.
SERVING_STATUSES = ("provisioning", "active", "scaling", "draining", "error")
# Template app types (server enum) — templates --type goes to the server filter (appType).
TEMPLATE_TYPES = (
    "ide", "framework", "db", "mlops", "llmops", "inference",
    "generative", "science", "os", "custom",
)
# Storage types (server enum). Compared case-insensitively (hostPath → hostpath).
STORAGE_TYPES = ("nfs", "hostpath", "ephemeral", "emptydir")
# Asset types / statuses (server enums) — assets --type / --status go to the server filter.
ASSET_TYPES = ("dataset", "model", "adapter", "checkpoint", "output", "config", "file")
ASSET_STATUSES = ("active", "source_missing", "frozen", "deleted", "purged", "merged")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshive",
        description="Meshive GPU Cloud CLI",
        epilog=(
            "Examples:\n"
            "  meshive login                  Save your API key (then commands need no --api-key)\n"
            "  meshive me                     Show the current API key's owner\n"
            "  meshive workspaces             List workspaces\n"
            "  meshive workspace <id>         Show a workspace's cost & resource summary\n"
            "  meshive pods <workspace>       List pods in a workspace\n"
            "  meshive pod <workspace> <pod>  Show a single pod\n"
            "  meshive storages <workspace>   List storages (volumes) in a workspace\n"
            "  meshive gpus                   List GPUs available to rent, with prices\n"
            "  meshive templates              List templates\n"
            "  meshive assets <workspace>     List assets (datasets, models, outputs, ...)\n"
            "  meshive tasks <workspace>      List serverless tasks\n"
            "  meshive servings <workspace>   List serverless serving deployments\n"
            "  meshive credit                 Show your credit balance\n"
            "  meshive api-keys               List your API keys\n"
            "  meshive machines               List your machines (as a host)\n"
            "  meshive machine <id>           Show a single machine\n"
            "  meshive earnings               Show your earnings (as a host)\n"
            "\n"
            "Write commands (need a key with the write scope; they ask for confirmation unless --yes):\n"
            "  meshive pod-create <ws> <name> --template ID --gpu MODEL [--estimate]\n"
            "  meshive pod-stop|pod-start|pod-restart|pod-delete <ws> <pod>\n"
            "  meshive storage-create <ws> <name> --size GiB  meshive storage-delete <ws> <pv>\n"
            "  meshive task-submit <ws> <name> --script FILE --image IMG --cpu-preset micro-2c8g\n"
            "  meshive logs <ws> <pod>          meshive task-logs <task>\n"
            "\n"
            "Run `meshive <command> --help` for filters and options.\n"
            "Scripting: `-o name` prints just the IDs, one per line (pipe into xargs).\n"
            "Auth: `meshive login`, or set MESHIVE_API_KEY / pass --api-key.\n"
            "Docs: https://github.com/meshive/meshive-python"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Global options shared by every subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--api-key", default=None,
        help="Meshive API key (overrides MESHIVE_API_KEY). "
             "Note: visible in shell history and `ps`; prefer `meshive login` or the env var.",
    )
    common.add_argument("--base-url", default=None, help="API base URL (overrides MESHIVE_BASE_URL).")
    common.add_argument(
        "-o", "--output", choices=["table", "json", "name"], default=None,
        help="Output format: table (default), json (raw payload), name (IDs only, one per line).",
    )
    # --json is an alias of -o json (a flag used since before 0.0.5 — kept for backward compatibility).
    common.add_argument("--json", action="store_true", dest="as_json", help="Shorthand for -o json.")
    common.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="HTTP timeout in seconds (default: 30).",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("login", parents=[common],
                   help="Save your API key to ~/.meshive/credentials (verifies it first).")
    sub.add_parser("logout", parents=[common],
                   help="Remove saved credentials.")

    sub.add_parser("me", parents=[common], aliases=["whoami"],
                   help="Show the current API key's owner.")

    # --- workspaces -----------------------------------------------------------
    sub.add_parser("workspaces", parents=[common], aliases=["ws"],
                   help="List your workspaces.")

    p_workspace = sub.add_parser("workspace", parents=[common],
                                 help="Show a single workspace (cost & resource summary).")
    p_workspace.add_argument("workspace", help="Workspace ID (namespace name). See ID column of `meshive workspaces`.")

    p_members = sub.add_parser("members", parents=[common], help="List members of a workspace.")
    p_members.add_argument("workspace", help="Workspace ID (namespace name).")

    # --- pods -----------------------------------------------------------------
    p_pods = sub.add_parser("pods", parents=[common], help="List pods in a workspace (or all).")
    p_pods.add_argument("workspace", nargs="?",
                        help="Workspace ID (namespace name). Omit when using --all.")
    p_pods.add_argument("--all", action="store_true", dest="all_workspaces",
                        help="List pods across every workspace (adds a WORKSPACE column).")
    # Client-side filter (the server takes no filter parameters).
    p_pods.add_argument(
        "--status", action="append", metavar="STATUS",
        help="Filter by status (repeatable or comma-separated), e.g. running,error. "
             f"One of: {', '.join(POD_STATUSES)}.",
    )
    p_pods.add_argument(
        "--rental", choices=["spot", "demand"], default=None,
        help="Filter by rental type.",
    )
    p_pods.add_argument(
        "--name", default=None, metavar="SUBSTR",
        help="Filter by display name (alias) substring. Note: a label, not a unique key.",
    )

    p_pod = sub.add_parser("pod", parents=[common], help="Show a single pod.")
    p_pod.add_argument("workspace", help="Workspace ID (namespace name).")
    p_pod.add_argument("pod_name", help="Pod ID (pod name). See ID column of `meshive pods`.")
    p_pod.add_argument(
        "--wait", default=None, metavar="STATUS",
        help="Poll until the pod reaches STATUS, then show it. "
             f"One of: {', '.join(POD_STATUSES)}. Gives up early if the pod errors out.",
    )
    p_pod.add_argument(
        "--wait-timeout", type=float, default=600.0, metavar="SECONDS",
        help="Give up waiting after this long (default: 600).",
    )
    p_pod.add_argument("--show-secrets", action="store_true",
                       help="Print secret connect credentials (e.g. ACCESS_PASSWORD) instead of hiding them.")

    p_pod_metrics = sub.add_parser("pod-metrics", parents=[common],
                                   help="Show live resource usage (CPU/RAM/GPU/disk) of a pod.")
    p_pod_metrics.add_argument("workspace", help="Workspace ID (namespace name).")
    p_pod_metrics.add_argument("pod_name", help="Pod ID (pod name). See ID column of `meshive pods`.")

    # --- transactions ---------------------------------------------------------
    p_txn = sub.add_parser("transactions", parents=[common], aliases=["txn"],
                           help="Show in-flight pod operations (why a pod is still creating).")
    p_txn.add_argument("workspace", help="Workspace ID (namespace name).")

    # --- storages -------------------------------------------------------------
    p_storages = sub.add_parser("storages", parents=[common],
                                help="List storages (volumes) in a workspace.")
    p_storages.add_argument("workspace", help="Workspace ID (namespace name).")
    p_storages.add_argument(
        "--type", choices=STORAGE_TYPES, type=str.lower, default=None, dest="storage_type",
        help="Filter by storage type (case-insensitive).",
    )
    p_storages.add_argument(
        "--status", action="append", metavar="STATUS",
        help="Filter by status (repeatable or comma-separated). "
             f"One of: {', '.join(POD_STATUSES)}.",
    )
    p_storages.add_argument(
        "--name", default=None, metavar="SUBSTR",
        help="Filter by display name (alias) substring. Note: a label, not a unique key.",
    )

    p_storage = sub.add_parser("storage", parents=[common], help="Show a single storage (volume).")
    p_storage.add_argument("workspace", help="Workspace ID (namespace name).")
    p_storage.add_argument("storage_name", help="Storage ID (volume name). See ID column of `meshive storages`.")

    # --- machines (host) ------------------------------------------------------
    p_machines = sub.add_parser("machines", parents=[common], aliases=["m"],
                                help="List your machines (as a host).")
    p_machines.add_argument(
        "--type", choices=["gpu", "cpu", "storage"], default=None, dest="machine_type",
        help="Filter by machine type.",
    )
    # The machine status (state.name) enum is synthesized and open-ended, so it isn't validated on the client —
    # only compared case-insensitively with what the server returns.
    p_machines.add_argument(
        "--status", action="append", metavar="STATUS",
        help="Filter by status (repeatable or comma-separated), e.g. online,offline.",
    )
    p_machines.add_argument(
        "--name", default=None, metavar="SUBSTR",
        help="Filter by display name substring. Note: a label, not a unique key.",
    )

    p_machine = sub.add_parser("machine", parents=[common], help="Show a single machine.")
    p_machine.add_argument("machine_id", help="Machine ID. See ID column of `meshive machines`.")

    p_machine_metrics = sub.add_parser("machine-metrics", parents=[common],
                                       help="Show live metrics (CPU/RAM/GPU/disk/network) of a machine.")
    p_machine_metrics.add_argument("machine_id", help="Machine ID. See ID column of `meshive machines`.")

    p_earnings = sub.add_parser("earnings", parents=[common], help="Show your earnings (as a host).")
    p_earnings.add_argument("--since", default=None, metavar="DATE",
                            help="Start of the history window, YYYY-MM-DD (default: 90 days ago).")
    p_earnings.add_argument("--until", default=None, metavar="DATE",
                            help="End of the history window, YYYY-MM-DD (default: today).")
    p_earnings.add_argument("--days", type=int, default=7, metavar="N",
                            help="Show only the most recent N days of the daily table (default: 7; 0 = all).")

    # --- gpus / templates -----------------------------------------------------
    p_gpus = sub.add_parser("gpus", parents=[common],
                            help="List GPUs available to rent right now, with prices.")
    p_gpus.add_argument("--rental", choices=["demand", "spot"], default="demand",
                        help="Rental type to price for (default: demand).")
    p_gpus.add_argument("--vram", type=int, default=None, metavar="GB",
                        help="Only GPUs with at least this much VRAM.")
    p_gpus.add_argument("--model", default=None, metavar="SUBSTR",
                        help="Filter by GPU model substring (client-side), e.g. h100.")

    p_templates = sub.add_parser("templates", parents=[common],
                                 help="List templates (official, plus a workspace's custom ones).")
    p_templates.add_argument("--workspace", default=None, metavar="ID",
                             help="Also include this workspace's custom templates.")
    p_templates.add_argument("--type", choices=TEMPLATE_TYPES, type=str.lower, default=None,
                             dest="app_type", help="Filter by app type.")
    p_templates.add_argument("--name", default=None, metavar="SUBSTR",
                             help="Filter by name substring (client-side).")

    p_template = sub.add_parser("template", parents=[common], help="Show a single template.")
    p_template.add_argument("template_id", type=int, help="Template ID. See ID column of `meshive templates`.")
    p_template.add_argument("--workspace", default=None, metavar="ID",
                            help="Required for a custom template: the workspace that owns it.")

    # --- serverless -----------------------------------------------------------
    p_servings = sub.add_parser("servings", parents=[common],
                                help="List serverless serving deployments in a workspace.")
    p_servings.add_argument("workspace", help="Workspace ID (namespace name).")
    p_servings.add_argument(
        "--status", action="append", metavar="STATUS",
        help="Filter by status (repeatable or comma-separated). "
             f"One of: {', '.join(SERVING_STATUSES)}.",
    )
    p_servings.add_argument("--name", default=None, metavar="SUBSTR",
                            help="Filter by model name substring (client-side).")

    p_serving = sub.add_parser("serving", parents=[common], help="Show a single serving deployment.")
    p_serving.add_argument("serving_id", type=int, help="Serving ID. See ID column of `meshive servings`.")

    p_tasks = sub.add_parser("tasks", parents=[common],
                             help="List serverless tasks in a workspace (newest first).")
    p_tasks.add_argument("workspace", help="Workspace ID (namespace name).")
    p_tasks.add_argument(
        "--status", action="append", metavar="STATUS",
        help="Filter by status (server-side; repeatable or comma-separated). "
             f"One of: {', '.join(TASK_STATUSES)}.",
    )
    p_tasks.add_argument("--name", default=None, metavar="SUBSTR",
                         help="Filter by name substring (client-side).")
    p_tasks.add_argument("--limit", type=int, default=50, metavar="N",
                         help="Page size, 1-200 (default: 50).")
    p_tasks.add_argument("--offset", type=int, default=0, metavar="N",
                         help="Skip the first N tasks (default: 0).")

    p_task = sub.add_parser("task", parents=[common], help="Show a single task.")
    p_task.add_argument("task_id", help="Task ID (task_...). See ID column of `meshive tasks`.")

    p_task_out = sub.add_parser("task-outputs", parents=[common], help="List a task's output files, or download them.")
    p_task_out.add_argument("task_id", help="Task ID (task_...).")
    p_task_out.add_argument("--download", default=None, metavar="DIR", help="Save the files into DIR.")

    # --- assets (Asset Hub) ---------------------------------------------------
    p_assets = sub.add_parser("assets", parents=[common],
                              help="List assets in a workspace (datasets, models, outputs, ...).")
    p_assets.add_argument("workspace", help="Workspace ID (namespace name).")
    p_assets.add_argument("--type", choices=ASSET_TYPES, type=str.lower, default=None, dest="asset_type",
                          help="Filter by asset type.")
    p_assets.add_argument("--status", choices=ASSET_STATUSES, type=str.lower, default=None,
                          help="Filter by status. By default deleted, purged and merged assets are hidden.")
    p_assets.add_argument("--name", default=None, metavar="SUBSTR",
                          help="Filter by name substring (client-side, within the page).")
    p_assets.add_argument("--page", type=int, default=1, metavar="N", help="Page number (default: 1).")
    p_assets.add_argument("--page-size", type=int, default=20, metavar="N", dest="page_size",
                          help="Assets per page, 1-100 (default: 20).")

    p_asset = sub.add_parser("asset", parents=[common], help="Show a single asset with its files.")
    p_asset.add_argument("asset_id", help="Asset ID (asset_...). See ID column of `meshive assets`.")

    p_asset_dl = sub.add_parser("asset-download", parents=[common],
                                help="Download an asset's files, keeping their paths inside the asset.")
    p_asset_dl.add_argument("asset_id", help="Asset ID (asset_...).")
    p_asset_dl.add_argument("-d", "--dir", default=None, metavar="DIR",
                            help="Where to save (default: ./<asset_id>). Existing files are overwritten.")
    p_asset_dl.add_argument("--path", action="append", metavar="GLOB",
                            help="Only files whose path matches (fnmatch, repeatable), e.g. 'config/*'.")

    p_asset_storage = sub.add_parser("asset-storage", parents=[common],
                                     help="Show a workspace's managed asset storage, its cost, and credit status.")
    p_asset_storage.add_argument("workspace", help="Workspace ID (namespace name).")

    # --- account --------------------------------------------------------------
    write_cli.add_parsers(sub, common)

    sub.add_parser("api-keys", parents=[common], aliases=["keys"],
                   help="List your API keys (prefixes only; the secret is never shown).")
    sub.add_parser("credit", parents=[common],
                   help="Show your credit balance (`-o name` prints just the balance).")

    p_credit_history = sub.add_parser("credit-history", parents=[common],
                                      help="List credit top-ups and refunds.")
    p_credit_history.add_argument("--since", default=None, metavar="DATE",
                                  help="Start date, YYYY-MM-DD (default: 90 days ago).")
    p_credit_history.add_argument("--until", default=None, metavar="DATE",
                                  help="End date, YYYY-MM-DD (default: today).")

    return parser


# =============================================================================
# Filter / validation helpers
# =============================================================================

def _parse_status_filter(values: list[str] | None) -> set[str]:
    """--status values (repeated/comma-separated) as a lowercase set. Empty set if none."""
    result: set[str] = set()
    for value in values or []:
        result.update(s.strip().lower() for s in value.split(",") if s.strip())
    return result


def _reject_unknown_statuses(statuses: set[str], valid: tuple[str, ...]) -> int | None:
    """If there is an unknown status, print the valid values to stderr and return exit code 2."""
    unknown = statuses - set(valid)
    if unknown:
        print(f"Error: unknown status: {', '.join(sorted(unknown))}. "
              f"Valid: {', '.join(valid)}.", file=sys.stderr)
        return 2
    return None


def _contains(haystack: str | None, needle: str | None) -> bool:
    """Substring filter for --name and the like (case-insensitive). Always True without a needle."""
    if not needle:
        return True
    return needle.lower() in (haystack or "").lower()


def _gather_all_pods(client: Meshive) -> list[Pod]:
    """Collect pods from every workspace. If one workspace fails to load,
    warn and keep going instead of failing the whole thing (it's an overview)."""
    pods: list[Pod] = []
    for ws in client.list_workspaces():
        try:
            pods.extend(client.list_pods(ws.namespace_name))
        except MeshiveError as err:
            print(fmt.clean(f"Warning: skipped workspace {ws.namespace_name}: {err}"), file=sys.stderr)
    return pods


def _filter_pods(pods: list[Pod], statuses: set[str], rental: str | None, name: str | None) -> list[Pod]:
    if statuses:
        pods = [p for p in pods if p.status.lower() in statuses]
    if rental:
        pods = [p for p in pods if p.rental_type.lower() == rental]
    if name:
        pods = [p for p in pods if _contains(p.user_alias, name)]
    return pods


def _filter_machines(
    machines: list[Machine], statuses: set[str], machine_type: str | None, name: str | None
) -> list[Machine]:
    if statuses:
        machines = [m for m in machines if m.status.lower() in statuses]
    if machine_type:
        machines = [m for m in machines if m.machine_type.lower() == machine_type]
    if name:
        machines = [m for m in machines if _contains(m.name, name)]
    return machines


def _filter_storages(
    storages: list[Storage], statuses: set[str], storage_type: str | None, name: str | None
) -> list[Storage]:
    if statuses:
        storages = [s for s in storages if s.status.lower() in statuses]
    if storage_type:
        storages = [s for s in storages if s.storage_type.lower() == storage_type]
    if name:
        storages = [s for s in storages if _contains(s.user_alias, name)]
    return storages


def _filter_servings(servings: list[Serving], statuses: set[str], name: str | None) -> list[Serving]:
    if statuses:
        servings = [s for s in servings if s.status.lower() in statuses]
    if name:
        servings = [s for s in servings if _contains(_serving_label(s), name)]
    return servings


# =============================================================================
# Human-readable output (table format)
# =============================================================================

def _pod_price(pod: Pod) -> str:
    """Same as the web pod page: a price only while compute is billing, otherwise '-'.
    Based on the server's billing_active; for older servers that don't send it, running counts as billing."""
    billed = pod.billing_active if pod.billing_active is not None else pod.status.lower() == "running"
    return fmt.money_hourly(pod.price_per_hour) if billed else "-"


def _print_whoami(me: WhoAmI, color: bool) -> None:
    # Server-provided strings go through fmt.clean to strip control characters (defense against terminal escape injection).
    print(f"email:    {fmt.clean(me.email)}")
    print(f"username: {fmt.clean(me.username or '-')}")
    print(f"role:     {fmt.clean(me.user_role)}")


def _print_workspaces(workspaces: list[Workspace], color: bool) -> None:
    if not workspaces:
        print("No workspaces.")
        return
    # NAME = workspace_name (user label), ID = namespace_name (lookup key).
    rows = [
        [ws.workspace_name or "-", ws.namespace_name, ws.member_role or "-", fmt.status_cell(ws.status),
         str(ws.resources.pod), fmt.money_hourly(ws.price_per_hour)]
        for ws in workspaces
    ]
    colors = [[None, None, None, fmt.status_color(ws.status), None, None] for ws in workspaces]
    fmt.render_table(
        ["NAME", "ID", "ROLE", "STATUS", "PODS", "PRICE/HR"],
        rows,
        aligns=["l", "l", "l", "l", "r", "r"],
        colors=colors,
        enabled=color,
    )


def _print_workspace(ws: WorkspaceDetail, color: bool) -> None:
    print(f"name:      {fmt.clean(ws.workspace_name or '-')}")  # user label
    print(f"id:        {fmt.clean(ws.namespace_name)}")         # lookup key
    print(f"price/hr:  {fmt.money_hourly(ws.price_per_hour)}")
    print(f"avg/day:   {fmt.money(ws.weekly_avg_daily_cost)}  (last 7 days)")
    print(f"gpus:      {ws.gpus}")
    print(f"vcpus:     {ws.vcpus}")
    print(f"ram:       {fmt.gib(ws.ram)}")
    print(f"storage:   {fmt.gib(ws.total_storage)}")
    if ws.resources:
        print()
        rows = [[r.type, str(r.active), str(r.paused), str(r.disabled)] for r in ws.resources]
        fmt.render_table(["RESOURCE", "ACTIVE", "PAUSED", "DISABLED"], rows,
                         aligns=["l", "r", "r", "r"], enabled=color)
    if ws.costs:
        print()
        rows = [[fmt.date_str(c.date), fmt.money(c.pod), fmt.money(c.storage), fmt.money(c.serverless),
                 fmt.money(c.task), fmt.money(c.asset), fmt.money(c.total)]
                for c in ws.costs[-7:]]  # the server sends the last 30 days (oldest first) — only the last 7
        fmt.render_table(["DATE", "POD", "STORAGE", "SERVERLESS", "TASK", "ASSET", "TOTAL"], rows,
                         aligns=["l", "r", "r", "r", "r", "r", "r"], enabled=color)


def _print_members(members: list[Member], color: bool) -> None:
    if not members:
        print("No members.")
        return
    rows = [[m.user, m.role or "-", fmt.relative_time(m.joined_at)] for m in members]
    colors = [[None, None, "dim"] for _ in members]
    fmt.render_table(["USER", "ROLE", "JOINED"], rows, colors=colors, enabled=color)


def _print_pods(pods: list[Pod], color: bool, show_workspace: bool = False) -> None:
    if not pods:
        print("No pods.")
        return
    # NAME = user_alias (user label), ID = pod_name (lookup key). --all adds WORKSPACE (namespace_name).
    headers = ["NAME", "ID"]
    aligns = ["l", "l"]
    if show_workspace:
        headers.append("WORKSPACE")
        aligns.append("l")
    headers += ["STATUS", "RENTAL", "PRICE/HR", "CREATED"]
    aligns += ["l", "l", "r", "l"]

    rows = []
    colors = []
    for pod in pods:
        row = [pod.user_alias or "-", pod.pod_name]
        color_row = [None, None]
        if show_workspace:
            row.append(pod.namespace_name)
            color_row.append(None)
        row += [fmt.status_cell(pod.status), pod.rental_type,
                _pod_price(pod), fmt.relative_time(pod.created_at)]
        color_row += [fmt.status_color(pod.status), None, None, "dim"]
        rows.append(row)
        colors.append(color_row)

    fmt.render_table(headers, rows, aligns=aligns, colors=colors, enabled=color)


def _print_pod(pod: Pod, color: bool, show_secrets: bool = False) -> None:
    created = "-"
    if pod.created_at:
        created = f"{fmt.relative_time(pod.created_at)} ({pod.created_at.isoformat()})"
    print(f"name:      {fmt.clean(pod.user_alias or '-')}")  # user label
    print(f"id:        {fmt.clean(pod.pod_name)}")           # lookup key
    print(f"workspace: {fmt.clean(pod.namespace_name)}")
    print(f"status:    {fmt.paint(fmt.clean(fmt.status_cell(pod.status)), fmt.status_color(pod.status), color)}")
    print(f"rental:    {fmt.clean(pod.rental_type)}")
    print(f"price/hr:  {_pod_price(pod)}")
    print(f"created:   {created}")
    # The lines below are printed only when they apply (only when meaningful, not a list of booleans).
    premiums = [n for n, on in (("cpu", pod.cpu_premium), ("uptime", pod.uptime_premium),
                                ("internet", pod.internet_premium)) if on]
    if premiums:
        print(f"premium:   {', '.join(premiums)}")
    if pod.waiting_mode:
        print(f"waiting:   for {fmt.clean(pod.waiting_mode.replace('_', ' '))}")
    if pod.stop_reason_code and pod.stop_reason_code != "user":
        reason = pod.stop_reason_code + (f": {pod.stop_reason_detail}" if pod.stop_reason_detail else "")
        print(fmt.paint(fmt.clean(f"stopped by the system ({reason})"), "yellow", color))
    if pod.same_node_unavailable_reason:
        print(fmt.paint(fmt.clean(f"can't start on its node now: {pod.same_node_unavailable_reason.replace('_', ' ')}"),
                        "yellow", color))
    if pod.is_maintenance:
        print(fmt.paint("⚠ under maintenance", "yellow", color))
    if pod.endpoints:
        print()
        rows = [[e.name or "-", str(e.port), e.external_url or e.internal_url or "-", e.readiness]
                for e in pod.endpoints]
        colors = [[None, None, None, "green" if e.readiness == "ready" else "yellow"] for e in pod.endpoints]
        fmt.render_table(["ENDPOINT", "PORT", "URL", "STATE"], rows, aligns=["l", "r", "l", "l"],
                         colors=colors, enabled=color)
    if pod.connect_credentials:
        print()
        hidden = False
        rows = []
        for c in pod.connect_credentials:
            masked = c.is_secret and not show_secrets
            hidden = hidden or masked
            rows.append([c.key, "••••••••" if masked else c.value])
        fmt.render_table(["CREDENTIAL", "VALUE"], rows, enabled=color)
        if hidden:
            print(fmt.paint("Secret values are hidden. Add --show-secrets to print them.", "dim", color))


def _print_gpu_usages(gpus, color: bool) -> None:
    for g in gpus:
        # When total VRAM can't be read the server sends None (missing DCGM FB metrics, GPU lookup failure); when both FB values are 0 it sends 0
        # — neither is the card size, so print n/a like utilization rather than '0 GB'.
        size = fmt.vram(g.vram_size) if g.vram_size else "n/a"
        print(f"gpu {g.gpu_number}:     {fmt.usage(g.core_usage_rate)} core, "
              f"{fmt.usage(g.vram_usage_rate)} of {size} vram, {fmt.temperature(g.temp)}")


def _print_pod_metrics(m: PodMetrics, color: bool) -> None:
    print(f"pod:       {fmt.clean(m.pod_name)}")
    print(f"cpu:       {m.cpu_cores:g} cores, {fmt.usage(m.cpu_usage_rate)} used")
    print(f"ram:       {fmt.gib(m.ram_size)}, {fmt.usage(m.ram_usage_rate)} used")
    _print_gpu_usages(m.gpus, color)
    print(f"ephemeral: {fmt.gib(m.ephemeral_storage_usage)} used of {fmt.gib(m.ephemeral_storage_request)}")


def _print_machine_metrics(m: MachineMetrics, color: bool) -> None:
    print(f"machine:   {fmt.clean(m.machine_id)}")
    print(f"cpu:       {m.cpu_cores:g} cores, {fmt.usage(m.cpu_usage_rate)} used, "
          f"{m.cpu_allocated:g} allocated to pods")
    # Of the machine metrics only ram_size is in bytes (the server passes node_memory_MemTotal_bytes through). Allocated RAM, disk and
    # VRAM are MiB — reading them the same way prints a 64 GiB machine as '65,648,036 GB'.
    print(f"ram:       {fmt.gib(m.ram_size / 1024 ** 2)}, {fmt.usage(m.ram_usage_rate)} used, "
          f"{fmt.gib(m.ram_allocated)} allocated to pods")
    _print_gpu_usages(m.gpus, color)
    print(f"root disk: {fmt.gib(m.root_volume_size)}, {fmt.usage(m.root_volume_usage_rate)} used")
    print(f"pv disk:   {fmt.gib(m.pv_volume_size)}, {fmt.usage(m.pv_volume_usage_rate)} used")
    print(f"network:   rx {fmt.mbps(m.network_receive)}, tx {fmt.mbps(m.network_transmit)}")


def _print_storages(storages: list[Storage], color: bool) -> None:
    if not storages:
        print("No storages.")
        return
    # NAME = user_alias (user label), ID = pv_name (lookup key).
    rows = []
    colors = []
    for s in storages:
        rows.append([
            s.user_alias or "-", s.pv_name, s.storage_type or "-", fmt.status_cell(s.status),
            fmt.gib(s.total_size), fmt.usage(s.usage_rate), fmt.money_hourly(s.price_per_hour),
            str(len(s.linked_pods)), fmt.relative_time(s.created_at),
        ])
        colors.append([None, None, None, fmt.status_color(s.status), None, None, None, None, "dim"])
    fmt.render_table(
        ["NAME", "ID", "TYPE", "STATUS", "SIZE", "USED", "PRICE/HR", "PODS", "CREATED"],
        rows,
        aligns=["l", "l", "l", "l", "r", "r", "r", "r", "l"],
        colors=colors,
        enabled=color,
    )


def _print_storage(s: Storage, color: bool) -> None:
    created = "-"
    if s.created_at:
        created = f"{fmt.relative_time(s.created_at)} ({s.created_at.isoformat()})"
    print(f"name:      {fmt.clean(s.user_alias or '-')}")  # user label
    print(f"id:        {fmt.clean(s.pv_name)}")            # lookup key
    print(f"workspace: {fmt.clean(s.namespace_name)}")
    print(f"type:      {fmt.clean(s.storage_type or '-')}")
    print(f"status:    {fmt.paint(fmt.clean(fmt.status_cell(s.status)), fmt.status_color(s.status), color)}")
    print(f"size:      {fmt.gib(s.total_size)}, {fmt.usage(s.usage_rate)} used ({fmt.gib(s.available_size)} free)")
    print(f"price/hr:  {fmt.money_hourly(s.price_per_hour)}")
    print(f"pods:      {fmt.clean(', '.join(s.linked_pods)) if s.linked_pods else '-'}")
    print(f"encrypted: {fmt.yes_no(s.encrypted)}")
    print(f"created:   {created}")
    if s.is_maintenance:
        print(fmt.paint("⚠ under maintenance", "yellow", color))


def _gpu_cell(machine: Machine) -> str:
    """'8x NVIDIA H100' form. Machines without GPUs (cpu/storage) get '-'."""
    if not machine.gpu_count:
        return "-"
    return f"{machine.gpu_count}x {machine.gpu_model}" if machine.gpu_model else str(machine.gpu_count)


def _print_machines(machines: list[Machine], color: bool) -> None:
    if not machines:
        print("No machines.")
        return
    # NAME = name (user label), ID = machine_id (lookup key).
    rows = []
    colors = []
    for m in machines:
        rows.append([
            m.name or "-", m.machine_id, m.machine_type or "-",
            fmt.status_cell(m.status), _gpu_cell(m),
            fmt.money(m.earning_hourly), fmt.percent(m.uptime_rate),
        ])
        colors.append([None, None, None, fmt.status_color(m.status), None, None, None])
    fmt.render_table(
        ["NAME", "ID", "TYPE", "STATUS", "GPU", "EARN/HR", "UPTIME"],
        rows,
        aligns=["l", "l", "l", "l", "l", "r", "r"],
        colors=colors,
        enabled=color,
    )


def _print_machine(machine: Machine, color: bool) -> None:
    print(f"name:      {fmt.clean(machine.name or '-')}")  # user label
    print(f"id:        {fmt.clean(machine.machine_id)}")   # lookup key
    print(f"type:      {fmt.clean(machine.machine_type or '-')}")
    print(f"status:    {fmt.paint(fmt.clean(fmt.status_cell(machine.status)), fmt.status_color(machine.status), color)}")
    print(f"gpu:       {fmt.clean(_gpu_cell(machine))}")
    print(f"earn/hr:   {fmt.money(machine.earning_hourly)}")
    print(f"uptime:    {fmt.percent(machine.uptime_rate)}")
    print(f"tier:      {machine.host_tier or '-'}")


def _print_earnings(e: Earnings, days: int, color: bool) -> None:
    print(f"current/hr:    {fmt.money(e.current_hourly)}")
    print(f"today:         {fmt.money(e.daily)}")
    print(f"until payout:  {fmt.money(e.accumulated_until_payout)}")
    history = e.history[:days] if days > 0 else e.history  # the server sends newest first
    if history:
        print()
        rows = [[fmt.date_str(h.date), fmt.money(h.cpu), fmt.money(h.gpu), fmt.money(h.storage),
                 fmt.money(h.total)] for h in history]
        fmt.render_table(["DATE", "CPU", "GPU", "STORAGE", "TOTAL"], rows,
                         aligns=["l", "r", "r", "r", "r"], enabled=color)


def _print_gpus(gpus: list[GpuAvailability], color: bool) -> None:
    if not gpus:
        print("No GPUs available.")
        return
    rows = [[g.gpu_model, f"{g.vram} GB", g.rental_type or "-", fmt.money_hourly(g.price_per_hour),
             str(g.available_gpus), str(g.max_gpus_per_pod), str(g.machine_count)] for g in gpus]
    fmt.render_table(
        ["GPU", "VRAM", "RENTAL", "PRICE/HR", "AVAILABLE", "MAX/POD", "MACHINES"],
        rows,
        aligns=["l", "r", "l", "r", "r", "r", "r"],
        enabled=color,
    )


def _template_source(t: Template) -> str:
    return "official" if t.is_official else "custom"


def _print_templates(templates: list[Template], color: bool) -> None:
    if not templates:
        print("No templates.")
        return
    rows = [[t.name, str(t.template_id), t.app_type or "-", _template_source(t),
             t.hardware_type or "-", t.image] for t in templates]
    fmt.render_table(["NAME", "ID", "TYPE", "SOURCE", "HARDWARE", "IMAGE"], rows, enabled=color)


def _print_template(t: Template, color: bool) -> None:
    app_type = t.app_type or "-"
    if t.app_sub_type:
        app_type += f" / {t.app_sub_type}"
    framework = t.framework or "-"
    if t.framework and t.framework_version:
        framework += f" {t.framework_version}"
    print(f"name:        {fmt.clean(t.name)}")
    print(f"id:          {t.template_id}")   # lookup key
    print(f"source:      {_template_source(t)}")
    print(f"type:        {fmt.clean(app_type)}")
    print(f"deploy:      {fmt.clean(t.deploy_type or '-')}")
    print(f"hardware:    {fmt.clean(t.hardware_type or '-')}")
    print(f"image:       {fmt.clean(t.image)}")
    print(f"cuda:        {fmt.clean(t.cuda_version or '-')}")
    print(f"framework:   {fmt.clean(framework)}")
    print(f"description: {fmt.clean(t.description or '-')}")


def _serving_label(s: Serving) -> str:
    return s.model_name or s.api_model_id or "-"


def _serving_status_cell(s: Serving) -> str:
    cell = fmt.status_cell(s.status)
    return f"{cell} (paused)" if s.paused else cell


def _print_servings(servings: list[Serving], color: bool) -> None:
    if not servings:
        print("No servings.")
        return
    rows = []
    colors = []
    for s in servings:
        healthy = "-" if s.healthy_replicas is None else str(s.healthy_replicas)
        rows.append([_serving_label(s), str(s.serving_id), _serving_status_cell(s),
                     f"{s.current_replicas} ({s.min_replicas}-{s.max_replicas})", healthy,
                     fmt.money_hourly(s.price_per_hour) if s.billing_active else "-"])
        colors.append([None, None, fmt.status_color(s.status), None, None, None])
    fmt.render_table(
        ["NAME", "ID", "STATUS", "REPLICAS", "HEALTHY", "PRICE/HR"],
        rows,
        aligns=["l", "r", "l", "l", "r", "r"],
        colors=colors,
        enabled=color,
    )


def _print_serving(s: Serving, color: bool) -> None:
    healthy = "" if s.healthy_replicas is None else f", {s.healthy_replicas} healthy"
    print(f"name:      {fmt.clean(_serving_label(s))}")
    print(f"id:        {s.serving_id}")   # lookup key
    print(f"workspace: {fmt.clean(s.namespace_name)}")
    print(f"model id:  {fmt.clean(s.api_model_id or '-')}")
    print(f"framework: {fmt.clean(s.framework or '-')}")
    print(f"status:    {fmt.paint(fmt.clean(_serving_status_cell(s)), fmt.status_color(s.status), color)}")
    print(f"replicas:  {s.current_replicas} running, {s.min_replicas}-{s.max_replicas} configured{healthy}")
    print(f"endpoint:  {fmt.clean(s.endpoint_url or '-')}")
    print(f"price/hr:  {fmt.money_hourly(s.price_per_hour) if s.billing_active else '-'}")


def _task_gpu(t: Task) -> str:
    if not t.gpu_model:
        return "cpu"
    return f"{t.gpu_count}x {t.gpu_model}" if t.gpu_count else t.gpu_model


def _print_tasks(tasks: list[Task], color: bool) -> None:
    if not tasks:
        print("No tasks.")
        return
    rows = []
    colors = []
    for t in tasks:
        rows.append([t.name or "-", t.task_id, fmt.status_cell(t.status), _task_gpu(t),
                     fmt.money(t.cost_so_far), fmt.relative_time(t.created_at)])
        colors.append([None, None, fmt.status_color(t.status), None, None, "dim"])
    fmt.render_table(
        ["NAME", "ID", "STATUS", "GPU", "COST", "CREATED"],
        rows,
        aligns=["l", "l", "l", "l", "r", "l"],
        colors=colors,
        enabled=color,
    )


def _print_task(t: Task, color: bool) -> None:
    def when(dt):
        return f"{fmt.relative_time(dt)} ({dt.isoformat()})" if dt else "-"

    print(f"name:        {fmt.clean(t.name or '-')}")
    print(f"id:          {fmt.clean(t.task_id)}")   # lookup key
    print(f"workspace:   {fmt.clean(t.namespace_name)}")
    print(f"status:      {fmt.paint(fmt.clean(fmt.status_cell(t.status)), fmt.status_color(t.status), color)}")
    print(f"pod:         {fmt.clean(t.pod_name or '-')}")
    print(f"image:       {fmt.clean(t.image or '-')}")
    print(f"gpu:         {fmt.clean(_task_gpu(t))}")
    print(f"cpu/ram:     {t.cpu_cores} cores / {t.ram_gb} GiB")
    print(f"price/hr:    {fmt.money_hourly(t.price_per_hour)}")
    print(f"cost so far: {fmt.money(t.cost_so_far)}")
    print(f"total cost:  {fmt.money(t.total_cost)}")
    print(f"created:     {when(t.created_at)}")
    print(f"started:     {when(t.container_running_at)}")
    print(f"finished:    {when(t.finished_at)}")
    if t.exit_code is not None:
        print(f"exit code:   {t.exit_code}")
    if t.failure_reason:
        print(fmt.paint(fmt.clean(f"failure:     {t.failure_reason}"), "red", color))
    if t.failed_asset_id:
        print(fmt.paint(fmt.clean(f"failed on:   input asset {t.failed_asset_id}"), "red", color))
    if t.output_upload_state and t.output_upload_state not in ("skipped", "discarded"):
        files = f"{t.output_files_completed or 0} of {t.output_files_declared or 0} files"
        size = f"{fmt.bytes_human(t.output_bytes_completed)} of {fmt.bytes_human(t.output_bytes_declared)}"
        print(f"outputs:     {fmt.clean(t.output_upload_state)}, {files}, {size}")
    if t.output_upload_last_error:
        print(fmt.paint(fmt.clean(f"upload error: {t.output_upload_last_error}"), "red", color))
    if t.outputs_purged_at:
        print(f"outputs:     deleted {fmt.relative_time(t.outputs_purged_at)}")
    if t.input_assets:
        print()
        rows = [[a.name or "-", a.asset_id, a.target_dir or "-"] for a in t.input_assets]
        colors = [[None, None, "red" if a.asset_id == t.failed_asset_id else None] for a in t.input_assets]
        fmt.render_table(["INPUT", "ID", "DIR"], rows, colors=colors, enabled=color)


_STORAGE_PROVIDER_LABELS = {"meshive_r2": "managed", "user_s3": "s3", "external": "external"}


def _storage_label(provider: str | None) -> str:
    """meshive_r2 → managed, user_s3 → s3, external → external. Anything else as-is."""
    if not provider:
        return "-"
    return _STORAGE_PROVIDER_LABELS.get(provider.lower(), provider)


def _print_assets(assets: list[Asset], page: AssetPage, color: bool) -> None:
    if not assets:
        print("No assets.")
    else:
        rows = []
        colors = []
        for a in assets:
            rows.append([a.name or "-", a.asset_id, a.asset_type or "-", fmt.status_cell(a.status),
                         fmt.bytes_human(a.size_bytes), "-" if a.file_count is None else str(a.file_count),
                         _storage_label(a.storage_provider), fmt.relative_time(a.updated_at)])
            colors.append([None, None, None, fmt.status_color(a.status), None, None, None, "dim"])
        fmt.render_table(
            ["NAME", "ID", "TYPE", "STATUS", "SIZE", "FILES", "STORAGE", "UPDATED"],
            rows,
            aligns=["l", "l", "l", "l", "r", "r", "l", "l"],
            colors=colors,
            enabled=color,
        )
    if page.page > page.pages:
        print(fmt.paint(f"Page {page.page} is past the end: {page.total} asset"
                        f"{'s' if page.total != 1 else ''} on {page.pages} page{'s' if page.pages != 1 else ''}.",
                        "dim", color))
    elif page.pages > 1:
        print(fmt.paint(f"Page {page.page} of {page.pages} ({page.total} assets). Use --page to see more.",
                        "dim", color))


def _print_asset(a: Asset, color: bool) -> None:
    status = fmt.status_cell(a.status)
    if a.status_reason:
        status += f" ({a.status_reason})"
    print(f"name:       {fmt.clean(a.name or '-')}")
    print(f"id:         {fmt.clean(a.asset_id)}")   # lookup key
    print(f"workspace:  {fmt.clean(a.namespace_name or '-')}")
    print(f"type:       {fmt.clean(a.asset_type or '-')}")
    print(f"status:     {fmt.paint(fmt.clean(status), fmt.status_color(a.status), color)}")
    print(f"storage:    {fmt.clean(_storage_label(a.storage_provider))}")
    if a.size_bytes is None:
        print("size:       not measured yet")
    else:
        print(f"size:       {fmt.bytes_human(a.size_bytes)} in {a.file_count} file{'s' if a.file_count != 1 else ''}")
    if a.upload_status and a.upload_status != "ready":
        upload = "interrupted" if a.stale else a.upload_status
        print(f"upload:     {fmt.paint(fmt.clean(upload), 'red' if upload in ('failed', 'interrupted') else 'yellow', color)}")
    print(f"source:     {fmt.clean(a.ingest_source or '-')}")
    print(f"created by: {fmt.clean(a.created_by or '-')}")
    print(f"created:    {fmt.relative_time(a.created_at)}")
    print(f"updated:    {fmt.relative_time(a.updated_at)}")
    if a.in_use:
        where = ", ".join(f"{c.kind or '?'} {c.name or c.identifier or '?'}" for c in a.active_usage_contexts)
        print(f"in use:     yes{f' ({fmt.clean(where)})' if where else ''}")
    if a.import_failure_reason:
        print(fmt.paint(fmt.clean(f"import failed: {a.import_failure_reason}"), "red", color))
    if a.files:
        print()
        shown = a.files[:_ASSET_FILE_ROWS]
        rows = [[f.path, fmt.bytes_human(f.size_bytes), fmt.status_cell(f.status)] for f in shown]
        colors = [[None, None, fmt.status_color(f.status)] for f in shown]
        fmt.render_table(["FILE", "SIZE", "STATUS"], rows, aligns=["l", "r", "l"], colors=colors, enabled=color)
        if len(a.files) > len(shown):
            print(fmt.paint(f"... and {len(a.files) - len(shown)} more files (-o json lists them all).", "dim", color))


_ASSET_FILE_ROWS = 50


def _print_asset_storage(s: AssetStorage, color: bool) -> None:
    print(f"managed:       {fmt.bytes_human(s.managed_bytes)}")
    print(f"price:         {fmt.money(s.price_per_gb_month)} per GiB-month")
    print(f"est. monthly:  {fmt.money(s.estimated_monthly_cost)}")
    state = s.credit_state or "-"
    tone = None
    if s.credit_state == "grace":
        tone = "yellow"
        if s.blocks_at:
            state += f" (uploads block {fmt.relative_time(s.blocks_at)})"
    elif s.credit_state == "blocked" or s.credit_blocked:
        tone = "red"
        if s.purge_deadline_at:
            state += f" (managed assets are deleted {fmt.relative_time(s.purge_deadline_at)} unless credit is added)"
    print(f"credit:        {fmt.paint(fmt.clean(state), tone, color)}")
    print(f"billing credit: {'available' if s.paid_balance_available else 'none (pods and tasks cannot start)'}")


def _print_api_keys(keys: list[ApiKey], color: bool) -> None:
    if not keys:
        print("No API keys.")
        return
    rows = []
    colors = []
    for k in keys:
        rows.append([
            k.name or "-", str(k.key_id), k.prefix, ",".join(k.scopes) or "-",
            fmt.status_cell(k.status), fmt.relative_time(k.created_at),
            fmt.relative_time(k.last_used_at) if k.last_used_at else "never",
            fmt.relative_time(k.expires_at) if k.expires_at else "never",
        ])
        colors.append([None, None, None, None, fmt.status_color(k.status), "dim", "dim", "dim"])
    fmt.render_table(
        ["NAME", "ID", "PREFIX", "SCOPES", "STATUS", "CREATED", "LAST USED", "EXPIRES"],
        rows,
        aligns=["l", "r", "l", "l", "l", "l", "l", "l"],
        colors=colors,
        enabled=color,
    )


def _print_credit(c: Credit, color: bool) -> None:
    print(f"balance:         {fmt.money(c.balance)}")
    if c.auto_recharge:
        recharge = f"on (add {fmt.money(c.auto_recharge_amount)} when below {fmt.money(c.auto_recharge_threshold)})"
    else:
        recharge = "off"
    print(f"auto-recharge:   {recharge}")
    print(f"payment method:  {'on file' if c.has_default_payment_method else 'none'}")


def _print_credit_history(entries: list[CreditHistoryEntry], color: bool) -> None:
    if not entries:
        print("No credit history.")
        return
    rows = [[fmt.date_str(e.created_at.date() if e.created_at else None), fmt.money(e.amount),
             e.payment_method or "-", fmt.yes_no(e.is_paid), str(e.entry_id)] for e in entries]
    colors = [[None, "red" if e.amount < 0 else None, None, None, None] for e in entries]
    fmt.render_table(["DATE", "AMOUNT", "METHOD", "PAID", "ID"], rows,
                     aligns=["l", "r", "l", "l", "r"], colors=colors, enabled=color)


# =============================================================================
# Command handlers — (client, args, output, color) -> exit code
# =============================================================================

def _emit(output: str, raw: object, ids: list[str], show) -> None:
    """Print in the selected format. json = raw payload, name = IDs only, table = human renderer (show)."""
    if output == "json":
        print(json.dumps(raw, indent=2, ensure_ascii=False))
    elif output == "name":
        for value in ids:
            print(fmt.clean(value))
    else:
        show()


Handler = Callable[[Meshive, argparse.Namespace, str, bool], int]


def _cmd_me(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    me = client.me()
    _emit(output, me.raw, [me.email], lambda: _print_whoami(me, color))
    return 0


def _cmd_workspaces(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    workspaces = client.list_workspaces()
    _emit(output, [w.raw for w in workspaces], [w.namespace_name for w in workspaces],
          lambda: _print_workspaces(workspaces, color))
    return 0


def _cmd_workspace(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    ws = client.get_workspace(args.workspace)
    _emit(output, ws.raw, [ws.namespace_name], lambda: _print_workspace(ws, color))
    return 0


def _cmd_members(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    members = client.list_members(args.workspace)
    _emit(output, [m.raw for m in members], [m.user for m in members],
          lambda: _print_members(members, color))
    return 0


def _cmd_pods(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if args.all_workspaces and args.workspace:
        print("Error: pass a workspace or --all, not both.", file=sys.stderr)
        return 2
    if not args.all_workspaces and not args.workspace:
        print("Error: provide a workspace, or use --all for every workspace.", file=sys.stderr)
        return 2
    statuses = _parse_status_filter(args.status)
    code = _reject_unknown_statuses(statuses, POD_STATUSES)
    if code:
        return code
    raw_pods = _gather_all_pods(client) if args.all_workspaces else client.list_pods(args.workspace)
    pods = _filter_pods(raw_pods, statuses, args.rental, args.name)
    _emit(output, [p.raw for p in pods], [p.pod_name for p in pods],
          lambda: _print_pods(pods, color, show_workspace=args.all_workspaces))
    return 0


def _cmd_pod(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if args.wait and args.wait.lower() not in POD_STATUSES:
        print(f"Error: unknown status: {args.wait}. "
              f"Valid: {', '.join(POD_STATUSES)}.", file=sys.stderr)
        return 2
    pod = (
        client.wait_for_pod(args.pod_name, args.workspace,
                            until=args.wait, timeout=args.wait_timeout)
        if args.wait else client.get_pod(args.pod_name, args.workspace)
    )
    _emit(output, pod.raw, [pod.pod_name], lambda: _print_pod(pod, color, args.show_secrets))
    return 0


def _cmd_pod_metrics(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    metrics = client.get_pod_metrics(args.pod_name, args.workspace)
    _emit(output, metrics.raw, [metrics.pod_name], lambda: _print_pod_metrics(metrics, color))
    return 0


def _print_transactions(transactions: list[Transaction], color: bool) -> None:
    if not transactions:
        # Finished operations drop out of the list — an empty list means "nothing in progress", not "no failures".
        print("No in-flight pod operations.")
        return
    rows = []
    colors = []
    for t in transactions:
        rows.append([
            t.user_alias or t.resource_name or "-", str(t.transaction_id), t.action or "-",
            fmt.status_cell(t.status), t.step or "-",
            _txn_progress(t),
            fmt.clean(t.detail) if t.detail else "-",
            fmt.relative_time(t.updated_at),
        ])
        colors.append([None, None, None, fmt.status_color(t.status), None, None, None, "dim"])
    fmt.render_table(
        ["POD", "TXN", "ACTION", "STATUS", "STEP", "PROGRESS", "DETAIL", "UPDATED"],
        rows, colors=colors, enabled=color)
    # If an input asset download failed, a few lines from the end of that container's log — the same excerpt as the console's failure card (sanitized by the server).
    for t in transactions:
        for log in t.init_logs:
            if not log.lines:
                continue
            print()
            print(fmt.paint(f"txn {t.transaction_id} {log.container} log"
                            f"{' (before restart)' if log.previous else ''}:", "dim", color))
            for line in log.lines[-_INIT_LOG_LINES:]:
                print(f"  {fmt.clean(line)}")


_INIT_LOG_LINES = 10


def _txn_progress(t: Transaction) -> str:
    """Progress; if bytes have stalled, append the phase name (verifying · waiting for storage)."""
    cell = "-" if t.progress is None else f"{t.progress * 100:.0f}%"
    if t.phase:
        cell = t.phase.replace("_", " ") if cell == "-" else f"{cell} {t.phase.replace('_', ' ')}"
    return cell


def _cmd_transactions(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    transactions = client.list_transactions(args.workspace)
    _emit(output, [t.raw for t in transactions], [str(t.transaction_id) for t in transactions],
          lambda: _print_transactions(transactions, color))
    return 0


def _cmd_storages(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    statuses = _parse_status_filter(args.status)
    code = _reject_unknown_statuses(statuses, POD_STATUSES)
    if code:
        return code
    storages = _filter_storages(client.list_storages(args.workspace), statuses, args.storage_type, args.name)
    _emit(output, [s.raw for s in storages], [s.pv_name for s in storages],
          lambda: _print_storages(storages, color))
    return 0


def _cmd_storage(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    storage = client.get_storage(args.storage_name, args.workspace)
    _emit(output, storage.raw, [storage.pv_name], lambda: _print_storage(storage, color))
    return 0


def _cmd_machines(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    statuses = _parse_status_filter(args.status)
    machines = _filter_machines(client.list_machines(), statuses, args.machine_type, args.name)
    _emit(output, [m.raw for m in machines], [m.machine_id for m in machines],
          lambda: _print_machines(machines, color))
    return 0


def _cmd_machine(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    machine = client.get_machine(args.machine_id)
    _emit(output, machine.raw, [machine.machine_id], lambda: _print_machine(machine, color))
    return 0


def _cmd_machine_metrics(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    metrics = client.get_machine_metrics(args.machine_id)
    _emit(output, metrics.raw, [metrics.machine_id], lambda: _print_machine_metrics(metrics, color))
    return 0


def _cmd_earnings(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if args.days < 0:
        print("Error: --days must be 0 or greater.", file=sys.stderr)
        return 2
    earnings = client.get_earnings(start_date=args.since, end_date=args.until)
    # Single-value resource: -o name prints only the pending-settlement total (the number hosts ask about most).
    _emit(output, earnings.raw, [f"{earnings.accumulated_until_payout:.2f}"],
          lambda: _print_earnings(earnings, args.days, color))
    return 0


def _cmd_gpus(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    gpus = client.list_gpus(rental_type=args.rental, min_vram=args.vram)
    if args.model:
        gpus = [g for g in gpus if _contains(g.gpu_model, args.model)]
    _emit(output, [g.raw for g in gpus], [g.gpu_model for g in gpus], lambda: _print_gpus(gpus, color))
    return 0


def _cmd_templates(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    templates = client.list_templates(args.workspace, app_type=args.app_type)
    if args.name:
        templates = [t for t in templates if _contains(t.name, args.name)]
    _emit(output, [t.raw for t in templates], [str(t.template_id) for t in templates],
          lambda: _print_templates(templates, color))
    return 0


def _cmd_template(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    template = client.get_template(args.template_id, workspace=args.workspace)
    _emit(output, template.raw, [str(template.template_id)], lambda: _print_template(template, color))
    return 0


def _cmd_servings(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    statuses = _parse_status_filter(args.status)
    code = _reject_unknown_statuses(statuses, SERVING_STATUSES)
    if code:
        return code
    servings = _filter_servings(client.list_servings(args.workspace), statuses, args.name)
    _emit(output, [s.raw for s in servings], [str(s.serving_id) for s in servings],
          lambda: _print_servings(servings, color))
    return 0


def _cmd_serving(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    serving = client.get_serving(args.serving_id)
    _emit(output, serving.raw, [str(serving.serving_id)], lambda: _print_serving(serving, color))
    return 0


def _cmd_tasks(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    statuses = _parse_status_filter(args.status)
    code = _reject_unknown_statuses(statuses, TASK_STATUSES)
    if code:
        return code
    tasks = client.list_tasks(args.workspace, status=sorted(statuses) or None,
                              limit=args.limit, offset=args.offset)
    if args.name:
        tasks = [t for t in tasks if _contains(t.name, args.name)]
    _emit(output, [t.raw for t in tasks], [t.task_id for t in tasks], lambda: _print_tasks(tasks, color))
    return 0


def _cmd_task(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    task = client.get_task(args.task_id)
    _emit(output, task.raw, [task.task_id], lambda: _print_task(task, color))
    return 0


def _cmd_assets(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    page = client.list_assets(args.workspace, asset_type=args.asset_type, status=args.status,
                              page=args.page, page_size=args.page_size)
    assets = [a for a in page.items if _contains(a.name, args.name)] if args.name else page.items
    # json keeps the page metadata, but items are the --name filter result (the same set as table/name).
    raw = {**page.raw, "items": [a.raw for a in assets]}
    _emit(output, raw, [a.asset_id for a in assets], lambda: _print_assets(assets, page, color))
    return 0


def _cmd_asset(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    asset = client.get_asset(args.asset_id)
    _emit(output, asset.raw, [asset.asset_id], lambda: _print_asset(asset, color))
    return 0


def _print_saved(paths: list, color: bool) -> None:
    if not paths:
        print("No files to download.")
        return
    for path in paths:
        print(f"saved  {fmt.clean(str(path))}  {fmt.paint(fmt.bytes_human(path.stat().st_size), 'dim', color)}")
    print(fmt.paint(f"{len(paths)} file{'s' if len(paths) != 1 else ''} downloaded.", "dim", color))


def _cmd_asset_download(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    # download_asset fetches the URLs only once — for accounts without payment, daily egress is counted per issuance.
    paths = client.download_asset(args.asset_id, args.dir or args.asset_id, paths=args.path)
    _emit(output, [str(p) for p in paths], [str(p) for p in paths], lambda: _print_saved(paths, color))
    return 0


def _cmd_task_outputs(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    if args.download:
        paths = client.download_task_outputs(args.task_id, args.download)
        _emit(output, [str(p) for p in paths], [str(p) for p in paths], lambda: _print_saved(paths, color))
        return 0
    outs = client.task_outputs(args.task_id)

    def show() -> None:
        if outs.expired:
            print("The outputs were deleted.")
        elif not outs.files:
            print("No output files.")
        else:
            fmt.render_table(["FILE", "SIZE"], [[f.path, fmt.bytes_human(f.size_bytes)] for f in outs.files],
                             aligns=["l", "r"], enabled=color)

    _emit(output, outs.raw, [f.path for f in outs.files], show)
    return 0


def _cmd_asset_storage(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    storage = client.get_asset_storage(args.workspace)
    # Single-value resource: -o name prints only the estimated monthly cost.
    _emit(output, storage.raw, [f"{storage.estimated_monthly_cost:.2f}"],
          lambda: _print_asset_storage(storage, color))
    return 0


def _cmd_api_keys(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    keys = client.list_api_keys()
    _emit(output, [k.raw for k in keys], [str(k.key_id) for k in keys], lambda: _print_api_keys(keys, color))
    return 0


def _cmd_credit(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    credit = client.get_credit()
    # Single-value resource: -o name prints only the balance number (directly comparable in scripts).
    _emit(output, credit.raw, [f"{credit.balance:.2f}"], lambda: _print_credit(credit, color))
    return 0


def _cmd_credit_history(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    entries = client.list_credit_history(start_date=args.since, end_date=args.until)
    _emit(output, [e.raw for e in entries], [str(e.entry_id) for e in entries],
          lambda: _print_credit_history(entries, color))
    return 0


# Command/alias → handler. Keep aliases in sync with build_parser's aliases.
_HANDLERS: dict[str, Handler] = {
    "me": _cmd_me, "whoami": _cmd_me,
    "workspaces": _cmd_workspaces, "ws": _cmd_workspaces,
    "workspace": _cmd_workspace,
    "members": _cmd_members,
    "pods": _cmd_pods,
    "pod": _cmd_pod,
    "pod-metrics": _cmd_pod_metrics,
    "transactions": _cmd_transactions, "txn": _cmd_transactions,
    "storages": _cmd_storages,
    "storage": _cmd_storage,
    "machines": _cmd_machines, "m": _cmd_machines,
    "machine": _cmd_machine,
    "machine-metrics": _cmd_machine_metrics,
    "earnings": _cmd_earnings,
    "gpus": _cmd_gpus,
    "templates": _cmd_templates,
    "template": _cmd_template,
    "servings": _cmd_servings,
    "serving": _cmd_serving,
    "tasks": _cmd_tasks,
    "task": _cmd_task,
    "assets": _cmd_assets,
    "asset": _cmd_asset,
    "asset-download": _cmd_asset_download,
    "task-outputs": _cmd_task_outputs,
    "asset-storage": _cmd_asset_storage,
    "api-keys": _cmd_api_keys, "keys": _cmd_api_keys,
    "credit": _cmd_credit,
    "credit-history": _cmd_credit_history,
    **write_cli.HANDLERS,
}


def _cmd_login(args: argparse.Namespace) -> int:
    api_key = args.api_key or getpass.getpass("Meshive API key: ").strip()
    if not api_key:
        print("Error: no API key provided.", file=sys.stderr)
        return 1
    # base_url is decided *without reading* the credentials file — so a previously saved value doesn't leak into a new login.
    base_url = (args.base_url or os.getenv(_config.ENV_BASE_URL) or _config.DEFAULT_BASE_URL).rstrip("/")

    client = Meshive(api_key=api_key, base_url=base_url, timeout=args.timeout)
    try:
        me = client.me()  # validate the key before saving.
    except (MeshiveError, httpx.HTTPError) as err:
        print(fmt.clean(f"Error: could not verify API key against {base_url}: {err}"), file=sys.stderr)
        return 1
    finally:
        client.close()

    # With the production default, base_url isn't saved (the default is implied). Only remembered for non-standard ones like dev.
    stored_base = base_url if base_url != _config.DEFAULT_BASE_URL else None
    path = _credentials.save(api_key, stored_base)
    print(fmt.clean(f"Logged in as {me.email} ({base_url})."))
    print(f"Credentials saved to {path}.")
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    if _credentials.clear():
        print(f"Logged out. Removed {_credentials.credentials_path()}.")
    else:
        print("Not logged in (no saved credentials).")
    return 0


def _run_command(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        print("Error: --timeout must be greater than 0.", file=sys.stderr)
        return 2

    if args.command == "login":
        return _cmd_login(args)
    if args.command == "logout":
        return _cmd_logout(args)

    handler = _HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - unreachable (argparse rejects unknown commands)
        return 1

    output = args.output or ("json" if args.as_json else "table")
    color = fmt.color_enabled() and output == "table"
    client = Meshive(api_key=args.api_key, base_url=args.base_url, timeout=args.timeout)
    try:
        return handler(client, args, output, color)
    except ValueError as err:
        # SDK argument validation failed (date format, --limit range, empty ID, ...) — a usage error before reaching the server.
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        return 2
    except MeshiveError as err:
        # Includes the server's detail.message, so print it after stripping control characters.
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        return 1
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        return _run_command(args)
    except MeshiveError as err:
        # The client-construction step (bad base URL, ...) fails outside the inner try in _run_command.
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        return 1
    except httpx.HTTPError as err:
        # Never reached the server, or the connection dropped (where a traceback used to leak through instead of a MeshiveError).
        target = getattr(getattr(err, "request", None), "url", None)
        where = f" {target}" if target else ""
        print(fmt.clean(f"Error: could not reach the Meshive API{where}: {err}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130  # convention: 128 + SIGINT


if __name__ == "__main__":
    raise SystemExit(main())
