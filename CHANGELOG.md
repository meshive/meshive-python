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

## Unreleased

You can run commands in a running pod and upload files from your computer to Asset Hub, without SSH or the console.

### What's new

- **Run a command in a pod.** `run_command(pod, workspace, command)` runs it with `sh -c` in the pod's main container
  and returns its exit code, stdout and stderr (`CommandResult`, 64 KiB of output at most). It waits up to `timeout`
  seconds (1 to 25). For longer work pass `background=True`: it returns a `run_id` at once, and
  `get_command_run(pod, workspace, run_id)` returns a `CommandRun` with its state (running, exited or lost), exit code
  and the end of its output. A run is lost when the pod restarts. It needs a read & write key, like `ssh_access`.
  Calling it again runs the command again, so a failed call is resent only when the server surely never ran it.
- **Upload files to Asset Hub.** `upload_asset(path, workspace=...)` uploads a file, or a folder with everything in it,
  as a new asset and returns an `UploadedAsset`. Files over 200 MiB go up in parts. If it fails, the upload is
  cancelled. Symbolic links in a folder are skipped. The steps are also available on their own, on both clients:
  `start_asset_upload`, `asset_upload_part_urls`, `complete_asset_upload` and `abort_asset_upload`. Upload links are
  sent without your API key.
- **CLI.** `meshive pod-exec <workspace> <pod> -- <command>` prints the command's output and exits with its exit code
  (124 when it was stopped at `--max-seconds`). `--background` starts it detached; `meshive pod-exec-status
  <workspace> <pod> <run-id> --wait` follows it. `meshive asset-upload <workspace> <path>` uploads a file or folder.

## v1.8.0

Hosts can now run their machines from the SDK and CLI: maintenance windows, listing, setup retries, removal,
messages with the workspaces that use a machine, and payouts. You can also see what your API keys did, with the
result of each request. Tasks also report whether their outputs were saved, and how far saving has got, as the console shows it.
Code written for 1.0 to 1.7 keeps working.

### What's new

- **See who uses a machine.** `get_machine` returns a `MachineDetail`, which is a `Machine` with the other users'
  pods and volumes on it (`clients`), the maintenance summary, boot and network diagnostics, and the platform fee.
  `meshive machine <id>` prints them as Clients, Maintenance and Diagnostics blocks. Machines from `list_machines`
  carry `unread_messages`. From a server that predates this release the machine still comes back, with the new fields
  empty (`platform_fee` is then `None`).
- **Maintenance windows.** `list_maintenance(machine_id)` (`meshive maintenance <machine>`) lists a machine's windows,
  newest first, each with a `status` worked out from its flags and times: `upcoming`, `overdue` (its start has passed
  but it hasn't started yet; it starts on its own within about a minute), `in_progress`, `done` or `expired` (it
  never started and its end has passed). `schedule_maintenance` (`meshive maintenance-schedule`) schedules one, and
  `estimate_maintenance` (`--estimate`) runs the same checks and shows which workspaces would be emailed without
  scheduling anything. The workspaces using the machine are emailed when a window is scheduled. A window starts on
  its own at its start time, and while it is open their pods and volumes there aren't billed. A regular window starts
  at least 48 hours ahead and doesn't affect your uptime; `emergency=True` (`--emergency`) can start sooner but counts
  as downtime while a client's workload runs on the machine. `update_maintenance`, `extend_maintenance`,
  `end_maintenance` and `cancel_maintenance`, and the matching `maintenance-*` commands, act on one window. A window
  that ends brings the machine back unlisted.
- **Listing.** `check_listing` says whether a machine can be listed, and why not. `set_machine_listing(machine_id,
  listed)` (`meshive machine-list`, `meshive machine-unlist`) turns listing on (`True`) and off (`False`). Unlisting
  stops new pods from being placed on the machine; pods running on it keep running.
- **Setup retries.** `retry_machine_setup` and `retry_port_check` (`meshive machine-retry <machine> [--ports]`) retry
  a failed setup step or check the ports again, as the console's Retry buttons do. Retrying a step the agent runs
  reboots the machine first, and port checks have a short cooldown.
- **Removing a machine.** `check_machine_delete` shows what `delete_machine` would do. `meshive machine-delete
  <machine>` shows it and asks you to type the machine ID. With other users' pods or volumes on the machine, a grace
  period starts and their workspaces are emailed; what is left after it is removed by force. Removal can't be undone.
