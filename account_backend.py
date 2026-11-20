"""Native account operations shared by the operator CLI and optional companions.

No remote callback handling and no credential-content access belongs here.
The registered instance is profile-bound; companions resolve it from the loaded
Hermes namespace, never by importing a second snapshot of this package.
"""
from contextlib import contextmanager, nullcontext
import os
import threading

from . import router as R
from .account_lifecycle import lifecycle_guard

CONTRACT = "gws-native-account-v2"


class AccountError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def check_lifecycle(result, account):
    if (not isinstance(result, dict) or result.get("ok") is not True
            or result.get("contract") != "gws-account-lifecycle-v1"
            or result.get("account") != account
            or type(result.get("credentials_in_use")) is not bool):
        raise AccountError("companion_unavailable")
    if result["credentials_in_use"]:
        raise AccountError("credentials_in_use")


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
    def _guard(self, account, *, wait=True):
        self.destination(account)
        if os.name != "posix":
            raise AccountError("unsupported_lock_backend")  # Native writers must inherit the crash fence.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        with lifecycle_guard(self.root, account, wait=wait) as fd:
            self._require_active()
            try:
                state = self._check({"account": account, "check_lifecycle": True})
            except AccountError:
                raise
            except Exception:
                raise AccountError("companion_unavailable") from None
            check_lifecycle(state, account)
            yield fd

    @contextmanager
    def auth_process(self, account, action, flags=(), *, replace=True):
        """Fence one in-place native writer until its child exits and identity is checked."""
        if action not in ("login", "setup"):
            raise ValueError("Invalid auth operation")
        binary = self.binary()
        with self._guard(account, wait=False) as lock_fd:
            slot = self.destination(account)
            slot.parent.mkdir(mode=0o700, exist_ok=True)
            slot.mkdir(mode=0o700, exist_ok=True)
            with R._native_guard(self.root, account):
                R.account_dir(self.root, account, require_credentials=False, allow_unverified=True)
                if not replace and any((slot / n).exists() for n in ("credentials.enc", "credentials.json")):
                    raise AccountError("account_exists")
                # Native login replaces credentials.enc, not an old plaintext fallback or API cache.
                if any((slot / n).exists() for n in ("credentials.json", "token_cache.json", "sa_token_cache.json")):
                    raise AccountError("logout_required")
                marker = slot / R._UNVERIFIED
                marker.touch(mode=0o600, exist_ok=True)

            def verify(*, guard=nullcontext, timeout=20):
                if action == "setup" and not (slot / "credentials.enc").exists():
                    # The wizard can finish without login. Do not unblock this slot.
                    R.account_dir(self.root, account, require_credentials=False, allow_unverified=True)
                    if not (slot / "client_secret.json").is_file():
                        raise AccountError("setup_incomplete")
                    return {"ok": True, "account": account, "state": "configured"}
                identity = R.account_identity(self.root, account, gws_bin=binary, timeout=timeout)
                if not identity.get("ok"):
                    raise AccountError(identity.get("error_code", "identity_unconfirmed"))
                with self._mutation, guard():
                    self._require_active()
                    marker.unlink()
                return {"ok": True, "account": account, "state": "authorized"}

            with R.native_command(self.root, slot, ["auth", action, *flags], gws_bin=binary) as (argv, env, cwd):
                # POSIX: a native writer surviving a supervisor crash keeps the lifecycle fence.
                yield argv, {"cwd": cwd, "env": env, "pass_fds": (lock_fd,)}, verify

    def status(self, account):
        health = R.account_health(self.root, account, gws_bin=self.binary())
        return {"ok": health.get("valid") is True, "account": account, "health": health}

    def registered(self, account):
        try:
            R.account_dir(self.root, account)
            return True
        except (ValueError, TypeError):
            return False

    def logout(self, account):
        """Drain native readers and let gws, not Python, clear the local credentials."""
        binary = self.binary()
        with self._guard(account) as lock_fd, R._native_guard(self.root, account), self._mutation:
            self._require_active()
            slot = self.destination(account)
            if slot.exists():
                R.account_dir(self.root, account, require_credentials=False, allow_unverified=True)
                (slot / R._UNVERIFIED).touch(mode=0o600, exist_ok=True)
                native = R._run_gws(self.root, slot, ["auth", "logout"], gws_bin=binary, timeout=30,
                                    pass_fds=(lock_fd,))
                if native.returncode:
                    return {"ok": False, **R.classify_gws_failure(native.returncode, native.stdout, native.stderr)}
                if any((slot / name).exists() or (slot / name).is_symlink() for name in
                       ("credentials.enc", "credentials.json", "credentials.tmp", "token_cache.json", "sa_token_cache.json")):
                    return {"ok": False, "account": account, "state": "logout_unconfirmed"}
            return {"ok": True, "account": account, "remote_revoked": False, "state": "logged_out"}
