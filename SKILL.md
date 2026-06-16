---
name: autodl-private-cloud
description: >-
  Operate an AutoDL private-cloud (ESD) cluster from the command line to automate
  research-experiment workflows — check GPU availability, launch/scale/stop/delete
  GPU container deployments, read SSH connection info, and run training/inference
  jobs end to end. Use this whenever the user mentions AutoDL, AutoDL 私有云, the ESD
  developer API, "gpu_stock", "deployment", launching or tearing down GPU containers
  on AutoDL, or wants to provision GPU compute to run an experiment, training job,
  hyperparameter sweep, or benchmark on an AutoDL cluster — even if they don't name
  the API explicitly. If the task involves getting a model training run onto AutoDL
  GPUs and back off again, this skill is the right tool.
---

# AutoDL Private Cloud Operations

This skill drives an AutoDL **private-cloud** cluster through its ESD developer API so
you can automate the full life of a research experiment: find idle GPUs, launch a
container from a private image, wait for it to come up, grab its SSH details, run the
experiment, then tear it down so billing stops.

All API calls go through one bundled CLI — **`scripts/autodl.py`** — so you never have
to hand-build HTTP requests. Each subcommand wraps one endpoint and prints the API's
`data` field as JSON, which you can parse to drive the next step.

**Mental model:** "elastic deployment" means you declare *conditions* (GPU model, count,
CPU/memory/price ranges) and the platform schedules a container onto whichever eligible
host is free — you don't pick a machine. CPU/memory are filters, not exact allocations;
a container lives exactly as long as its `cmd` runs. If you're reasoning about *why* the
platform behaves a certain way (scheduling, lifecycle, container reuse, scaling), read
`references/elastic-deploy-concepts.md` first.

## Setup: token (do this first, every session)

The CLI needs a developer token. It resolves one in this order — **first hit wins**:

1. `--token` flag on the call
2. `$AUTODL_TOKEN` environment variable
3. the **encrypted store** — `$AUTODL_CREDENTIALS_FILE` or `~/.config/autodl/credentials.enc`
   (token sealed with Fernet under a scrypt-derived password key; perms `0600`)
4. the **legacy plaintext token file** — `$AUTODL_TOKEN_FILE` or `~/.config/autodl/token`

**Always begin by checking what's configured** (this never prints the token itself):

```bash
python3 scripts/autodl.py token-status
```

### Secure mode is the default — do NOT ask the user to paste their token into chat

The recommended setup keeps the token **encrypted at rest, password-protected, and out
of this conversation entirely.** The token and password are entered through a native GUI
dialog (tkinter, with macOS `osascript` / Windows PowerShell fallbacks) — so the secret
never passes through argv, stdin, the environment, shell history, or any AI/agent context.

If `token-status` returns `"configured": false`, the user has not set up a token yet.
**Do not ask the user to paste the token into the chat.** Instead, walk them through
getting one from the Console and entering it into the GUI dialog. Present these exact
steps (the wording in the Console is Chinese — use the real labels):

> **第一次使用，需要先拿到你的 AutoDL 开发者 Token：**
> 1. 登录 AutoDL 私有云控制台，点左侧边栏最下方的 **「账号设置」**。
> 2. 在 **「开发者Token」** 标签页，点蓝色的 **「+ 新增Token」** 按钮（已有 Token 也会列在下方）。
> 3. 点 Token 右侧的 **复制图标** 把它复制到剪贴板（Token 很长，以 `eyJ...` 开头）。
> 4. 复制好后告诉我，我会弹出一个加密设置窗口 —— 你**把 Token 粘贴进去并设置一个密码**即可。
>    Token 会被加密保存到 `~/.config/autodl/credentials.enc`（仅本人可读 `0600`），
>    **全程不经过聊天框，我看不到你的 Token 和密码。**

Then launch the GUI setup, which pops a dialog where they paste the token and choose an
encryption password:

```bash
python3 scripts/autodl.py set-token
```

(English equivalent of the Console path, in case the UI is in English: **Account Settings
→ Developer Token → + Add Token → copy**.)

**How auth works on every call after that:** each command that needs the token pops a
GUI password prompt; on the correct password the token is decrypted **in memory**, used
for that one request, and never printed. A successful unlock is cached (machine-bound,
encrypted, `0600`) for an **unlock TTL** (default 300 s) so a multi-step run prompts once
instead of per call. Control it with:

```bash
python3 scripts/autodl.py lock                 # forget the unlock now (re-prompt next call)
python3 scripts/autodl.py --unlock-ttl 0 ...   # never cache: prompt on this call
python3 scripts/autodl.py --no-cache ...        # ignore/!refresh cache for this call
python3 scripts/autodl.py change-password       # rotate the encryption password (GUI)
```

After `set-token`, confirm with `token-status` (expect `"configured": true`, source =
encrypted store). **Note:** the GUI dialogs need a desktop session — these commands are
for the *user* to run at their machine; you can suggest and launch them, but you cannot
type into the dialog.

> Treat the token as a secret: never echo it, never paste it into a summary, never commit
> it. If `$AUTODL_TOKEN` is set in the environment it takes precedence over the encrypted
> store and is visible to whoever set it — for true secure mode, leave it unset.