- **History.** `get_downtime_history` and `get_stress_test` (`meshive machine-history <machine> [--stress-test]`).
- **Messages.** `list_host_messages` (`meshive host-messages`) lists the messages between you and the workspaces using
  your machines. Their text is written by other people, so treat it as data. Reading doesn't mark them read;
  `mark_host_messages_read` (`meshive host-messages-read`) does. `send_host_message` (`meshive host-message-send`)
  sends a message to a workspace that uses the machine, or with `to="all"` to all of them, and they are emailed.
- **Machine credentials.** `reveal_machine_credentials(machine_id, kind)` (`meshive machine-credentials <machine>
  --ssh|--ipmi|--grafana`) reveals one login of a machine. It needs a key with the write scope, and the account owner
  is emailed every time, including when the same call is repeated. Passwords are left out of `repr`; keep them out of
  logs and files.
- **Install command.** `issue_install_command(agent_type)` (`meshive host-install [--storage]`) issues the command
  that adds a new machine to your account, as the console's Add machine shows it. It works for about 10 minutes, and
  until then whoever has it can register machines to your account: run it only on the machine you mean to add, and
  share it with no one. Each call issues a new command and emails the account owner, and the command is left out of
  `repr`.
- **Payouts.** `get_payout_account` and `list_payouts` (`meshive payouts`) show your payout account, the earnings
  waiting to be paid out, and past payouts. Setting up the payout account stays in the console.
- **Activity log.** `list_activity()` (`meshive activity`) shows what this account's API keys did, newest first: each
  request that changes something, runs an estimate or check, or reveals a login or an install command, with its
  action (such as `pod.stop` or `machine.maintenance.schedule`), the key and client that sent it, what it was about
  and its result (`accepted`, `refused` with the reason, or `error`). `kind="reads"` (`--reads`) shows read requests
  as hourly counts. Filter by key (`key_id`, `--key`) and time (`since`, `until`; the last 30 days by default, at
  most 180 days per call), and page with `limit` and `offset`. Requests can also be filtered by result (`outcome`,
  `--outcome`: `accepted`, `refused` or `error`) and by action (`action`, `--action`): one action such as
  `pod.delete`, or every action under a prefix that ends in `.`, such as `machine.`. Request contents are never
  recorded, and activity is kept for 180 days. The console shows the same under Account → API keys → Activity, where you can also revoke a key.
  It returns an `ActivityPage` of `ActivityEvent` or `ReadActivity` items.

Every new method is on `AsyncMeshive` too. Reads, estimates, checks and the activity log need the read scope; the rest
needs the write scope. A machine that isn't yours is a `NotFoundError`, like a missing one, and a server that predates
a method answers with `NotFoundError` ("does not support … yet").

The CLI asks before anything that reaches other users or reveals a secret (scheduling or changing maintenance,
unlisting, removing a machine, sending a message, revealing credentials, getting an install command) unless you pass
`--yes`. `machine-list`, `machine-retry` and `host-messages-read` change only your own machine and don't ask, as in the
console.
- **`Task.outputs`** is a `TaskOutputsStatus`: what happened to the files the task wrote to `/outputs`, worked out by
  the server. Its `phase` is `running` until the task ends, then `preparing`, `uploading`, `retrying` or
  `registering` (being added to Assets) while the files are saved (`saving` is `True`), and in the end `saved`,
  `partially_saved`, `not_saved`, `discarded` or `empty`. While saving, progress is `files_so_far` of
  `files_to_upload` files and `bytes_so_far` of `bytes_total` bytes. `files_total` also counts the files over the
  task's file limit (`files_over_limit` of them, over `files_limit`), which aren't uploaded, so use `files_to_upload`
  for progress. Some tasks don't report a total while they are saved: `files_total`, `bytes_total` and
  `files_to_upload` are then `None`. Once saved, `asset_id` and `asset_name` name the asset. `partially_saved` means
  `files_not_saved` files are missing; `last_error` says why an upload failed. `list_tasks`, `get_task` and
  `submit_task` return it. From a server that predates it, `outputs` is `None`, and the `output_*` fields are still
  there.
- **`meshive task <id>`** shows it on the `outputs:` line: the progress while saving
  (`uploading · 48/126 files · 41.2 of 108.6 MiB`, or
  `uploading · 48 files so far · 41.2 MiB · total not known yet`), the asset once saved
  (`saved to Assets as my-run (asset_...) · 126 files · 108.6 MiB`), and for a partial save how many files are
  missing (`Only 2,000 of 2,431 output files were saved — 431 were over the 2,000-file limit.`). `meshive tasks` adds
  an OUTPUTS column when the server sends the status.

