# System (base) image catalog & the v2 image-list endpoint

How to list platform **system/base images** (the `base-image-xxxx` values that
`deploy-create --image-uuid` accepts), plus a dated snapshot of one cluster's catalog.

> The live source of truth is the `system-image-list` command — run it to get current
> UUIDs. The table below is a **snapshot** (image UUIDs are cluster-specific and can change
> as the operator adds/removes images), useful as a quick reference and an example of the
> response shape.

## Endpoint

Listing system images lives on the **console v2 API**, not the v1 developer API:

`POST /api/v2/image/list`

- Auth: same `Authorization: <token>` header as every other call (the dev token works here).
- Body: `{"page_index": 1, "page_size": 100}`
- The `v1/dev` image paths (`image/list`, `image/base/list`, `image/public/list`,
  `image/official/list`) all **404** — only `image/private/list` exists there, and it
  returns *private* images (`image-xxxx`) only, not system images.

Response `data`:
```json
{
  "result_total": 17,
  "page_index": 1, "page_size": 100, "max_page": 1,
  "list": [
    {
      "image_uuid": "base-image-90df20b82987",
      "name": "torch:cuda12.4-cudnn-devel-ubuntu22.04-py312-torch2.5.1",
      "url": "ccr.ccs.tencentyun.com/autodl-private-cloud/torch:cuda12.4-...-torch2.5.1",
      "cuda_version": "12.4",
      "chip_corp": "nvidia",
      "cpu_arch": "x86"
    }
  ]
}
```

CLI:
```bash
python3 scripts/autodl.py system-image-list                 # all system images + UUIDs
python3 scripts/autodl.py system-image-list --filter torch  # substring match on name
```

Then launch from one: `deploy-create --image-uuid base-image-xxxx --cuda-v 0 ...`
(pair system images with `cuda_v: 0` — no CUDA constraint; passing the image *name*
string as the UUID fails with `镜像不存在`).

## Snapshot — 17 system images (captured 2026-06-14)

### PyTorch
| image_uuid | CUDA | name |
|---|---|---|
| `base-image-90df20b82987` | 12.4 | torch:cuda12.4-cudnn-devel-ubuntu22.04-py312-torch2.5.1 |
| `base-image-6e4cef09b534` | 12.1 | torch:cuda12.1-cudnn8-devel-ubuntu22.04-py312-torch2.3.0 |
| `base-image-14e83aa2cfa7` | 11.8 | torch:cuda11.8-cudnn8-devel-ubuntu22.04-py310-torch2.1.2 |
| `base-image-9ca5f981a049` | 11.8 | torch:cuda11.8-cudnn8-devel-ubuntu20.04-py38-torch2.0.0 |
| `base-image-e07d072ada59` | 11.3 | torch:cuda11.3-cudnn8-devel-ubuntu20.04-py38-torch1.11.0 |
| `base-image-61373f784939` | 11.3 | torch:cuda11.3-cudnn8-devel-ubuntu20.04-py38-torch1.10.0 |
| `base-image-de65e15f0910` | 11.1 | torch:cuda11.1-cudnn8-devel-ubuntu18.04-py38-torch1.9.0 |
| `base-image-86e1115cb2da` | 11.0 | torch:cuda11.0-cudnn8-devel-ubuntu18.04-py38-torch1.7.0 |
| `base-image-4f96060c9585` | 10.1 | torch:cuda10.1-cudnn7-devel-ubuntu18.04-py38-torch1.5.1 |

### TensorFlow
| image_uuid | CUDA | name |
|---|---|---|
| `base-image-d7c8c95bf875` | 11.2 | tensorflow:cuda11.2-cudnn8-devel-ubuntu20.04-py38-tf2.9.0 |
| `base-image-550a6216ebd7` | 11.2 | tensorflow:cuda11.2-cudnn8-devel-ubuntu18.04-py38-tf2.5.0 |
| `base-image-1d922d5b38e9` | 11.4 | tensorflow:cuda11.x-py38-tf1.15.5 |

### Miniconda
| image_uuid | CUDA | name |
|---|---|---|
| `base-image-55249b00b70c` | 12.2 | miniconda:cuda12.2-cudnn8-devel-ubuntu22.04-py310 |
| `base-image-8db4a70f8429` | 11.8 | miniconda:cuda11.8-cudnn8-devel-ubuntu22.04-py310 |
| `base-image-f10d921a4585` | 11.6 | miniconda:cuda11.6-cudnn8-devel-ubuntu20.04-py38 |
| `base-image-4f0c47707e3d` | 11.3 | miniconda:cuda11.3-cudnn8-devel-ubuntu18.04-py38 |

### Huawei Ascend (ARM)
| image_uuid | CUDA | name |
|---|---|---|
| `base-image-ff695d7278a5` | — (CANN 8.0, arm64) | huawei:arm64_cann8.0.0-ubuntu22.04-py310-torch2.1.0 |
