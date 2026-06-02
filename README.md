## Install

```bash
pip install meshive
```

ㅇ## Authentication

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

By default requests go to the production API. To target the dev endpoint, override the
base URL (same precedence: flag › env › login file › prod default) — no code change needed:

```bash
export MESHIVE_BASE_URL=https://api.dev.meshive.ai
# or remember it at login time:
meshive login --base-url https://api.dev.meshive.ai
```

> The dev endpoint needs a dev-issued key — pair `--base-url`/`MESHIVE_BASE_URL` with the
> matching key. The config directory can be relocated via `MESHIVE_CONFIG_DIR`.

## CLI

```bash
meshive --version
meshive me                     # current API key's owner
meshive workspaces             # list workspaces
meshive pods <workspace>       # list pods in a workspace
meshive pods --all             # list pods across every workspace (adds a WORKSPACE column)
meshive pod <workspace> <pod>  # show a single pod

# filter pods (client-side; the API itself returns the full list)
meshive pods <workspace-id> --status running
meshive pods <workspace-id> --status running,error   # comma-separated or repeatable
meshive pods <workspace-id> --rental spot
meshive pods <workspace-id> --name llama              # match the display name (alias)

# any command accepts --json for raw output, plus --api-key / --base-url overrides
meshive pods <workspace-id> --json
```

### IDs vs names

List output shows two columns:

- **ID** — the canonical identifier (`namespace_name` for workspaces, `pod_name` for pods).
  This is what you pass to `meshive pods <id>` / `meshive pod <id> <id>`. It is unique and stable.
- **NAME** — the display alias you set (`workspace_name` / `user_alias`). It is a *label*, not
  a key: it is not guaranteed unique and can change. Use `--name` to filter by it, but address
  resources by their **ID**.

## SDK

```python
from meshive import Meshive

with Meshive() as client:               # reads MESHIVE_API_KEY / MESHIVE_BASE_URL
    me = client.me()
    print(me.email, me.user_role)

    for ws in client.list_workspaces():
        print(ws.namespace_name, ws.status)

    pods = client.list_pods("my-workspace")
    pod = client.get_pod(pods[0].pod_name, "my-workspace")
    print(pod.status, pod.raw)           # .raw holds the full payload (machine, template, ...)
```

Credentials can also be passed explicitly: `Meshive(api_key="meshive_...", base_url="https://api.dev.meshive.ai")`.

### Async

```python
from meshive import AsyncMeshive

async with AsyncMeshive() as client:
    me = await client.me()
    pods = await client.list_pods("my-workspace")
```

### Errors

All errors subclass `meshive.MeshiveError`:

- `ConfigurationError` — missing API key
- `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404),
  `RateLimitError` (429, exposes `.retry_after`), and `MeshiveAPIError` for other HTTP errors
  (carry `.status_code`, `.title`, `.message`, `.raw`)

## License

[Apache License 2.0](LICENSE)
