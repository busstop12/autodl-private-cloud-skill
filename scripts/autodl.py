#!/usr/bin/env python3
"""
autodl.py — CLI wrapper for the AutoDL private-cloud (ESD) developer API.

One command per endpoint plus a few convenience commands for automating
research-experiment lifecycles (launch GPU container -> wait for it to come up
-> read SSH connection info -> tear it down so billing stops).

Auth & target resolve in this order (first hit wins):
  developer token: --token  >  $AUTODL_TOKEN  >  encrypted store  >  legacy token file
  base URL:        --base-url  >  $AUTODL_BASE_URL  >  https://private.autodl.com

SECURE MODE (recommended). The token is stored ENCRYPTED at rest, protected by a
password you choose. It is set through a native GUI dialog (no pasting into a
terminal or agent), so the secret never appears in argv, logs, or any AI context:
  autodl.py set-token        # GUI: type/paste token + choose password, encrypted (0600)
Each API call then pops a GUI password prompt; on the right password the token is
decrypted IN MEMORY, used for that one request, and never printed. To avoid
re-typing during a multi-step run, a successful unlock is cached (machine-bound,
encrypted, 0600) for an unlock TTL (default 300s; --unlock-ttl / $AUTODL_UNLOCK_TTL,
0 = never cache). Clear it anytime with:  autodl.py lock . Rotate the password
with:  autodl.py change-password . Encrypted store lives at $AUTODL_CREDENTIALS_FILE
or ~/.config/autodl/credentials.enc and needs the `cryptography` package.

LEGACY MODE. The plaintext token file lets you save the token unencrypted instead
of re-exporting it each session. Default $AUTODL_TOKEN_FILE or ~/.config/autodl/token
(0600). Save one with:  autodl.py save-token --token <T>  (or pipe it on stdin).
Check what's configured — without revealing it — with: token-status.
Get a token from Console -> 账号设置 -> 开发者Token -> 新增Token (Account Settings -> Developer Token).

Every endpoint returns the envelope {"code","msg","data"}. This tool checks
code == "Success"; on anything else it prints msg to stderr and exits 1.
By default it prints the `data` field as pretty JSON so output is easy to parse.

Run `autodl.py <command> -h` for per-command options, or `autodl.py -h` for the
full command list.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import platform
import subprocess
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


# --------------------------------------------------------------------------- #
# Encrypted credential store (token encrypted at rest, password-protected)     #
# --------------------------------------------------------------------------- #
# The token is sealed with Fernet (AES-128-CBC + HMAC-SHA256) under a key derived
# from the user's password via scrypt. Setting and unlocking both happen through a
# native GUI dialog, so the plaintext token and the password never pass through
# argv, stdout, environment, or any AI/agent context — only the API's JSON result
# is ever printed. A successful unlock is briefly cached, encrypted under a
# machine-bound key, so a multi-step run prompts once rather than per call.

SCRYPT_N = 1 << 14          # 16384 — CPU/memory cost
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
DEFAULT_UNLOCK_TTL = 300    # seconds an unlock stays valid (0 = never cache)


class _GuiUnavailable(Exception):
    """Raised when a GUI backend can't be reached, so we can try the next one."""


def config_dir():
    base = os.environ.get("AUTODL_CONFIG_DIR")
    if base:
        return os.path.expanduser(base)
    return os.path.join(os.path.expanduser("~"), ".config", "autodl")


def credentials_path():
    """Where the encrypted token lives: $AUTODL_CREDENTIALS_FILE or <config>/credentials.enc."""
    env = os.environ.get("AUTODL_CREDENTIALS_FILE")
    if env:
        return os.path.expanduser(env)
    return os.path.join(config_dir(), "credentials.enc")


def unlock_cache_path():
    return os.path.join(config_dir(), "unlock_cache.enc")


def machine_key_path():
    return os.path.join(config_dir(), ".machine_key")


