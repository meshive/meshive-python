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
