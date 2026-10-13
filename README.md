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

```bash
pip install meshive
```

Requires Python 3.10 or newer (tested on 3.10 to 3.13). This installs the `meshive` command and the `meshive`
Python package. Check it with `meshive --version`.

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

**1. Get an API key.** In the [console](https://console.meshive.ai), open your workspace's
**Settings → API keys**. Pick **Read & write** so you can create pods.

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
        print(endpoint.name, endpoint.external_url)

    client.stop_pod(pod.pod_name, workspace)
```

`AsyncMeshive` has the same methods for `async` code, and the package ships type hints. Prices come back
as the exact numbers the server sent; `format_hourly` and `format_usd` round them the way the console
shows them.

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
| `ConflictError` | 409: no capacity, a name already taken, a price above your cap, storage still in use, ... `err.title` says which. |
| `RateLimitError` | 429 after the automatic retries. `err.retry_after` says how long to wait. |
| `WaitTimeoutError` | A `wait_for_*` call ran out of time. Also a built-in `TimeoutError`. |
| `PodCreationFailedError` | The platform couldn't create the pod and cleaned it up; the message says why. |

## What else you can do

| To... | Run |
| --- | --- |
| Run a script on a GPU and collect the results | `meshive task-submit <workspace> train --script train.py --image python:3.12-slim --gpu "RTX 3060"` |
| Serve a Hugging Face model behind an API | `meshive model-register <workspace> Qwen/Qwen3-0.6B`, then `meshive serving-deploy` |
| Bring in a model or dataset from Hugging Face, CivitAI or a URL | `meshive asset-import <workspace> <repo or URL>` |
| Download files or task outputs | `meshive asset-download <asset>`, `meshive task-outputs <task> --download ./out` |
| SSH into a pod | `meshive ssh <workspace> <pod>` |
| See why a pod is still starting | `meshive transactions <workspace>` |
| Watch a pod's usage and logs | `meshive pod-metrics <workspace> <pod>`, `meshive logs <workspace> <pod>` |
| Check your credit and spending | `meshive credit`, `meshive workspace <workspace>` |
| Host your own machines and see earnings | `meshive machines`, `meshive earnings` |

Every command has `--help`. The [CLI reference](https://docs.meshive.ai/sdk-cli/cli/) lists them all.

## Good to know

- **Nothing is billed without asking.** Commands that spend credit or delete something show the price or
  a summary first and ask you to confirm. Add `--estimate` to see the price only, or `--yes` to skip the
  question in scripts.
- **Two kinds of keys.** A **Read only** key can see everything. A **Read & write** key is needed to create,
  change or delete, and it expires (after 30 days by default).
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
| `401` / `AuthenticationError` | The key is wrong or has expired. Create a new one under **Settings → API keys** and log in again. |
| `403` / `PermissionDeniedError` when creating or deleting | The key is **Read only**, or your role in that workspace is viewer. Use a **Read & write** key. |
| `402` / `InsufficientCreditError` | Top up credit in the [console](https://console.meshive.ai). |
| `409` with "No Capacity" | That GPU is sold out right now. `meshive gpus` shows what is available. |
| A pod stays `creating` for minutes | `meshive transactions <workspace>` shows the current step, such as an image download and its progress. |
| A script stops with exit code `2` | A command that spends credit wanted confirmation. Add `--yes`, or check with `--estimate` first. |
| "could not reach the Meshive API" | Check your network, and `MESHIVE_BASE_URL` if you set it. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The API returned an error, the key is missing or invalid, or the API couldn't be reached. |
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
