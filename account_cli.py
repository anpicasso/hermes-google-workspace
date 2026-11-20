"""Local operator CLI: native gws owns OAuth, tokens and refresh throughout."""
import argparse
import json
from pathlib import Path
import subprocess

from . import router as R
from .account_backend import AccountError


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


def _login(backend, args, binary):
    # Stage native login so cancellation, failure or wrong identity preserves the old slot.
    epoch = backend.begin(args.account)
    with backend.stage(args.account, args.client_file) as (staging, _):
        argv = []
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
        with backend.login_process(staging, args.account, argv, binary=binary) as (command, env, cwd):
            result = subprocess.run(command, env=env, cwd=cwd, timeout=600)
        if result.returncode:
            return {"ok": False, "error": "Native login did not complete; existing account unchanged."}
        return backend.activate(staging, args.account, binary=binary, epoch=epoch)


def run(args, get_backend):
    """Returns an exit status; injected callbacks retain profile and watch guards."""
    try:
        backend = get_backend()
        if args.gws_action == "accounts":
            result = {"ok": True, "accounts": R.list_accounts(backend.root)}
        elif args.gws_action == "status":
            result = backend.status(args.account)
        elif args.gws_action == "login":
            result = _login(backend, args, backend.binary())
        else:
            result = backend.logout(args.account)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted; incomplete login was not activated."}))
        return 130
    except FileNotFoundError:
        result = {"ok": False, "error": "Missing gws executable or Desktop client file. Configure plugins.entries.google-workspace.settings.gws_bin or supply --client-file."}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "Native gws timed out; incomplete login was not activated."}
    except AccountError as exc:
        message = {"identity_mismatch": "The authorized Google identity does not match the requested account.",
                   "identity_unconfirmed": "Identity not confirmed. Include email/userinfo or a suitable Gmail scope.",
                   "credentials_in_use": "Stop this account's watches and finish cleanup before login/logout.",
                   "client_missing": "Supply a Desktop client JSON with --client-file.",
                   "client_invalid": "Supply a readable, unlinked Desktop OAuth client JSON.",
                   "cancelled": "Account logout invalidated this login; credentials were not activated."}
        result = {"ok": False, "error_code": exc.reason, "error": message.get(exc.reason, "Account operation refused.")}
    except (OSError, ValueError):
        result = {"ok": False, "error": "Account operation failed safely. Check account name, paths, permissions and Desktop client JSON."}
    except Exception:
        result = {"ok": False, "error": "Account operation refused; check active profile and lifecycle availability."}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def register_cli(ctx, get_backend):
    def handler(args):
        raise SystemExit(run(args, get_backend))
    ctx.register_cli_command("gws", help="Manage profile-scoped native Google accounts",
                             setup_fn=setup_parser, handler_fn=handler)
