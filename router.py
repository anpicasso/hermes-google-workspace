"""Run gws using exactly one account under the current Hermes profile."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from contextlib import contextmanager

if __package__:
    from .api_policy import BLOCK, classify
else:
    from api_policy import BLOCK, classify

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


def _safe_path(path):
    """Reject symlinks before resolving, including links in parent directories."""
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Refusing a symlinked account path")
    return path.resolve()


def _open_lock(path):
    fd = os.open(_safe_path(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise ValueError("Refusing an unsafe account lock")
    return fd


@contextmanager
def _commit_lock(accounts, *, shared=False):
    """Serialize account swaps across threads and processes.

    Exclusive (default) is the profile-wide swap barrier used by OAuth installs.
    Native reads take it *shared* and then a per-account lock, so different
    accounts can run in parallel while an account swap still drains every reader
    before it renames a credential directory.

    ponytail: one profile-wide flock plus one lock per account; shard further only
    if profile-wide shared-lock contention is measurable.
    """
    accounts = _safe_path(accounts)
    accounts.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = _open_lock(accounts / ".lock")
    try:
        # Separate open descriptions serialize threads too. Never hold a Python
        # mutex across blocking flock: another thread needs to release its fd.
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            raise OSError("No supported credential lock backend")
        yield
    finally:
        # Closing also releases the OS lock, including on acquisition failure.
        os.close(fd)


@contextmanager
def _account_lock(root, account):
    """Cross-process lock for exactly one account, outside its credential dir."""
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise ValueError("Invalid account name")
    directory = _safe_path(Path(root) / ".locks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(account.encode("utf-8")).hexdigest()[:32]
    fd = _open_lock(directory / f"{name}.lock")
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            raise OSError("No supported credential lock backend")
        yield
    finally:
        os.close(fd)


@contextmanager
def _native_guard(root, account):
    """Shared swap barrier + one account lock for the whole native call."""
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise ValueError("Invalid account name")
    root = _safe_path(root)
    with _commit_lock(root / "accounts", shared=True):
        with _account_lock(root, account):
            yield


_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+-]{0,127}\Z")
_HTTP_STATUS = re.compile(r'"code"\s*:\s*(\d{3})')
_HTTP_STATUS_ANY = re.compile(r'(?:\bHTTP[ /]|\bstatus[=: ]{1,3})(\d{3})\b', re.IGNORECASE)
_HEALTH_TIMEOUT = 20
_REAUTH = ("Run gws auth login with this account's GOOGLE_WORKSPACE_CLI_CONFIG_DIR "
           "and the file keyring backend.")
# Scope URLs/names only; anything else in the status payload is never echoed.
_SCOPE = re.compile(r"(?:https://www\.googleapis\.com/auth/[A-Za-z0-9._/-]{1,120}|openid|email|profile)\Z")

# ponytail: fixed allowlist of native signals; never echo gws output, which may
# contain request bodies, paths, or token fragments.
_PERMISSION_MARKERS = (
    "permission_denied", "insufficientpermissions", "insufficient_permissions",
    "access_token_scope_insufficient", "accessnotconfigured", "service_disabled",
    "api is not enabled", "has not been used in project",
)


def account_dir(root, account):
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account) or account in (".", ".."):
        raise ValueError("Invalid account name")
    path = _safe_path(Path(root) / "accounts" / account)
    if not path.is_dir():
        raise ValueError("Account is not registered in this profile")
    # Inspect metadata only; native gws alone reads credentials, keys and caches.
    for name in ("credentials.enc", "credentials.json", ".encryption_key", "client_secret.json",
                 "token_cache.json", "sa_token_cache.json"):
        entry = path / name
        if entry.is_symlink():
            raise ValueError("Refusing a symlinked native account file")
        if entry.exists():
            info = entry.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Refusing an unsafe native account file")
    if not any((path / name).is_file() for name in ("credentials.enc", "credentials.json")):
        raise ValueError("Account is not registered in this profile")
    return path


def files_dir(root):
    """Controlled native CWD; relative files stay relative to root/files.

    gws main.rs calls dotenvy::dotenv() before dispatch. An empty .env stops
    its ancestor search; never truncate an existing file or follow a link.
    Absolute file arguments are passed unchanged; ~ is not shell-expanded.
    """
    try:
        path = _safe_path(Path(root) / "files")
        sentinel = _safe_path(path / ".env")
    except ValueError as exc:
        raise OSError("Refusing an unsafe native working directory") from exc
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    fd = os.open(sentinel, os.O_CREAT | os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
            raise OSError("Refusing an unsafe native dotenv sentinel")
    finally:
        os.close(fd)
    return path


def list_accounts(root):
    try:
        base = _safe_path(Path(root) / "accounts")
    except ValueError:
        return []
    if not base.is_dir():
        return []
    names = []
    for path in base.iterdir():
        try:
            account_dir(root, path.name)
        except ValueError:
            continue
        names.append(path.name)
    return sorted(names)


@contextmanager
def _account_env(path):
    """Keep native credentials persistent, but give each child a fresh HOME.

    gws may invoke gcloud, which creates ~/.config during status checks. Never
    reuse that HOME: it is an ADC fallback location even with CONFIG_DIR set.
    """
    path = _safe_path(path)
    with tempfile.TemporaryDirectory(prefix=".exec-home-", dir=path) as home:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": home,
               "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(path),
               "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file"}
        if os.name == "nt":  # pragma: no cover - Windows
            env["USERPROFILE"] = home
            if "SYSTEMROOT" in os.environ:
                env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        yield env


def _run_gws(root, path, args, *, gws_bin, timeout, capture_output=True):
    """Caller holds _native_guard. Resolve the CLI before changing child CWD."""
    binary = shutil.which(os.fspath(gws_bin))
    if binary is None:
        raise FileNotFoundError("gws executable not found")
    # absolute(), not resolve(): retain executable symlink/wrapper semantics.
    with _account_env(path) as env:
        return subprocess.run(
            [str(Path(binary).absolute()), *args], env=env, cwd=files_dir(root),
            capture_output=capture_output, text=True, timeout=timeout,
        )


def _failure(code, message, action, *, category, reauth_required, returncode, http_status=None):
    failure = {
        "error_code": code,
        "category": category,
        "error": message,
        "action": action,
        "reauth_required": reauth_required,
        "exit_code": returncode,
    }
    if http_status is not None:
        failure["http_status"] = int(http_status)
    return failure


def _http_status(text):
    match = _HTTP_STATUS.search(text) or _HTTP_STATUS_ANY.search(text)
    return int(match.group(1)) if match else None


def classify_gws_failure(returncode, stdout="", stderr=""):
    """Map a native gws failure to a fixed, actionable message.

    Raw stdout/stderr are inspected only for known signals and are never
    returned: they can contain request bodies, file paths, or token fragments.
    """
    text = (stdout + "\n" + stderr).lower()
    status = _http_status(stdout + "\n" + stderr)
    if returncode == 3:
        return _failure(
            "invalid_arguments",
            "gws rejected the arguments (syntax or validation); check the command and its options.",
            "check_args", category="syntax", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if re.search(r"\binvalid_rapt\b", text):
        return _failure(
            "invalid_rapt",
            "Google requires the account to reauthenticate (invalid_rapt). " + _REAUTH,
            "reauthorize", category="auth", reauth_required=True, returncode=returncode,
            http_status=status,
        )
    if re.search(r"\binvalid_grant\b", text):
        return _failure(
            "invalid_grant",
            "The Google authorization is no longer valid (invalid_grant). " + _REAUTH,
            "reauthorize", category="auth", reauth_required=True, returncode=returncode,
            http_status=status,
        )
    if any(marker in text for marker in _PERMISSION_MARKERS):
        return _failure(
            "permission_denied",
            "Google denied permission; check the granted scopes and that the required API is enabled.",
            "check_scopes_or_api", category="permission", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if status == 401:
        return _failure(
            "unauthenticated",
            "Google rejected authentication (HTTP 401). " + _REAUTH,
            "reauthorize", category="auth", reauth_required=True, returncode=returncode,
            http_status=status,
        )
    if status == 403:
        return _failure(
            "permission_denied",
            "Google denied permission (HTTP 403); check the granted scopes and that the required API is enabled.",
            "check_scopes_or_api", category="permission", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if status == 429:
        return _failure(
            "rate_limited",
            "Google rate-limited the request (HTTP 429); retry with backoff.",
            "retry", category="api", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if status == 410:
        return _failure(
            "gone",
            "The sync cursor expired (HTTP 410); a full resync is required.",
            "resync", category="api", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if returncode == 2:
        return _failure(
            "auth_unavailable",
            "gws could not authenticate; check account health and connectivity before reauthorizing.",
            "check_account_health", category="auth", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if returncode == 1:
        suffix = f" (HTTP {status})" if status else ""
        return _failure(
            "api_error",
            f"gws returned an API error{suffix}; check the arguments and the requested resource.",
            "check_request", category="api", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if returncode == 4:
        return _failure(
            "discovery_error",
            "Could not load the Google API schema; check the network or retry.",
            "retry_or_check_network", category="discovery", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    if returncode == 5:
        return _failure(
            "internal_error",
            "gws failed internally; retry the operation.",
            "retry", category="internal", reauth_required=False, returncode=returncode,
            http_status=status,
        )
    return _failure(
        "gws_error",
        f"gws exited with status {returncode}; the error could not be classified.",
        "inspect_native_gws", category="unknown", reauth_required=False, returncode=returncode,
        http_status=status,
    )


def account_health(root, account, *, gws_bin="gws", timeout=_HEALTH_TIMEOUT):
    """Ask native gws whether stored credentials still authenticate.

    This health path leaves decryption and refresh to native gws. Output is
    reduced to fixed states and messages.
    """
    try:
        with _native_guard(root, account):
            path = account_dir(root, account)
            result = _run_gws(root, path, ["auth", "status"], gws_bin=gws_bin, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"stored": True, "valid": None, "state": "stored", "reauth_required": False,
                "detail": "gws auth status timed out; the session was not validated live."}
    except OSError:
        return {"stored": True, "valid": None, "state": "stored", "reauth_required": False,
                "detail": "Could not run gws auth status; the session was not validated live."}
    if result.returncode:
        failure = classify_gws_failure(result.returncode, result.stdout, result.stderr)
        invalid = bool(failure["reauth_required"])
        return {"stored": True, "valid": False if invalid else None,
                "state": "invalid" if invalid else "stored",
                "reauth_required": invalid,
                "detail": failure["error"], "error_code": failure["error_code"],
                "action": failure["action"]}
    try:
        status = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        status = None
    if not isinstance(status, dict):
        return {"stored": True, "valid": None, "state": "stored", "reauth_required": False,
                "detail": "gws auth status returned no readable state; the session was not validated live."}
    health = _status_health(status, account)
    scopes = status.get("scopes")
    if isinstance(scopes, list):
        health["scopes"] = sorted({x for x in scopes if isinstance(x, str) and _SCOPE.fullmatch(x)})
    return health


def account_identity(root, account, *, gws_bin="gws", timeout=_HEALTH_TIMEOUT):
    """Explicit pre-activation identity check, NOT an extra request per API call.

    Caller holds lifecycle_guard through this check and activation. Native auth
    status supplies `user` when userinfo scope is available. Otherwise only
    Gmail getProfile-capable grants permit a fallback. Drive/Calendar-only
    grants without userinfo cannot prove identity: provision with email scope
    or an explicit trusted identity-binding flow; never treat a slot name or
    token_valid as identity proof. No credential contents are read by Python.
    """
    unconfirmed = {"ok": False, "error_code": "identity_unconfirmed",
                   "error": "Identity could not be verified; provision an identity-capable grant before activation."}
    mismatch = {"ok": False, "error_code": "identity_mismatch",
                "error": "The authorized Google identity does not match this account."}
    deadline = time.monotonic() + timeout
    try:
        with _native_guard(root, account):
            path = account_dir(root, account)

            def read(args):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {}
                result = _run_gws(root, path, args, gws_bin=gws_bin, timeout=remaining)
                if result.returncode or len(result.stdout) > 200_000:
                    return {}
                data = json.loads(result.stdout)
                return data if isinstance(data, dict) else {}

            status = read(["auth", "status"])
            health = _status_health(status, account)
            if health.get("error_code") == "identity_mismatch":
                return mismatch
            if health["valid"] is False:
                return unconfirmed
            if status.get("token_valid") is True and any(
                    isinstance(status.get(key), str) and status[key].lower() == account.lower()
                    for key in ("user", "email")):
                return {"ok": True, "account": account, "source": "native_userinfo"}
            allowed = {"https://mail.google.com/", *(
                "https://www.googleapis.com/auth/gmail." + scope
                for scope in ("metadata", "readonly", "modify", "compose"))}
            scopes = status.get("scopes")
            if not isinstance(scopes, list) or not any(
                    isinstance(scope, str) and scope in allowed for scope in scopes):
                return unconfirmed
            profile = read(["gmail", "users", "getProfile", "--params",
                            '{"userId":"me","fields":"emailAddress"}'])
            email = profile.get("emailAddress")
            if not isinstance(email, str) or not email:
                return unconfirmed
            if email.lower() != account.lower():
                return mismatch
            return {"ok": True, "account": account, "source": "gmail_profile"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return unconfirmed


def _status_health(status, account):
    # Native v0.22.5 emits user; retain email compatibility with older adapters.
    identities = [status.get(key) for key in ("user", "email")]
    if any(isinstance(email, str) and email and email.lower() != account.lower()
           for email in identities):
        return {"stored": True, "valid": False, "state": "invalid", "reauth_required": True,
                "detail": "gws holds credentials for another identity. " + _REAUTH,
                "error_code": "identity_mismatch", "action": "reauthorize"}
    if status.get("credentials_readable") is False or status.get("encryption_valid") is False:
        return {"stored": True, "valid": False, "state": "unreadable", "reauth_required": True,
                "detail": "The stored credentials cannot be read. " + _REAUTH,
                "error_code": "credentials_unreadable", "action": "reauthorize"}
    token_valid = status.get("token_valid")
    if token_valid is True:
        return {"stored": True, "valid": True, "state": "valid", "reauth_required": False,
                "detail": "gws confirmed a valid session."}
    if status.get("token_error"):
        failure = classify_gws_failure(2, json.dumps(status["token_error"]))
        invalid = failure["reauth_required"]
        return {"stored": True, "valid": False if invalid else None,
                "state": "invalid" if invalid else "stored", "reauth_required": invalid,
                "detail": failure["error"], "error_code": failure["error_code"],
                "action": failure["action"]}
    if token_valid is False:
        return {"stored": True, "valid": False, "state": "invalid", "reauth_required": True,
                "detail": "gws reported the session is invalid. " + _REAUTH,
                "error_code": "token_invalid", "action": "reauthorize"}
    if status.get("auth_method") == "none":
        return {"stored": True, "valid": False, "state": "invalid", "reauth_required": True,
                "detail": "gws found no session for this account. " + _REAUTH,
                "error_code": "auth_missing", "action": "reauthorize"}
    return {"stored": True, "valid": None, "state": "stored", "reauth_required": False,
            "detail": "Credentials present; gws auth status did not confirm a valid session."}


def execute_gws(root, account, args, *, gws_bin="gws"):
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ValueError("args must be a list of strings")
    if classify(args)[0] == BLOCK:
        # Public tools never dispatch credential-management commands.
        return {"ok": False, "error_code": "auth_blocked", "category": "policy",
                "action": "use_gws_accounts_or_native_login", "reauth_required": False,
                "error": "gws auth commands are not available to the agent; use gws_accounts with check_health=true. " + _REAUTH}
    try:
        with _native_guard(root, account):
            path = account_dir(root, account)
            result = _run_gws(root, path, args, gws_bin=gws_bin, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "gws is unavailable or timed out"}
    if result.returncode:
        return {"ok": False, **classify_gws_failure(result.returncode, result.stdout, result.stderr)}
    if len(result.stdout) > 200_000:
        # ponytail: spill only large successes; native stdout is still buffered in memory.
        directory = output = None
        try:
            directory = Path(tempfile.mkdtemp(prefix="response-", dir=Path(root).resolve()))
            output = directory / "stdout.txt"
            with output.open("x", encoding="utf-8") as handle:
                os.chmod(output, 0o600)
                handle.write(result.stdout)
            return {"ok": True, "account": account, "output_file": str(output),
                    "output_bytes": output.stat().st_size, "output_encoding": "utf-8",
                    "detail": "Full output saved locally; process it with code. For a Gmail attachment, decode the JSON data field as base64url. Delete the file and its directory when done."}
        except OSError:
            if output is not None:
                with suppress(OSError):
                    output.unlink()
            if directory is not None:
                with suppress(OSError):
                    directory.rmdir()
            return {"ok": False, "error_code": "output_write_failed",
                    "error": "gws succeeded, but the large output could not be saved. Check local space and permissions; do not repeat a remote write without verifying it."}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        data = result.stdout.strip()
    return {"ok": True, "account": account, "data": data}


def bounded_gws(root, account, args, *, gws_bin="gws", timeout=30, max_output=1_000_000):
    """One bounded native call for background readers.

    Unlike ``execute_gws`` this never spills large output to a file, never
    returns raw failure text, and exposes a numeric HTTP status for 404/410/429
    handling. The subprocess is killed and reaped before the locks release.
    """
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ValueError("args must be a list of strings")
    if classify(args)[0] == BLOCK and args != ["auth", "status"]:
        return {"ok": False, "error_code": "auth_blocked", "retryable": False,
                "error": "Only native auth status is available to background readers."}
    try:
        with _native_guard(root, account):
            path = account_dir(root, account)
            result = _run_gws(root, path, args, gws_bin=gws_bin, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "timeout", "retryable": True,
                "error": "The native call timed out; retry."}
    except (OSError, ValueError):
        return {"ok": False, "error_code": "transport_unavailable", "retryable": True,
                "error": "gws is unavailable or the account does not belong to this profile."}
    if result.returncode:
        failure = classify_gws_failure(result.returncode, result.stdout, result.stderr)
        status = failure.get("http_status")
        failure["retryable"] = bool(
            status == 429 or (isinstance(status, int) and 500 <= status <= 599)
            or failure["error_code"] in {"timeout", "transport_unavailable", "rate_limited"}
        )
        return {"ok": False, **failure}
    if len(result.stdout) > max_output:
        return {"ok": False, "error_code": "output_too_large", "retryable": False,
                "error": "The native response exceeded the worker metadata limit."}
    try:
        # Native no-content methods (including Gmail users.stop) emit no JSON.
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"ok": False, "error_code": "invalid_response", "retryable": True,
                "error": "gws returned a non-JSON response."}
    return {"ok": True, "account": account, "data": data}