### Changes

- **A write cut off mid-request can be looked up like one that timed out.** When the connection broke after a write
  was sent (`httpx.RemoteProtocolError` "Server disconnected without sending a response.", `httpx.ReadError`,
  `httpx.WriteError`, ...) or its answer couldn't be decoded (`httpx.DecodingError`), the error came without
  `idempotency_key`, `operation_method` and `operation_path`, so a generated key was lost. Every `httpx.RequestError`
  a write ends with now carries them, as timeouts and API errors already did: pass them to `get_operation` to see
  whether it was applied. Which errors are resent hasn't changed; these still are not, since the server may have
  applied the write.
- **`framework="sglang"` is no longer accepted.** Meshive serves text models with vLLM, and image and video models
  with ComfyUI. `register_model(..., framework="sglang")` now gets a 400 from the server (`MeshiveAPIError`) that says
  so; older SDK versions get the same answer. Leave `framework` out or pass `"vllm"`. Models you registered with
  SGLang before stay registered.
- **`meshive model-register --framework`** accepts only `vllm`, the default.
- **The server checks `framework`.** The SDK no longer keeps its own list of engines: it only checks that
  `framework` is a non-empty string (`ValueError` otherwise), and the server answers an engine it doesn't serve with a
  400.
- **`meshive task <id>` says `log: deleted …` where it said `outputs: deleted …`.** After its retention period only a
  task's log is deleted; its outputs stay in Assets. `Task.outputs_purged_at` is when the log was deleted.
- **A write the server already answered says so.** When a write is sent with an `idempotency_key` the server has
  already answered (a retry, or a key used again), the server returns its first answer and does nothing new. The
  result used to look like a fresh success; it now has `replayed=True`: the write was done once, by the earlier
  request. Every result of a write that takes `idempotency_key` has the field, except `connect_storage`, which
  returns the storage's connection state (`ResourceAction`, `PodCreated`, `PodUpdated`, `StorageCreated`,
  `StorageUpdated`, `WorkspaceCreated`, `WorkspaceUpdated`, `TaskSubmitted`, `AssetImported`, `WatchedFolders`,
  `MaintenanceAction`, `ListingResult`, `RetryResult`, `MachineDeleted`, `MessageSent` and `MessagesRead`), and it is
  `False` when this request did the write. To do it again as a new write, use a new key.

## v1.7.0

Creating a client is much cheaper, and clients can share connections. Code written for 1.0 to 1.6 keeps working.

### What's new

- **Share connections between clients.** Code that creates many clients, such as a server with one per request and
  API key, can create one transport and pass it to each:

  ```python
  transport = httpx.AsyncHTTPTransport()                         # once, at startup
  async with AsyncMeshive(key, transport=transport) as client:   # per request
      ...
  await transport.aclose()                                       # at shutdown
  ```

  `Meshive` takes an `httpx.HTTPTransport` the same way. Only the connections are shared. Every client still sends
  its own key and headers with each request, keeps its own cookies and applies its own `timeout`, so nothing one
  client receives reaches another. `close()` and leaving a `with` block don't close the transport; you do. All the
  clients wait for the same pool, so size it for them (`limits=httpx.Limits(...)`), and use an async transport from
  one event loop only. Proxy and certificate settings come from the transport as well (`proxy=`, `verify=`):
  `HTTPS_PROXY` and the other proxy variables aren't read for it. With many requests in flight at once, separate
  clients can use less CPU than one shared pool, so measure before you switch.

### Changes

- **Creating a client is cheaper.** Each client used to build its own SSL context, which loads the whole CA bundle
  and took a few milliseconds of CPU, most of the cost of a client. Clients now reuse one per thread, built the same
  way, so the same certificates are trusted. It is built again when `SSL_CERT_FILE` or `SSL_CERT_DIR` changes or
  `truststore.inject_into_ssl()` runs. Other changes to the CA files reach only threads that haven't created a client
  yet.
- **A long-running create is resent when no connection was free.** `create_pod` with a model set or a Hugging Face
  token treated `httpx.PoolTimeout` like a timeout whose outcome is unknown. Nothing had been sent, so it is now
  resent like a refused connection.

## v1.6.0

