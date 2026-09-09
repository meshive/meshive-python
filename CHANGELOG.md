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
