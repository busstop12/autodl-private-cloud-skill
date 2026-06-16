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

- Python 3 with [`requests`](https://pypi.org/project/requests/) (`pip install requests`)
- [`cryptography`](https://pypi.org/project/cryptography/) (`pip install cryptography`) —
  for the encrypted token store (secure mode)
- An AutoDL developer token (Console → **账号设置 → 开发者Token → 新增Token**)

## Setup: the token

The CLI resolves a token in this order — **first hit wins**:

1. `--token` flag on the call
2. `$AUTODL_TOKEN` environment variable
3. the **encrypted store** — `$AUTODL_CREDENTIALS_FILE` or `~/.config/autodl/credentials.enc`
4. the legacy plaintext token file — `$AUTODL_TOKEN_FILE` or `~/.config/autodl/token`

### Secure mode (recommended)

Keep the token **encrypted at rest, password-protected, and never pasted into a terminal
or AI/agent context.** `set-token` pops a native GUI dialog (tkinter, with macOS
`osascript` / Windows PowerShell fallbacks) where you paste the token and choose a
password; it's sealed with Fernet under a scrypt-derived key and written `0600`. The
token and password never pass through argv, stdin, env, shell history, or stdout.

```bash
python3 scripts/autodl.py set-token      # GUI: paste token + choose a password
python3 scripts/autodl.py token-status   # confirms config + unlock state (never prints the token)
```

Get the token from the Console: **账号设置 → 开发者Token → + 新增Token**, then copy it
(it's long, starts with `eyJ...`).

Every later call that needs the token pops a GUI password prompt; on the right password
the token is decrypted **in memory** for that one request and never printed. A successful
unlock is cached (machine-bound, encrypted, `0600`) for a TTL (default 300 s) so a
multi-step run prompts once instead of per call:

```bash
python3 scripts/autodl.py lock                 # forget the unlock now (re-prompt next call)
python3 scripts/autodl.py --unlock-ttl 0 ...    # never cache: prompt on this call
python3 scripts/autodl.py change-password       # rotate the encryption password (GUI)
```

The GUI dialogs need a desktop session. If `$AUTODL_TOKEN` is set it takes precedence over
the encrypted store and is visible to whoever set it — leave it unset for true secure mode.

### Legacy / headless (no GUI)

For CI or a headless box with no display, use the env var or an **unencrypted** token file
(less secure — the token sits in plaintext at rest):

```bash
printf %s '<YOUR_TOKEN>' | python3 scripts/autodl.py save-token   # plaintext file, 0600, stdin (no argv leak)
```

Optionally point at a self-hosted cluster:

```bash
export AUTODL_BASE_URL="https://private.autodl.com"   # or pass --base-url per call
```

Confirm connectivity (triggers the password prompt the first time in secure mode):

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
| `token-status` | show whether/where a token is configured + unlock state (never prints it) | no |
| `set-token` | **secure**: encrypt the token under a password via GUI (`credentials.enc`, `0600`) | no |
| `change-password` | rotate the encryption password via GUI | no |
| `lock` | clear the unlock cache so the next op re-prompts | no |
| `save-token [--token T]` | legacy: save an **unencrypted** token to `~/.config/autodl/token` (`0600`); stdin if no `--token` | no |
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

## Installation

### Option 1 — the `skills` CLI (recommended)

[vercel-labs/skills](https://github.com/vercel-labs/skills) installs the skill into the
correct directory for your agent automatically.

```bash
# Interactive: prompts for scope (global/project) and target agent(s)
npx skills add https://github.com/busstop12/autodl-private-cloud-skill
```

Pick the agent and scope explicitly — `-g` global, `-a` agent, `-y` skip prompts:

```bash
# Claude Code, global
npx skills add https://github.com/busstop12/autodl-private-cloud-skill -g -a claude-code -y

# Codex, global
npx skills add https://github.com/busstop12/autodl-private-cloud-skill -g -a codex -y

# Both at once
npx skills add https://github.com/busstop12/autodl-private-cloud-skill -g -a claude-code -a codex -y
```

Drop `-g` to install into the current project instead. Update or remove later:

```bash
npx skills update autodl-private-cloud
npx skills remove autodl-private-cloud -a claude-code -a codex
```

### Option 2 — manual clone / download

A skill is just a directory containing `SKILL.md`. Clone this repo into your agent's skills
directory, naming the folder `autodl-private-cloud`.

**Claude Code**

```bash
# global (available in all projects)
git clone https://github.com/busstop12/autodl-private-cloud-skill \
  ~/.claude/skills/autodl-private-cloud

# current project only
git clone https://github.com/busstop12/autodl-private-cloud-skill \
  .claude/skills/autodl-private-cloud
```

**Codex**

```bash
# global
git clone https://github.com/busstop12/autodl-private-cloud-skill \
  ~/.codex/skills/autodl-private-cloud

# current project only
git clone https://github.com/busstop12/autodl-private-cloud-skill \
  .agents/skills/autodl-private-cloud
```

Codex skill loading: see the [Codex Skills docs](https://developers.openai.com/codex/skills).

Prefer not to use git? Download the repo ZIP (**Code → Download ZIP**), unzip it, and place
the folder at the path above — make sure it is named `autodl-private-cloud` with `SKILL.md`
at its root.

| Agent | Global path | Project path |
|---|---|---|
| Claude Code | `~/.claude/skills/autodl-private-cloud/` | `.claude/skills/autodl-private-cloud/` |
| Codex | `~/.codex/skills/autodl-private-cloud/` | `.agents/skills/autodl-private-cloud/` |

After installing, the agent loads the skill automatically when a task involves AutoDL GPU
provisioning. Then configure your token — see **Setup: the token** above.
