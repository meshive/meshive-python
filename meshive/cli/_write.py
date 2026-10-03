"""CLI 쓰기 커맨드 (0.1.0): pod-create/stop/start/restart/delete, storage-create/delete,
serving-deploy/scale/pause/resume/delete, task-submit/stop, logs, task-logs.
0.1.3: models, hf-tokens, model-detect, model-register, model-delete (서빙용 모델 등록), asset-import, civitai-keys.

규칙: 돈이 들거나 되돌릴 수 없는 커맨드(create/start/deploy/submit/delete, 그리고 비용이 늘 수 있는
serving-scale·serving-resume)는 먼저 견적·요약을 보여주고 `--yes` 가 없으면 확인을 묻는다(TTY 가 아니면 exit 2).
`--estimate` 는 견적만 보고 끝낸다. `pod-create --wait` 가 시간 안에 파드를 못 찾으면 exit 1 (수락된 transaction 은 출력).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import _format as fmt
from .._client import Meshive
from ..exceptions import MeshiveError
from ..models import Logs, PodEstimate, ResourceAction, StorageEstimate, TaskEstimate

Handler = Callable[[Meshive, argparse.Namespace, str, bool], int]


def _emit(output: str, raw: object, ids: list[str], show: Callable[[], None]) -> None:
    if output == "json":
        print(json.dumps(raw, indent=2, ensure_ascii=False))
    elif output == "name":
        for value in ids:
            print(fmt.clean(value))
    else:
        show()


def _emit_estimate(output: str, estimate: PodEstimate | StorageEstimate | TaskEstimate, show: Callable[[], None]) -> None:
    """`--estimate` 출력. json 은 서버 payload 만, table 은 견적 표, name 은 credit·asset-storage 처럼 헤드라인 숫자
    하나 — 시간당 견적을 표와 같은 반올림(3자리)으로 `$`·천 단위 쉼표 없이. 단가를 모르면(CPU 프리셋 태스크) 안 찍는다."""
    ids: list[str] = []
    if output == "name":
        price = fmt.money_hourly(estimate.price_per_hour)
        if price != "-":
            ids.append(price.replace("$", "").replace(",", ""))
    _emit(output, estimate.raw, ids, show)


def _confirm(args: argparse.Namespace, question: str) -> bool:
    """--yes 면 통과. 아니면 TTY 에서 묻고, 비대화형이면 안내 후 거절."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("Error: this command spends credit or deletes resources. Re-run with --yes to confirm.",
              file=sys.stderr)
        return False
    # 질문은 stderr 로 — input() 은 stdout 이 터미널이 아니면 질문을 stdout 에 써서, `-o json > out.json`·`| jq`·
    # `id=$(… -o name)` 에서 결과에 질문이 섞이고 정작 사람은 질문을 못 본다. 묻기 전에 stdout 은 input() 처럼 비운다 —
    # 파이프면 블록 버퍼라 앞서 찍은 견적 표가 질문 뒤로 밀린다(비우기 오류도 input() 처럼 무시). 답 없이 EOF 면 거절.
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


def _parse_input_assets(values: list[str] | None) -> list[dict[str, Any]]:
    out = []
    for item in values or []:
        asset, sep, _ = item.partition(":")
        if sep:
            # 0.1.2 까지 받던 ASSET_ID:VERSION — 자산에 버전이 없어 서버도 무시했다. 스크립트가 깨지지 않게 경고만.
            print(f"Warning: {item!r}: assets no longer have versions; using {asset.strip()!r}.", file=sys.stderr)
        out.append({"asset": asset.strip()})
    return out


# =============================================================================
# 출력
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
        print("volumes:   " + ", ".join(f"{v.get('storage')}→{v.get('mount_path')}" for v in est.volumes))
    print(fmt.paint(est.note, "dim", color))


def _print_action(action: ResourceAction, color: bool) -> None:
    _kv([(action.resource, action.id), ("action", action.action),
         ("accepted", fmt.yes_no(action.accepted)),
         ("result", json.dumps(action.result) if action.result not in (None, "") else "-")])


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
# 파드
# =============================================================================

def _pod_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return dict(
        gpu_model=args.gpu, gpu_count=args.gpu_count, gpu_vram_gb=args.vram,
        rental_type="spot" if args.spot else "demand", vcpu=args.vcpu, ram_gb=args.ram,
        volumes=_parse_volumes(args.volume), env=_parse_env(args.env), secret_keys=args.secret or [],
        ports=_parse_ports(args.port), command=args.overwrite_command, internet_premium=args.internet_premium,
        uptime_premium=args.uptime_premium, cpu_premium=args.cpu_premium, region=args.region,
        max_price_per_hour=args.max_price,
    )


