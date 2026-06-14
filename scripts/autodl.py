#!/usr/bin/env python3
"""
autodl.py — CLI wrapper for the AutoDL private-cloud (ESD) developer API.

One command per endpoint plus a few convenience commands for automating
research-experiment lifecycles (launch GPU container -> wait for it to come up
-> read SSH connection info -> tear it down so billing stops).

Auth & target resolve in this order (first hit wins):
  developer token: --token  >  $AUTODL_TOKEN  >  token file
  base URL:        --base-url  >  $AUTODL_BASE_URL  >  https://private.autodl.com

The token file lets you save the token once instead of re-exporting it each
session. Default location $AUTODL_TOKEN_FILE or ~/.config/autodl/token (created
with 0600 perms). Save one with:  autodl.py save-token --token <T>  (or pipe it
on stdin). Check what's configured — without revealing it — with: token-status.
Get a token from Console -> Settings -> Developer Token.

Every endpoint returns the envelope {"code","msg","data"}. This tool checks
code == "Success"; on anything else it prints msg to stderr and exits 1.
By default it prints the `data` field as pretty JSON so output is easy to parse.

Run `autodl.py <command> -h` for per-command options, or `autodl.py -h` for the
full command list.
"""

import argparse
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("ERROR: this tool needs the `requests` package. Install with: pip install requests")

DEFAULT_BASE_URL = "https://private.autodl.com"
GIB = 1024 ** 3  # responses report memory in bytes; create requests take whole GB


# --------------------------------------------------------------------------- #
# Token storage                                                               #
# --------------------------------------------------------------------------- #
def token_file_path():
    """Where the saved token lives: $AUTODL_TOKEN_FILE or ~/.config/autodl/token."""
    env = os.environ.get("AUTODL_TOKEN_FILE")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), ".config", "autodl", "token")


def read_token_file():
    """Return the token saved on disk, or None if there's no usable file."""
    path = token_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        return tok or None
    except (OSError, IOError):
        return None


def write_token_file(token):
    """Persist the token to disk with owner-only (0600) perms; return the path."""
    token = token.strip()
    if not token:
        sys.exit("ERROR: refusing to save an empty token.")
    path = token_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Create with 0600 so the secret isn't world/group readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # enforce perms even if the file pre-existed
    return path


def resolve_token(args):
    """Token from --token, then $AUTODL_TOKEN, then the saved token file."""
    return (getattr(args, "token", None)
            or os.environ.get("AUTODL_TOKEN")
            or read_token_file())


def token_source(args):
    """Human-readable origin of the active token, without revealing its value."""
    if getattr(args, "token", None):
        return "--token flag"
    if os.environ.get("AUTODL_TOKEN"):
        return "$AUTODL_TOKEN env var"
    if read_token_file():
        return f"token file ({token_file_path()})"
    return None


