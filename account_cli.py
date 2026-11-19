"""Local operator CLI: native gws owns OAuth, tokens and refresh throughout."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import uuid

from . import router as R
from .account_lifecycle import lifecycle_guard


def setup_parser(parser):
    commands = parser.add_subparsers(dest="gws_action", required=True)
    commands.add_parser("accounts", help="List this profile's native account slots")
    status = commands.add_parser("status", help="Check sanitized native account health")
    status.add_argument("account", type=_account)
    login = commands.add_parser("login", help="Run native browser consent for one account")
    login.add_argument("account", type=_account)
    login.add_argument("--client-file", type=Path, help="Downloaded Desktop OAuth client JSON (not tokens)")
    scopes = login.add_mutually_exclusive_group()
    scopes.add_argument("--readonly", action="store_true", help="Native read-only scope preset")
    scopes.add_argument("--full", action="store_true", help="Native full scope preset, including cloud scopes")
    scopes.add_argument("--scopes", help="Native comma-separated custom scopes; include email identity scope")
    login.add_argument("--services", help="Native comma-separated service scope-picker filter")
    commands.add_parser("logout", help="Clear only this account's local native credentials").add_argument(
        "account", type=_account)


def _account(value):
    if not R._ACCOUNT.fullmatch(value) or "@" not in value:
        raise argparse.ArgumentTypeError("Use the Google account email, not a path or alias")
    return value.lower()


def _client_bytes(path):
    # Only Desktop client configuration is read, never native credentials or keys.
    path = R._safe_path(path.expanduser())
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Desktop client must be a regular, unlinked file")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Desktop client JSON is too large")
    parsed = json.loads(data)
    client = parsed.get("installed") if isinstance(parsed, dict) else None
    if not isinstance(client, dict) or not all(isinstance(client.get(k), str) and client[k]
                                              for k in ("client_id", "client_secret")):
        raise ValueError("Use a Desktop OAuth client JSON with client_id and client_secret")
    return data


def _login(root, args, binary):
    destination = R._safe_path(root / "accounts" / args.account)
    source = args.client_file
    if source is None:
        source = destination / "client_secret.json"
        if not source.exists() and not source.is_symlink():
            source = root / "client_secret.json"
    data = _client_bytes(source)
    # Stage native login so cancellation, failure or wrong identity preserves the old slot.
    with tempfile.TemporaryDirectory(prefix=".native-login-", dir=root) as directory:
        staging = Path(directory)
        slot = staging / "accounts" / args.account
        slot.mkdir(mode=0o700, parents=True)
        client = slot / "client_secret.json"
        with client.open("xb") as stream:
            os.chmod(client, 0o600)
            stream.write(data)
        argv = ["auth", "login"]
        for flag in ("readonly", "full"):
            if getattr(args, flag):
                argv.append("--" + flag)
        for flag in ("scopes", "services"):
            value = getattr(args, flag)
            if value is not None:
                if not value.strip() or "\0" in value:
                    raise ValueError("Scope and service lists must be nonempty")
                argv.extend(["--" + flag, value])
        print("Complete native gws consent in this terminal/browser. Existing credentials stay unchanged until identity is verified.", flush=True)
        with R._native_guard(staging, args.account):
            result = R._run_gws(staging, slot, argv, gws_bin=binary, timeout=600, capture_output=False)
        if result.returncode:
            return {"ok": False, "error": "Native login did not complete; existing account unchanged."}
        identity = R.account_identity(staging, args.account, gws_bin=binary)
        if not identity["ok"]:
            return identity
        with R._commit_lock(root / "accounts"):
            R.account_dir(staging, args.account)
            destination = R._safe_path(destination)
            backup = destination.parent / ("." + args.account + ".backup-" + uuid.uuid4().hex)
            had_slot = destination.exists()
            if had_slot:
                if not destination.is_dir():
                    raise ValueError("Account slot is not a directory")
                os.rename(destination, backup)
            try:
                os.rename(slot, destination)
            except BaseException:
                if had_slot:
                    os.rename(backup, destination)
                raise
            if had_slot:
                # If cleanup fails, retain the private backup for operator recovery.
                try:
                    shutil.rmtree(backup)
                except OSError:
                    return {"ok": True, "account": args.account, "state": "authorized",
                            "warning": "Old account backup remains; remove it locally after verification."}
        return {"ok": True, "account": args.account, "state": "authorized"}


def run(args, *, root, gws_bin, check_lifecycle):
    """Returns an exit status; injected callbacks retain profile and watch guards."""
    try:
        root = R._safe_path(root())
        if args.gws_action == "accounts":
            result = {"ok": True, "accounts": R.list_accounts(root)}
        else:
            binary = gws_bin()
            if not isinstance(binary, str) or not binary or shutil.which(binary) is None:
                raise FileNotFoundError
            if args.gws_action == "status":
                health = R.account_health(root, args.account, gws_bin=binary)
                result = {"ok": health.get("valid") is True, "account": args.account, "health": health}
            else:
                with lifecycle_guard(root, args.account):
                    state = check_lifecycle({"account": args.account, "check_lifecycle": True})
                    if (state.get("ok") is not True or state.get("contract") != "gws-account-lifecycle-v1"
                            or state.get("account") != args.account
                            or state.get("credentials_in_use") is not False):
                        result = {"ok": False, "error": "Stop this account's watches and finish cleanup before login/logout."}
                    elif args.gws_action == "login":
                        result = _login(root, args, binary)
                    else:
                        with R._native_guard(root, args.account):
                            slot = R.account_dir(root, args.account)
                            native = R._run_gws(root, slot, ["auth", "logout"], gws_bin=binary, timeout=30)
                            if native.returncode:
                                result = R.classify_gws_failure(native.returncode, native.stdout, native.stderr)
                            else:
                                remaining = any((slot / name).exists() for name in ("credentials.enc", "credentials.json"))
                                result = {"ok": not remaining, "account": args.account,
                                          "state": "logout_unconfirmed" if remaining else "logged_out"}
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted; incomplete login was not activated."}))
        return 130
    except FileNotFoundError:
        result = {"ok": False, "error": "Missing gws executable or Desktop client file. Configure plugins.entries.google-workspace.settings.gws_bin or supply --client-file."}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "Native gws timed out; incomplete login was not activated."}
    except (OSError, ValueError):
        result = {"ok": False, "error": "Account operation failed safely. Check account name, paths, permissions and Desktop client JSON."}
    except Exception:
        result = {"ok": False, "error": "Account operation refused; check active profile and lifecycle availability."}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def register_cli(ctx, root, check_lifecycle):
    def handler(args):
        raise SystemExit(run(args, root=root,
                             gws_bin=lambda: ctx.get_config("gws_bin", default="gws"),
                             check_lifecycle=check_lifecycle))
    ctx.register_cli_command("gws", help="Manage profile-scoped native Google accounts",
                             setup_fn=setup_parser, handler_fn=handler)