def _write_private(path, data_bytes):
    """Write bytes to path with owner-only (0600) perms."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data_bytes)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _require_cryptography():
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        sys.exit("ERROR: encrypted credentials need the `cryptography` package. "
                 "Install with: pip install cryptography")
    return Fernet, InvalidToken


def _fernet_key_from_password(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN):
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                        dklen=dklen, maxmem=SCRYPT_MAXMEM)
    return base64.urlsafe_b64encode(dk)


def save_credentials(token, password):
    """Encrypt the token under the password and persist it (0600). Returns the path."""
    token = token.strip()
    if not token:
        sys.exit("ERROR: refusing to save an empty token.")
    if not password:
        sys.exit("ERROR: refusing to save with an empty password.")
    Fernet, _ = _require_cryptography()
    salt = os.urandom(16)
    key = _fernet_key_from_password(password, salt)
    ciphertext = Fernet(key).encrypt(token.encode("utf-8")).decode("ascii")
    blob = {
        "version": 1, "kdf": "scrypt", "cipher": "fernet",
        "salt": base64.b64encode(salt).decode("ascii"),
        "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN,
        "ciphertext": ciphertext,
    }
    path = credentials_path()
    _write_private(path, json.dumps(blob).encode("utf-8"))
    return path


def decrypt_credentials(password):
    """Return the plaintext token, or raise ValueError on wrong password / bad file."""
    Fernet, InvalidToken = _require_cryptography()
    try:
        with open(credentials_path(), "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, IOError, ValueError) as e:
        raise ValueError(f"cannot read encrypted credentials: {e}")
    try:
        salt = base64.b64decode(blob["salt"])
        key = _fernet_key_from_password(
            password, salt, blob.get("n", SCRYPT_N), blob.get("r", SCRYPT_R),
            blob.get("p", SCRYPT_P), blob.get("dklen", SCRYPT_DKLEN))
        token = Fernet(key).decrypt(blob["ciphertext"].encode("ascii"))
    except (KeyError, ValueError):
        raise ValueError("corrupted credentials file")
    except InvalidToken:
        raise ValueError("wrong password")
    return token.decode("utf-8")


# --- machine-bound unlock cache: skip re-prompting for a short TTL ---------- #
def _machine_key():
    """A stable, local-only key for the unlock cache. A random 32-byte secret stored
    0600, mixed with host/user identity so a copied cache file won't decrypt elsewhere."""
    path = machine_key_path()
    secret = None
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 32:
            secret = data
    except (OSError, IOError):
        secret = None
    if secret is None:
        secret = os.urandom(32)
        _write_private(path, secret)
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    binding = f"{platform.node()}|{user}|{os.path.expanduser('~')}".encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(secret + binding).digest())


def cache_unlock(token, ttl):
    if ttl <= 0:
        return
    Fernet, _ = _require_cryptography()
    ciphertext = Fernet(_machine_key()).encrypt(token.encode("utf-8")).decode("ascii")
    blob = {"expires_at": time.time() + ttl, "ciphertext": ciphertext}
    _write_private(unlock_cache_path(), json.dumps(blob).encode("utf-8"))


