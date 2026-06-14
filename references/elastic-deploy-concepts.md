# Elastic Deployment (弹性部署) — concepts

Background on the platform's mental model. Read this when you need to reason about
*why* the API behaves a certain way — how scheduling works, why CPU/memory are ranges
not exact values, when containers stop, and how container reuse changes behavior.
Source: `https://private.autodl.com/docs/elastic_deploy/`.

## What it is

**Elastic deployment = use the API (or console) to batch-schedule and start a set of
containers, and manage their whole lifecycle.** You don't pick a specific host — you
declare *conditions* (GPU model, count, CPU/memory/price ranges), and the platform
schedules a container onto whichever eligible host is available. That indirection is the
"elastic" part: you describe the need, the platform places it. Billing is hourly on the
container that actually runs.

## Scheduling units (调度单元) — the key idea

A physical host's CPU and memory are sliced **in proportion to its GPU count** into
indivisible scheduling units. Example from the docs:

> A host with `8× RTX 3090 + 128 vCPU + 720 GB` → one unit = `3090×1 + 16 vCPU + 90 GB`.

Consequences that matter when building requests:

- **You cannot tune the CPU/memory/GPU ratio independently.** Ask for 1 GPU and you get
  that host's per-GPU slice of CPU+RAM; ask for 2 GPUs and you get 2× the slice. A
  container's size is 1–8× a unit.
- **`cpu_num_from/to` and `memory_size_from/to` are host-selection *filters*, not exact
  allocations.** They constrain which hosts qualify; the actual CPU/RAM the container
  gets is decided by the host it lands on. (In practice a 4090 container came up with
  12 vCPU / 60 GB — that's the host's unit, not a number we requested.)
- Likewise `gpu_name_set`, `price_from/to`, and `cuda_v` are all *conditions*. When
  several hosts qualify, the platform picks one; its real resources define the container.

## Deployment types

| Type | Behavior | Use for |
|---|---|---|
| **ReplicaSet** | Maintains N live replicas; rebuilds containers that die or stop matching the conditions; changing replica_num scales immediately | long-lived services, dev boxes, sweep workers |
| **Job** | Runs containers until a target completion count is reached, then they exit; does **not** restart finished containers to keep a count | batch experiments that exit when done |
| **Container** | Exactly one container; equivalent to a Job with target = 1 | a single one-shot run |

Editing the scheduling conditions of a ReplicaSet destroys non-matching containers and
starts new ones. **Gotcha:** stopping one container in a ReplicaSet makes the controller
spin up a replacement to maintain replica_num — to actually shrink it, drop replica_num
(`deploy-scale`) or stop with `--decrease-replica`; to shut it all down use
`deploy-stop`/`deploy-delete`.

## Container lifecycle = the lifecycle of `cmd`

**A container lives as long as its startup command (`cmd`) runs. When `cmd` exits, the
container stops.** This single rule explains most "why did my container die?" confusion.

- Foreground run: `python train.py` — container stops when training finishes (good for Job).
- Stay alive (so you can SSH in and work): append `sleep infinity` — this is why the
  smoke-test/dev containers here use `--cmd "sleep infinity"`.
- Background app + keep alive: `nohup python app.py & && sleep infinity` (you then manage
  the app's own lifecycle).
- **Debug trick:** if a container won't start properly, set `cmd` to `sleep infinity`,
  let it stay up, SSH in, and run the real command by hand to see the error.

A container can also be stopped manually via the stop endpoints.

## Container reuse (复用容器)

A mechanism to avoid repeated image pulls:

- A stopped container is kept (platform policy, up to ~7 days) in a reuse pool instead of
  being destroyed immediately.
- When you create a new container, the platform first tries to match an eligible stopped
  container from the pool; only if none matches does it pull the image normally → faster start.
- **Data:** a reused container keeps the previous container's files. Useful to skip
  re-copying data — but you may need to clean up stale files yourself.
- **Env:** `AutoDLContainerUUID` is always a fresh unique value, never the old one.
- **API:** controlled by `reuse_container` (true/false) at the deployment level — CLI flag
  `--reuse-container` / `--no-reuse-container` (default on).

## Best practices (from the docs)

- **Image vs network storage:** bake static environment/dependencies into the image (rarely
  changes); put frequently-changing files (code, model weights) on **network storage**,
  which is shared across instances and simpler to manage.
- **Startup commands:**
  - Don't background the whole command (`python app.py &` alone exits the container) —
    pair it with `sleep infinity`.
  - Put complex commands in a script file and call that.
  - With relative paths, `cd` first: `cd /root/ && python app.py`.
  - In a conda env, call the env's interpreter directly:
    `/root/miniconda3/envs/my-env/bin/python xxx.py`.
