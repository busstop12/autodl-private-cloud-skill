# AutoDL Private Cloud (ESD) API Reference

Complete reference for every endpoint the `autodl.py` CLI wraps. Source:
`https://private.autodl.com/docs/esd_api_doc/`. Read this when you need a field the
CLI doesn't expose, when interpreting a response, or when building a `raw` call.

## Contents
- [Conventions](#conventions)
- [Enums & units](#enums--units)
- [Status values](#status-values)
- [Images (private vs system)](#images-private-vs-system)
- [Endpoints](#endpoints)
  - [GPU stock](#gpu-stock)
  - [List private images](#list-private-images)
  - [Create deployment](#create-deployment)
  - [List deployments](#list-deployments)
  - [Set replica number](#set-replica-number)
  - [Stop deployment](#stop-deployment)
  - [Delete deployment](#delete-deployment)
  - [List containers](#list-containers)
  - [Stop container](#stop-container)
  - [Container events](#container-events)
  - [Scheduling blacklist](#scheduling-blacklist)

## Conventions

- **Base URL:** `https://private.autodl.com` (override per cluster via `AUTODL_BASE_URL`).
- **Auth:** every request sends header `Authorization: <developer-token>` plus
  `Content-Type: application/json`. The token comes from Console → Settings → Developer Token.
- **Response envelope:** every response is `{"code": "...", "msg": "...", "data": ...}`.
  Success is `code == "Success"`; otherwise `msg` explains the failure. The CLI exits
  non-zero on any non-Success code and prints only the `data` field on success.
- **Note on DELETE:** the delete-deployment endpoint sends a JSON **body** with a DELETE
  method (unusual, but that's the API).
- **CLI confirmation guard:** the six state-changing commands (`deploy-create`,
  `deploy-scale`, `container-stop`, `deploy-stop`, `deploy-delete`, `blacklist`) only
  execute with `--yes`; without it they print a preview and exit 2. `raw` is **not** gated,
  so a `raw` call to a state-changing endpoint runs immediately — confirm with the user first.

## Enums & units

| Thing | Detail |
|---|---|
| `deployment_type` | `"ReplicaSet"`, `"Job"`, `"Container"` |
| `cuda_v` | integer CUDA version: `111, 113, 116, 117, 118, 120, 122` (= 11.1 … 12.2); **`0` = no CUDA constraint** (what the console uses for base images — schedule on any host) |
| `image_uuid` | accepts a **private** image (`image-xxxx`) **or a system/base** image (`base-image-xxxx`) — see [Images](#images-private-vs-system) |
| memory in **create** request | whole **GB** integers (`memory_size_from/to`) |
| memory in **responses** | **bytes** (e.g. 1 GB → `1073741824`, 256 GB → `274877906944`) |
| `price*` fields | units of **0.001 CNY/hour** (e.g. `9000` = 9 CNY/hr) |
| `gpu_name` / `gpu_name_set` | exact model strings, e.g. `"RTX A5000"`, `"RTX 4090"`, `"Tesla V100-SXM2-32GB"`, `"NVIDIA TITAN Xp"` |
| `reuse_container` | bool; reuse a previously released container's environment when possible |
| `parallelism_num` | Job only: how many containers run concurrently |

## Status values

- **Container status:** `creating`, `created`, `starting`, `running`, `oss_merged`,
  `shutting_down`, `shutdown`. A container is reachable (SSH/service) only at `running`.
- **Deployment status:** includes `stopped` (and running states reflected by
  `running_num` / `starting_num` / `finished_num` counts on the deployment object).

## Images (private vs system)

`deploy-create` needs an `image_uuid`. There are two kinds, and the field accepts both:

- **Private images** — UUID looks like `image-db8346e037`. Listed by `image/private/list`
  (CLI: `image-list`). Many private clouds have **none** by default.
- **System / base images** — UUID looks like `base-image-90df20b82987` (e.g.
  `torch:cuda12.4-cudnn-devel-ubuntu22.04-py312-torch2.5.1`). These are what the console's
  「系统镜像」 dropdown offers. List them with **`POST /api/v2/image/list`** (CLI:
  `system-image-list`) — see below.

**Listing system images** uses the console **v2** API, not the v1 developer API:
`POST /api/v2/image/list` with body `{"page_index":1,"page_size":100}`, authorized by the
same token. (The `v1/dev` image paths — `image/list`, `image/base/list`, etc. — all 404;
only `image/private/list` exists there, and it returns private images only.) Response `data`:
```json
{
  "result_total": 17,
  "list": [
    {
      "image_uuid": "base-image-90df20b82987",
      "name": "torch:cuda12.4-cudnn-devel-ubuntu22.04-py312-torch2.5.1",
      "url": "ccr.ccs.tencentyun.com/autodl-private-cloud/torch:cuda12.4-...-torch2.5.1",
      "cuda_version": "12.4", "chip_corp": "nvidia", "cpu_arch": "x86"
    }
  ]
}
```
CLI: `system-image-list [--filter torch]`. If you can't list (older cluster), you can also
recover a UUID by reading an existing deployment — `deploy-list` returns
`template.image_uuid` alongside `template.image_name`. A dated catalog of one cluster's 17
system images lives in `references/system-images.md`.

Pair a system image with `cuda_v: 0` (no CUDA constraint), matching what the console does —
passing the image *name* string as `image_uuid` fails with `镜像不存在`.

---

## Endpoints

### GPU stock
`GET /api/v1/dev/machine/gpu_stock` — idle and total GPU counts by model. No body.

Response `data` is a **dict keyed by GPU name** (verified against a live private
cluster); each value repeats the name plus counts:
```json
{
  "NVIDIA A40":               { "gpu_name": "NVIDIA A40",               "idle_gpu_num": 4, "total_gpu_num": 6 },
  "NVIDIA GeForce RTX 4090":  { "gpu_name": "NVIDIA GeForce RTX 4090",  "idle_gpu_num": 7, "total_gpu_num": 8 }
}
```
(Some older docs show a list of single-key objects like `[{"RTX 4090": {...}}]`;
the CLI normalizes both forms.) The `gpu_name` here is the **exact** string to pass
as `--gpu-name` to `deploy-create` — e.g. `"NVIDIA GeForce RTX 4090"`, not `"RTX 4090"`.
CLI: `gpu-stock` (add `--idle-only` to keep only models with idle GPUs).

### List private images
`POST /api/v1/dev/image/private/list` — your private images; source of `image_uuid`.

Request:
```json
{ "page_index": 1, "page_size": 10 }
```
Response `data`:
```json
{
  "list": [
    { "id": 111, "image_uuid": "image-db8346e037", "name": "image name", "status": "finished" }
  ],
  "page_index": 1,
  "page_size": 10
}
```
CLI: `image-list`. Only images with `status: "finished"` are launchable.

### Create deployment
`POST /api/v1/dev/deployment` — launch containers. **Bills by the hour.**

Request (ReplicaSet example):
```json
{
  "name": "api auto-created",
  "deployment_type": "ReplicaSet",
  "replica_num": 2,
  "reuse_container": true,
  "container_template": {
    "gpu_name_set": ["RTX A5000"],
    "cuda_v": 113,
    "gpu_num": 1,
    "cpu_num_from": 1,
    "cpu_num_to": 100,
    "memory_size_from": 1,
    "memory_size_to": 256,
    "cmd": "sleep 100",
    "price_from": 100,
    "price_to": 9000,
    "image_uuid": "image-db8346e037"
  }
}
```
Notes:
- `memory_size_from/to` here are **GB** (unlike responses, which are bytes).
- For a Job, add top-level `"parallelism_num": N`.
- `region_sign` is an optional `container_template` field to constrain placement; include
  it only when the cluster requires it.

Response `data`:
```json
{ "deployment_uuid": "833f1cd5a764fa3" }
```
CLI: `deploy-create` (or `deploy-create --spec-file body.json` to send a body verbatim).

### List deployments
`POST /api/v1/dev/deployment/list`

Request:
```json
{ "page_index": 1, "page_size": 10 }
```
Response `data` (one list item shown; note `template.memory_size_*` are **bytes** here):
```json
{
  "list": [
    {
      "id": 214, "uid": 58, "uuid": "53a677bb3e281b8", "name": "xxxx",
      "deployment_type": "Container", "status": "stopped",
      "replica_num": 1, "parallelism_num": 1, "reuse_container": true,
      "starting_num": 0, "running_num": 0, "finished_num": 2,
      "image_uuid": "image-db8346e037",
      "template": {
        "gpu_name_set": ["Tesla V100-SXM2-32GB"], "gpu_num": 1,
        "image_uuid": "image-db8346e037", "image_name": "xxxx", "cmd": "sleep 100",
        "memory_size_from": 1073741824, "memory_size_to": 274877906944,
        "cpu_num_from": 1, "cpu_num_to": 100,
        "price_from": 10, "price_to": 9000, "cuda_v": 118
      },
      "price_estimates": 0,
      "created_at": "2023-01-05T20:34:07+08:00",
      "updated_at": "2023-01-05T20:34:07+08:00",
      "stopped_at": null
    }
  ],
  "page_index": 1, "page_size": 10, "offset": 0, "max_page": 1, "result_total": 3
}
```
CLI: `deploy-list`. The `uuid` here is the `deployment_uuid` other commands need.

### Set replica number
`PUT /api/v1/dev/deployment/replica_num` — resize a ReplicaSet (scale up or down).

Request:
```json
{ "deployment_uuid": "5be3045703152b9", "replica_num": 10 }
```
Response: `data: null`. CLI: `deploy-scale`.

### Stop deployment
`PUT /api/v1/dev/deployment/operate` — stop all containers in a deployment.

Request:
```json
{ "deployment_uuid": "5be3045703152b9", "operate": "stop" }
```
Response: `data: null`. CLI: `deploy-stop`.

### Delete deployment
`DELETE /api/v1/dev/deployment` — delete (auto-stops if still running). **Body + DELETE.**

Request:
```json
{ "deployment_uuid": "5be3045703152b9" }
```
Response: `data: null`. CLI: `deploy-delete`.

### List containers
`POST /api/v1/dev/deployment/container/list` — containers and their SSH/service info.

Request (all filters; empty/0 = no filter):
```json
{
  "deployment_uuid": "da497aea1eb8343",
  "container_uuid": "",
  "date_from": "", "date_to": "",
  "gpu_name": "",
  "cpu_num_from": 0, "cpu_num_to": 0,
  "memory_size_from": 0, "memory_size_to": 0,
  "price_from": 0, "price_to": 0,
  "released": false,
  "page_index": 1, "page_size": 10
}
```
Response `data` (one item; **`info` carries the connection details**):
```json
{
  "list": [
    {
      "id": 195,
      "uuid": "53a677bb3e281b8-f94411a60c-63c24009",
      "machine_id": "f94411a60c",
      "deployment_uuid": "da497aea1eb8343",
      "status": "running",
      "gpu_name": "NVIDIA TITAN Xp", "gpu_num": 1, "cpu_num": 4,
      "memory_size": 2147483648,
      "image_uuid": "image-db8346e037",
      "price": 1881,
      "info": {
        "ssh_command": "ssh -p 21305 root@region-1.autodl.com",
        "root_password": "xxxxxxxxxx",
        "service_url": "https://region-1.autodl.com:21294",
        "proxy_host": "region-1.autodl.com",
        "custom_port": 21294
      },
      "started_at": "2022-12-13T16:43:03+08:00",
      "stopped_at": null,
      "created_at": "2022-12-13T16:42:50+08:00",
      "updated_at": "2022-12-13T16:43:03+08:00"
    }
  ],
  "page_index": 1, "page_size": 10, "max_page": 1
}
```
The container's `uuid` is the `deployment_container_uuid` that `container-stop` and
`blacklist` take. CLI: `container-list` (add `--released` to include finished ones).

### Stop container
`PUT /api/v1/dev/deployment/container/stop` — stop one container.

Request:
```json
{
  "deployment_container_uuid": "da497aea1eb8343-f94411a60c-a394fb30",
  "decrease_one_replica_num": false
}
```
`decrease_one_replica_num`: in a ReplicaSet, `false` lets the controller reschedule a
replacement; `true` also lowers `replica_num` by one so the container stays gone.
Response: `data: null`. CLI: `container-stop` (`--decrease-replica` sets it true).

### Container events
`POST /api/v1/dev/deployment/container/event/list` — lifecycle events, for debugging
slow starts or failures.

Request:
```json
{
  "deployment_uuid": "da497aea1eb8343",
  "deployment_container_uuid": "",
  "page_index": 1, "page_size": 10, "offset": 0
}
```
Response `data`:
```json
{
  "list": [
    {
      "deployment_container_uuid": "da497aea1eb8343-f94411a60c-1502e6e2",
      "status": "running",
      "created_at": "2022-12-13T16:34:57+08:00"
    }
  ]
}
```
CLI: `container-events`.

### Scheduling blacklist
`POST /api/v1/dev/deployment/blacklist` — bar a misbehaving host (e.g. slow to boot) from
scheduling this deployment's containers for 24 hours.

Request:
```json
{
  "deployment_container_uuid": "da497aea1eb8343-f94411a60c-1502e6e2",
  "comment": "slow to start; do not schedule here"
}
```
Response: `data: null`. CLI: `blacklist`.