def read_unlock_cache():
    """Return (token, seconds_remaining) for a valid, unexpired cache, else (None, 0)."""
    try:
        with open(unlock_cache_path(), "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, IOError, ValueError):
        return None, 0
    remaining = blob.get("expires_at", 0) - time.time()
    if remaining <= 0:
        clear_unlock_cache()
        return None, 0
    try:
        Fernet, InvalidToken = _require_cryptography()
        token = Fernet(_machine_key()).decrypt(blob["ciphertext"].encode("ascii")).decode("utf-8")
    except Exception:
        # Unreadable cache (rotated machine key, tamper) -> force a fresh unlock.
        return None, 0
    return token, int(remaining)


def clear_unlock_cache():
    try:
        os.remove(unlock_cache_path())
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Native GUI dialogs (tkinter primary; osascript / PowerShell fallbacks)       #
# --------------------------------------------------------------------------- #
# These collect the token/password directly from the human at the keyboard. The
# entered text stays inside this process and is used only to (de)serialize the
# encrypted store — it is never echoed to stdout, so it can't reach the agent.

def _bring_process_to_front():
    """Make THIS process the active application so its Tk window gets keyboard focus.

    macOS-specific: a Tk window launched from a terminal/agent process opens in the
    background and never becomes the active app, so even `-topmost` + `focus_force`
    leave it behind other windows without focus. Prefer Cocoa (no permission prompt);
    fall back to AppleScript activation by PID."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return
    except Exception:
        pass
    try:
        script = ("tell application \"System Events\" to set frontmost of "
                  "(first process whose unix id is %d) to true" % os.getpid())
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
    except Exception:
        pass


def _tk_grab_focus(root):
    """Raise the window, activate the app, and put keyboard focus on the input field."""
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        _bring_process_to_front()
        root.focus_force()
        widget = getattr(root, "_focus_widget", None)
        if widget is not None:
            widget.focus_set()
    except Exception:
        pass


def _tk_root(title):
    import tkinter as tk
    try:
        root = tk.Tk()
    except Exception as e:  # no display / Tk not functional
        raise _GuiUnavailable(str(e))
    root.title(title)
    root.attributes("-topmost", True)
    # Activate + focus once the event loop starts. Schedule twice: the first
    # activation can land before the window is fully mapped on screen.
    root.after(20, lambda: _tk_grab_focus(root))
    root.after(200, lambda: _tk_grab_focus(root))
    return tk, root


def _tk_password(title, message):
    try:
        import tkinter  # noqa: F401
    except Exception as e:
        raise _GuiUnavailable(str(e))
    tk, root = _tk_root(title)
    result = {"value": None}
    tk.Label(root, text=message, justify="left", wraplength=360).pack(padx=18, pady=(16, 6))
    var = tk.StringVar()
    entry = tk.Entry(root, show="•", textvariable=var, width=38)
    entry.pack(padx=18)
    entry.focus_set()
    root._focus_widget = entry
    show = tk.BooleanVar(value=False)

    def toggle():
        entry.config(show="" if show.get() else "•")
    tk.Checkbutton(root, text="显示", variable=show, command=toggle).pack(anchor="w", padx=16)

    def submit(_=None):
        result["value"] = var.get()
        root.destroy()

    def cancel(_=None):
        result["value"] = None
        root.destroy()
    btns = tk.Frame(root)
    btns.pack(pady=12)
    tk.Button(btns, text="确定", width=10, command=submit).pack(side="left", padx=6)
    tk.Button(btns, text="取消", width=10, command=cancel).pack(side="left", padx=6)
    root.bind("<Return>", submit)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result["value"]


def _tk_set_credentials(existing, want_token=True):
    try:
        import tkinter  # noqa: F401
    except Exception as e:
        raise _GuiUnavailable(str(e))
    tk, root = _tk_root("AutoDL — 设置加密 Token")
    result = {"ok": False, "token": None, "password": None}
    intro = ("更新加密 Token / 密码。" if existing else "首次设置：加密保存你的开发者 Token。")
    tk.Label(root, text=intro, justify="left", wraplength=380).pack(padx=18, pady=(16, 8))
    frm = tk.Frame(root)
    frm.pack(padx=18)
    token_var, pw_var, pw2_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    show = tk.BooleanVar(value=False)
    rows = []
    if want_token:
        rows.append(("开发者 Token：", token_var, False))
    rows.append(("加密密码：", pw_var, True))
    rows.append(("确认密码：", pw2_var, True))
    entries = []
    for i, (label, var, _mask) in enumerate(rows):
        tk.Label(frm, text=label, anchor="e", width=12).grid(row=i, column=0, sticky="e", pady=4)
        e = tk.Entry(frm, textvariable=var, show="•", width=40)
        e.grid(row=i, column=1, pady=4)
        entries.append(e)
    if entries:
        entries[0].focus_set()
        root._focus_widget = entries[0]

    def toggle():
        for e in entries:
            e.config(show="" if show.get() else "•")
    tk.Checkbutton(root, text="显示输入", variable=show, command=toggle).pack(anchor="w", padx=16, pady=(6, 0))
    err = tk.Label(root, text="", fg="#c0392b", wraplength=380)
    err.pack(padx=18)

    def submit(_=None):
        token = token_var.get().strip() if want_token else ""
        pw, pw2 = pw_var.get(), pw2_var.get()
        if want_token and not token:
            err.config(text="Token 不能为空"); return
        if not pw:
            err.config(text="密码不能为空"); return
        if pw != pw2:
            err.config(text="两次输入的密码不一致"); return
        result.update(ok=True, token=token, password=pw)
        root.destroy()

    def cancel(_=None):
        result["ok"] = False
        root.destroy()
    btns = tk.Frame(root)
    btns.pack(pady=14)
    tk.Button(btns, text="保存", width=10, command=submit).pack(side="left", padx=6)
    tk.Button(btns, text="取消", width=10, command=cancel).pack(side="left", padx=6)
    root.bind("<Return>", submit)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result


# --- osascript (macOS) fallback -------------------------------------------- #
def _as_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript_password(title, message):
    script = ("display dialog " + _as_str(message) + " with title " + _as_str(title)
              + ' default answer "" with hidden answer'
              + ' buttons {"取消", "确定"} default button "确定"')
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        raise _GuiUnavailable("osascript unavailable")
    if out.returncode != 0:
        return None  # user cancelled
    text = out.stdout
    marker = "text returned:"
    i = text.find(marker)
    return text[i + len(marker):].rstrip("\n") if i >= 0 else ""


def _osascript_set_credentials(existing, want_token=True):
    token = ""
    if want_token:
        token = _osascript_password("AutoDL — 设置 Token", "粘贴开发者 Token：")
        if token is None:
            return {"ok": False}
        token = token.strip()
        if not token:
            sys.exit("ERROR: token 为空。")
    pw = _osascript_password("AutoDL — 设置密码", "设置用于加密的密码：")
    if pw is None:
        return {"ok": False}
    pw2 = _osascript_password("AutoDL — 确认密码", "再次输入密码确认：")
    if pw2 is None:
        return {"ok": False}
    if pw != pw2:
        sys.exit("ERROR: 两次密码不一致。")
    return {"ok": True, "token": token, "password": pw}


# --- PowerShell (Windows) fallback ----------------------------------------- #
def _ps_str(s):
    return "'" + s.replace("'", "''") + "'"


def _powershell_password(title, message):
    script = f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$f=New-Object Windows.Forms.Form; $f.Text={_ps_str(title)}; $f.Width=400; $f.Height=180; $f.TopMost=$true; $f.StartPosition='CenterScreen'
$l=New-Object Windows.Forms.Label; $l.Text={_ps_str(message)}; $l.AutoSize=$true; $l.Top=15; $l.Left=15; $f.Controls.Add($l)
$t=New-Object Windows.Forms.TextBox; $t.UseSystemPasswordChar=$true; $t.Top=55; $t.Left=15; $t.Width=355; $f.Controls.Add($t)
$ok=New-Object Windows.Forms.Button; $ok.Text='OK'; $ok.Top=95; $ok.Left=200; $ok.DialogResult='OK'; $f.Controls.Add($ok); $f.AcceptButton=$ok
$cn=New-Object Windows.Forms.Button; $cn.Text='Cancel'; $cn.Top=95; $cn.Left=290; $cn.DialogResult='Cancel'; $f.Controls.Add($cn); $f.CancelButton=$cn
if($f.ShowDialog() -eq 'OK'){{[Console]::Out.Write($t.Text)}}else{{exit 1}}
"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                             capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        raise _GuiUnavailable("powershell unavailable")
    if out.returncode != 0:
        return None
    return out.stdout


def _powershell_set_credentials(existing, want_token=True):
    token = ""
    if want_token:
        token = _powershell_password("AutoDL — 设置 Token", "粘贴开发者 Token：")
        if token is None:
            return {"ok": False}
        token = token.strip()
        if not token:
            sys.exit("ERROR: token 为空。")
    pw = _powershell_password("AutoDL — 设置密码", "设置用于加密的密码：")
    if pw is None:
        return {"ok": False}
    pw2 = _powershell_password("AutoDL — 确认密码", "再次输入密码确认：")
    if pw2 is None:
        return {"ok": False}
    if pw != pw2:
        sys.exit("ERROR: 两次密码不一致。")
    return {"ok": True, "token": token, "password": pw}


def gui_prompt_password(title, message):
    """Pop a native password dialog; return the typed string, or None if cancelled."""
    try:
        return _tk_password(title, message)
    except _GuiUnavailable:
        pass
    if sys.platform == "darwin":
        return _osascript_password(title, message)
    if os.name == "nt":
        return _powershell_password(title, message)
    sys.exit("ERROR: no GUI available to prompt for the password (tkinter missing and no "
             "platform fallback). Install python tkinter, or run on the desktop session.")


def gui_set_credentials(existing, want_token=True):
    """Pop a native dialog to collect token+password (or just password). Returns
    {'ok': bool, 'token': str, 'password': str}."""
    try:
        return _tk_set_credentials(existing, want_token)
    except _GuiUnavailable:
        pass
    if sys.platform == "darwin":
        return _osascript_set_credentials(existing, want_token)
    if os.name == "nt":
        return _powershell_set_credentials(existing, want_token)
    sys.exit("ERROR: no GUI available to set credentials (tkinter missing and no platform "
             "fallback). Install python tkinter, or run on the desktop session.")


# --------------------------------------------------------------------------- #
# Token resolution                                                            #
# --------------------------------------------------------------------------- #
def _unlock_ttl(args):
    v = getattr(args, "unlock_ttl", None)
    if v is None:
        v = os.environ.get("AUTODL_UNLOCK_TTL")
    if v is None:
        return DEFAULT_UNLOCK_TTL
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return DEFAULT_UNLOCK_TTL


def unlock_token(args):
    """Decrypt the stored token via GUI password prompt (or a valid unlock cache).
    The token is returned in memory and is never printed."""
    no_cache = getattr(args, "no_cache", False)
    ttl = _unlock_ttl(args)
    if not no_cache and ttl > 0:
        cached, _ = read_unlock_cache()
        if cached:
            return cached
    op = getattr(args, "command", "operation") or "operation"
    for attempt in range(1, 4):
        suffix = "" if attempt == 1 else f"\n（密码错误，重试 {attempt}/3）"
        pw = gui_prompt_password(
            "AutoDL — 解锁 Token",
            f"输入密码以授权执行操作：{op}{suffix}")
        if pw is None:
            sys.exit("ERROR: password entry cancelled; operation aborted.")
        try:
            token = decrypt_credentials(pw)
        except ValueError as e:
            if "wrong password" not in str(e):
                sys.exit(f"ERROR: {e}")
            continue
        if not no_cache and ttl > 0:
            cache_unlock(token, ttl)
        return token
    sys.exit("ERROR: wrong password (3 attempts); operation aborted.")


def resolve_token(args):
    """Token from --token, then $AUTODL_TOKEN, then the encrypted store (GUI password
    prompt / unlock cache), then the legacy plaintext token file."""
    flag = getattr(args, "token", None)
    if flag:
        return flag
    env = os.environ.get("AUTODL_TOKEN")
    if env:
        return env
    if os.path.exists(credentials_path()):
        return unlock_token(args)
    return read_token_file()


def token_source(args):
    """Human-readable origin of the active token, without revealing its value."""
    if getattr(args, "token", None):
        return "--token flag"
    if os.environ.get("AUTODL_TOKEN"):
        return "$AUTODL_TOKEN env var"
    if os.path.exists(credentials_path()):
        return f"encrypted store ({credentials_path()})"
    if read_token_file():
        return f"legacy token file ({token_file_path()})"
    return None


# --------------------------------------------------------------------------- #
# Low-level HTTP                                                               #
# --------------------------------------------------------------------------- #
def api_request(args, method, path, body=None):
    """Call the API and return the unwrapped `data` field, or exit non-zero."""
    token = resolve_token(args)
    if not token:
        sys.exit("ERROR: no token. Set one securely (encrypted, GUI) with "
                 "`autodl.py set-token`, or provide --token / $AUTODL_TOKEN. "
                 "Get a token from Console -> 账号设置 -> 开发者Token -> 新增Token (Account Settings -> Developer Token).")
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


def cmd_set_token(args):
    """Set the encrypted token through a native GUI dialog.

    Both the token and the password are typed into the dialog by the human — they
    never pass through argv, stdin, the environment, or any agent context. The token
    is sealed with Fernet under a scrypt-derived key and written 0600."""
    existing = os.path.exists(credentials_path())
    res = gui_set_credentials(existing, want_token=True)
    if not res.get("ok"):
        sys.exit("ERROR: cancelled; no credentials saved.")
    path = save_credentials(res["token"], res["password"])
    clear_unlock_cache()  # force a fresh unlock under the new password
    emit({
        "saved": True, "path": path, "permissions": "0600",
        "encryption": "Fernet(AES-128-CBC+HMAC) / scrypt-derived key",
        "note": "Token encrypted at rest. Each operation pops a password prompt; on the "
                "correct password the token is decrypted in memory for that one request "
                "and never printed. Tip: unset $AUTODL_TOKEN and remove any legacy plaintext "
                "token file so secure mode is actually used.",
    })


def cmd_change_password(args):
    """Re-encrypt the stored token under a new password (prompts old, then new)."""
    if not os.path.exists(credentials_path()):
        sys.exit("ERROR: no encrypted credentials to change. Run `set-token` first.")
    old = gui_prompt_password("AutoDL — 修改密码", "输入当前密码以解锁：")
    if old is None:
        sys.exit("ERROR: cancelled.")
    try:
        token = decrypt_credentials(old)
    except ValueError as e:
        sys.exit(f"ERROR: {e}.")
    res = gui_set_credentials(existing=True, want_token=False)
    if not res.get("ok"):
        sys.exit("ERROR: cancelled; password unchanged.")
    path = save_credentials(token, res["password"])
    clear_unlock_cache()
    emit({"changed": True, "path": path,
          "note": "Re-encrypted under the new password. Unlock cache cleared."})


def cmd_lock(args):
    """Clear the unlock cache so the next operation prompts for the password again."""
    clear_unlock_cache()
    emit({"locked": True,
          "note": "Unlock cache cleared. The next operation will prompt for your password."})


def cmd_token_status(args):
    """Report whether a token is configured and from where — never prints the token."""
    src = token_source(args)
    enc_exists = os.path.exists(credentials_path())
    unlocked, remaining = (False, 0)
    if enc_exists:
        cached, remaining = read_unlock_cache()
        unlocked = cached is not None
    emit({
        "configured": src is not None,
        "source": src,
        "encrypted_store": credentials_path(),
        "encrypted_store_exists": enc_exists,
        "unlocked": unlocked,
        "unlock_seconds_remaining": remaining,
        "legacy_token_file": token_file_path(),
        "legacy_token_file_exists": os.path.exists(token_file_path()),
        "hint": (None if src else
                 "No token found. Get one from Console -> 账号设置 -> 开发者Token -> 新增Token (Account Settings -> Developer Token), "
                 "then set it securely with: autodl.py set-token"),
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
    p.add_argument("--unlock-ttl", type=int, default=None,
                   help="seconds a GUI unlock stays cached (default 300 / $AUTODL_UNLOCK_TTL; 0 = prompt every call)")
    p.add_argument("--no-cache", action="store_true",
                   help="never use/refresh the unlock cache; prompt for the password this call")
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

    # set-token (secure, GUI)
    sp = sub.add_parser("set-token",
                        help="set the ENCRYPTED token via a GUI dialog (recommended; no pasting into terminal/agent)")
    sp.set_defaults(func=cmd_set_token)

    # change-password
    sp = sub.add_parser("change-password",
                        help="re-encrypt the stored token under a new password (GUI)")
    sp.set_defaults(func=cmd_change_password)

    # lock
    sp = sub.add_parser("lock",
                        help="clear the unlock cache so the next op re-prompts for the password")
    sp.set_defaults(func=cmd_lock)

    # save-token (legacy, plaintext)
    sp = sub.add_parser("save-token",
                        help="LEGACY: save an UNENCRYPTED token to the token file (0600) for reuse")
    sp.add_argument("--token", help="the token; if omitted, read from stdin")
    sp.set_defaults(func=cmd_save_token)

    # token-status
    sp = sub.add_parser("token-status",
                        help="show whether/where a token is configured + unlock state (never prints it)")
    sp.set_defaults(func=cmd_token_status)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
