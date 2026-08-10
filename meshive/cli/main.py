import argparse
import getpass
import json
import os
import sys

import httpx

from . import _format as fmt
from .. import _config, _credentials
from .._version import __version__
from .._client import Meshive
from ..exceptions import MeshiveError
from ..models import Machine, Pod, WhoAmI, Workspace

# K8sResourceLifecycleStatus (백엔드 enum). --status 검증용 — 서버가 필터를 안 받으므로
# 클라이언트에서 오타를 잡아 "조용히 빈 결과" 대신 유효값을 알려준다.
POD_STATUSES = (
    "pending", "creating", "running", "waiting", "stopping",
    "stopped", "error", "unreachable", "terminating", "terminated",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshive",
        description="Meshive GPU Cloud CLI",
        epilog=(
            "Examples:\n"
            "  meshive login                  Save your API key (then commands need no --api-key)\n"
            "  meshive me                     Show the current API key's owner\n"
            "  meshive workspaces             List workspaces\n"
            "  meshive pods <workspace>       List pods in a workspace\n"
            "  meshive pod <workspace> <pod>  Show a single pod\n"
            "  meshive machines               List your machines (as a host)\n"
            "  meshive machine <id>           Show a single machine\n"
            "\n"
            "Scripting: `-o name` prints just the IDs, one per line (pipe into xargs).\n"
            "Auth: `meshive login`, or set MESHIVE_API_KEY / pass --api-key. For dev, set MESHIVE_BASE_URL.\n"
            "Docs: https://github.com/meshive/meshive-python"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # 모든 서브커맨드가 공유하는 전역 옵션.
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
    # --json 은 -o json 의 별칭 (0.0.5 이전부터 쓰던 플래그 — 하위 호환 유지).
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

    sub.add_parser("workspaces", parents=[common], aliases=["ws"],
                   help="List your workspaces.")

    p_pods = sub.add_parser("pods", parents=[common], help="List pods in a workspace (or all).")
    p_pods.add_argument("workspace", nargs="?",
                        help="Workspace ID (namespace name). Omit when using --all.")
    p_pods.add_argument("--all", action="store_true", dest="all_workspaces",
                        help="List pods across every workspace (adds a WORKSPACE column).")
    # 클라이언트 측 필터 (서버는 필터 파라미터를 받지 않음).
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

    p_machines = sub.add_parser("machines", parents=[common], aliases=["m"],
                                help="List your machines (as a host).")
    p_machines.add_argument(
        "--type", choices=["gpu", "cpu", "storage"], default=None, dest="machine_type",
        help="Filter by machine type.",
    )
    # 머신 status(state.name) enum 은 합성/확장적이라 클라이언트에서 검증하지 않는다 —
    # 서버가 주는 값과 대소문자 무시 비교만 한다.
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

    return parser


def _parse_status_filter(values: list[str] | None) -> set[str]:
    """--status 값(반복/쉼표)을 소문자 set 으로. 빈 경우 빈 set."""
    result: set[str] = set()
    for value in values or []:
        result.update(s.strip().lower() for s in value.split(",") if s.strip())
    return result


def _gather_all_pods(client: Meshive) -> list[Pod]:
    """모든 워크스페이스의 파드를 모아 반환. 한 워크스페이스 조회가 실패해도
    전체를 죽이지 않고 경고만 내고 계속 (overview 성격)."""
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
        needle = name.lower()
        pods = [p for p in pods if needle in (p.user_alias or "").lower()]
    return pods


def _pod_price(pod: Pod) -> str:
    """웹 pod 페이지와 동일: running 일 때만 가격, 아니면 '-'.
    (꺼진/대기 중인 pod 는 과금되지 않으므로 가격을 노출하지 않는다.)"""
    return fmt.money(pod.price_per_hour) if pod.status.lower() == "running" else "-"


def _print_whoami(me: WhoAmI, color: bool) -> None:
    # 서버 유래 문자열은 fmt.clean 으로 제어문자 제거 후 출력 (터미널 이스케이프 인젝션 방어).
    print(f"email:    {fmt.clean(me.email)}")
    print(f"username: {fmt.clean(me.username or '-')}")
    print(f"role:     {fmt.clean(me.user_role)}")


def _print_workspaces(workspaces: list[Workspace], color: bool) -> None:
    if not workspaces:
        print("No workspaces.")
        return
    # NAME = workspace_name(유저 라벨), ID = namespace_name(조회 키).
    rows = [
        [ws.workspace_name or "-", ws.namespace_name, fmt.status_cell(ws.status),
         str(ws.resources.pod), fmt.money(ws.price_per_hour)]
        for ws in workspaces
    ]
    colors = [[None, None, fmt.status_color(ws.status), None, None] for ws in workspaces]
    fmt.render_table(
        ["NAME", "ID", "STATUS", "PODS", "PRICE/HR"],
        rows,
        aligns=["l", "l", "l", "r", "r"],
        colors=colors,
        enabled=color,
    )


def _print_pods(pods: list[Pod], color: bool, show_workspace: bool = False) -> None:
    if not pods:
        print("No pods.")
        return
    # NAME = user_alias(유저 라벨), ID = pod_name(조회 키). --all 이면 WORKSPACE(namespace_name) 추가.
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


def _print_pod(pod: Pod, color: bool) -> None:
    created = "-"
    if pod.created_at:
        created = f"{fmt.relative_time(pod.created_at)} ({pod.created_at.isoformat()})"
    print(f"name:      {fmt.clean(pod.user_alias or '-')}")  # 유저 라벨
    print(f"id:        {fmt.clean(pod.pod_name)}")           # 조회 키
    print(f"workspace: {fmt.clean(pod.namespace_name)}")
    print(f"status:    {fmt.paint(fmt.clean(fmt.status_cell(pod.status)), fmt.status_color(pod.status), color)}")
    print(f"rental:    {fmt.clean(pod.rental_type)}")
    print(f"price/hr:  {_pod_price(pod)}")
    print(f"created:   {created}")
    # maintenance 는 진행 중일 때만 노출 (boolean 나열 대신 의미 있을 때만).
    if pod.is_maintenance:
        print(fmt.paint("⚠ under maintenance", "yellow", color))


def _gpu_cell(machine: Machine) -> str:
    """'8x NVIDIA H100' 형태. GPU 없는(cpu/storage) 머신은 '-'."""
    if not machine.gpu_count:
        return "-"
    return f"{machine.gpu_count}x {machine.gpu_model}" if machine.gpu_model else str(machine.gpu_count)


def _filter_machines(
    machines: list[Machine], statuses: set[str], machine_type: str | None, name: str | None
) -> list[Machine]:
    if statuses:
        machines = [m for m in machines if m.status.lower() in statuses]
    if machine_type:
        machines = [m for m in machines if m.machine_type.lower() == machine_type]
    if name:
        needle = name.lower()
        machines = [m for m in machines if needle in (m.name or "").lower()]
    return machines


def _print_machines(machines: list[Machine], color: bool) -> None:
    if not machines:
        print("No machines.")
        return
    # NAME = name(유저 라벨), ID = machine_id(조회 키).
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
    print(f"name:      {fmt.clean(machine.name or '-')}")  # 유저 라벨
    print(f"id:        {fmt.clean(machine.machine_id)}")   # 조회 키
    print(f"type:      {fmt.clean(machine.machine_type or '-')}")
    print(f"status:    {fmt.paint(fmt.clean(fmt.status_cell(machine.status)), fmt.status_color(machine.status), color)}")
    print(f"gpu:       {fmt.clean(_gpu_cell(machine))}")
    print(f"earn/hr:   {fmt.money(machine.earning_hourly)}")
    print(f"uptime:    {fmt.percent(machine.uptime_rate)}")
    print(f"tier:      {machine.host_tier or '-'}")


def _emit(output: str, raw: object, ids: list[str], show) -> None:
    """선택된 포맷으로 출력. json=원본 payload, name=ID 만, table=사람용 렌더러(show)."""
    if output == "json":
        print(json.dumps(raw, indent=2, ensure_ascii=False))
    elif output == "name":
        for value in ids:
            print(fmt.clean(value))
    else:
        show()


def _cmd_login(args: argparse.Namespace) -> int:
    api_key = args.api_key or getpass.getpass("Meshive API key: ").strip()
    if not api_key:
        print("Error: no API key provided.", file=sys.stderr)
        return 1
    # base_url 은 credentials 파일을 *읽지 않고* 결정 — 기존 저장값이 새 로그인에 새지 않도록.
    base_url = (args.base_url or os.getenv(_config.ENV_BASE_URL) or _config.DEFAULT_BASE_URL).rstrip("/")

    client = Meshive(api_key=api_key, base_url=base_url, timeout=args.timeout)
    try:
        me = client.me()  # 저장 전에 키를 검증.
    except (MeshiveError, httpx.HTTPError) as err:
        print(fmt.clean(f"Error: could not verify API key against {base_url}: {err}"), file=sys.stderr)
        return 1
    finally:
        client.close()

    # prod 기본값이면 base_url 은 저장 안 함(암묵적으로 기본 사용). dev 등 비표준일 때만 기억.
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

    output = args.output or ("json" if args.as_json else "table")
    color = fmt.color_enabled() and output == "table"
    client = Meshive(api_key=args.api_key, base_url=args.base_url, timeout=args.timeout)
    try:
        if args.command in ("me", "whoami"):
            me = client.me()
            _emit(output, me.raw, [me.email], lambda: _print_whoami(me, color))
        elif args.command in ("workspaces", "ws"):
            workspaces = client.list_workspaces()
            _emit(output, [w.raw for w in workspaces], [w.namespace_name for w in workspaces],
                  lambda: _print_workspaces(workspaces, color))
        elif args.command == "pods":
            if args.all_workspaces and args.workspace:
                print("Error: pass a workspace or --all, not both.", file=sys.stderr)
                return 2
            if not args.all_workspaces and not args.workspace:
                print("Error: provide a workspace, or use --all for every workspace.", file=sys.stderr)
                return 2
            statuses = _parse_status_filter(args.status)
            unknown = statuses - set(POD_STATUSES)
            if unknown:
                print(f"Error: unknown status: {', '.join(sorted(unknown))}. "
                      f"Valid: {', '.join(POD_STATUSES)}.", file=sys.stderr)
                return 2
            raw_pods = _gather_all_pods(client) if args.all_workspaces else client.list_pods(args.workspace)
            pods = _filter_pods(raw_pods, statuses, args.rental, args.name)
            _emit(output, [p.raw for p in pods], [p.pod_name for p in pods],
                  lambda: _print_pods(pods, color, show_workspace=args.all_workspaces))
        elif args.command == "pod":
            if args.wait and args.wait.lower() not in POD_STATUSES:
                print(f"Error: unknown status: {args.wait}. "
                      f"Valid: {', '.join(POD_STATUSES)}.", file=sys.stderr)
                return 2
            pod = (
                client.wait_for_pod(args.pod_name, args.workspace,
                                    until=args.wait, timeout=args.wait_timeout)
                if args.wait else client.get_pod(args.pod_name, args.workspace)
            )
            _emit(output, pod.raw, [pod.pod_name], lambda: _print_pod(pod, color))
        elif args.command in ("machines", "m"):
            statuses = _parse_status_filter(args.status)
            machines = _filter_machines(client.list_machines(), statuses, args.machine_type, args.name)
            _emit(output, [m.raw for m in machines], [m.machine_id for m in machines],
                  lambda: _print_machines(machines, color))
        elif args.command == "machine":
            machine = client.get_machine(args.machine_id)
            _emit(output, machine.raw, [machine.machine_id], lambda: _print_machine(machine, color))
        else:  # pragma: no cover - unreachable (handled by caller)
            return 1
    except MeshiveError as err:
        # 서버 detail.message 가 포함되므로 제어문자 정제 후 출력.
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        return _run_command(args)
    except MeshiveError as err:
        # 클라이언트 생성 단계(잘못된 base URL 등)는 _run_command 안쪽 try 밖에서 터진다.
        print(fmt.clean(f"Error: {err}"), file=sys.stderr)
        return 1
    except httpx.HTTPError as err:
        # 서버까지 못 갔거나 연결이 끊긴 경우 (MeshiveError 가 아니라 트레이스백이 그대로 새던 자리).
        target = getattr(getattr(err, "request", None), "url", None)
        where = f" {target}" if target else ""
        print(fmt.clean(f"Error: could not reach the Meshive API{where}: {err}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130  # 관례: 128 + SIGINT


if __name__ == "__main__":
    raise SystemExit(main())
