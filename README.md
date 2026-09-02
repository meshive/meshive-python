## Install

```bash
pip install meshive
```

Full SDK & CLI documentation: [docs.meshive.ai/sdk-cli](https://docs.meshive.ai/sdk-cli/)

## Authentication

The SDK and CLI authenticate with a **Meshive API Key** (READ scope). Issue one from
the [console](https://console.meshive.ai).

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

Everything is read-only. `meshive --help` lists the commands; `meshive <command> --help`
shows its filters.

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
