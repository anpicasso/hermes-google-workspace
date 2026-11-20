"""Native account operations shared by the operator CLI and optional companions.

No remote callback handling and no credential-content access belongs here.
The registered instance is profile-bound; companions resolve it from the loaded
Hermes namespace, never by importing a second snapshot of this package.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading

from . import router as R
from .account_lifecycle import lifecycle_guard

CONTRACT = "gws-native-account-v1"


class AccountError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def client_bytes(path):
    """Read only a bounded Desktop client configuration, never tokens."""
    path = R._safe_path(Path(path).expanduser())
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


def check_lifecycle(result, account):
    if (not isinstance(result, dict) or result.get("ok") is not True
            or result.get("contract") != "gws-account-lifecycle-v1"
            or result.get("account") != account
            or type(result.get("credentials_in_use")) is not bool):
        raise AccountError("companion_unavailable")
    if result["credentials_in_use"]:
        raise AccountError("credentials_in_use")


def _take_lease(pending_root, token):
    """Hold a flock for the staging dir's lifetime; a crash releases it."""
    fd = os.open(pending_root / f"{token}.lease", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        if R.fcntl is not None:
            R.fcntl.flock(fd, R.fcntl.LOCK_EX | R.fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sweep_staging(pending_root):
    """Remove staging dirs whose lease no process holds; leaseless dirs are never guessed at."""
    if R.fcntl is None:
        return  # ponytail: no flock on Windows, staging there is only removed by its owner
    for lease in pending_root.glob("*.lease"):
        if not re.fullmatch(r"[0-9a-f]{24}", lease.stem):
            continue
        try:
            fd = os.open(lease, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                R.fcntl.flock(fd, R.fcntl.LOCK_EX | R.fcntl.LOCK_NB)
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


class AccountBackend:
    contract = CONTRACT

    def __init__(self, root, gws_bin, check):
        self.root = R._safe_path(root)
        self._gws_bin = gws_bin
        self._check = check
        self._mutation = threading.Lock()
        self.active = True

    def close(self):
        with self._mutation:
            self.active = False

    def _require_active(self):
        if not self.active:
            raise AccountError("companion_unavailable")

    def binary(self):
        self._require_active()
        return R.resolve_binary(self._gws_bin())

    def destination(self, account):
        if not isinstance(account, str) or not R._ACCOUNT.fullmatch(account) or "@" not in account:
            raise ValueError("Use the Google account email")
        return R._safe_path(self.root / "accounts" / account)

    @contextmanager
    def _guard(self, account):
        self.destination(account)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        with lifecycle_guard(self.root, account):
            self._require_active()
            try:
                state = self._check({"account": account, "check_lifecycle": True})
            except AccountError:
                raise
            except Exception:
                raise AccountError("companion_unavailable") from None
            check_lifecycle(state, account)
            yield

    def begin(self, account):
        """Check before launching consent and capture the cross-process logout fence."""
        with self._guard(account):
            return self.epoch(account)

    def _epoch_path(self, account):
        self.destination(account)
        name = hashlib.sha256(account.encode("utf-8")).hexdigest()[:32]
        return R._safe_path(self.root / ".locks" / (name + ".epoch"))

    def epoch(self, account):
        path = self._epoch_path(account)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 24:
                    raise ValueError("Invalid account epoch")
                value = stream.read(25)
                if not re.fullmatch(b"[0-9a-f]{24}", value):
                    raise ValueError("Invalid account epoch")
                return value.decode("ascii")
        except FileNotFoundError:
            return ""

    def _fence(self, account):
        """Caller holds lifecycle and the native account lock."""
        path = self._epoch_path(account)
        temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            with temporary.open("x") as stream:
                os.chmod(temporary, 0o600)
                stream.write(secrets.token_hex(12))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def select_client(self, account, fallback=None):
        destination = self.destination(account)
        for path, source in ((destination / "client_secret.json", "account"),
                             (self.root / "client_secret.json", "profile")):
            if path.exists() or path.is_symlink():
                return R._safe_path(path), source
        if fallback is not None:
            return R._safe_path(fallback), "gws_global"
        return self.root / "client_secret.json", "profile"

    @contextmanager
    def stage(self, account, client_file=None):
        """Lease a disposable native store; activate moves only its verified slot."""
        self.destination(account)
        if client_file is None:
            client_file, _ = self.select_client(account)
        try:
            data = client_bytes(client_file)
        except FileNotFoundError:
            raise AccountError("client_missing") from None
        except (OSError, ValueError):
            raise AccountError("client_invalid") from None
        pending = self.root / ".pending"
        token = secrets.token_hex(12)
        with R._commit_lock(pending):
            _sweep_staging(pending)
            lease = _take_lease(pending, token)
        directory = pending / token
        created = False
        try:
            directory.mkdir(mode=0o700)
            created = True
            slot = directory / "accounts" / account
            slot.mkdir(mode=0o700, parents=True)
            with (slot / "client_secret.json").open("xb") as stream:
                os.chmod(slot / "client_secret.json", 0o600)
                stream.write(data)
            yield directory, slot
        finally:
            try:
                if created:
                    _remove_path(directory)
                (pending / (token + ".lease")).unlink(missing_ok=True)
            finally:
                os.close(lease)

    def _staged_slot(self, staging, account):
        self.destination(account)
        staging = R._safe_path(staging)
        if staging.parent != self.root / ".pending" or not re.fullmatch(r"[0-9a-f]{24}", staging.name):
            raise ValueError("Staging does not belong to this profile")
        return R.account_dir(staging, account, require_credentials=False)

    @contextmanager
    def login_process(self, staging, account, argv, *, binary):
        """Subprocess launch inputs; caller owns process lifetime inside this scope."""
        slot = self._staged_slot(staging, account)
        with R._native_guard(staging, account), R.native_command(
                staging, slot, ["auth", "login", *argv], gws_bin=binary) as launch:
            yield launch

    def activate(self, staging, account, *, binary, epoch, replace=True, commit_guard=nullcontext, timeout=20):
        """Only install the verified staging slot, under the watch and logout barriers."""
        self._staged_slot(staging, account)
        identity = R.account_identity(staging, account, gws_bin=binary, timeout=timeout)
        if not identity.get("ok"):
            raise AccountError(identity.get("error_code", "identity_unconfirmed"))
        with self._guard(account), R._commit_lock(self.root / "accounts"), self._mutation, commit_guard():
            self._require_active()
            if self.epoch(account) != epoch:
                raise AccountError("cancelled")
            slot = R.account_dir(staging, account)
            destination = self.destination(account)
            had_slot = destination.exists()
            if had_slot:
                R.account_dir(self.root, account, require_credentials=False)
            if had_slot and not replace and self.registered(account):
                raise AccountError("account_exists")
            backup = destination.parent / ("." + account + ".backup-" + secrets.token_hex(12))
            if had_slot:
                os.rename(destination, backup)
            try:
                os.rename(slot, destination)
            except BaseException:
                if had_slot:
                    os.rename(backup, destination)
                raise
            result = {"ok": True, "account": account, "state": "authorized"}
            if had_slot:
                try:
                    shutil.rmtree(backup)
                except OSError:
                    result["warning"] = "Old account backup remains; remove it locally after verification."
            return result

    def status(self, account):
        health = R.account_health(self.root, account, gws_bin=self.binary())
        return {"ok": health.get("valid") is True, "account": account, "health": health}

    def registered(self, account):
        try:
            R.account_dir(self.root, account)
            return True
        except (ValueError, TypeError):
            return False

    def logout(self, account, *, before_logout=None):
        """Refuse busy watches before fencing consent and running native local logout."""
        binary = self.binary()
        with self._guard(account), R._native_guard(self.root, account), self._mutation:
            self._require_active()
            if before_logout is not None:
                before_logout()
            self._fence(account)
            slot = self.destination(account)
            if not slot.exists():
                return {"ok": True, "account": account, "state": "logged_out", "remote_revoked": False}
            R.account_dir(self.root, account, require_credentials=False)
            files = ("credentials.enc", "credentials.json", "token_cache.json", "sa_token_cache.json")
            if not any((slot / name).exists() for name in files):
                return {"ok": True, "account": account, "state": "logged_out", "remote_revoked": False}
            native = R._run_gws(self.root, slot, ["auth", "logout"], gws_bin=binary, timeout=30)
            if native.returncode:
                return {"ok": False, **R.classify_gws_failure(native.returncode, native.stdout, native.stderr)}
            remaining = any((slot / name).exists() or (slot / name).is_symlink()
                            for name in files)
            return {"ok": not remaining, "account": account, "remote_revoked": False,
                    "state": "logout_unconfirmed" if remaining else "logged_out"}
