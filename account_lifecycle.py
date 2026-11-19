"""Shared public/private account lifecycle protocol (v1); stdlib-only, vendorable.

lifecycle_guard(root, account) uses root/.locks/<digest>.lifecycle.lock, where
<digest> is the first 32 lowercase hex characters of SHA256(account UTF-8).
Account spelling is exact, matching router execution locks; callers that store
lowercase accounts must pass the canonical lowercase slot name to BOTH plugins.
root is the shared gws-oauth root, NOT the accounts directory or a slot.
Never unlink/replace lock files: the inode must survive logout/reauth swaps.

Public watch start holds this guard across validation and durable registration.
Private logout/reauth holds it across the public nonsecret watch-status query
and the credential commit/removal. The status query itself MUST NOT acquire
this guard (the querying process already holds it). No SQLite internals needed.
Always acquire lifecycle BEFORE router swap/execution locks or store transactions.
Never acquire lifecycle from inside a native call. This lock is non-reentrant.

POSIX flock and Windows first-byte locking match router's OS primitives.
Acquisition is bounded to 10 seconds; timeout/OSError means fail closed, retry
later, never proceed unlocked. Closing releases on success, error or crash.
This is cooperative synchronization, not protection against a malicious UID
that can replace parent directories while they are being inspected.
"""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+-]{0,127}\Z")
_LOCK_TIMEOUT = 10.0


@contextmanager
def lifecycle_guard(root, account):
    """Fence one account's lifecycle; raises TimeoutError/OSError/ValueError."""
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise ValueError("Invalid account name")
    directory = Path(root).absolute() / ".locks"
    if any(part.is_symlink() for part in (directory, *directory.parents)):
        raise ValueError("Refusing a symlinked lifecycle path")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(account.encode("utf-8")).hexdigest()[:32]
    path = directory / f"{name}.lifecycle.lock"
    if path.is_symlink():
        raise ValueError("Refusing a symlinked lifecycle lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Refusing an unsafe lifecycle lock")
        if fcntl is None and msvcrt is None:
            raise OSError("No supported lifecycle lock backend")
        if fcntl is None and info.st_size == 0:  # pragma: no cover - Windows
            os.write(fd, b"\0")
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover - Windows
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Account lifecycle is busy; retry later") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
