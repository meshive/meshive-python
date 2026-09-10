## Install

```bash
pip install "meshive==0.1.1"
```

Docs: [SDK & CLI reference](https://docs.meshive.ai/sdk-cli/) · [Quickstart](https://docs.meshive.ai/getting-started/quickstart-client/) · [Serverless API](https://docs.meshive.ai/api-reference/) · [GPU pricing](https://docs.meshive.ai/documentation/pricing/)

## Authentication

The SDK and CLI authenticate with a **Meshive API Key**. Issue one from the
[console](https://console.meshive.ai) (workspace Settings → Secret). A **Read only** key can view
everything; a **Read & write** key is needed to create, change or delete resources, and it always
expires (30 days by default, 90 at most).
New read-only keys also expire (365 days by default and at most); existing keys retain their original expiry policy.

The easiest way is `meshive login` — it verifies the key and stores it (file mode `0600`)
under `~/.meshive/credentials.json`, so later commands need no flags or env vars:

```bash
meshive login                 # prompts for the key (hidden input)
meshive me                    # now works with no --api-key
meshive logout                # removes the saved credentials
```

Alternatively, provide the key explicitly. Resolution order is
**`--api-key` flag › `MESHIVE_API_KEY` env › `meshive login` file**:

```bash
export MESHIVE_API_KEY=meshive_xxxxxxxx
# or per-command: meshive me --api-key meshive_xxxxxxxx
```

Requests go to the production API. If you have been given a different API address, override
it with `--base-url` or `MESHIVE_BASE_URL` (same precedence: flag › env › login file › default);
`meshive login --base-url <url>` remembers it. The config directory can be relocated via
`MESHIVE_CONFIG_DIR`.

## CLI

`meshive --help` lists the commands; `meshive <command> --help` shows their options.
Commands that spend credit or delete something show an estimate and ask for confirmation
(`--yes` to skip, `--estimate` to only see the price).
`--estimate` is available on `pod-create`, `storage-create`, and `task-submit`.
Resuming a serving and changes that can raise its cost also require confirmation. `--yes` does
not replace `--allow-data-loss` for an unattended pod move that can lose unpreserved files.

Automatic HTTP retries reuse a key, but **rerunning a CLI command creates a new key**. CLI 0.1.1
has no `--idempotency-key` or operation lookup command, and terminal errors do not print recovery
metadata. A successful write's `-o json` output does carry `idempotencyKey`, `operationMethod`
and `operationPath`, so record those if you may need to reconcile later. Inspect the resource and transaction before retrying a timed-out write; use the SDK's
explicit `idempotency_key` and `get_operation` for automation that must survive process restarts.

```bash
meshive --version
meshive me                     # current API key's owner
meshive api-keys               # your API keys (prefixes only — the secret is never shown)
meshive credit                 # credit balance, paid vs bonus, auto-recharge
meshive credit-history         # top-ups and refunds (--since/--until YYYY-MM-DD)

meshive workspaces             # list workspaces
meshive workspace <workspace>  # cost & resource summary of one workspace
meshive members <workspace>    # members and roles

meshive pods <workspace>       # list pods in a workspace
meshive pods --all             # list pods across every workspace (adds a WORKSPACE column)
meshive pod <workspace> <pod>  # show a single pod
meshive pod-metrics <workspace> <pod>   # live CPU/RAM/GPU/disk usage
meshive transactions <workspace>        # in-flight pod operations (why a pod is still creating)

meshive storages <workspace>   # storages (volumes) in a workspace
meshive storage <workspace> <storage>   # show a single storage

meshive gpus                   # GPUs available to rent right now, with prices
meshive templates              # official templates (--workspace <id> adds its custom ones)
meshive template <id>          # show a template

meshive assets <workspace>     # assets in a workspace (datasets, models, outputs, ...); --page/--page-size
meshive asset <id>             # show an asset with its versions
meshive asset-storage <workspace>   # managed asset storage, monthly cost, credit status

meshive servings <workspace>   # serverless serving deployments
meshive serving <id>           # show a serving deployment
meshive tasks <workspace>      # serverless tasks (newest first; --limit/--offset paging)
meshive task <id>              # show a task

meshive machines               # list your machines (as a host)
meshive machine <id>           # show a single machine
meshive machine-metrics <id>   # live CPU/RAM/GPU/disk/network of a machine
meshive earnings               # host earnings (--since/--until, --days N for the daily table)

# wait for a pod to reach a status (polls every 5s, gives up early if it errors out)
meshive pod <workspace> <pod> --wait running
meshive pod <workspace> <pod> --wait running --wait-timeout 120

# filter pods (client-side; the API itself returns the full list)
meshive pods <workspace-id> --status running
meshive pods <workspace-id> --status running,error   # comma-separated or repeatable
meshive pods <workspace-id> --rental spot
meshive pods <workspace-id> --name llama              # match the display name (alias)

# same style of filters elsewhere
meshive storages <workspace-id> --type nfs --status running --name datasets
meshive machines --status online --type gpu --name node-a
meshive gpus --rental spot --vram 40 --model h100     # --vram/--rental go to the server, --model is client-side
meshive templates --workspace <workspace-id> --type ide --name jupyter
meshive servings <workspace-id> --status active --name llama
meshive tasks <workspace-id> --status running,failed --limit 20 --offset 20
meshive assets <workspace-id> --type dataset --status active --page 2   # --type/--status/--page go to the server

# output format: table (default) | json (raw payload) | name (IDs only, one per line)
meshive pods <workspace-id> -o json          # --json is a shorthand for this
meshive pods <workspace-id> -o name          # pipe-friendly: one ID per line
meshive credit -o name                       # single-value commands print just the number

# every command also takes --api-key / --base-url / --timeout overrides
meshive machines --timeout 60
```

Exit codes: `0` success, `1` API or network error, `2` usage error (unknown status, bad date,
out-of-range `--limit`, …), `130` interrupted.

### IDs vs names

List output shows two columns:

- **ID** — the canonical identifier (`namespace_name` for workspaces, `pod_name` for pods,
  the volume name for storages, numeric IDs for templates/servings, `task_…` for tasks,
  `asset_…` for assets).
  This is what you pass to the singular commands. It is unique and stable.
- **NAME** — the display alias you set (`workspace_name` / `user_alias` / model name). It is a
  *label*, not a key: it is not guaranteed unique and can change. Use `--name` to filter by it,
  but address resources by their **ID**.

### Sizes and rates

RAM, storage and VRAM are shown in GB (the same conversion the console uses); usage rates in
percent, with `n/a` when a measurement is unavailable; network throughput in Mbps.

### Write commands (Read & write key)

```bash
meshive pod-create <workspace> my-pod --template 457 --gpu "RTX 3060" --estimate   # price only
meshive pod-create <workspace> my-pod --template 457 --gpu "RTX 3060" --max-price 0.10 --wait running
meshive pod-stop <workspace> <pod>          # billing stops (storage still billed)
meshive pod-start <workspace> <pod>         # --any-node to move to another machine
meshive pod-delete <workspace> <pod> --yes
meshive logs <workspace> <pod> --tail 100

meshive storage-create <workspace> data --size 50 --type nfs
meshive storage-delete <workspace> <pv>

meshive task-submit <workspace> train --script train.py --image python:3.12-slim --gpu "RTX 3060"
meshive task-logs <task>; meshive task-stop <task>
meshive serving-deploy <workspace> <model_id> --price-cap 1.5 --max-replicas 2
```

## SDK

```python
from meshive import Meshive

with Meshive() as client:               # reads MESHIVE_API_KEY / MESHIVE_BASE_URL
    me = client.me()
    print(me.email, me.user_role)

    for ws in client.list_workspaces():
        print(ws.namespace_name, ws.status)
    detail = client.get_workspace("my-workspace")
    print(detail.price_per_hour, detail.gpus, [r.type for r in detail.resources])

    pods = client.list_pods("my-workspace")
    pod = client.get_pod(pods[0].pod_name, "my-workspace")
    print(pod.status, pod.raw)           # .raw holds the full payload (machine, template, ...)

    # block until a pod is up (polls every `interval` seconds)
    pod = client.wait_for_pod(pod.pod_name, "my-workspace", until="running", timeout=600)

    usage = client.get_pod_metrics(pod.pod_name, "my-workspace")
    print(usage.cpu_usage_rate, [g.vram_usage_rate for g in usage.gpus])

    for storage in client.list_storages("my-workspace"):
        print(storage.pv_name, storage.storage_type, storage.usage_rate)

    page = client.list_assets("my-workspace", asset_type="dataset")   # one page (20 by default)
    for asset in page:
        print(asset.asset_id, asset.name, asset.size_bytes, asset.storage_provider)
    print(page.total, page.pages)
    print(client.get_asset_storage("my-workspace").estimated_monthly_cost)

    # what can I rent right now?
    for gpu in client.list_gpus(rental_type="demand", min_vram=40):
        print(gpu.gpu_model, gpu.vram, gpu.price_per_hour, gpu.available_gpus)

    # account
    credit = client.get_credit()
    print(credit.paid_balance, credit.bonus_balance)
    for key in client.list_api_keys():
        print(key.prefix, key.last_used_at)   # prefixes only; the secret is never returned

    # host view: the machines you contribute to the network
    machines = client.list_machines()
    for m in machines:
        print(m.machine_id, m.status, m.gpu_count, m.gpu_model)
    machine = client.get_machine(machines[0].machine_id)
    print(machine.earning_hourly, machine.raw)   # .raw holds specs, state, podUses, ...
    print(client.get_earnings().accumulated_until_payout)
```

All methods (identical on `AsyncMeshive`, awaited):

| Method | Returns |
| --- | --- |
| `me()` | `WhoAmI` |
| `list_api_keys()` | `list[ApiKey]` |
| `get_credit()` / `list_credit_history(start_date=, end_date=)` | `Credit` / `list[CreditHistoryEntry]` |
| `list_workspaces()` / `get_workspace(workspace)` | `list[Workspace]` / `WorkspaceDetail` |
| `list_members(workspace)` | `list[Member]` |
| `list_pods(workspace)` / `get_pod(pod_name, workspace)` / `wait_for_pod(...)` | `list[Pod]` / `Pod` |
| `list_transactions(workspace)` | `list[Transaction]` — the step a pod is on, with progress |
| `get_operation(operation_id, method=, path=)` | `dict` with acceptance state and task/transaction references |
| `get_pod_metrics(pod_name, workspace)` | `PodMetrics` |
| `list_storages(workspace)` / `get_storage(storage_name, workspace)` | `list[Storage]` / `Storage` |
| `list_gpus(rental_type=, min_vram=)` | `list[GpuAvailability]` |
| `list_templates(workspace=None, app_type=)` / `get_template(template_id, workspace=None)` | `list[Template]` / `Template` |
| `list_servings(workspace)` / `get_serving(serving_id)` | `list[Serving]` / `Serving` |
| `list_tasks(workspace, status=, limit=, offset=)` / `get_task(task_id)` | `list[Task]` / `Task` |
| `list_assets(workspace, asset_type=, status=, page=, page_size=)` / `get_asset(asset_id)` | `AssetPage` (iterable, `.total`, `.pages`) / `Asset` |
| `get_asset_storage(workspace)` | `AssetStorage` |
| `list_machines()` / `get_machine(machine_id)` | `list[Machine]` / `Machine` |
| `get_machine_metrics(machine_id)` | `MachineMetrics` |
| `get_earnings(start_date=, end_date=)` | `Earnings` |

Dates accept `datetime.date`, `datetime.datetime`, or a `"YYYY-MM-DD"` string. Every model keeps
the exact server payload on `.raw`, so nested or newly added fields are always reachable. Credit
history entries carry the amount, method and date only; Stripe receipt links stay in the console.

Credentials can also be passed explicitly: `Meshive(api_key="meshive_...")`, and `base_url=` overrides the API address.

### Retries

Rate limits (429), gateway errors (5xx), and dropped connections are retried automatically —
twice by default, with exponential backoff, honouring the server's `Retry-After` header. Other
4xx responses are never retried. Turn it off with `Meshive(max_retries=0)`.

If `Retry-After` asks for more than 60 seconds, the SDK raises `RateLimitError` instead of
blocking that long — sleeping through it is your call, via `.retry_after`.

### Async

```python
from meshive import AsyncMeshive

async with AsyncMeshive() as client:
    me = await client.me()
    pods = await client.list_pods("my-workspace")
    gpus = await client.list_gpus(min_vram=80)
```

### Errors

All errors subclass `meshive.MeshiveError`:

- `ConfigurationError` — missing API key
- `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404),
  `RateLimitError` (429, exposes `.retry_after`), and `MeshiveAPIError` for other HTTP errors
  (carry `.status_code`, `.title`, `.message`, `.raw`)
- `WaitTimeoutError` — `wait_for_pod` ran out of time (it is also a built-in `TimeoutError`).
  A pod that reaches `error`/`terminated` while waiting raises `MeshiveError` immediately
  rather than burning the full timeout.

Invalid arguments (a malformed date, `limit` out of range, an empty ID) raise `ValueError`
before any request is sent. Transport failures (DNS, refused connections) surface as `httpx`
exceptions once retries are exhausted. The package ships a `py.typed` marker, so mypy/pyright
read its annotations.

### API compatibility

Version 0.0.7 relies on the extended `/v1/sdk` read surface (workspace detail, storages,
metrics, GPUs, API keys, credit, earnings, members, templates, servings, tasks, assets). Against an
older API those calls return `NotFoundError`; the commands that existed in 0.0.6 keep working.

## License

[Apache License 2.0](LICENSE)

### Writing (0.1.0+)

```python
import time
import uuid
from meshive import Meshive

with Meshive() as client:  # key with the write scope
    workspace = "<workspace>"
    name = "my-pod-" + uuid.uuid4().hex[:12]
    key = str(uuid.uuid4())  # save durably before sending in an application
    print("create operation:", key)
    est = client.estimate_pod(name, 457, workspace=workspace, gpu_model="RTX 3060")
    print(est.price_per_hour, est.resources)
    created = client.create_pod(name, 457, workspace=workspace, gpu_model="RTX 3060",
                                max_price_per_hour=0.10, idempotency_key=key)
    deadline = time.monotonic() + 120
    while True:
        pod = next((p for p in client.list_pods(workspace) if p.user_alias == name), None)
        if pod is not None:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Accepted transaction {created.transaction_id}; reconcile before retrying")
        time.sleep(5)
    client.wait_for_pod(pod.pod_name, workspace, until="running")
    print(client.get_pod_logs(pod.pod_name, workspace, tail=50).text)
    client.stop_pod(pod.pod_name, workspace)
    client.wait_for_pod(pod.pod_name, workspace, until="stopped")
    client.delete_pod(pod.pod_name, workspace)
```

### Why a pod is still `creating`

A pod that stays `creating` is usually pulling its image, not stuck. `list_transactions(workspace)`
(CLI: `meshive transactions`) returns the operations still in flight: `step` is the current stage,
`progress` is `0.0`-`1.0` for stages that report it and **`None` when the stage reports none** — do
not render that as 0%. `detail` carries the failure reason when one is known. Finished work drops
off the list, so an empty result means "nothing in flight", never "nothing failed".

### Write safety contract

`start_pod(..., placement="any_node")` can permanently delete unpreserved workspace files after a node move. Inspect `Pod.has_unpreserved_workspace`; use `allow_data_loss=True` only after separate consent for that pod and move. The CLI requires `--allow-data-loss` in addition to `--yes` for an unattended move that can lose data. `Pod.storage_rate_per_hour` is separate from compute pricing.

`max_price_per_hour` on pods/tasks caps the final compute rate, excluding attached/automatic storage and Asset Hub retention. Over-cap pod placements fail asynchronously. CPU capped requests are refused when a quote is unavailable. Task `max_cost` is unknown because fetch time and storage charges can exceed the script-runtime estimate.

Keep one `idempotency_key` for each logical write and reuse it after a timeout. Results expose `raw["idempotencyKey"]`, `raw["operationMethod"]` and `raw["operationPath"]`; terminal network/API exceptions expose `idempotency_key`, `operation_method` and `operation_path`. `get_operation(key, method="POST", path="/tasks")` checks the durable acceptance record without resubmitting work. Pending/unknown records require reconciliation. `done` records API acceptance, not completion of the asynchronous resource operation.
