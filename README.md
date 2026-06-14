# autodl-private-cloud

A [Claude Code](https://claude.com/claude-code) **skill** for driving an AutoDL
**private-cloud** (ESD) GPU cluster from the command line. It automates the full life of
a research experiment: find idle GPUs → launch a container → wait for it to come up →
read SSH details → run the job → tear it down so billing stops.

All operations go through one bundled CLI, [`scripts/autodl.py`](scripts/autodl.py) —
each subcommand wraps one ESD endpoint and prints the API's `data` field as JSON, so
output is easy to parse and chain.

## What's in here

| Path | Purpose |
|---|---|
| `SKILL.md` | The skill instructions Claude follows (setup, safety, the core loop). |
| `scripts/autodl.py` | The CLI wrapping the ESD developer API. |
| `references/api.md` | Full endpoint reference: request/response bodies, fields, enums, image rules. |
| `references/elastic-deploy-concepts.md` | The platform's mental model: scheduling, lifecycle, deployment types. |
| `references/system-images.md` | The image-list endpoint + a dated snapshot of one cluster's system images. |
| `evals/` | Evaluation cases for the skill. |

## Requirements

- Python 3 with the [`requests`](https://pypi.org/project/requests/) package
  (`pip install requests`)
- An AutoDL developer token (Console → **Settings → Developer Token**)

## Setup: the token

The CLI resolves a token in this order — **first hit wins**:

1. `--token` flag on the call
2. `$AUTODL_TOKEN` environment variable
3. the **token file** — `$AUTODL_TOKEN_FILE` or `~/.config/autodl/token` (saved `0600`)

Save it once and never paste it again. Prefer stdin so the secret never lands in shell
history:

```bash
printf %s '<YOUR_TOKEN>' | python3 scripts/autodl.py save-token
python3 scripts/autodl.py token-status   # confirms config without printing the token
```

Optionally point at a self-hosted cluster:

```bash
export AUTODL_BASE_URL="https://private.autodl.com"   # or pass --base-url per call
```

Confirm connectivity:

```bash
python3 scripts/autodl.py gpu-stock --idle-only
```

## The core loop

```bash
# 1. Find capacity & an image
python3 scripts/autodl.py gpu-stock --idle-only
python3 scripts/autodl.py system-image-list --filter torch

# 2. Launch (sensitive — previews until you add --yes)
python3 scripts/autodl.py deploy-create \
  --name "exp-1" --type ReplicaSet --replica-num 1 \
  --image-uuid base-image-90df20b82987 --gpu-name "NVIDIA GeForce RTX 4090" --gpu-num 1 \
  --cuda-v 0 --mem-from-gb 16 --mem-to-gb 64 --price-to 100000 --cmd "sleep infinity" --yes

# 3. Wait for it to come up, read SSH info
python3 scripts/autodl.py wait-running --deployment-uuid <UUID> --timeout 600

# 4. Run your experiment over SSH...

# 5. Tear down so billing stops
python3 scripts/autodl.py deploy-stop   --deployment-uuid <UUID> --yes
python3 scripts/autodl.py deploy-delete --deployment-uuid <UUID> --yes
```

## Commands

| Command | What it does | Sensitive? |
|---|---|---|
| `token-status` | show whether/where a token is configured (never prints it) | no |
| `save-token [--token T]` | save a token to `~/.config/autodl/token` (`0600`); reads stdin if no `--token` | no |
| `gpu-stock [--idle-only]` | idle/total GPU counts by model | no |
| `system-image-list [--filter X]` | platform system/base images + UUIDs | no |
| `image-list` | your private images | no |
| `deploy-list` | your deployments + status | no |
| `container-list --deployment-uuid U` | containers + SSH info | no |
| `container-events --deployment-uuid U` | lifecycle events for debugging | no |
| `wait-running --deployment-uuid U` | poll until a container runs, emit SSH info | no |
| `deploy-create ...` | launch a deployment (costs money) | **needs `--yes`** |
| `deploy-scale --deployment-uuid U --replica-num N` | resize a ReplicaSet | **needs `--yes`** |
| `container-stop --container-uuid C` | stop one container | **needs `--yes`** |
| `deploy-stop --deployment-uuid U` | stop all containers in a deployment | **needs `--yes`** |
| `deploy-delete --deployment-uuid U` | delete a deployment | **needs `--yes`** |
| `blacklist --container-uuid C` | block a slow host for 24h | **needs `--yes`** |
| `raw --method M --path P [--body J]` | call any endpoint directly | you own it |

Run `python3 scripts/autodl.py <command> -h` for full options.

## Safety

State-changing commands (`deploy-create`, `deploy-scale`, `container-stop`,
`deploy-stop`, `deploy-delete`, `blacklist`) **require an explicit `--yes`**. Without it
they only print a preview of the exact request and exit — a built-in human-confirmation
gate. `deploy-create` / `deploy-scale` start containers that **bill by the hour**, so
treat teardown as part of the same task.

## Installation as a Claude Code skill

Clone or symlink this directory into your skills directory:

```bash
ln -s "$PWD/autodl-private-cloud" ~/.claude/skills/autodl-private-cloud
```

Claude will then load it automatically when a task involves AutoDL GPU provisioning.
