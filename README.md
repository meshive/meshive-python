# Meshive Python SDK & CLI

[![PyPI](https://img.shields.io/pypi/v/meshive)](https://pypi.org/project/meshive/)
[![Python](https://img.shields.io/pypi/pyversions/meshive)](https://pypi.org/project/meshive/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/meshive/meshive-python/blob/dev/LICENSE)

Rent GPUs on [Meshive](https://meshive.ai) from your terminal or from Python. Launch pods, run serverless
tasks, serve models and manage your files: the same things you can do in the console, scriptable.

[Documentation](https://docs.meshive.ai/sdk-cli/) · [Console](https://console.meshive.ai) ·
[GPU pricing](https://docs.meshive.ai/documentation/pricing/) ·
[Use it from AI agents](https://github.com/meshive/meshive-mcp)

## Install

Install the `meshive` command with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install meshive
```

or with [pipx](https://pipx.pypa.io/): `pipx install meshive`. Either one keeps the CLI in its own environment, and
uv downloads a suitable Python if you don't have 3.10 or newer. Check it with `meshive --version`. If the command
isn't found, run `uv tool update-shell` (or `pipx ensurepath`) and open a new terminal. Upgrade with
`uv tool upgrade meshive` (or `pipx upgrade meshive`).

To use the Python SDK, install the package in your project's virtual environment:

```bash
pip install "meshive>=1,<2"
```

Requires Python 3.10 or newer (tested on 3.10 to 3.13). A `pip install` outside a virtual environment fails with
`externally-managed-environment` on Homebrew Python and on recent Debian and Ubuntu releases; use the commands above.

In a requirements file, pin the major version so you get fixes but nothing that breaks your code:

```text
meshive>=1,<2
```

### Upgrading from a pre-1.0 release

1.0.0 works with code written for the 0.1 releases; no changes are needed. Change your pin to `meshive>=1,<2`
and reinstall, and regenerate your lock file if you use one. Most pre-1.0 releases are being removed from PyPI
(see the [changelog](https://github.com/meshive/meshive-python/blob/dev/CHANGELOG.md) for dates), so a pin to
one of them stops installing after its removal date. Environments where it is already installed keep working.

## Quickstart

**1. Get an API key.** In the [console](https://console.meshive.ai), open
**Account → API keys**. Pick **Read & write** so you can create pods.

**2. Log in.**

```bash
meshive login          # paste the key; it is saved for later commands
meshive workspaces     # note the ID of your workspace
```

**3. Pick a GPU and a template.**

```bash
meshive gpus                       # GPUs you can rent right now, with hourly prices
meshive templates --name jupyter   # note the template ID
```

**4. Launch a pod.**

```bash
meshive pod-create <workspace> my-first-pod --template <template-id> --gpu "RTX 3060" --wait running
```

The CLI shows the hourly price and asks before anything is billed. With `--wait running` it returns
once the pod is up and prints its ID.

**5. Open it.**

```bash
meshive pod <workspace> <pod>      # the pod's URLs (Jupyter, ...) and its login, if it has one
```

**6. Stop or delete it when you're done.**

```bash
meshive pod-stop <workspace> <pod>     # compute billing stops; attached storage is still billed
meshive pod-delete <workspace> <pod>
```

## Use it from Python

```python
from meshive import Meshive, format_hourly

with Meshive() as client:  # uses the key saved by `meshive login`, or MESHIVE_API_KEY
    workspace = client.list_workspaces()[0].namespace_name

    for gpu in client.list_gpus():
        print(gpu.gpu_model, f"{gpu.vram} GB", format_hourly(gpu.price_per_hour), gpu.available_gpus)

    template = next(t for t in client.list_templates() if t.name == "Jupyter")
    estimate = client.estimate_pod("my-first-pod", template.template_id, workspace=workspace, gpu_model="RTX 3060")
    print("estimate:", format_hourly(estimate.price_per_hour))

    client.create_pod("my-first-pod", template.template_id, workspace=workspace, gpu_model="RTX 3060",
                      max_price_per_hour=0.10)  # never pay more than $0.10/hr for compute

    pod = client.wait_for_new_pod("my-first-pod", workspace, until="running")
    for endpoint in pod.endpoints:
        print(endpoint.name, endpoint.external_url, endpoint.access)   # locked: members and access keys only

    client.stop_pod(pod.pod_name, workspace)
```

`AsyncMeshive` has the same methods for `async` code, and the package ships type hints. Prices come back
as the exact numbers the server sent; `format_hourly` and `format_usd` round them the way the console
shows them.

A program that creates many clients, such as a server with one per API key, can let them share connections: create
one `transport = httpx.AsyncHTTPTransport()` (or `httpx.HTTPTransport()` for `Meshive`) at startup and pass
`transport=transport` to each client. Every client still sends only its own key. Closing a client leaves the
transport open; close it yourself at shutdown.

Errors are exceptions you can catch by kind. All of them derive from `MeshiveError`:

```python
from meshive import InsufficientCreditError, PodCreationFailedError, WaitTimeoutError

try:
    pod = client.wait_for_new_pod("my-first-pod", workspace, until="running")
except PodCreationFailedError as err:
    print("the pod could not start:", err)
except WaitTimeoutError:
    print("still not running; `meshive transactions <workspace>` shows what it is waiting for")
```

| Exception | When |
| --- | --- |
| `ConfigurationError` | No API key was found, or the base URL is invalid. Nothing was sent. |
| `AuthenticationError` | 401: the key is missing, wrong or expired. |
| `PermissionDeniedError` | 403: the key is **Read only**, or your workspace role can't make changes. |
| `NotFoundError` | 404: no such pod, task, asset, ... |
| `InsufficientCreditError` | 402: not enough credit to create or start something. |
| `ConflictError` | 409: no capacity, a name already taken, a price above your cap, storage still in use, a workspace that isn't empty, ... `err.title` says which. |
| `EndpointLockUnavailableError` | A pod was to be created or changed with a locked port, and the server can't lock ports right now. Nothing was created or changed. A `ConflictError`. |
| `RateLimitError` | 429 after the automatic retries. `err.retry_after` says how long to wait. |
| `WaitTimeoutError` | A `wait_for_*` call ran out of time. Also a built-in `TimeoutError`. |
| `PodCreationFailedError` | The platform couldn't create the pod and cleaned it up; the message says why. |
| `PortNotLockedError` | `wait_for_new_pod(port_locks=...)` found a port open that was created to be locked. |

## What else you can do

| To... | Run |
| --- | --- |
| Create a workspace (free by itself) and wait until it is running | `meshive workspace-create <name> --wait` |
| Rename a workspace, or delete an empty one | `meshive workspace-update <workspace> --name <name>`, `meshive workspace-delete <workspace>` |
| Run a script on a GPU and collect the results | `meshive task-submit <workspace> train --script train.py --image python:3.12-slim --gpu "RTX 3060"` |
| Serve a Hugging Face model behind an API | `meshive model-register <workspace> Qwen/Qwen3-0.6B`, then `meshive serving-deploy` |
| Bring in a model or dataset from Hugging Face, CivitAI or a URL | `meshive asset-import <workspace> <repo or URL>` |
| Download files or task outputs | `meshive asset-download <asset>`, `meshive task-outputs <task> --download ./out` |
| SSH into a pod | `meshive ssh <workspace> <pod>` |
| Run a command in a running pod (add `--background` for long work) | `meshive pod-exec <workspace> <pod> -- nvidia-smi` |
| Upload a file or folder from your computer as an asset | `meshive asset-upload <workspace> ./my-data` |
| Change a pod's GPU, ports, environment or storage | `meshive pod-update <workspace> <pod> --gpu "RTX 4090"` |
| Lock or unlock a pod's public port | `meshive port-lock <workspace> <pod> <port>`, `meshive port-unlock <workspace> <pod> <port>` |
| Give a program access to a locked port | `meshive port-key-create <workspace> <pod> <port> --name ci`, then send `Authorization: Bearer <key>` |
| List, roll or revoke a port's access keys, and see when each was last used | `meshive port-keys <workspace> <pod> <port>`, `meshive port-key-roll ...`, `meshive port-key-revoke ...` |
| Let a page on another site call a locked port (CORS preflight) | `meshive port-preflight <workspace> <pod> <port> on` (`off` to undo) |
| Sign out everyone who opened a pod's locked ports in a browser | `meshive pod-browser-sessions-revoke <workspace> <pod>` |
| Do the same for every pod in a workspace | `meshive workspace-browser-sessions-revoke <workspace>` |
| Use a locked port from an app that only takes an address | `meshive port-forward <workspace> <pod> <port>` with the key in `MESHIVE_ACCESS_KEY`, then point the app at `http://127.0.0.1:<port>` |
| Grow or rename storage, or SSH into it | `meshive storage-update <workspace> <storage> --size 100`, `meshive storage-connect <workspace> <storage>` |
| Start a pod with a ready-made set of models | `meshive model-sets <workspace> --template <template-id>`, then `meshive pod-create ... --model-set <slug>` |
| See why a pod is still starting | `meshive transactions <workspace>` |
| Watch a pod's usage and logs | `meshive pod-metrics <workspace> <pod>`, `meshive logs <workspace> <pod>` |
| See why a pod keeps restarting, died or couldn't be created (out of RAM, out of GPU memory, ...) | `meshive pod <workspace> <pod>` (the line under its status, its EXITED table), `meshive logs <workspace> <pod>` (the run that ended comes first) |
| Check your credit and spending | `meshive credit`, `meshive workspace <workspace>` |
| See what your API keys (and agents using them) did, with results | `meshive activity`, `meshive activity --reads` |
| Host your own machines and see earnings | `meshive machines`, `meshive earnings`, `meshive payouts` |
| Schedule maintenance on a hosted machine, list or unlist it for rent, or remove it | `meshive maintenance-schedule <machine> ...`, `meshive machine-list-for-rent <machine>`, `meshive machine-unlist <machine>`, `meshive machine-delete <machine>` |
| Read and answer messages from the workspaces using your machines | `meshive host-messages`, `meshive host-message-send <machine> --to <workspace>` |
| Report a bug or send an idea to the Meshive team | `meshive feedback "one line" --kind bug --details "what happened"` |

Every command has `--help`. The [CLI reference](https://docs.meshive.ai/sdk-cli/cli/) lists them all.

## Good to know

- **Nothing is billed without asking.** Commands that spend credit or delete something show the price or
  a summary first and ask you to confirm. Add `--estimate` to see the price only, or `--yes` to skip the
  question in scripts.
- **Two kinds of keys.** A **Read only** key can see everything. A **Read & write** key is needed to create,
  change or delete, and it expires (after 30 days by default).
- **Public ports are created locked.** A port that is newly exposed over HTTPS, on a new pod or by `pod-update`,
  lets in only workspace members signed in to the console, and programs that send one of the port's access keys
  (`meshive port-key-create`). Pass `--no-lock` (`lock_public_ports=False`) to create them open to anyone with the
  URL, as before 1.10. Ports that are public already keep the lock they have.
  An access key starts with `mshv_`. Send a pod's URL only that key, never your Meshive account API key
  (`meshive_...`).
- **Apps that can't send a key.** `meshive port-forward <workspace> <pod> <port>` listens on `http://127.0.0.1:<port>`
  on your computer and passes every request on to the locked port with the access key added, WebSockets included.
  The key is read from the `MESHIVE_ACCESS_KEY` environment variable or from `--key-file`, never from the command
  line. It answers only on 127.0.0.1 and refuses requests made by web pages on other sites; programs on your
  computer reach the port through it without a key, so stop it (Ctrl+C) when you are done.
- **IDs and names.** Lists show the NAME you chose and an ID. Commands take the ID.
- **Scripting.** `-o json` prints the full response and `-o name` prints only IDs, one per line.
- **Retries are safe.** Rate limits and network errors are retried automatically. Every create or change carries an
  idempotency key, so a retried request is never applied twice.

## Configuration

The API key is looked up in this order: the `--api-key` option (or `Meshive(api_key=...)`), the
`MESHIVE_API_KEY` environment variable, then the file saved by `meshive login`.

| Variable | Meaning |
| --- | --- |
| `MESHIVE_API_KEY` | API key. Handy in CI, where you don't run `meshive login`. |
| `MESHIVE_BASE_URL` | API address. Leave it unset unless you were given a different one. |
| `MESHIVE_CONFIG_DIR` | Where `meshive login` keeps the key. Defaults to `~/.meshive/` (the directory is `0700`, `credentials.json` is `0600`). |
| `NO_COLOR` or `MESHIVE_NO_COLOR` | Turn off colors. Colors are already off when the output isn't a terminal. |

`meshive logout` deletes the saved key.

## Troubleshooting

| You see | What to do |
| --- | --- |
| An error about a missing API key | Run `meshive login`, or set `MESHIVE_API_KEY`. |
| `401` / `AuthenticationError` | The key is wrong or has expired. Create a new one under **Account → API keys** and log in again. |
| `403` / `PermissionDeniedError` when creating or deleting | The key is **Read only**, or your role in that workspace is viewer. Use a **Read & write** key. |
| `402` / `InsufficientCreditError` | Top up credit in the [console](https://console.meshive.ai). |
| `409` with "No Capacity" | That GPU is sold out right now. `meshive gpus` shows what is available. |
| A pod stays `creating` for minutes | `meshive transactions <workspace>` shows the current step, such as an image download and its progress. |
| A script stops with exit code `2` | A command needed confirmation. Add `--yes`, or check first with `--estimate` (`--check` for `workspace-delete`). |
| "could not reach the Meshive API" | Check your network, and `MESHIVE_BASE_URL` if you set it. |
| `CERTIFICATE_VERIFY_FAILED`, or another message about a certificate that isn't trusted | Something between you and the API (an antivirus, a company proxy) inspects HTTPS and signs with its own root. Upgrade with `pip install -U meshive`: the roots your operating system trusts are trusted from 1.10.1 on. If it still fails, see [Certificate errors](https://docs.meshive.ai/sdk-cli/cli/#certificate-errors). |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The API returned an error, the key is missing or invalid, the API couldn't be reached, `workspace-delete` found the workspace isn't empty, or `port-lock`/`port-unlock` couldn't confirm within 60 seconds that the choice is in effect. |
| `2` | Bad arguments, or a command needed confirmation and didn't get it (no terminal and no `--yes`, or you answered no). |
| `130` | Interrupted with Ctrl-C. |

## Versioning

This package follows [semantic versioning](https://semver.org). Within 1.x, upgrades don't break your code: new
features arrive in minor versions and fixes in patch versions. Anything slated for removal first warns with a
`DeprecationWarning` and is removed only in the next major version. Every change is listed in the
[changelog](https://github.com/meshive/meshive-python/blob/dev/CHANGELOG.md).

## Use Meshive from an AI agent

The [Meshive MCP server](https://github.com/meshive/meshive-mcp) lets Claude Code, Codex, Cursor and other
agents do all of this in plain language.

## Learn more

- [CLI reference](https://docs.meshive.ai/sdk-cli/cli/): every command and option
- [SDK reference](https://docs.meshive.ai/sdk-cli/sdk/): methods, data models, errors, retries
- [Authentication](https://docs.meshive.ai/sdk-cli/#authentication): keys, environment variables, config location
- [Changelog](https://github.com/meshive/meshive-python/blob/dev/CHANGELOG.md)
- Contributing: [DEVELOPMENT.md](https://github.com/meshive/meshive-python/blob/dev/DEVELOPMENT.md)

## License

[Apache License 2.0](https://github.com/meshive/meshive-python/blob/dev/LICENSE)