You can now create, rename and delete workspaces from the SDK and CLI, and wait until a new one is ready to use. Code
written for 1.0 to 1.5 keeps working.

### What's new

- **Create a workspace.** `create_workspace("research")` and `meshive workspace-create research` create an empty
  workspace and return its ID (`created.workspace`) at once. A workspace costs nothing by itself; the pods, storage
  and tasks in it are billed as usual, so the command doesn't ask for confirmation. Leave the name out and the server
  generates one (like `my-workspace-ab12`). A name is up to 63 characters and must be unique among your workspaces: a
  taken one raises `ConflictError` ("Name Taken"). API keys work across your whole account, so the key that created
  the workspace can use it once it is running. `estimate_workspace` and `meshive workspace-create --estimate` check
  the name and create nothing; a read-only key can run them. `AsyncMeshive` has the same methods.
- **Wait until it is running.** A new workspace is `creating` while it is set up, and pods and tasks are refused
  until it is `running`. Storage and serving don't check, so they can fail if you use them earlier.
  `wait_for_workspace(created.workspace)` and `meshive workspace-create --wait` poll until the workspace is
  `running` (up to 180 seconds by default; `--wait-timeout SECONDS` on the command line). Status `error` raises
  `MeshiveError` at once. A workspace that isn't in your workspace list yet, or is listed as `terminated`, is waited
  for like `creating`, because the list can lag a moment behind a create. If the time runs out, `WaitTimeoutError`
  names the workspace and the last status seen; a workspace that is still `terminated` then failed to set up.
- **Rename a workspace.** `update_workspace(workspace, name="research-2")` and
  `meshive workspace-update <workspace> --name research-2` change its name. The ID stays the same, so whatever uses it
  keeps working, and every member sees the new name. The admin and billing roles can rename; a viewer gets
  `PermissionDeniedError`. The rules for a new name are the ones for a new workspace, a taken name raises
  `ConflictError` ("Name Taken"), and a blank one raises `ValueError`. `estimate_workspace_update` and
  `meshive workspace-update --estimate` show the current and new name and change nothing; a read-only key can run
  them, with the same roles. The command asks before renaming unless you pass `--yes`.
- **Delete an empty workspace.** `delete_workspace(workspace)` and `meshive workspace-delete <workspace>` delete a
  workspace that holds nothing, which can't be undone. The same admin and billing roles can do this. Anything still in
  it refuses the delete with `ConflictError` ("Workspace Not Empty"), and `err.raw["detail"]["blockers"]` counts what
  is left: pods, storage, servings, tasks (finished ones too), task outputs still being saved, assets, custom templates
  and models, credentials, webhooks, inference keys, other members, open invitations, payouts still pending and
  operations in progress. `check_workspace_delete(workspace)` and `meshive workspace-delete <workspace> --check` show
  the same list and delete nothing; a read-only key can run them, with the same roles, and `--check` exits 1 if the
  workspace can't be deleted. A person can still delete a workspace that holds things in the console, which removes
  what's in it for good. The deletion runs in the background: the workspace leaves your list within a moment, and its
  name stays taken for a while.
- **Older servers.** A server that predates this release answers with `NotFoundError`, saying it doesn't support
  workspace creation, renaming or deletion yet.

### Changes

- **`meshive ssh` and `meshive storage-connect` no longer print a browser terminal link.** The browser terminal now
  gets its login from the console, so the link can't connect on its own. Open it from the console with **Connect →
  Open Web Terminal**. Both commands still print the ssh command and the one-time password. `SshAccess.web_url` is
  still there, but it no longer connects by itself.

## v1.5.0

A task can now keep only some of the files it writes to `/outputs`, as the console's submit form does: the rest are
not uploaded and don't become part of the task's output asset. Code written for 1.0 to 1.4 keeps working.

### What's new

