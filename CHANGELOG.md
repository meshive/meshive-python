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

## v1.0.0rc2

Second release candidate for 1.0.0. `pip install meshive` keeps installing the current stable release; try this one with `pip install "meshive==1.0.0rc2"`. It requires httpx below 1.0: installing 1.0.0rc1 with `pip install --pre` picked up an httpx 1.0 development release, which the SDK does not support. Its contents are what 1.0.0 will be:

The first stable release of the Meshive Python SDK and the `meshive` command. Everything you can do in the
Meshive console can be scripted: find GPUs, launch and manage pods, run serverless tasks, serve models and move
files in and out of Asset Hub. From here on the package follows semantic versioning, so upgrades within 1.x
don't break your code.

If you used a pre-1.0 release, change your pin to `meshive>=1,<2` and reinstall. No code changes are needed.

**Pre-1.0 releases on PyPI.** 0.1.4 stays available. 0.0.1 to 0.1.1 are removed on November 12, 2026, and 0.1.2 and 0.1.3
on January 11, 2027. Environments that already have one installed keep working, but a fresh install pinned to a removed
version fails; change the pin to `meshive>=1,<2`.

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

## v1.0.0rc1

Release candidate for 1.0.0, published to check the release before the final version. Same code as 1.0.0.