def _wait_for_new_pod(client: Meshive, workspace: str, name: str, until: str, timeout: float) -> str | None:
    """생성 직후에는 pod_name 이 없다 — 목록에서 user_alias == name 을 찾은 뒤 wait_for_pod 로 넘긴다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pod in client.list_pods(workspace):
            if pod.user_alias == name:
                client.wait_for_pod(pod.pod_name, workspace, until=until, timeout=max(1.0, deadline - time.monotonic()))
                return pod.pod_name
        time.sleep(5)
    return None


def cmd_pod_create(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    kwargs = _pod_kwargs(args)
    estimate = client.estimate_pod(args.name, args.template, workspace=args.workspace, **kwargs)
    if args.estimate:
        _emit_estimate(output, estimate, lambda: _print_pod_estimate(estimate, color))
        return 0
    if output == "table":      # 확인을 묻기 전에 견적을 보여 준다 — json/name 출력에는 섞지 않는다
        _print_pod_estimate(estimate, color)
    if not _confirm(args, f"Create pod '{args.name}' at {fmt.money_hourly(estimate.price_per_hour)}/hr?"):
        return 2
    created = client.create_pod(args.name, args.template, workspace=args.workspace, **kwargs)
    pod_name = None
    if args.wait:
        pod_name = _wait_for_new_pod(client, args.workspace, args.name, args.wait, args.wait_timeout)
    _emit(output, created.raw, [pod_name or str(created.transaction_id)], lambda: _kv([
        ("accepted", fmt.yes_no(created.accepted)), ("name", created.name),
        ("pod", pod_name or "(assigned shortly — see `meshive pods`)"),
        ("transaction", str(created.transaction_id))]))
    if args.wait and pod_name is None:
        # 수락은 됐지만 파드가 시간 안에 안 나타났다 — 자동화가 `--wait running` 성공으로 오인하면 안 된다.
        print(fmt.clean(f"Error: pod '{args.name}' did not appear within {args.wait_timeout:g}s. The create was accepted "
                        f"(transaction {created.transaction_id}); check `meshive pods {args.workspace}`."), file=sys.stderr)
        return 1
    return 0


def _pod_action(method: str, needs_confirm: bool, question: str):
    def handler(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
        if needs_confirm and not _confirm(args, question.format(pod=args.pod_name)):
            return 2
        extra: dict[str, Any] = {}
        if method == "start_pod":
            extra["placement"] = "any_node" if args.any_node else "same_node"
            extra["allow_data_loss"] = bool(getattr(args, "allow_data_loss", False))
            if args.any_node:
                current = client.get_pod(args.pod_name, args.workspace)
                if current.has_unpreserved_workspace is not False:
                    warning = ("Moving this pod to another node permanently deletes unpreserved workspace files. "
                               "Attached local hostPath storage stays on the old node.")
                    print(warning, file=sys.stderr)
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
        action: ResourceAction = getattr(client, method)(args.pod_name, args.workspace, **extra)
        _emit(output, action.raw, [action.id], lambda: _print_action(action, color))
        return 0
    return handler


# =============================================================================
# 스토리지 / 서빙 / 태스크
# =============================================================================

def _print_storage_estimate(est: StorageEstimate, color: bool) -> None:
    _kv([("estimate", fmt.paint(f"{fmt.money_hourly(est.price_per_hour)}/hr", "green", color)),
         ("per GiB·month", fmt.money(est.price_per_gb_month)), ("size", f"{est.size_gb} GiB"),
         ("type", f"{est.storage_type} / {est.disk_type}"), ("max size", f"{est.max_size_gb} GiB")])
    print(fmt.paint(est.note, "dim", color))


def cmd_storage_create(client: Meshive, args: argparse.Namespace, output: str, color: bool) -> int:
    kwargs = dict(storage_type=args.type, disk_type=args.disk, encrypted=args.encrypted, region=args.region,
                  max_price_per_hour=args.max_price)
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
    # 비용이 늘 수 있는 변경(범위 확대·autoscale 켜기·상한 인상)만 확인 — 줄이는 변경은 바로 적용.
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
    # 비용 없음(DB 등록만, 다운로드는 배포 때) → 확인을 묻지 않는다.
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
    # 링크 등록이라 비용이 없다(저장 요금 0, 바이트는 쓸 때 원본에서) → 확인을 묻지 않는다.
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
                  max_price_per_hour=args.max_price)
    estimate = client.estimate_task(args.name, script or "", workspace=args.workspace, **kwargs)
    if args.estimate:
        _emit_estimate(output, estimate, lambda: _print_task_estimate(estimate, color))
        return 0
    if output == "table":
        _print_task_estimate(estimate, color)
    cost = fmt.money(estimate.max_cost) if estimate.max_cost is not None else "an unknown amount"
    if not _confirm(args, f"Submit task '{args.name}' (up to {cost})?"):
        return 2
    submitted = client.submit_task(args.name, script or "", workspace=args.workspace, **kwargs)
    task = submitted.task
    _emit(output, submitted.raw, [task.task_id], lambda: _kv([
        ("accepted", "yes"), ("task", task.task_id), ("name", task.name), ("status", task.status),
        ("pod", task.pod_name or "-"), ("logs", f"meshive task-logs {task.task_id}")]))
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
# 파서
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
    p.add_argument("--vcpu", type=int, default=None, metavar="N", help="vCPU (default: recommended for the GPU).")
    p.add_argument("--ram", type=int, default=None, metavar="GiB", help="RAM in GiB (default: recommended).")
    p.add_argument("--volume", action="append", metavar="PV:/mount", help="Attach an existing storage (repeatable).")
    p.add_argument("--env", action="append", metavar="KEY=VALUE", help="Environment variable (repeatable).")
    p.add_argument("--secret", action="append", metavar="KEY", help="Mark an --env key as secret (repeatable).")
    p.add_argument("--port", action="append", metavar="PORT[:NAME[:internal]]", help="Expose a port (repeatable).")
    # dest 를 바꾸지 않으면 서브커맨드 이름을 담는 args.command 를 덮어써 커맨드가 사라진다.
    p.add_argument("--command", dest="overwrite_command", default=None, help="Override the template command.")
    p.add_argument("--region", default=None, metavar="CODE", help="Target location, e.g. KR.")
    p.add_argument("--internet-premium", action="store_true")
    p.add_argument("--uptime-premium", action="store_true")
    p.add_argument("--cpu-premium", action="store_true")
    p.add_argument("--max-price", default=None, metavar="USD", help="Cap the final compute USD/hour rate; storage/Asset Hub charges are separate.")
    p.add_argument("--estimate", action="store_true", help="Only show the estimate; create nothing.")
    p.add_argument("--wait", default=None, metavar="STATUS", help="After creating, wait until the pod reaches STATUS (e.g. running).")
    p.add_argument("--wait-timeout", type=float, default=600.0, metavar="SECONDS")
    _yes(p)

    for name, help_text in (("pod-stop", "Stop a pod (pod billing stops; storage billing continues)."),
                            ("pod-start", "Start a stopped pod (billing resumes)."),
                            ("pod-restart", "Restart a pod."),
                            ("pod-delete", "Delete a pod.")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("workspace", help="Workspace ID (namespace name).")
        p.add_argument("pod_name", help="Pod ID (pod name).")
        if name == "pod-start":
            p.add_argument("--any-node", action="store_true",
                           help="Start on any available node; unpreserved workspace files are permanently deleted on migration.")
            p.add_argument("--allow-data-loss", action="store_true",
                           help="Separately consent to permanent loss of unpreserved workspace files for this pod's migration.")
            _yes(p)
        if name == "pod-delete":
            p.add_argument("--delete-local-storage", action="append", metavar="PV",
                           help="Also delete this attached local (hostPath) storage (repeatable).")
            _yes(p)

    # --- storages ---
    p = sub.add_parser("storage-create", parents=[common], help="Create a storage volume (shows the estimate first).")
    p.add_argument("workspace"); p.add_argument("name", help="Storage name (label).")
    p.add_argument("--size", type=int, required=True, metavar="GiB")
    p.add_argument("--type", default="nfs", choices=["nfs", "hostPath"], help="nfs (network, default) or hostPath (local).")
    p.add_argument("--disk", default="NVMe", choices=["NVMe", "SSD", "HDD"])
    p.add_argument("--encrypted", action="store_true", help="At-rest encryption (network storage, --type nfs, only).")
    p.add_argument("--region", default=None, metavar="CODE")
    p.add_argument("--max-price", default=None, metavar="USD", help="Cap this volume's final initial USD/hour rate.")
    p.add_argument("--estimate", action="store_true", help="Only show the estimate; create nothing.")
    _yes(p)
    p = sub.add_parser("storage-delete", parents=[common], help="Delete a storage volume.")
    p.add_argument("workspace"); p.add_argument("storage_name", help="Storage ID (pv name).")
    _yes(p)

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

    # --- asset import (0.1.3) ---
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

    # --- serving models (0.1.3) ---
    p = sub.add_parser("models", parents=[common], help="List the models registered for serving (their ID goes to serving-deploy).")
    p.add_argument("workspace")
    p = sub.add_parser("hf-tokens", parents=[common], help="List the workspace's Hugging Face tokens (for private repos).")
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
    "pod-stop": _pod_action("stop_pod", False, ""),
    "pod-start": _pod_action("start_pod", True, "Start pod {pod} (billing resumes)?"),
    "pod-restart": _pod_action("restart_pod", False, ""),
    "pod-delete": _pod_action("delete_pod", True, "Delete pod {pod}?"),
    "storage-create": cmd_storage_create,
    "storage-delete": cmd_storage_delete,
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