- **Output filter for tasks.** `submit_task(..., output_include=["*.safetensors", "samples/*.png"])` and
  `meshive task-submit ... --output-include '*.safetensors' --output-include 'samples/*.png'` upload only the files
  in `/outputs` that match. Patterns are relative to `/outputs` and case-sensitive: `*.png` matches at any depth and
  `samples/*.png` only from `/outputs` (a slash anchors a pattern; a trailing one doesn't). A trailing slash takes
  whole folders: `checkpoints/` matches every folder named `checkpoints`, at any depth, with everything in it.
  Leaving it out uploads every file, as before. `estimate_task` takes the same argument, and the server explains a
  pattern it can't use. Files left out still use the `/outputs` disk while the task runs and are discarded when it
  ends. If no file matches, the task still succeeds and creates no output asset.
- **See a task's filter.** `Task.output_include` lists its patterns (`None` = every file), and `meshive task` prints
  them as `output filter:`. A server that predates the filter accepts the task and uploads every file;
  `submit_task` then issues a `RuntimeWarning` that names the task (it is submitted either way), and
  `meshive task-submit` prints a warning.

## v1.4.0

Pods made from templates without an output folder (for example NIM or vLLM) can now save files from your own folders,
and a template's output folder can be moved when you create a pod. Code written for 1.0 to 1.3 keeps working.

### What's new

- **Folders on any pod with storage.** `create_pod(..., watched_folders=[...])` and `meshive pod-create --watch PATH`
  work on templates that don't declare an output folder, as long as the pod has storage to hold the folders (a
  volume, or local storage). `set_watched_folders` / `meshive pod-watch --add` add them to a running pod without a
  restart.
- **Move the template's output folder.** `watched_template_folders={"/workspace/output": {"path": "/workspace/results"}}`
  or `meshive pod-create --watch-move /workspace/output=/workspace/results` puts the template's output folder
  somewhere else. `$MESHIVE_OUTPUT_DIR` and the folder's storage follow. Only output folders move; a running pod's
  template folders can't be moved (turn one off and add a folder of yours instead).
- **Turn on folder saving for an older pod.** Pods made before this release on such templates have no uploader:
  `get_watched_folders` reports `editable_reason == "no_sidecar"`, and `WatchedFolders.can_enable_saving` is true
  when the pod has storage for folders. `update_pod(..., enable_folder_saving=True)` or
  `meshive pod-update --enable-folder-saving` adds it (a running pod restarts once). A pod that already has the
  uploader, or has no storage for folders, is refused before anything restarts. `meshive pod-watch --add` on such
  a pod says which of the two to do first.

## v1.3.0

Quick Deploy model sets that are gated on Hugging Face can now be deployed from the SDK and CLI, with your own
Hugging Face token, as in the console's Quick Deploy panel. Code written for 1.0 to 1.2 keeps working.

### What's new

- **Gated model sets.** Some model sets (for example FLUX 3 Image Klein) come from Hugging Face repos that ask you to
  accept a license first. Their files download with your own workspace token: accept the license on the model page
  with your Hugging Face account, then pass that account's token to `create_pod(..., hf_token_id=...)` or
  `meshive pod-create ... --hf-token ID` on the set's Quick Deploy template. `list_hf_tokens` /
  `meshive hf-tokens WORKSPACE` list the token IDs; tokens are added in the console.
- **`list_model_sets` shows which sets are gated.** `ModelSet.hf_token_required` is true for them,
  `ModelSet.gated_sources` lists each repo with the page where you accept its license (`mode` is `"auto"` when
  accepting is enough, `"manual"` when the repo owner approves each account), and `ModelSetItem.hf_gated` marks the
  gated files. `meshive model-sets` prints where to accept the license.

### Changes

- A create with `hf_token_id` waits for the server to link the set's files, like `model_set`, and isn't resent after
  a timeout.
- Refusals name what to fix: `Hugging Face token required` (no token), `License not accepted` or
  `Access not approved` (the token's account hasn't accepted the license or isn't approved yet),
  `Hugging Face token rejected` (the token is invalid or expired). A gated set passed as `model_set` on another
  template is refused with `Model Set Needs a Token`. The CLI prints the next step for each.

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
- In `input_assets`, a `paths` entry that matches no file of the asset is now refused instead of skipped while the
  other entries are attached, and a Korean or other accented name typed in a different Unicode form (NFC or NFD)
  than the stored file name now attaches that file instead of leaving it out.
- Reading a pod now returns `***` for secret variables, as reading a task already did. Keys with write scope still
  get the logins shown on Connect, such as `ACCESS_PASSWORD`, so `meshive pod --show-secrets` needs one; with a
  read-only key the CLI says so.
- Pods and storage on a machine whose host has an open maintenance window aren't billed until the window ends. For
  such a pod, `is_maintenance` is `True`, `billing_active` is `False` and `price_per_hour` is `0`, and the workspace
  price leaves it out. `meshive pod` and `meshive storage` now say "host maintenance: not billed while the window is
  open" instead of "under maintenance".

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
