"""Local operator CLI: native gws owns OAuth, tokens and refresh throughout."""
import argparse
import json
import subprocess

from . import router as R
from .account_backend import AccountError


def setup_parser(parser):
    commands = parser.add_subparsers(dest="gws_action", required=True)
    commands.add_parser("accounts", help="List this profile's native account slots")
    status = commands.add_parser("status", help="Check sanitized native account health")
    status.add_argument("account", type=_account)
    commands.add_parser("setup", help="Run native Google Cloud and Desktop OAuth client setup").add_argument(
        "account", type=_account)
    login = commands.add_parser("login", help="Run native browser consent for one account")
    login.add_argument("account", type=_account)
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


def _auth(backend, args):
    flags = []
    if args.gws_action == "login":
        for flag in ("readonly", "full"):
            if getattr(args, flag):
                flags.append("--" + flag)
        for flag in ("scopes", "services"):
            value = getattr(args, flag)
            if value is not None:
                if not value.strip() or "\0" in value:
                    raise ValueError("Scope and service lists must be nonempty")
                flags.extend(["--" + flag, value])
    with backend.auth_process(args.account, args.gws_action, flags) as (command, options, verify):
        print(f"Native gws {args.gws_action} for {args.account}: {options['env']['GOOGLE_WORKSPACE_CLI_CONFIG_DIR']}\n"
              "The account is blocked until verified. Native changes cannot be rolled back.", flush=True)
        result = subprocess.run(command, **options, timeout=600)
        if result.returncode:
            return {"ok": False, "error": "Native auth did not complete. Check its terminal instructions."}
        return verify()


def run(args, get_backend):
    """Returns an exit status; injected callbacks retain profile and watch guards."""
    try:
        backend = get_backend()
        if args.gws_action == "accounts":
            result = {"ok": True, "accounts": R.list_accounts(backend.root)}
        elif args.gws_action == "status":
            result = backend.status(args.account)
        elif args.gws_action in ("login", "setup"):
            result = _auth(backend, args)
        else:
            result = backend.logout(args.account)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted; incomplete native changes are not rolled back."}))
        return 130
    except FileNotFoundError:
        result = {"ok": False, "error": "Missing native executable. Install gws on PATH or configure plugins.entries.google-workspace.settings.gws_bin."}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "Native gws timed out; incomplete native changes are not rolled back."}
    except AccountError as exc:
        message = {"identity_mismatch": "The authorized Google identity does not match the requested account.",
                   "unsupported_lock_backend": "Managed native auth requires POSIX process locks.",
                   "identity_unconfirmed": "Identity not confirmed. Include email/userinfo or a suitable Gmail scope.",
                   "credentials_in_use": "Stop this account's watches and finish cleanup before login/logout.",
                   "logout_required": "Existing native token cache or plaintext fallback: explicitly log out before changing identity.",
                   "setup_incomplete": "Native setup did not configure a Desktop client."}
        result = {"ok": False, "error_code": exc.reason, "error": message.get(exc.reason, "Account operation refused.")}
    except TimeoutError:
        result = {"ok": False, "error_code": "account_busy", "error": "Account lifecycle is busy; finish or cancel the active operation, then retry."}
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
