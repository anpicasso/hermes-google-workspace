"""Supervise one gws Desktop OAuth login per profile/chat/sender.

The gateway hook is leased only while a child waits on its loopback callback.
Explicit callbacks pass through the model; neither path puts codes in CLI argv.

Two completion paths exist:
- Remote browser: the redirect to ``http://localhost:<port>`` fails in the
  user's browser; the user pastes that URL and the plugin forwards only the
  code to the live ``gws`` loopback server.
- Local browser (same host as Hermes): the redirect reaches the ``gws``
  loopback directly, ``gws`` exchanges the code and exits by itself. A watcher
  thread validates and commits that result; a later paste of the same URL is
  still intercepted and answered with the stored outcome.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

if __package__:
    from .router import _account_lock, _commit_lock, account_dir
else:  # Direct module use by the standalone test suite.
    from router import _account_lock, _commit_lock, account_dir

_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+%-]*@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}\Z")
_URL = re.compile(r"https://accounts\.google\.com/[^\s]+")
_CODE = re.compile(r"[A-Za-z0-9_./~-]{1,2048}")
_TOKEN = re.compile(r"[0-9a-f]{24}")
_CLIENT_MAX = 65536


def _login_command(gws_bin, scope_mode, scopes):
    if scope_mode not in {"default", "readonly", "full", "custom"}:
        raise ValueError("scope_mode must be default, readonly, full or custom")
    if scopes is None:
        scopes = []
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise ValueError("scopes must be a list of strings")
    if scope_mode != "custom" and scopes:
        raise ValueError("scopes is only used with scope_mode=custom")
    if scope_mode == "custom" and (not scopes or len(scopes) > 100 or any(
            not scope or len(scope) > 512 or "," in scope or any(char.isspace() for char in scope)
            for scope in scopes)):
        raise ValueError("invalid custom scopes")
    command = [gws_bin, "auth", "login"]
    if scope_mode in {"readonly", "full"}:
        command.append("--" + scope_mode)
    elif scope_mode == "custom":
        command.extend(("--scopes", ",".join(scopes)))
    return command


def _native_client_path(gws_bin, env):
    """Ask gws for its native client path, with the documented path as fallback."""
    fallback = Path.home() / ".config" / "gws" / "client_secret.json"
    try:
        result = subprocess.run(
            [gws_bin, "auth", "status"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
        status = json.loads(result.stdout) if result.returncode == 0 else {}
        candidate = Path(status.get("client_config", "") if isinstance(status, dict) else "").expanduser()
        return candidate if candidate.is_absolute() else fallback
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return fallback


def _select_client(root, gws_bin, env, global_client=None):
    """Return (path, source) for the client JSON; source is a label, never an ID or path."""
    profile = root / "client_secret.json"
    if profile.is_symlink():
        raise ValueError(f"The profile OAuth client must not be a symlink: {profile}")
    if profile.exists():
        if not profile.is_file():
            raise ValueError(f"The profile OAuth client is not a regular file: {profile}")
        return profile, "profile"

    shared = Path(global_client).expanduser() if global_client else _native_client_path(gws_bin, env)
    if shared.is_symlink():
        raise ValueError(f"The global gws OAuth client must not be a symlink: {shared}")
    if shared.is_file():
        return shared, "gws_global"
    raise ValueError(
        f"Missing Desktop client_secret.json: install the profile override at {profile} "
        f"or configure the global gws client at {shared}."
    )


class ClientError(ValueError):
    """The selected client JSON exists but is not a usable Desktop client."""


def _read_client(path, source):
    """Bounded read + Desktop schema sanity; returns the exact bytes to stage."""
    with open(path, "rb") as handle:
        data = handle.read(_CLIENT_MAX + 1)
    if len(data) > _CLIENT_MAX:
        raise ClientError(f"The OAuth client ({source}) is too large to be a client_secret.json.")
    try:
        parsed = json.loads(data)
    except ValueError:
        raise ClientError(f"The OAuth client ({source}) is not valid JSON.") from None
    installed = parsed.get("installed") if isinstance(parsed, dict) else None
    if not isinstance(installed, dict):
        kind = "Web" if isinstance(parsed, dict) and "web" in parsed else "unknown"
        raise ClientError(f"The OAuth client ({source}) is of type {kind}; create a 'Desktop app' client "
                          "(JSON with an 'installed' key).")
    missing = [k for k in ("client_id", "client_secret") if not isinstance(installed.get(k), str) or not installed[k]]
    if missing:
        raise ClientError(f"The Desktop OAuth client ({source}) is missing: {', '.join(missing)}.")
    return data


def _epoch_path(root, account):
    # Same .locks directory and naming as router._account_lock.
    name = hashlib.sha256(account.encode("utf-8")).hexdigest()[:32]
    return Path(root).resolve() / ".locks" / f"{name}.epoch"


def _read_epoch(root, account):
    try:
        return _epoch_path(root, account).read_text()
    except OSError:
        return ""


def _bump_epoch(root, account):
    """Invalidate every login of this account started before now, in any process."""
    path = _epoch_path(root, account)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
    tmp.write_text(secrets.token_hex(12))
    os.replace(tmp, path)


def _take_lease(pending_root, token):
    """Hold an flock for the staging dir's lifetime; a crash releases it."""
    fd = os.open(pending_root / f"{token}.lease", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sweep_staging(pending_root):
    """Remove staging dirs whose lease no process holds; leaseless dirs are never guessed at."""
    if fcntl is None:
        return  # ponytail: no flock on Windows, staging there is only removed by its owner
    for lease in pending_root.glob("*.lease"):
        if not _TOKEN.fullmatch(lease.stem):
            continue
        try:
            fd = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # a live attempt (any process) owns it
            _remove_path(pending_root / lease.stem)
            lease.unlink(missing_ok=True)
        finally:
            os.close(fd)


def _remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


class LoginError(RuntimeError):
    """Login failure with a stable, secret-free reason code and an optional safe detail."""

    def __init__(self, reason, message="", *, detail=""):
        super().__init__(message or reason)
        self.reason = reason
        self.detail = detail


_REASON_HINTS = {
    "invalid_callback": "the pasted URL is not this login's localhost callback",
    "expired": "the login deadline passed",
    "callback_rejected": "gws did not accept the local callback",
    "gws_failed": "gws exited without saving credentials (code rejected or consent cancelled)",
    "identity_unconfirmed": "gws could not read the authorized account's email (userinfo)",
    "identity_mismatch": "the authorized Google account is not the requested one; check the exact email spelling",
    "account_exists": "the account was already registered; use reauthorize=true",
    "commit_failed": "the credentials could not be saved in the profile",
    "cancelled": "the login was cancelled (gws_login cancel or account logout)",
    "access_denied": "Google consent was denied or cancelled in the browser",
    "oauth_error": "Google returned an OAuth error in the callback",
}


def _failure_message(reason, detail=""):
    hint = _REASON_HINTS.get(reason, "internal error")
    extra = f"; {detail}" if detail else ""
    return f"the login could not be completed ({reason}: {hint}{extra}). Start a new login; do not reuse the link."


def _mask_identity(email):
    """Masked, non-secret hint of the identity gws reported: first/last local char + domain."""
    local, _, domain = str(email).partition("@")
    if not local or not domain:
        return ""
    shown = local[0] + "***" + (local[-1] if len(local) > 1 else "")
    return f"{shown}@{domain}"


def _install_attempt(attempt, destination, accounts):
    """Install validated credentials, replacing the old account only on success."""
    with _commit_lock(accounts):
        with _account_lock(Path(accounts).resolve().parent, destination.name):
            if attempt.cancelled or _read_epoch(Path(accounts).resolve().parent, destination.name) != attempt.epoch:
                raise LoginError("cancelled")  # a logout (any process) happened after this login started
            if time.monotonic() >= attempt.deadline:
                raise LoginError("expired", "The login expired before the credentials were saved")
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise LoginError("commit_failed", "The existing account path is not a valid directory")
                if not attempt.reauthorize:
                    raise LoginError("account_exists", "The account was already registered")
                backup = accounts / f".{destination.name}.backup-{secrets.token_hex(6)}"
                os.rename(destination, backup)
                try:
                    os.rename(attempt.directory, destination)
                except BaseException:
                    os.rename(backup, destination)
                    raise
                _remove_path(backup)
            else:
                os.rename(attempt.directory, destination)
            attempt.committed = True


@dataclass
class _Attempt:
    process: subprocess.Popen
    directory: Path
    redirect: str
    account: str
    deadline: float
    reauthorize: bool = False
    epoch: str = ""
    client_source: str = ""
    lease_fd: int | None = None
    scope_mode: str = "full"
    requested_scopes: list = field(default_factory=list)
    cancelled: bool = False
    committed: bool = False
    timer: threading.Timer | None = None
    watcher: threading.Thread | None = None
    processing: bool = False
    expired: bool = False
    finished: bool = False
    result: dict | None = None
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def scope_info(self):
        # What was *requested*; Google may grant less. Granted scopes come from the account itself.
        return {"scope_mode": self.scope_mode, "requested_scopes": list(self.requested_scopes)}


class LoginManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._attempts: dict[tuple[str, str, str, str, str], _Attempt] = {}
        self._hook = None

    @staticmethod
    def _key(root, platform, chat_id, user_id, profile=""):
        return (str(Path(root).resolve()), str(platform), str(chat_id), str(user_id), str(profile or ""))

    def has_pending(self, root, platform, chat_id, user_id):
        with self._lock:
            return self._key(root, platform, chat_id, user_id) in self._attempts

    @staticmethod
    def _authorization_url(proc, timeout=25):
        end = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < end and len(data) < 16384:
            if proc.poll() is not None:
                break
            ready, _, _ = select.select([proc.stdout], [], [], min(0.2, end - time.monotonic()))
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                data.extend(chunk)
                decoded = data.decode("utf-8", "replace")
                if match := _URL.search(decoded):
                    if match.end() < len(decoded) and decoded[match.end()].isspace():
                        return match.group(0).rstrip(".,)")
        raise RuntimeError("gws did not print an OAuth URL")

    def start(self, root, platform, chat_id, user_id, account, gws_bin, register_hook, *,
              timeout: float = 300, profile="", scope_mode="full", scopes=None, global_client=None,
              reauthorize=False):
        root = Path(root).expanduser().resolve()
        if not isinstance(reauthorize, bool):
            return {"ok": False, "reason": "invalid_arguments", "error": "reauthorize must be a boolean."}
        key = self._key(root, platform, chat_id, user_id, profile)
        if not platform or not chat_id or not user_id:
            return {"ok": False, "reason": "no_session_identity", "error": "This session needs a stable identity to complete OAuth."}
        if not _EMAIL.fullmatch(account or ""):
            return {"ok": False, "reason": "invalid_arguments", "error": "Give the exact Google account email."}
        if not 0 < timeout <= 900:
            return {"ok": False, "reason": "invalid_arguments", "error": "Invalid OAuth timeout."}
        with self._lock:
            existing = self._attempts.get(key)
            if existing is not None:
                with existing.lock:
                    finished, stored = existing.finished, existing.result
                if finished:
                    # Outcome of a login that gws completed by itself (local browser)
                    # or that failed without a paste: hand it over exactly once.
                    self._remove(key, existing)
                    if existing.account == account.lower() and stored is not None:
                        return dict(stored, consumed=True)
                else:
                    return {"ok": False, "pending": True, "reason": "pending", "account": existing.account,
                            "error": "A login is already pending in this chat. If the browser already showed "
                                     "'Success', check gws_login status with the same account; otherwise paste the "
                                     "localhost URL, or cancel it with gws_login cancel."}
        try:
            command = _login_command(gws_bin, scope_mode, scopes)
        except ValueError as exc:
            return {"ok": False, "reason": "invalid_arguments", "error": str(exc)}
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GOOGLE_WORKSPACE_CLI_") and k != "GOOGLE_APPLICATION_CREDENTIALS"}
        if shutil.which(gws_bin) is None:
            return {"ok": False, "reason": "gws_not_found",
                    "error": "The configured gws binary (gws_bin) was not found; install it or fix the path."}
        client_source = "unknown"
        try:
            client, client_source = _select_client(root, gws_bin, env, global_client)
            client_bytes = _read_client(client, client_source)
        except ClientError as exc:
            return {"ok": False, "reason": "client_invalid", "client_source": client_source, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "reason": "client_missing", "error": str(exc)}
        except OSError:
            return {"ok": False, "reason": "client_invalid", "client_source": client_source,
                    "error": f"The OAuth client ({client_source}) could not be read."}
        destination = root / "accounts" / account.lower()
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                return {"ok": False, "reason": "commit_failed", "error": "The existing account path is not a valid directory."}
            if not reauthorize:
                # Common repeat-login path: a local-browser login already committed and its
                # one-shot outcome was released (deadline/status), so the account simply exists.
                return {"ok": False, "reason": "account_exists", "account": account.lower(), "account_registered": True,
                        "error": "The account is already set up in this profile (the previous login may have finished). "
                                 "Check it with gws_accounts; use reauthorize=true only to renew it, which keeps the "
                                 "current credentials until the new identity is confirmed."}
        epoch = _read_epoch(root, account.lower())
        with self._lock:
            if key in self._attempts:
                return {"ok": False, "pending": True, "reason": "pending", "error": "A login is already pending in this chat."}
            pending_root = root / ".pending"
            lease = directory = None
            try:
                # Short pending-root mutex (any process) so a sweep never sees a lease between
                # create and flock. ponytail: reuses the router flock helper -> .pending/.lock.
                with _commit_lock(pending_root):
                    _sweep_staging(pending_root)
                    token = secrets.token_hex(12)
                    lease = _take_lease(pending_root, token)
                directory = pending_root / token
                directory.mkdir(mode=0o700)
                target = directory / "client_secret.json"
                target.write_bytes(client_bytes)
                target.chmod(0o600)
                env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(directory)
                env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
                proc = subprocess.Popen(
                    command,
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                )
                url = self._authorization_url(proc)
                redirect = parse_qs(urlsplit(url).query).get("redirect_uri", [""])[0]
                parsed = urlsplit(redirect)
                if parsed.scheme != "http" or parsed.hostname != "localhost" or not parsed.port or parsed.path not in ("", "/"):
                    raise ValueError("gws returned an unexpected redirect")
                attempt = _Attempt(
                    proc, directory, redirect, account.lower(), time.monotonic() + timeout,
                    reauthorize=reauthorize, epoch=epoch, client_source=client_source, lease_fd=lease,
                    scope_mode=scope_mode, requested_scopes=list(scopes or []),
                )
                self._attempts[key] = attempt
                if register_hook is not None and (self._hook is None or not self._hook.active):
                    self._hook = register_hook(self.on_message)
                timer = threading.Timer(timeout, self._expire, args=(key, attempt))
                timer.daemon = True
                attempt.timer = timer
                timer.start()
                watcher = threading.Thread(target=self._watch_process, args=(key, attempt), daemon=True)
                attempt.watcher = watcher
                watcher.start()
                return {"ok": True, "url": url, "account": account.lower(), "timeout_seconds": timeout,
                        "client_source": client_source, "reauthorize": reauthorize, **attempt.scope_info(),
                        "instructions": (
                            f"Open the link and sign in as {account.lower()}, then accept the permissions. "
                            "Browser on the same machine as Hermes: the page shows 'Success' and the account is "
                            "saved automatically; confirm with gws_login status (same account) or gws_accounts. "
                            "Browser on another device: it ends on a 'localhost' connection error page. That is "
                            "expected; copy the full localhost URL from the address bar and paste it in this same "
                            "chat (or pass it as callback_url). A mistyped URL can be pasted again; cancel with "
                            "gws_login cancel."
                        )}
            except (OSError, ValueError, RuntimeError, TypeError):
                self._attempts.pop(key, None)
                if not self._attempts and self._hook is not None:
                    self._hook.dispose()
                    self._hook = None
                if "proc" in locals():
                    self._terminate(proc)
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
                if lease is not None:
                    self._release_lease(pending_root, token, lease)
                return {"ok": False, "reason": "launch_failed", "error": "Could not start gws auth login; check the binary and the OAuth client."}

    @staticmethod
    def _terminate(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if proc.stdout is not None:
            proc.stdout.close()

    @staticmethod
    def _release_lease(pending_root, token, fd):
        (pending_root / f"{token}.lease").unlink(missing_ok=True)
        os.close(fd)

    @classmethod
    def _release(cls, attempt):
        """Drop staging, then its lease; idempotent. Committed staging was already renamed away."""
        with attempt.lock:
            fd, attempt.lease_fd = attempt.lease_fd, None
        shutil.rmtree(attempt.directory, ignore_errors=True)
        if fd is not None:
            cls._release_lease(attempt.directory.parent, attempt.directory.name, fd)

    def _remove(self, key, attempt):
        with self._lock:
            if self._attempts.get(key) is not attempt:
                return
            self._attempts.pop(key)
            if attempt.timer:
                attempt.timer.cancel()
            if not self._attempts and self._hook is not None:
                self._hook.dispose()
                self._hook = None

    def _expire(self, key, attempt, *, finished=False):
        """Deadline/unload/completion cleanup; an exchange in flight owns its own cleanup."""
        with attempt.lock:
            attempt.expired = True
            in_flight = attempt.processing and not attempt.finished and not finished
        if in_flight and attempt.process.poll() is not None:
            # The exchange thread is reading the output of a process that already
            # exited (local browser); closing its stdout here would corrupt that read.
            return
        self._terminate(attempt.process)
        if in_flight:
            return
        self._remove(key, attempt)
        self._release(attempt)

    def close(self):
        """Plugin unload: stop every attempt and wait briefly for owners to finish cleanup."""
        with self._lock:
            pending = list(self._attempts.items())
        for key, attempt in pending:
            self._expire(key, attempt)
            self._remove(key, attempt)
        # An exchange thread may still be reading an exited process; give it a
        # bounded chance to finish (commit or remove staging) without holding locks.
        for _, attempt in pending:
            if attempt.watcher is not None and attempt.watcher is not threading.current_thread():
                attempt.watcher.join(3)
            if attempt.processing and not attempt.finished:
                attempt.done.wait(3)
            if attempt.process.stdout is not None and not attempt.process.stdout.closed and attempt.finished:
                attempt.process.stdout.close()
            if attempt.finished:
                self._release(attempt)

    def _watch_process(self, key, attempt):
        """Commit a login that gws completed by itself (browser on the Hermes host)."""
        try:
            attempt.process.wait()
        except Exception:
            return
        with attempt.lock:
            if attempt.expired or attempt.processing or attempt.finished:
                return  # terminated by the plugin, or a pasted callback drives the exchange
            attempt.processing = True
        result = self._run_exchange(key, attempt, None, local=True)
        self._release(attempt)
        with attempt.lock:
            attempt.result = result
            attempt.finished = True
            attempt.done.set()
            expired = attempt.expired
        if expired:
            self._remove(key, attempt)
        # Otherwise stay registered until the deadline, a pasted callback or a
        # gws_login status call hands the stored outcome over exactly once.

    @staticmethod
    def _fetch_and_commit(attempt, code, root):
        """Forward the code (if the loopback is still up), then validate and commit."""
        process = attempt.process
        if code is not None and process.poll() is None:
            port = urlsplit(attempt.redirect).port
            status = None
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    conn.request("GET", "/?code=" + code)
                    response = conn.getresponse()
                    response.read()
                    status = response.status
                finally:
                    conn.close()
            except OSError:
                status = None
            if status != 200:
                # The loopback may be gone because the browser already reached it;
                # accept that only if gws then finishes on its own.
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise LoginError("callback_rejected") from None
        remaining = max(0.1, attempt.deadline - time.monotonic())
        try:
            out, _ = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise LoginError("expired") from None
        if process.returncode != 0 or not (attempt.directory / "credentials.enc").is_file():
            raise LoginError("gws_failed")
        text = out.decode("utf-8", "replace")
        try:
            result, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
        except (ValueError, KeyError):
            raise LoginError("identity_unconfirmed") from None
        if not isinstance(result, dict):
            raise LoginError("identity_unconfirmed")
        reported = str(result.get("account", "") or "").strip().lower()
        if not reported or reported == "(unknown)" or "@" not in reported:
            # gws prints "(unknown)" when its userinfo call failed; the identity was never confirmed.
            raise LoginError("identity_unconfirmed")
        if reported != attempt.account:
            raise LoginError("identity_mismatch",
                             detail=f"Google authorized {_mask_identity(reported)}, requested {attempt.account}")
        accounts = root / "accounts"
        accounts.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = accounts / attempt.account
        with attempt.lock:
            if attempt.cancelled:
                raise LoginError("cancelled")
            if attempt.expired or time.monotonic() >= attempt.deadline:
                raise LoginError("expired")
            try:
                _install_attempt(attempt, destination, accounts)
            except LoginError:
                raise
            except OSError as exc:
                raise LoginError("commit_failed") from exc
        return attempt.account

    def _run_exchange(self, key, attempt, code, *, local=False):
        """Run the exchange for one attempt; never raises, never echoes the code."""
        try:
            if time.monotonic() >= attempt.deadline:
                raise LoginError("expired")
            account = self._fetch_and_commit(attempt, code, Path(key[0]))
            via = " (completed from the local browser)" if local else ""
            return {"ok": True, "account": account, "local_browser": local, "client_source": attempt.client_source,
                    "reauthorized": attempt.reauthorize, **attempt.scope_info(),
                    "message": f"[Google Workspace: {account} authorized in this profile; credentials saved{via}. Verify with gws_accounts and gws_api.]"}
        except LoginError as exc:
            if attempt.cancelled:  # cancel kills gws mid-exchange; report why, not the side effect
                exc = LoginError("cancelled")
            return {"ok": False, "reason": exc.reason, "local_browser": local, "error": _failure_message(exc.reason, exc.detail)}
        except Exception:
            return {"ok": False, "reason": "internal", "local_browser": local, "error": _failure_message("internal")}

    def finish(self, root, platform, chat_id, user_id, account, callback_url, *, profile=""):
        """Complete a login explicitly on any surface, bound to its original identity."""
        key = self._key(root, platform, chat_id, user_id, profile)
        with self._lock:
            attempt = self._attempts.get(key)
        if attempt is None or not isinstance(account, str) or account.lower() != attempt.account:
            return {"ok": False, "reason": "no_pending", "error": "No pending login for this account in this session."}
        try:
            if not isinstance(callback_url, str) or len(callback_url) > 8192:
                raise ValueError("Invalid callback")
            text = callback_url.strip()
            if text.startswith("localhost:"):
                text = "http://" + text
            parsed = urlsplit(text)
            redirect = urlsplit(attempt.redirect)
            if (parsed.scheme != "http" or parsed.hostname != "localhost" or
                    parsed.port != redirect.port or (parsed.path or "/") != (redirect.path or "/")):
                raise ValueError("Invalid callback")
        except ValueError:
            return {"ok": False, "reason": "invalid_callback",
                    "error": "Invalid callback URL; copy this login's full localhost URL."}
        # ponytail: explicit completion uses normal tool arguments, not another ingress hook.
        return self._complete_callback(key, attempt, parsed)

    def _lookup(self, root, platform, chat_id, user_id, account, profile):
        key = self._key(root, platform, chat_id, user_id, profile)
        with self._lock:
            attempt = self._attempts.get(key)
        if attempt is None or not isinstance(account, str) or account.lower() != attempt.account:
            return key, None
        return key, attempt

    def status(self, root, platform, chat_id, user_id, account, *, profile=""):
        """Read-only progress, except a finished outcome is handed over (once), like start()."""
        key, attempt = self._lookup(root, platform, chat_id, user_id, account, profile)
        if attempt is None:
            try:
                account_dir(Path(root).expanduser().resolve(), account.lower())
                registered = True
            except (ValueError, AttributeError):
                registered = False
            return {"ok": True, "state": "none", "account_registered": registered}
        taken = self._take_result(key, attempt)
        if taken is not None:
            return dict(taken, state="finished", consumed=True)
        with attempt.lock:
            state = "processing" if attempt.processing else "waiting"
        return {"ok": True, "state": state, "account": attempt.account, "client_source": attempt.client_source,
                **attempt.scope_info(),
                "seconds_left": max(0, round(attempt.deadline - time.monotonic()))}

    def _cancel(self, key, attempt):
        """Stop an attempt for good; an exchange in flight sees `cancelled` before it can install."""
        with attempt.lock:  # serialises with _install_attempt, which runs under this lock
            attempt.cancelled = True
            committed = attempt.committed
        self._expire(key, attempt)
        self._remove(key, attempt)
        return committed

    def cancel(self, root, platform, chat_id, user_id, account, *, profile=""):
        key, attempt = self._lookup(root, platform, chat_id, user_id, account, profile)
        if attempt is None:
            return {"ok": False, "reason": "no_pending", "error": "No pending login for this account in this session."}
        committed = self._cancel(key, attempt)
        # committed=True: gws finished first (local browser); the account stays registered.
        return {"ok": True, "cancelled": True, "account": attempt.account, "committed": committed}

    def logout(self, root, account, remove):
        """Cancel this account's logins here, invalidate them in other processes, then call remove().

        remove() runs with no oauth lock held (it takes the router commit/account locks itself).
        """
        if not isinstance(account, str) or not _EMAIL.fullmatch(account):
            return {"ok": False, "reason": "invalid_arguments", "error": "Give the exact Google account email."}
        root, account = Path(root).expanduser().resolve(), account.lower()
        with self._lock:
            mine = [(k, a) for k, a in self._attempts.items() if k[0] == str(root) and a.account == account]
        for key, attempt in mine:
            self._cancel(key, attempt)
        accounts = root / "accounts"
        accounts.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _commit_lock(accounts), _account_lock(root, account):
            _bump_epoch(root, account)
        return dict(remove(), cancelled_logins=len(mine))

    def _take_result(self, key, attempt):
        """Hand a locally completed outcome over once and release the attempt."""
        with self._lock, attempt.lock:
            if not attempt.finished or attempt.result is None:
                return None
            self._remove(key, attempt)
            return dict(attempt.result)

    @staticmethod
    def _parse_callback(parsed):
        """Return (code, oauth_error); raise LoginError('invalid_callback') for retryable junk."""
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            raise LoginError("invalid_callback") from None
        if parsed.username or parsed.password or parsed.fragment:
            raise LoginError("invalid_callback")
        error = query.get("error", [])
        if error:
            # Google redirected with an explicit error: the consent is over, no code will follow.
            return None, "access_denied" if error == ["access_denied"] else "oauth_error"
        code = query.get("code", [])
        if len(code) != 1 or not _CODE.fullmatch(code[0]):
            raise LoginError("invalid_callback")
        return code[0], None

    def _complete_callback(self, key, attempt, parsed):
        try:
            code, oauth_error = self._parse_callback(parsed)
        except LoginError as exc:
            # Malformed paste (typo, truncated, missing code): keep the attempt for a retry.
            return {"ok": False, "reason": exc.reason, "retryable": True,
                    "error": "The pasted URL is not a valid callback for this login (missing code or truncated); "
                             "copy the full localhost URL again. The login is still pending."}
        with self._lock, attempt.lock:
            if attempt.finished:
                return self._take_result(key, attempt) or {"ok": False, "reason": "in_progress",
                                                           "error": "The authorization is already being processed."}
            waiting = attempt.processing
            if not waiting:
                attempt.processing = True
        if waiting:
            # The watcher is validating a login that the local browser already
            # completed; report its outcome instead of racing it.
            attempt.done.wait(timeout=max(0.1, attempt.deadline - time.monotonic()) + 5)
            return self._take_result(key, attempt) or {"ok": False, "reason": "in_progress",
                                                       "error": "The authorization is already being processed."}
        if oauth_error:
            result = {"ok": False, "reason": oauth_error, "error": _failure_message(oauth_error)}
        else:
            result = self._run_exchange(key, attempt, code)
        with attempt.lock:
            attempt.result = result
            attempt.finished = True
            attempt.done.set()
        self._expire(key, attempt, finished=True)
        return result

    async def on_message(self, event, **kwargs):
        source = event.source
        platform = getattr(getattr(source, "platform", None), "value", None)
        with self._lock:
            matching = [(key, attempt) for key, attempt in self._attempts.items()
                        if key[1:4] == (str(platform), str(source.chat_id), str(source.user_id))]
        if not matching:
            return None
        text = str(event.text or "").strip()
        if text.startswith("localhost:"):
            text = "http://" + text
        elif not text.startswith("http://localhost:"):
            return None
        if len(text) > 8192:
            return {"action": "rewrite", "text": "[Google Workspace: callback URL too long; start a new login.]"}
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError:
            return {"action": "rewrite", "text": "[Google Workspace: invalid callback URL; copy it again.]"}
        with self._lock:
            own_port = [(key, a) for key, a in matching if
                        key[4] == str(getattr(source, "profile", "") or "") and
                        parsed.scheme == "http" and parsed.hostname == "localhost" and
                        port == urlsplit(a.redirect).port]
            if not own_port:
                return None
            matches = [(key, a) for key, a in own_port if
                       (parsed.path or "/") == (urlsplit(a.redirect).path or "/")]
            if not matches:
                return {"action": "rewrite", "text": "[Google Workspace: wrong callback path; copy it again.]"}
            key, attempt = matches[0]
        # Every recognised callback is rewritten, including all errors. Never log the URL.
        try:
            result = await asyncio.to_thread(self._complete_callback, key, attempt, parsed)
            message = result.get("message") or f"[Google Workspace: {result['error']}]"
        except (Exception, asyncio.CancelledError):
            message = f"[Google Workspace: {_failure_message('internal')}]"
        gateway = kwargs.get("gateway")
        message_id = str(getattr(source, "message_id", "") or getattr(event, "message_id", "") or "")
        if gateway is not None and message_id:
            try:
                adapter = gateway._delivery_adapter_for(source)
                if adapter is not None:
                    await asyncio.wait_for(adapter.delete_message(str(source.chat_id), message_id), timeout=2)
            except (Exception, asyncio.CancelledError):
                pass  # Best-effort platform deletion, not a security guarantee.
        return {"action": "rewrite", "text": message}
