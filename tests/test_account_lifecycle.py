import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import account_lifecycle as L
from router import _native_guard


class LifecycleTests(unittest.TestCase):
    def test_protocol_independent_execution_and_survives_logout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = "person@example.com"
            slot = root / "accounts" / account
            slot.mkdir(parents=True)
            (slot / "credentials.enc").write_text("synthetic")
            digest = hashlib.sha256(account.encode()).hexdigest()[:32]
            lock = root / ".locks" / (digest + ".lifecycle.lock")
            with L.lifecycle_guard(root, account):
                inode = lock.stat().st_ino
                with _native_guard(root, account):
                    pass  # Not the router's execution lock; no self-deadlock.
                (slot / "credentials.enc").unlink()
                slot.rmdir()  # Native/operator slot removal must not unlink the barrier.
                with mock.patch.object(L, "_LOCK_TIMEOUT", 0.05):
                    with self.assertRaises(TimeoutError):
                        with L.lifecycle_guard(root, account):
                            self.fail("Reentered a nonreentrant lifecycle guard")
                with L.lifecycle_guard(root, "other@example.com"):
                    pass
            with L.lifecycle_guard(root, account):
                self.assertEqual(lock.stat().st_ino, inode)

    def test_cross_process_barrier_and_release_after_exception(self):
        code = ("import sys\nfrom account_lifecycle import lifecycle_guard\n"
                "print('ready', flush=True)\n"
                "with lifecycle_guard(sys.argv[1], 'person@example.com'):\n"
                "    print('acquired', flush=True)\n")
        with tempfile.TemporaryDirectory() as tmp:
            proc = None
            try:
                with self.assertRaisesRegex(RuntimeError, "release"):
                    with L.lifecycle_guard(tmp, "person@example.com"):
                        proc = subprocess.Popen([sys.executable, "-c", code, tmp],
                            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
                        assert proc.stdout is not None
                        self.assertEqual(proc.stdout.readline().strip(), "ready")
                        with self.assertRaises(subprocess.TimeoutExpired):
                            proc.communicate(timeout=0.15)
                        raise RuntimeError("release")
                assert proc is not None
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(out.strip(), "acquired")
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def test_invalid_names_and_unsupported_backend_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in (None, "", ".", "..", "../x", "bad/name", "a" * 129):
                with self.assertRaises(ValueError):
                    with L.lifecycle_guard(tmp, name):
                        self.fail("Invalid account admitted")
            with mock.patch.object(L, "fcntl", None), mock.patch.object(L, "msvcrt", None):
                with self.assertRaises(OSError):
                    with L.lifecycle_guard(tmp, "person@example.com"):
                        self.fail("Unsupported backend admitted")

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem safety")
    def test_unsafe_paths_fail_closed_without_mutation(self):
        for kind in ("root", "directory", "symlink", "hardlink", "fifo", "nonregular"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "root"
                root.mkdir()
                foreign = Path(tmp) / "foreign"
                foreign.mkdir()
                directory = root / ".locks"
                directory.mkdir()
                digest = hashlib.sha256(b"person@example.com").hexdigest()[:32]
                path = directory / (digest + ".lifecycle.lock")
                if kind == "root":
                    directory.rmdir()
                    root.rmdir()
                    root.symlink_to(foreign)
                elif kind == "directory":
                    directory.rmdir()
                    directory.symlink_to(foreign)
                elif kind == "symlink":
                    path.symlink_to(foreign / "missing")
                elif kind == "hardlink":
                    (foreign / "file").write_text("unchanged")
                    os.link(foreign / "file", path)
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    path.mkdir()
                with self.assertRaises((ValueError, OSError)):
                    with L.lifecycle_guard(root, "person@example.com"):
                        self.fail("Unsafe lock admitted")
                if kind == "hardlink":
                    self.assertEqual((foreign / "file").read_text(), "unchanged")
                else:
                    self.assertEqual(list(foreign.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
