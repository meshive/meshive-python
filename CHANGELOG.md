# Changelog

All notable changes to the Meshive Python SDK and CLI. Each release is
published to [PyPI](https://pypi.org/project/meshive/) and to
[GitHub Releases](https://github.com/meshive/meshive-python/releases).
The Publish workflow reads the matching `## vX.Y.Z` section of this file
and uses it as the release notes, so every released version needs a section.

Upgrade with:

```bash
pip install -U meshive
```

## v0.1.3

Models can be registered for serving and asset files and task outputs downloaded without the console, `meshive pod` shows a pod's URLs and connect credentials (secret
values only with `--show-secrets`) and why it was stopped or cannot start on its node, assets are read without versions, and transactions and tasks explain input
asset downloads and output uploads. Confirmation questions no longer break `-o json` and `-o name` output.

### SDK

- `import_asset(target, workspace=, …)` links a Hugging Face repo (`owner/name` or its URL), a CivitAI model or a
  direct file URL as an asset. Nothing is copied, so there is no storage charge and the asset is ready at once; pods
  and tasks download it from the source when they start. Narrow it with `paths`, pin a Hugging Face `revision`, and
  reach private or gated sources with a token or key saved in the console (`hf_token_id` / `civitai_key_id`, listed by
  `list_hf_tokens` / `list_civitai_keys`). Errors such as a gated repo come back with the server's message.
- Serving without the console: `detect_model(repo, workspace=)` checks whether a Hugging Face repo can be served,
  `register_model(repo, workspace=, name=, framework=, hf_token_id=, context_length=)` registers it (no charge — the
  model downloads when deployed; registering the same repo again returns the existing registration),
  `list_models(workspace)` gives the registration IDs that `deploy_serving` takes, and `delete_model(id)` removes one
  that is not deployed. Private repos use a token added in the console, by its ID from `list_hf_tokens(workspace)`.
- Downloads: `asset_download_urls(asset_id, paths=)` returns each file's short-lived link, `download_asset(asset_id,
  dest, paths=)` saves the files under `dest` with their paths inside the asset, and `task_outputs(task_id)` /
  `download_task_outputs(task_id, dest)` do the same for a task's results. `paths` takes globs such as `config/*`.
  A read key is enough. The links go to storage without your API key, and a path that would land outside `dest` is
  refused. Linked assets (fetched from Hugging Face or CivitAI when a pod starts) can't be downloaded. Against a
  server that predates this, the 404 says so.
- `Pod` has the pod's connection details: `endpoints` (name, port, URL, `readiness` ready/preparing/interrupted, read
  the same way as the console's Connect tab) and `connect_credentials` (the values the template shows on Connect,
  such as ComfyUI's auto-generated `ACCESS_PASSWORD`). Secret values are left out of `repr`.
- `Pod` also has its state fields: `billing_active`, `same_node_unavailable_reason`, `can_restart_on_same_node` /
  `_any_node`, `waiting_mode`, `stop_reason_code` / `_detail` / `_at`, `container_running_at`, `cpu_premium` /
  `uptime_premium` / `internet_premium` and `is_downloader`. A field the server did not send is `None`, not `False`.
- `Workspace.member_role`: `admin`, `billing` or `viewer`. A viewer key cannot make changes.
- `Transaction` has `live` and `phase` (`verifying`, `waiting_for_storage`) while input assets download, and
  `init_logs`: the end of the download container's log when it failed (the server removes URLs and tokens first).
- `Task` has `input_assets`, `failed_asset_id`, the output upload progress (`output_upload_state`,
  `output_files_*`, `output_bytes_*`, `output_upload_last_error`) and `outputs_purged_at`.
- Assets no longer have versions. `Asset` reads the asset's own `size_bytes`, `file_count`, `upload_status`, `stale`,
  `ingest_source`, `kind` and `import_failure_reason`, and `get_asset` adds `files` and `active_usage_contexts`. An
  external asset whose size was never measured has `size_bytes` and `file_count` of `None` instead of 0.
  `Asset.version_count`, `latest_version` and `versions` still work but raise a `DeprecationWarning` and will be
  removed in 0.2, together with `AssetVersion`. `input_assets[].version` is ignored (the server already ignored it).

### CLI

- `meshive asset-import <workspace> <repo|url>` links a source as an asset (no confirmation — it costs nothing), and
  `meshive civitai-keys` lists the workspace's CivitAI keys.
- `meshive model-detect`, `model-register`, `models`, `model-delete` and `hf-tokens` register Hugging Face models for
  `serving-deploy`. `model-detect` exits 1 when the repo can't be served.
- `meshive asset-download <id> [-d DIR] [--path GLOB]` saves an asset's files (into `./<id>` by default), and
  `meshive task-outputs <task_id> [--download DIR]` lists or saves a task's output files.
- `meshive pod` lists the pod's URLs and connect credentials. Secret values are hidden unless you pass
  `--show-secrets`. It also says when the system stopped the pod and why, why it cannot start on its node now, and
  what it is waiting for. `-o json` prints the server's payload as before, secret values included.
- `meshive workspaces` has a ROLE column.
- `meshive transactions` names the stage when downloaded bytes stop moving (`verifying`, `waiting for storage`) and,
  when an input asset download failed, prints the last 10 lines of that container's log.
- `meshive task` shows the output upload progress and its last error, the input assets, and which asset a failed
  download stopped on.
- `meshive assets` drops the VERSIONS column and shows `-` for a size that was never measured. `meshive asset` lists
  the asset's files instead of a version table. `--input-asset ASSET_ID:VERSION` warns and uses the asset.
- Confirmation questions (`[y/N]`) go to stderr. When stdout was redirected (`-o json > out.json`, `| jq`,
  `id=$(meshive … -o name)`), the question went there instead: it ended up in the output and never showed on screen,
  so the command seemed to hang.

### Docs

- The README sends you to the console's **Settings → API keys** to issue a key (it said "Settings → Secret", a menu
  that no longer exists).

## v0.1.2

Sizes and rates now read the same as in the Meshive web console, a GPU whose memory cannot be read is no longer shown
as having none, `meshive transactions` shows why a pod is still starting, and `--estimate -o json` prints only JSON.

### CLI

- Sizes use the same 1024-based units as the console: RAM, storage and disk sizes in GiB (MiB below 1 GiB), file and
  asset sizes in KiB/MiB/GiB/TiB, storage prices per GiB·month. The numbers were already 1024-based; only the labels
  said GB/MB/KB. GPU VRAM keeps the usual GB label (`24 GB vram`, `80 GB`).
- `machine-metrics`: network throughput is decimal Mbps (bits per second ÷ 1,000,000). It was divided by 1024², so
  1 Gbps of traffic showed as 953.7 Mbps.
- `machine-metrics`: RAM is the machine's total memory as its operating system reports it, a little below the
  installed size. The server sends it in bytes and the CLI read it as MiB, so a machine with 64 GiB installed showed
  `65,648,036 GB`; it now shows `63 GiB`.
- `pod-metrics` / `machine-metrics`: when the server cannot read a GPU's memory, the GPU line shows `n/a of n/a vram`.
  It showed `0 GB vram`, as if the card had no memory.
- `storage-create --size` and `pod-create --ram` are in GiB, as they always were; the help now says so.
- `pod-create`, `storage-create` and `task-submit` with `--estimate`: `-o json` prints only the estimate's JSON; the
  human-readable estimate came first and broke `jq`. `-o name` prints just the hourly rate as the table rounds it
  (`0.068`), or nothing when a CPU task's rate cannot be quoted; it printed the whole estimate.
- New `transactions` command (alias `txn`): the pod operations still in flight in a workspace, with the
  current step, progress and failure detail — why a pod is still `creating`.

### SDK

- **Changed** `GpuUsage.vram_size` to `float | None` (default `None`). When a GPU's memory cannot be read the API sends
  no size (`null`), and the SDK turned that into `0.0` — a card with no memory. It is now `None`, as the documentation
  already said for every `GpuUsage` field except `gpu_number`. Code that computes with it (`g.vram_size / 1024`) now
  raises `TypeError` for such a GPU, so check for `None` first; type checkers flag the unchecked uses. A size of `0`
  (the GPU reported 0 used and 0 free) is not a real size either; the CLI shows both as `n/a`.
- `MachineMetrics.ram_size` is in bytes (the machine's total memory); its other sizes are MiB and its network rates
  are bytes per second. The value has not changed; the documentation wrongly said MiB.
- The script size error says `256 KiB`.
- New `list_transactions(workspace)` (sync and async) returns `list[Transaction]`: `step`, `progress` (`None` when
  the step reports none — not 0%) and `detail` (the failure reason when known).

## v0.1.1

Fixes from the 2026-09-09 review of the write surface (server-side changes ship with the matching Meshive release).

### SDK

- `Serving` now carries `autoscale` and `price_cap_per_hour`, and has `scale_raises_cost(...)` — the rule the CLI and the
  MCP server use to decide whether a `scale_serving` change needs the user's go-ahead (larger replica range, turning
  autoscale on, raising the per-replica cap).
- `StorageEstimate.disk_type`: storage is priced per `(storage_type, disk_type)`; the server now quotes the disk type you
  asked for (and refuses combinations it has no price for) instead of always quoting NVMe.
- `get_task_logs(task_id, cursor=...)`: for tasks on an external provider, `cursor=None`/`0` returns the **last** `tail`
  lines (previously the oldest ones in the buffer); pass the previous response's `next_cursor` to read only new lines.
- Pod logs: when nobody has been watching a pod, the server wakes the log watcher before answering instead of returning a
  stale buffer; if it cannot (`wait=0` or the watcher is down) the response `note` says the lines may be behind.
- **Removed** `disk_gb` from `estimate_pod()` / `create_pod()`. The system disk is sized by the server and always was —
  the value was silently overridden after the estimate. The estimate's `resources["disk_gb"]` shows the real size.

### CLI

- `serving-scale` asks for confirmation (or `--yes`) when the change can raise the hourly cost; `serving-resume` asks
  because billing resumes. Lowering the range, pausing, and cap decreases apply immediately as before.
- `pod-create --wait` exits with code 1 when the pod does not appear within `--wait-timeout`; the accepted transaction
  id is still printed. Previously it exited 0 as if the wait had succeeded.
- `pod-create --disk` is gone (see above). `storage-create` output shows the disk type that was priced.

## v0.1.0rc1

Pre-release of 0.1.0 for early testing. `pip install meshive` keeps installing the latest stable
release; use `pip install --pre meshive` (or `pip install meshive==0.1.0rc1`) to try it. Contents are
those of the v0.1.0 section below.

## v0.1.0

The SDK and CLI can now **create and manage resources**, not just read them. This needs an API key issued with the
**write** scope (console → workspace Settings → Secret → "Read & write"; such keys always expire, 30 days by default).
Read-only keys keep working for everything that existed before. Newly issued read keys now expire too (365 days) —
they can fetch pod and task logs, which often contain tokens your container printed. Keys issued earlier are unaffected.

### New in the SDK (sync and async)

- **Pods**: `estimate_pod()` shows the hourly price before you spend anything; `create_pod()` takes a template ID, a GPU model
  and count (or nothing for a CPU pod), optional vCPU/RAM/disk, existing storage volumes, env vars, ports and a price cap.
  Then `stop_pod()`, `start_pod()`, `restart_pod()`, `delete_pod()`.
- **Storage**: `estimate_storage()`, `create_storage()`, `delete_storage()`. At-rest encryption (`encrypted=True`) is
  available for `nfs` (network) volumes only; for `hostPath` the SDK raises `ValueError` before sending anything.
- **Serverless**: `deploy_serving()`, `scale_serving()`, `pause_serving()`, `delete_serving()`; `estimate_task()`,
  `submit_task()`, `stop_task()`.
- **Logs**: `get_pod_logs()` and `get_task_logs()` return the last N lines (up to 1000). If nothing is buffered yet the server
  starts a log watcher and waits briefly. Tip: in task scripts use `print(..., flush=True)` so output is captured.
- **Safety**: every write request carries an `Idempotency-Key`, so retries after a timeout never create a second pod.
  Pass `max_price_per_hour=` to have the server refuse anything more expensive than you expect.
- New exceptions: `ConflictError` (no capacity, name taken, price above cap, storage in use) and `InsufficientCreditError`.
- `Meshive(headers={...})` adds custom headers to every request (used by the Meshive MCP server).

### New in the CLI

- `pod-create`, `pod-stop`, `pod-start`, `pod-restart`, `pod-delete`, `storage-create`, `storage-delete`, `serving-deploy`,
  `serving-scale`, `serving-pause`, `serving-resume`, `serving-delete`, `task-submit`, `task-stop`, `logs`, `task-logs`.
- Commands that spend credit or delete something show the estimate and ask for confirmation; pass `--yes` in scripts,
  or `--estimate` to only see the price.
- Amounts print exactly as the Meshive web console shows them: hourly rates to three decimals (`$0.068/hr`), every
  other amount to two (`$2.10`). Previously an hourly rate was rounded to two decimals, so a workspace the console
  showed as `$0.068/hr` read `$0.07/hr` in the terminal and a small storage volume read `$0.00/hr`. Use `-o json`
  for the unrounded number.
- `meshive.format_hourly()` and `meshive.format_usd()` are public, so your own output can show the same amounts the
  console and CLI do. The SDK still returns the server's unrounded number (`pod.price_per_hour == "0.06770833"`) —
  format it only when you print it.

## v0.0.7

Many more read-only views of your account are now available from Python and the terminal.

### New in the SDK and CLI

- **Workspaces**: view a workspace's details and its member list.
- **Storage**: list the storage volumes attached to a workspace.
- **Metrics**: read CPU, memory, and GPU usage for a pod or a host machine.
- **GPU availability**: see which GPU types are available to rent right now.
- **API keys**: list the API keys on your account.
- **Credits**: check your credit balance and browse your credit history.
- **Host earnings**: if you host machines, view your earnings.
- **Templates**: browse the pod templates you can launch from.
- **Serverless**: list your serverless servings and inspect their tasks.
- **Asset Hub**: browse published assets with paging, and view an asset's details and storage.

This adds about 20 read-only SDK methods and CLI subcommands. Run `meshive --help` to see the full command list.

### Fixes

- Sizes under 1 GB are now shown in MB instead of `0.0 GB`.
- Asking for a page past the end of the asset list now explains how many assets and pages exist instead of showing a confusing page number.

## v0.0.6

This release makes scripts more resilient and the CLI easier to use in pipelines.

### SDK

- **Automatic retries**: requests that hit rate limits (429), server errors (5xx), or connection failures are retried automatically (2 retries by default, with exponential backoff and respect for `Retry-After`). If the server asks you to wait more than 60 seconds, the SDK raises `RateLimitError` right away instead of silently stalling your script. Other 4xx errors are never retried. Disable with `Meshive(max_retries=0)`.
- **`wait_for_pod()`** (sync and async): poll until a pod reaches the state you want. If the pod ends up in `error` or `terminated`, the call fails immediately instead of waiting for the timeout. Timeouts raise `WaitTimeoutError`, which is both a `MeshiveError` and a built-in `TimeoutError`.
- **Type hints for your editor**: the package now ships `py.typed`, so mypy and pyright see the SDK's type annotations instead of treating it as untyped.

### CLI

- Network errors and invalid base URLs now print a short message and exit with code 1 instead of a Python traceback. Ctrl-C exits with code 130.
- New `-o {table,json,name}` output option. `-o name` prints one ID per line, which is handy for piping into other commands. `--json` still works as an alias for `-o json`.
- `meshive pod --wait STATUS` and `--wait-timeout` let you block until a pod reaches a given state.

## v0.0.5

A security-hardening release. There are no new features, but upgrading is recommended.

- Identifiers you pass to the SDK (pod names, machine IDs) are URL-encoded, so unusual characters can't change which endpoint is called.
- The base URL from the environment or the credentials file must be an absolute `http(s)` URL. Anything else is rejected before your API key is sent.
- The credentials file is written atomically with `0600` permissions and without following symlinks, so a crash mid-write can't corrupt it and a planted symlink can't redirect your key elsewhere.
- Text returned by the server is stripped of control and bidirectional-override characters before it is printed, preventing terminal escape injection and spoofed output.
- Error messages from the server are truncated to a reasonable length. The full response is still available via `MeshiveAPIError.raw`.
- Added a CI workflow (tests on Python 3.10–3.13 plus `pip-audit`) and switched PyPI publishing to trusted publishing.

## v0.0.4

Housekeeping only.

- Cleaned up the project links shown on the PyPI page.
- Fixed a heading typo in the README.

## v0.0.3

If you host GPU machines on Meshive, you can now check on them from the SDK and CLI.

### SDK

- `list_machines()` and `get_machine()` return the machines your account hosts.

### CLI

- `meshive machines` and `meshive machine <id>` show your hosted machines with GPU, earnings, and uptime columns.
- `meshive pods --all` lists pods across all of your workspaces at once, with a new `WORKSPACE` column.

## v0.0.2

The first usable release: read-only access to your account from Python and the terminal.

### SDK

- Sync and async clients (`Meshive` and `AsyncMeshive`) built on httpx.
- Read endpoints: current user, workspaces, pod list, and single pod lookup.

### CLI

- `meshive login` / `meshive logout` to store or remove your API key.
- `meshive me`, `meshive workspaces`, `meshive pods`, `meshive pod <name>`.
- Colored tables with currency and relative-time formatting, `--status` / `--rental` / `--name` filters, and `--json` output.
- Credentials are read from `~/.meshive/credentials.json` or from the `MESHIVE_BASE_URL` and `MESHIVE_API_KEY` environment variables.

## v0.0.1

Initial release. CLI and SDK skeleton with version reporting.
