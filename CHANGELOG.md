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

## v1.1.0

Pods can now get local storage of their own, CPU pods can choose their CPU line, and a pod that fails to come up
tells you why as soon as it happens. Pod creation now follows the same rules as the web console. Code written for
1.0 keeps working.

### What's new

- **Local storage with a pod.** You can now create local storage on a pod's machine together with the pod:
  `create_pod(local_volumes=[{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}])`, or
  `meshive pod-create --local-volume scratch:/scratch:50` (add `:encrypted` to encrypt it). The estimate includes
  its price. Not available for spot pods.
- **CPU lines.** `list_cpus()` and `meshive cpus` list the CPU lines you can rent, with prices per vCPU and per GiB
  of RAM. Pass one to `create_pod(cpu_line=...)` or `pod-create --cpu-line` to choose where a CPU pod runs; without
  it, the cheapest line that fits is used.
- **Starts that wait for room.** `start_pod` and `meshive pod-start --placement` now take `wait_same_node` and
  `wait_any_node`: the pod waits until a machine has room, then starts. `wait_any_node` needs the same data-loss
  consent as `any_node`.
- **Failed creations reported right away.** Pass `transaction_id=created.transaction_id` to `wait_for_new_pod` and a
  creation that fails ends the wait at once with the reason, such as an image that can't be pulled, instead of
  running into the timeout. `meshive pod-create --wait` does this for you.
- **One operation by ID.** `get_transaction(transaction_id, workspace)` and `meshive transaction ID --workspace WS`
  show a pod operation's status, steps and failure reason, even after it has dropped out of `transactions`.

### Changed

- **Pod creation follows the console's rules.** vCPU and RAM must be values the console offers, a port the
  template already opens or a port listed twice is refused, required environment variables of the template need a
  value, and a command can be at most 4096 characters. The error says what is allowed. CPU pods now default to
  8 vCPU and 32 GiB of RAM (or the nearest the line offers), as in the console, instead of 2 vCPU and 4 GiB.
- **Local storage is created only together with a pod.** Creating local (`hostPath`) storage on its own is refused,
  and a new pod's `volumes` can attach existing network storage only. Use network storage, or `local_volumes`.
- The SDK and CLI no longer check placements, storage types, disk types or the GPU count themselves. The server
  decides, so new values work without upgrading.

### Deprecated

These are still accepted but ignored by the server. They warn with a `DeprecationWarning` (the CLI prints a note)
and will be removed in 2.0.

- `region` on `create_pod`, `estimate_pod`, `create_storage` and `estimate_storage`, and `--region` on `pod-create`
  and `storage-create`. Pods and storage are placed without a region.
- `disk_type` on `create_storage` and `estimate_storage`, and `--disk` on `storage-create`. Storage is always NVMe.

## v1.0.0

The first stable release of the Meshive Python SDK and the `meshive` command. Everything you can do in the
Meshive console can be scripted: find GPUs, launch and manage pods, run serverless tasks, serve models and move
files in and out of Asset Hub. From here on the package follows semantic versioning, so upgrades within 1.x
don't break your code.

If you used a pre-1.0 release, change your pin to `meshive>=1,<2` and reinstall. No code changes are needed.

**Pre-1.0 releases on PyPI.** 0.1.4 stays available. 0.0.1 to 0.1.0 were removed on October 13, 2026; 0.1.1 is removed
on November 12, 2026, and 0.1.2 and 0.1.3 on January 11, 2027. Environments that already have one installed keep
working, but a fresh install pinned to a removed version fails; change the pin to `meshive>=1,<2`.

### What's included

- **Account and workspaces:** your account, workspaces and members, API keys, credit balance and history, and
  daily spending per workspace.
- **GPUs and templates:** GPUs you can rent right now with hourly prices, and the templates to start from.
- **Pods:** estimate, create, start, stop, restart and delete. Wait for a pod to come up, see its URLs and login,
  watch its metrics and logs, open a one-time SSH session, and see why it is still creating. A pod that fails to
  start says why.
- **Storage:** create and delete volumes, with the price before you commit.
- **Serving:** check whether a Hugging Face model can be served, register it, deploy it behind an API, and scale,
  pause or delete the deployment.
- **Serverless tasks:** run a script on a GPU or CPU, follow its logs and download its outputs.
- **Asset Hub:** list and inspect assets, link models and datasets from Hugging Face, CivitAI or a URL, download
  files, attach assets to pods, and upload new files from a pod's watched folders.
- **Hosting:** your machines, their live metrics, and your earnings.

### Built in

- **Asks before spending.** CLI commands that cost money or delete something show the price or a summary and wait
  for your confirmation. `--estimate` shows the price only; `--yes` skips the question in scripts.
- **Price caps.** `max_price_per_hour` / `--max-price` refuses a pod that would cost more than you set.
- **Safe retries.** Rate limits and network errors are retried automatically, and every write carries an
  idempotency key, so a retried request is never applied twice.
- **Amounts as the console shows them.** `format_hourly` and `format_usd` round prices the same way the console
  does, and sizes use the console's units (GiB, MiB; GPU memory in GB).
- **Scripting output.** `-o json` prints the full response and `-o name` prints only IDs.
- **Typed.** Sync (`Meshive`) and async (`AsyncMeshive`) clients with the same methods, typed models and a
  specific exception for each kind of error.

### Deprecated

- `Asset.versions`, `Asset.latest_version` and `Asset.version_count` warn with a `DeprecationWarning` and will be
  removed in 2.0. Assets no longer have versions; use `Asset.size_bytes`, `file_count`, `upload_status` and
  `files`.

## v1.0.0rc2

Second release candidate for 1.0.0. Requires httpx below 1.0. Same code as 1.0.0.

## v1.0.0rc1

Release candidate for 1.0.0, published to check the release before the final version. Same code as 1.0.0.