# --------------------------------------------------------------------------- #
# Low-level HTTP                                                               #
# --------------------------------------------------------------------------- #
def api_request(args, method, path, body=None):
    """Call the API and return the unwrapped `data` field, or exit non-zero."""
    token = resolve_token(args)
    if not token:
        sys.exit("ERROR: no token. Provide one with --token, $AUTODL_TOKEN, or save "
                 f"it once with `autodl.py save-token` (stored at {token_file_path()}). "
                 "Get a token from Console -> Settings -> Developer Token.")
    base = (args.base_url or os.environ.get("AUTODL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    url = base + path
    headers = {"Authorization": token, "Content-Type": "application/json"}

    try:
        resp = requests.request(method, url, headers=headers,
                                json=body if body is not None else None,
                                timeout=args.http_timeout)
    except requests.RequestException as e:
        sys.exit(f"ERROR: request to {url} failed: {e}")

    # Surface HTTP-level problems with whatever body the server returned.
    if resp.status_code >= 400:
        sys.exit(f"ERROR: HTTP {resp.status_code} from {path}: {resp.text[:500]}")

    try:
        payload = resp.json()
    except ValueError:
        sys.exit(f"ERROR: non-JSON response from {path}: {resp.text[:500]}")

    if payload.get("code") != "Success":
        sys.exit(f"ERROR: API returned code={payload.get('code')!r} "
                 f"msg={payload.get('msg')!r} for {path}")
    return payload.get("data")


def emit(data):
    """Print the API data field. Pretty JSON keeps it both readable and parseable."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #
def _normalize_gpu_stock(data):
    """Return a flat list of {gpu_name, idle_gpu_num, total_gpu_num}.

    The live API returns a dict keyed by GPU name ({"NVIDIA A40": {gpu_name,
    idle_gpu_num, total_gpu_num}, ...}); older docs show a list of single-key
    objects ([{"RTX 4090": {idle_gpu_num, total_gpu_num}}, ...]). Handle both."""
    rows = []
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        # Each element is either {name: {...}} or already a flat {gpu_name, ...}.
        items = []
        for entry in data:
            if isinstance(entry, dict) and "idle_gpu_num" in entry:
                items.append((entry.get("gpu_name", ""), entry))
            elif isinstance(entry, dict):
                items.extend(entry.items())
    else:
        return rows
    for name, stock in items:
        stock = stock or {}
        rows.append({
            "gpu_name": stock.get("gpu_name") or name,
            "idle_gpu_num": stock.get("idle_gpu_num", 0),
            "total_gpu_num": stock.get("total_gpu_num", 0),
        })
    return rows


def cmd_gpu_stock(args):
    data = api_request(args, "GET", "/api/v1/dev/machine/gpu_stock")
    if args.idle_only:
        rows = [r for r in _normalize_gpu_stock(data) if r["idle_gpu_num"] > 0]
        emit(rows)
    else:
        emit(data)


def cmd_image_list(args):
    body = {"page_index": args.page_index, "page_size": args.page_size}
    emit(api_request(args, "POST", "/api/v1/dev/image/private/list", body))


def cmd_system_image_list(args):
    """List platform system/base images and their UUIDs (the `base-image-xxxx`
    values `deploy-create --image-uuid` accepts). This lives on the console v2 API
    (`/api/v2/image/list`), not the v1 developer API, but the same token works."""
    body = {"page_index": args.page_index, "page_size": args.page_size}
    data = api_request(args, "POST", "/api/v2/image/list", body)
    rows = (data or {}).get("list", []) or []
    if args.filter:
        f = args.filter.lower()
        rows = [r for r in rows if f in (r.get("name", "") or "").lower()]
    emit([
        {
            "image_uuid": r.get("image_uuid"),
            "name": r.get("name"),
            "cuda_version": r.get("cuda_version"),
            "cpu_arch": r.get("cpu_arch"),
            "chip_corp": r.get("chip_corp"),
        }
        for r in rows
    ])


# --------------------------------------------------------------------------- #
# Confirmation guard for sensitive (state-changing) operations                #
# --------------------------------------------------------------------------- #
# Destructive or billable commands must not run on a whim. Each one checks
# `args.yes` first: without --yes it prints a preview of exactly what WOULD happen
# and exits 2 WITHOUT calling the API, so the operator can review and confirm. The
# skill instructs the agent to surface this preview to the human and only re-run
# with --yes after explicit confirmation.

def _describe_deployment(args, deployment_uuid):
    """Best-effort read-only lookup so a preview names the real target, not just a UUID."""
    try:
        data = api_request(args, "POST", "/api/v1/dev/deployment/list",
                           {"page_index": 1, "page_size": 100})
    except SystemExit:
        return f"deployment {deployment_uuid} (lookup failed)"
    for d in (data or {}).get("list", []) or []:
        if d.get("uuid") == deployment_uuid:
            t = d.get("template", {}) or {}
            return (f"name={d.get('name')!r} type={d.get('deployment_type')} "
                    f"status={d.get('status')} "
                    f"running={d.get('running_num')}/{d.get('replica_num')} "
                    f"gpu={t.get('gpu_name_set')} uuid={deployment_uuid}")
    return f"deployment {deployment_uuid} (not found in current deployment list)"


def preview_and_exit(action, target, method, path, body):
    """Print what a sensitive op WOULD do, without doing it, then exit 2."""
    emit({
        "preview": True,
        "executed": False,
        "action": action,
        "target": target,
        "request": {"method": method, "path": path, "body": body},
        "note": "SENSITIVE OP NOT EXECUTED. Show this to the user, get explicit "
                "confirmation, then re-run the SAME command with --yes.",
    })
    sys.exit(2)


def cmd_deploy_create(args):
    if args.spec_file:
        # Escape hatch: send a hand-authored request body verbatim. Lets the skill
        # build deployment shapes this CLI's flags don't anticipate.
        with open(args.spec_file, encoding="utf-8") as f:
            body = json.load(f)
    else:
        if not args.image_uuid:
            sys.exit("ERROR: --image-uuid is required (or use --spec-file).")
        if not args.gpu_name:
            sys.exit("ERROR: at least one --gpu-name is required (or use --spec-file).")
        template = {
            "gpu_name_set": args.gpu_name,
            "gpu_num": args.gpu_num,
            "cuda_v": args.cuda_v,
            "cpu_num_from": args.cpu_from,
            "cpu_num_to": args.cpu_to,
            "memory_size_from": args.mem_from_gb,   # create takes whole GB
            "memory_size_to": args.mem_to_gb,
            "price_from": args.price_from,           # units of 0.001 CNY/hr
            "price_to": args.price_to,
            "image_uuid": args.image_uuid,
            "cmd": args.cmd,
        }
        if args.region_sign:
            template["region_sign"] = args.region_sign
        body = {
            "name": args.name,
            "deployment_type": args.type,
            "replica_num": args.replica_num,
            "reuse_container": args.reuse_container,
            "container_template": template,
        }
        if args.type == "Job" and args.parallelism_num is not None:
            body["parallelism_num"] = args.parallelism_num
    if not args.yes:
        tmpl = body.get("container_template", {}) or {}
        target = (f"create {body.get('deployment_type')} {body.get('name')!r} "
                  f"x{body.get('replica_num')} on {tmpl.get('gpu_name_set')} "
                  f"image={tmpl.get('image_uuid')} cmd={tmpl.get('cmd')!r}")
        preview_and_exit("CREATE deployment (starts billable GPU containers)", target,
                         "POST", "/api/v1/dev/deployment", body)
    emit(api_request(args, "POST", "/api/v1/dev/deployment", body))


def cmd_deploy_list(args):
    body = {"page_index": args.page_index, "page_size": args.page_size}
    emit(api_request(args, "POST", "/api/v1/dev/deployment/list", body))


def cmd_deploy_stop(args):
    body = {"deployment_uuid": args.deployment_uuid, "operate": "stop"}
    if not args.yes:
        preview_and_exit("STOP deployment (stops ALL its containers)",
                         _describe_deployment(args, args.deployment_uuid),
                         "PUT", "/api/v1/dev/deployment/operate", body)
    emit(api_request(args, "PUT", "/api/v1/dev/deployment/operate", body))


def cmd_deploy_delete(args):
    body = {"deployment_uuid": args.deployment_uuid}
    if not args.yes:
        preview_and_exit("DELETE deployment (irreversible; auto-stops if running)",
                         _describe_deployment(args, args.deployment_uuid),
                         "DELETE", "/api/v1/dev/deployment", body)
    emit(api_request(args, "DELETE", "/api/v1/dev/deployment", body))


def cmd_deploy_scale(args):
    body = {"deployment_uuid": args.deployment_uuid, "replica_num": args.replica_num}
    if not args.yes:
        preview_and_exit("SCALE deployment replica_num (may add billable containers or destroy some)",
                         _describe_deployment(args, args.deployment_uuid) + f"  ->  replica_num={args.replica_num}",
                         "PUT", "/api/v1/dev/deployment/replica_num", body)
    emit(api_request(args, "PUT", "/api/v1/dev/deployment/replica_num", body))


def cmd_container_list(args):
    body = {
        "deployment_uuid": args.deployment_uuid,
        "container_uuid": args.container_uuid,
        "released": args.released,
        "page_index": args.page_index,
        "page_size": args.page_size,
    }
    if args.gpu_name:
        body["gpu_name"] = args.gpu_name
    emit(api_request(args, "POST", "/api/v1/dev/deployment/container/list", body))


def cmd_container_stop(args):
    body = {
        "deployment_container_uuid": args.container_uuid,
        "decrease_one_replica_num": args.decrease_replica,
    }
    if not args.yes:
        target = (f"container {args.container_uuid}"
                  + ("  (and lower replica_num by 1)" if args.decrease_replica
                     else "  (ReplicaSet will reschedule a replacement)"))
        preview_and_exit("STOP container", target,
                         "PUT", "/api/v1/dev/deployment/container/stop", body)
    emit(api_request(args, "PUT", "/api/v1/dev/deployment/container/stop", body))


def cmd_container_events(args):
    body = {
        "deployment_uuid": args.deployment_uuid,
        "deployment_container_uuid": args.container_uuid,
        "page_index": args.page_index,
        "page_size": args.page_size,
        "offset": 0,
    }
    emit(api_request(args, "POST", "/api/v1/dev/deployment/container/event/list", body))


def cmd_blacklist(args):
    body = {"deployment_container_uuid": args.container_uuid, "comment": args.comment}
    if not args.yes:
        preview_and_exit("BLACKLIST host (bars scheduling on this container's host for 24h)",
                         f"container {args.container_uuid} comment={args.comment!r}",
                         "POST", "/api/v1/dev/deployment/blacklist", body)
    emit(api_request(args, "POST", "/api/v1/dev/deployment/blacklist", body))


def cmd_wait_running(args):
    """Poll a deployment's containers until one is `running`, then emit its info.

    This is the linchpin for automation: after deploy-create you don't know when
    the container is actually reachable, so block here and hand back the SSH
    connection details the experiment needs."""
    deadline = time.time() + args.timeout
    last_status = None
    while time.time() < deadline:
        body = {
            "deployment_uuid": args.deployment_uuid,
            "container_uuid": "",
            "released": False,
            "page_index": 1,
            "page_size": 100,
        }
        data = api_request(args, "POST", "/api/v1/dev/deployment/container/list", body)
        containers = (data or {}).get("list", []) or []
        running = [c for c in containers if c.get("status") == "running"]
        if running:
            emit(running if args.all else running[0])
            return
        statuses = sorted({c.get("status") for c in containers}) or ["<no containers yet>"]
        if statuses != last_status:
            print(f"… waiting: container status = {statuses}", file=sys.stderr)
            last_status = statuses
        time.sleep(args.interval)
    sys.exit(f"ERROR: timed out after {args.timeout}s waiting for a running container "
             f"in deployment {args.deployment_uuid} (last status: {last_status})")


def cmd_raw(args):
    """Escape hatch: call any endpoint directly. Useful for endpoints this CLI
    doesn't wrap, or for future additions to the API."""
    body = json.loads(args.body) if args.body else None
    emit(api_request(args, args.method.upper(), args.path, body))


def cmd_save_token(args):
    """Persist a developer token to the token file (0600) for reuse across sessions.

    Token source: --token, else read from stdin (so it never shows in argv/logs)."""
    token = args.token
    if not token and not sys.stdin.isatty():
        token = sys.stdin.read()
    if not token or not token.strip():
        sys.exit("ERROR: no token given. Pass --token <T> or pipe it on stdin, e.g.\n"
                 "  printf %s \"$TOKEN\" | autodl.py save-token")
    path = write_token_file(token)
    emit({"saved": True, "path": path, "permissions": "0600",
          "note": "Token stored. Future commands read it automatically; no env var needed."})


def cmd_token_status(args):
    """Report whether a token is configured and from where — never prints the token."""
    src = token_source(args)
    emit({
        "configured": src is not None,
        "source": src,
        "token_file": token_file_path(),
        "token_file_exists": os.path.exists(token_file_path()),
        "hint": (None if src else
                 "No token found. Get one from Console -> Settings -> Developer Token, "
                 "then save it with: autodl.py save-token --token <T>"),
    })


# --------------------------------------------------------------------------- #
# Argument parsing                                                            #
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="autodl.py",
        description="CLI for the AutoDL private-cloud developer API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--token", help="developer token (default: $AUTODL_TOKEN)")
    p.add_argument("--base-url", help=f"API base URL (default: $AUTODL_BASE_URL or {DEFAULT_BASE_URL})")
    p.add_argument("--http-timeout", type=float, default=30, help="per-request HTTP timeout seconds (default 30)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_page(sp):
        sp.add_argument("--page-index", type=int, default=1)
        sp.add_argument("--page-size", type=int, default=10)

    def add_confirm(sp):
        # Sensitive (state-changing) commands require this to actually execute.
        # Without it they only print a preview and exit — see the confirmation guard.
        sp.add_argument("-y", "--yes", action="store_true",
                        help="actually execute this state-changing op (default: preview only)")

    # gpu-stock
    sp = sub.add_parser("gpu-stock", help="list idle/total GPU counts by type")
    sp.add_argument("--idle-only", action="store_true", help="only GPUs with idle_gpu_num > 0")
    sp.set_defaults(func=cmd_gpu_stock)

    # image-list
    sp = sub.add_parser("image-list", help="list your private images (image-xxxx)")
    add_page(sp)
    sp.set_defaults(func=cmd_image_list)

    # system-image-list
    sp = sub.add_parser("system-image-list",
                        help="list platform system/base images + UUIDs (base-image-xxxx)")
    sp.add_argument("--filter", help="substring match on image name, e.g. 'torch' or 'cuda12'")
    sp.add_argument("--page-index", type=int, default=1)
    sp.add_argument("--page-size", type=int, default=100)
    sp.set_defaults(func=cmd_system_image_list)

    # deploy-create
    sp = sub.add_parser("deploy-create", help="create a deployment (launches GPU containers — costs money)")
    sp.add_argument("--name", default="api-deployment", help="deployment name")
    sp.add_argument("--type", default="ReplicaSet", choices=["ReplicaSet", "Job", "Container"])
    sp.add_argument("--replica-num", type=int, default=1)
    sp.add_argument("--parallelism-num", type=int, default=None, help="Job type: concurrent containers")
    sp.add_argument("--reuse-container", action=argparse.BooleanOptionalAction, default=True)
    sp.add_argument("--image-uuid", help="image to launch (from image-list)")
    sp.add_argument("--gpu-name", nargs="+", help="acceptable GPU model(s), e.g. --gpu-name 'RTX A5000'")
    sp.add_argument("--gpu-num", type=int, default=1)
    sp.add_argument("--cuda-v", type=int, default=118, help="CUDA version int, e.g. 118 = 11.8")
    sp.add_argument("--cpu-from", type=int, default=1)
    sp.add_argument("--cpu-to", type=int, default=100)
    sp.add_argument("--mem-from-gb", type=int, default=1, help="min memory in GB")
    sp.add_argument("--mem-to-gb", type=int, default=256, help="max memory in GB")
    sp.add_argument("--price-from", type=int, default=0, help="min price, units of 0.001 CNY/hr")
    sp.add_argument("--price-to", type=int, default=100000, help="max price, units of 0.001 CNY/hr")
    sp.add_argument("--cmd", default="sleep infinity", help="container startup command")
    sp.add_argument("--region-sign", help="optional region constraint")
    sp.add_argument("--spec-file", help="send this JSON file as the request body verbatim")
    add_confirm(sp)
    sp.set_defaults(func=cmd_deploy_create)

    # deploy-list
    sp = sub.add_parser("deploy-list", help="list your deployments")
    add_page(sp)
    sp.set_defaults(func=cmd_deploy_list)

    # deploy-stop
    sp = sub.add_parser("deploy-stop", help="stop all containers in a deployment")
    sp.add_argument("--deployment-uuid", required=True)
    add_confirm(sp)
    sp.set_defaults(func=cmd_deploy_stop)

    # deploy-delete
    sp = sub.add_parser("deploy-delete", help="delete a deployment (auto-stops first)")
    sp.add_argument("--deployment-uuid", required=True)
    add_confirm(sp)
    sp.set_defaults(func=cmd_deploy_delete)

    # deploy-scale
    sp = sub.add_parser("deploy-scale", help="set ReplicaSet replica count")
    sp.add_argument("--deployment-uuid", required=True)
    sp.add_argument("--replica-num", type=int, required=True)
    add_confirm(sp)
    sp.set_defaults(func=cmd_deploy_scale)

    # container-list
    sp = sub.add_parser("container-list", help="list containers (SSH info is in .info)")
    sp.add_argument("--deployment-uuid", required=True)
    sp.add_argument("--container-uuid", default="")
    sp.add_argument("--gpu-name", default="")
    sp.add_argument("--released", action="store_true", help="include released/finished containers")
    add_page(sp)
    sp.set_defaults(func=cmd_container_list)

    # container-stop
    sp = sub.add_parser("container-stop", help="stop one container")
    sp.add_argument("--container-uuid", required=True, help="deployment_container_uuid")
    sp.add_argument("--decrease-replica", action="store_true",
                    help="also drop replica_num by one (else it gets rescheduled)")
    add_confirm(sp)
    sp.set_defaults(func=cmd_container_stop)

    # container-events
    sp = sub.add_parser("container-events", help="lifecycle events for a deployment/container")
    sp.add_argument("--deployment-uuid", required=True)
    sp.add_argument("--container-uuid", default="")
    add_page(sp)
    sp.set_defaults(func=cmd_container_events)

    # blacklist
    sp = sub.add_parser("blacklist", help="blacklist a container's host for 24h")
    sp.add_argument("--container-uuid", required=True, help="deployment_container_uuid")
    sp.add_argument("--comment", default="", help="reason note")
    add_confirm(sp)
    sp.set_defaults(func=cmd_blacklist)

    # wait-running
    sp = sub.add_parser("wait-running", help="poll until a container is running, emit its SSH info")
    sp.add_argument("--deployment-uuid", required=True)
    sp.add_argument("--timeout", dest="timeout", type=float, default=600,
                    help="max seconds to wait (default 600)")
    sp.add_argument("--interval", type=float, default=5, help="poll interval seconds (default 5)")
    sp.add_argument("--all", action="store_true", help="emit all running containers, not just the first")
    sp.set_defaults(func=cmd_wait_running)

    # raw
    sp = sub.add_parser("raw", help="call any endpoint directly")
    sp.add_argument("--method", required=True, help="GET/POST/PUT/DELETE")
    sp.add_argument("--path", required=True, help="e.g. /api/v1/dev/machine/gpu_stock")
    sp.add_argument("--body", help="JSON string request body")
    sp.set_defaults(func=cmd_raw)

    # save-token
    sp = sub.add_parser("save-token",
                        help="save a developer token to the token file (0600) for reuse")
    sp.add_argument("--token", help="the token; if omitted, read from stdin")
    sp.set_defaults(func=cmd_save_token)

    # token-status
    sp = sub.add_parser("token-status",
                        help="show whether/where a token is configured (never prints it)")
    sp.set_defaults(func=cmd_token_status)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
