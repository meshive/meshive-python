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

## v1.2.0

Pods and storage can now be changed after they are created, storage can be opened over SSH, and pods can start with
a model set, all as in the web console. Code written for 1.0 and 1.1 keeps working.

### What's new

- **Edit pods.** `update_pod(pod_name, workspace, ...)` and `meshive pod-update WORKSPACE POD` change a pod's
  name, template, GPU, vCPU and RAM, CPU line, rental type, environment variables, ports, command and storage, like
  the console's edit screen. Only what you pass changes. `estimate_pod_update` (`--estimate`) lists the changes, the
  new hourly price and whether the running pod restarts; a rename alone applies without a restart. If the pod's
  machine can't fit the change, the update is refused until you allow a move with `allow_node_migration`
  (`--allow-node-migration`), plus `allow_data_loss` (`--allow-data-loss`) when the pod has unpreserved files. A
  change that removes watched folders needs `drop_watched_folders` (`--drop-watched-folder`). The CLI says what to
  rerun with.
- **Edit storage.** `update_storage(pv_name, workspace, ...)` and `meshive storage-update WORKSPACE STORAGE` rename a
  storage (a name another volume in the workspace uses is refused, as in the console), grow it (`size_gb`,
  `--size`; storage can't shrink) and turn its usage warning on or off
  (`--usage-warning [PERCENT]`, `--no-usage-warning`). `estimate_storage_update` shows the new price first.
- **SSH into storage.** `connect_storage(pv_name, workspace)` and `meshive storage-connect WORKSPACE STORAGE` open a
  storage for SSH and SFTP, like the console's Connect button: they wait until it is ready and return a one-time SSH
  command and password. Encrypted storage is unlocked for this and locks again automatically after a while.
  `get_storage_connection` (`--no-wait`) only reads the status.
- **Model sets.** `list_model_sets(workspace, template_id)` and `meshive model-sets WORKSPACE --template ID`
  list the model sets a template can start with: their files, size, minimum VRAM, licenses and workflows. Pass a
  set's slug to `create_pod(model_set=...)` or `meshive pod-create --model-set SLUG`, and the estimate shows it. The
  server fetches the set's files before it answers, which can take minutes, so this create waits up to 15 minutes
  and is not resent automatically after a timeout (the pod may still be created; check before trying again).
- **Watched folders and input assets as on the console's create screen.** `create_pod` and `estimate_pod` take
  `watched_template_folders` to turn off the template's own watched folders or change their patterns from the start
  (`{"/outputs": {"enabled": False}}`; `meshive pod-create --watch-off PATH`). A folder in `watched_folders` can be
  added turned off with `"enabled": False`, and an input asset with `paths` takes `strip_prefix`: `""` keeps the
  folder structure of the chosen files, like the console's Keep structure.

### Changes

- An `input_assets` item with a key the SDK doesn't know, such as `include_paths` instead of `paths`, now raises
  `ValueError` instead of being ignored, which attached the whole asset.
- A pod created from a template that declares no output folders now keeps the default `/outputs` rule it was created
  with, as console-created pods do, so a later change to the template doesn't change it when the pod is edited.
- Changing a pod's template with `update_pod(template_id=...)` starts it from the new template's variables, ports and
  command; the estimate's `changes` now lists what you added that doesn't carry over.

## v1.1.0

Pods can now get local storage of their own, CPU pods can choose their CPU line, and a pod that fails to come up
tells you why as soon as it happens. Creating and starting pods now follows the same rules as the web console. Code
written for 1.0 keeps working.

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
- **Template ports can be changed.** `create_pod(template_ports=[{"port": 8080, "name": "ide"}])` or
  `meshive pod-create --template-port 8080:ide` renames a port the template opens; `new_port` (`8080::8443`) gives it
  another number and `external` (`8080:::internal`) exposes or hides it, as on the console's deploy screen. Template
  ports can't be removed. `estimate_pod` takes the same argument.
- **Storage usage warnings.** `create_storage(usage_warning=True, usage_warning_threshold=0.8)` or
  `meshive storage-create --usage-warning 80` emails you when the storage is more than 80% full (90% if you leave
  out the threshold), like the console's usage warning.
- **One operation by ID.** `get_transaction(transaction_id, workspace)` and `meshive transaction WORKSPACE ID`
  show a pod operation's status, steps and failure reason, even after it has dropped out of `transactions`.

### Changed

- **Pod creation follows the console's rules.** vCPU and RAM must be values the console offers, a port the
  template already opens or a port listed twice is refused, required environment variables of the template need a
  value, and a command can be at most 4096 characters. The error says what is allowed. CPU pods now default to
  8 vCPU and 32 GiB of RAM (or the nearest the line offers), as in the console, instead of 2 vCPU and 4 GiB.
- **Storage is at least 10 GiB**, as in the console. A smaller size is refused with an error.
- **Starting without a placement picks what the console would.** `start_pod` without `placement` and
  `meshive pod-start` without `--placement` used to mean `same_node`. Now the server chooses: `same_node`, or
  `wait_same_node` when the pod's machine has no room right now; `any_node` or `wait_any_node` when that machine can
  no longer run the pod's configuration. The result's `placement` and the CLI output say which one was used. A choice
  that moves the pod still needs data-loss consent: without it the start is refused, and the CLI tells you which
  `--placement` to rerun with. Pass `placement="same_node"` (`--placement same_node`) to keep the old behavior.
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