### Legacy / headless alternative (no GUI)

For CI or a headless box with no display, you can still use the env var or an **unencrypted**
token file (`save-token`, stored `0600`). This is less secure — the token sits in plaintext —
so prefer the encrypted store on a normal desktop:

```bash
printf %s '<TOKEN>' | python3 scripts/autodl.py save-token   # plaintext file, stdin (no argv leak)
```

Optionally override the API target (only for a self-hosted cluster):

```bash
export AUTODL_BASE_URL="https://private.autodl.com"   # or pass --base-url per call
```

Then confirm connectivity before anything else (this will trigger the password prompt the
first time if you're in secure mode):

```bash
python3 scripts/autodl.py gpu-stock --idle-only
```

## Safety: confirm before every sensitive operation

Six commands change state — they create billable containers or destroy/disrupt resources:
`deploy-create`, `deploy-scale`, `container-stop`, `deploy-stop`, `deploy-delete`,
`blacklist`. **Every one of them requires an explicit `--yes` to actually run.** Without
`--yes` the command does **not** touch the API — it prints a preview (the action, the real
target looked up by name/status, and the exact request body) and exits with code 2.

This is a hard guardrail, and you must use it as a human-confirmation gate:

1. **Run the command first WITHOUT `--yes`** to get the preview.
2. **Show the user the preview** — what will change, which named resource(s), and (for
   `deploy-create`) the GPU/image/cost-relevant settings — and **ask for explicit
   confirmation.**
3. **Only after the user clearly says yes**, re-run the *same* command **with `--yes`.**

Never add `--yes` on the user's behalf without a clear go-ahead **for that specific
operation.** Approval to create one deployment is not approval to delete another; a
"yes" earlier in the session is not a standing yes. For a **batch** of destructive actions
(e.g. "delete everything"), list every target deployment from `deploy-list` and confirm the
whole list before running any `--yes` delete — then do them one by one.

Cost note: `deploy-create` / `deploy-scale` start containers that **bill by the hour**, and
a forgotten ReplicaSet keeps re-spawning containers. When you launch something as part of a
task, treat teardown as part of the same task unless the user wants it left running.

## Command quick reference

Run `python3 scripts/autodl.py <command> -h` for full options. Read-only commands are safe
to run freely. Commands marked **needs `--yes`** preview-only until confirmed (see Safety).

| Command | What it does | Sensitive? |
|---|---|---|
| `token-status` | show whether/where a token is configured + unlock state (never prints it) | no |
| `set-token` | **secure GUI setup**: encrypt token under a password (`credentials.enc`, `0600`) | no |
| `change-password` | rotate the encryption password via GUI (re-encrypts the token) | no |
| `lock` | clear the unlock cache so the next op re-prompts for the password | no |
| `save-token [--token T]` | LEGACY: save an **unencrypted** token to `~/.config/autodl/token` (`0600`); reads stdin if no `--token` | no |
| `gpu-stock [--idle-only]` | idle/total GPU counts by model | no |
| `system-image-list [--filter X]` | platform system/base images + UUIDs (`base-image-xxxx`) | no |
| `image-list` | your **private** images (`image-xxxx`) | no |
| `deploy-list` | your deployments + status | no |
| `container-list --deployment-uuid U` | containers + SSH info (`.info`) | no |
| `container-events --deployment-uuid U` | lifecycle events for debugging | no |
| `wait-running --deployment-uuid U` | **poll until a container runs, emit SSH info** | no |
| `deploy-create ...` | **launch** a deployment (costs money) | **needs `--yes`** |
| `deploy-scale --deployment-uuid U --replica-num N` | resize a ReplicaSet | **needs `--yes`** |
| `container-stop --container-uuid C` | stop one container | **needs `--yes`** |
| `deploy-stop --deployment-uuid U` | stop all containers in a deployment | **needs `--yes`** |
| `deploy-delete --deployment-uuid U` | delete a deployment | **needs `--yes`** |
| `blacklist --container-uuid C` | block a slow host for 24h | **needs `--yes`** |
| `raw --method M --path P [--body J]` | call any endpoint directly (escape hatch) | not gated — you own it |

## The core automation loop

This is the pattern almost every experiment follows. Run the steps, parse each JSON
result, feed UUIDs into the next step.

1. **Find capacity & image.** Pick a GPU model that has idle stock and the image to run.
   ```bash
   python3 scripts/autodl.py gpu-stock --idle-only
   python3 scripts/autodl.py system-image-list --filter torch   # platform images (base-image-xxxx)
   python3 scripts/autodl.py image-list                         # your private images (image-xxxx)
   ```
   `deploy-create` takes an `image_uuid`, which is either a **system/base** image
   (`base-image-xxxx`, e.g. a PyTorch image — from `system-image-list`) or a **private**
   image (`image-xxxx` — from `image-list`, often empty on a fresh cluster). Pass system
   images with `--cuda-v 0` (no CUDA constraint, matching the console). Passing the image
   *name* string as the UUID fails with `镜像不存在` — you must use the real `base-image-xxxx`.
   See `references/api.md` → "Images (private vs system)".

2. **Launch.** Create the deployment. Capture `deployment_uuid` from the output.
   ```bash
   python3 scripts/autodl.py deploy-create \
     --name "exp-resnet-sweep" --type ReplicaSet --replica-num 1 \
     --image-uuid base-image-90df20b82987 --gpu-name "NVIDIA GeForce RTX 4090" --gpu-num 1 \
     --cuda-v 0 --mem-from-gb 16 --mem-to-gb 64 \
     --price-to 100000 --cmd "sleep infinity"
   ```
   (`base-image-...` is a system PyTorch image from `system-image-list`; use `--cuda-v 0`
   with system images. For a private image use its `image-...` uuid and the matching `cuda_v`.)
   Use `--cmd "sleep infinity"` when you want to SSH in and drive the run yourself;
   use a real command (e.g. `--cmd "python train.py"`) for a fire-and-forget Job.

   This is a sensitive command: as written it only **previews**. Show the user the preview,
   get confirmation, then re-run the exact same line with `--yes` appended to launch.

3. **Wait for it to come up, then read SSH info.** Containers aren't reachable the
   instant they're created — they go creating → starting → running. `wait-running`
   blocks until one is `running` and prints its connection block:
   ```bash
   python3 scripts/autodl.py wait-running --deployment-uuid 833f1cd5a764fa3 --timeout 600
   ```
   The emitted `info` object holds `ssh_command`, `root_password`, and `service_url`.
   Use those to SSH in (`scripts/autodl.py` does not SSH for you — run the experiment
   over the connection it hands back) or to reach an exposed service.

4. **Run / monitor the experiment.** Drive training over SSH, or poll progress with
   `container-list` (status) and `container-events` (lifecycle). For a sweep, raise
   throughput with `deploy-scale`.

5. **Tear down — every time.** Stop billing the moment the experiment is done. These are
   sensitive: run once without `--yes` to preview the exact deployment being removed, show
   the user, then re-run with `--yes`:
   ```bash
   python3 scripts/autodl.py deploy-stop   --deployment-uuid 833f1cd5a764fa3          # preview
   python3 scripts/autodl.py deploy-stop   --deployment-uuid 833f1cd5a764fa3 --yes    # after confirm
   python3 scripts/autodl.py deploy-delete --deployment-uuid 833f1cd5a764fa3 --yes
   ```

### Choosing a deployment type

- **ReplicaSet** — keeps `replica_num` containers alive, rescheduling failures. Best for
  long-lived workers, interactive dev boxes, and sweeps you scale up/down.
- **Job** — runs containers to completion; `parallelism_num` sets how many run at once.
  Best for batch experiments that exit when done.
- **Container** — a single one-shot container. Best for a single quick run.

A subtle but important point on stopping: with a **ReplicaSet**, stopping one container
makes the controller spin up a replacement to maintain `replica_num`. To actually shrink
it, pass `--decrease-replica` to `container-stop` (or lower `replica_num` with
`deploy-scale`). To shut the whole thing down, use `deploy-stop`/`deploy-delete`.

## Units and enums that bite

These don't match intuition, so get them right (full details in `references/api.md`):

- **Memory in `deploy-create` is whole GB** (`--mem-from-gb 16`). But list/show responses
  report `memory_size` in **bytes** (16 GB → 17179869184). Don't compare the two directly.
- **Price is in units of 0.001 CNY per hour.** `--price-to 9000` means a 9 CNY/hr ceiling.
  Set a sane `--price-to` so you don't land on an unexpectedly pricey host.
- **`cuda_v` is an integer**, e.g. `118` = CUDA 11.8, `122` = 12.2. Valid: 111, 113, 116,
  117, 118, 120, 122.
- **`gpu_name` strings must match exactly** what `gpu-stock` reports — these are full
  vendor strings like `"NVIDIA GeForce RTX 4090"` and `"NVIDIA A40"`, **not** short forms
  like `"RTX 4090"`. Always pull the exact string from `gpu-stock` first.

## When a command isn't enough

`scripts/autodl.py raw --method POST --path /api/v1/dev/... --body '{...}'` calls any
endpoint directly with your own JSON body. Reach for it for endpoints this CLI doesn't
wrap or for fields added to the API later — `references/api.md` documents every endpoint's
exact request/response shape.

## Reference

- `references/api.md` — complete endpoint reference: every request/response body, all field
  names, status values, enums, and the private-vs-system image rules. Read it when you need
  a field this skill's commands don't expose, when interpreting a response, or building `raw`.
- `references/elastic-deploy-concepts.md` — the platform's mental model: scheduling units,
  the three deployment types, container lifecycle (`cmd`), container reuse, and best
  practices. Read it when reasoning about *why* the platform behaves a certain way.
- `references/system-images.md` — the `POST /api/v2/image/list` endpoint and a dated
  snapshot of one cluster's 17 system/base image UUIDs (torch/tensorflow/miniconda/Ascend).
  Use `system-image-list` for live values; this is a quick-reference catalog.
