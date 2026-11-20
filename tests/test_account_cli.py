"""Exercise local native auth with real subprocesses and synthetic stores only."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gws_cli_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
C = importlib.import_module(spec.name + ".account_cli")
AccountBackend = importlib.import_module(spec.name + ".account_backend").AccountBackend

FAKE = '''import json, os, sys
from pathlib import Path
p = Path(os.environ["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"])
assert p.is_absolute()
assert Path(os.environ["HOME"]).parent == p
assert os.environ["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] == "file"
assert not any(k in os.environ for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_WORKSPACE_CLI_TOKEN", "CLOUDSDK_CONFIG"))
assert Path(".env").read_bytes() == b""
a = sys.argv[1:]
if a == ["auth", "setup"]:
    (p / "client_secret.json").write_text(json.dumps({"installed": {"client_id":"ok", "client_secret":"fixture"}}))
elif a[:2] == ["auth", "login"]:
    if not (p / "client_secret.json").is_file(): sys.exit(2)
    client = json.loads((p / "client_secret.json").read_text())["installed"]
    mode = client["client_id"]
    (p / "credentials.enc").write_text("synthetic-new")
    (p / "fake-argv.json").write_text(json.dumps(a))
    (p / "fake-user").write_text("other@example.com" if mode == "wrong" else p.name)
    sys.exit(3 if mode == "fail" else 0)
elif a == ["auth", "status"]:
    print(json.dumps({"token_valid": True, "user": (p / "fake-user").read_text(), "scopes": ["email"]}))
elif a == ["auth", "logout"]:
    (p / "credentials.enc").unlink()
    print("synthetic-private-native-output")
else:
    raise AssertionError(a)
'''


class AccountCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "profile" / "gws-oauth"
        self.binary = self.base / "gws"
        self.binary.write_text("#!" + sys.executable + "\n" + FAKE)
        self.binary.chmod(0o700)
        self.parser = argparse.ArgumentParser()
        C.setup_parser(self.parser)
        self.used = False
        self.check = lambda args: {"ok": True, "contract": "gws-account-lifecycle-v1",
            "account": args["account"], "credentials_in_use": self.used}

    def invoke(self, *argv, binary=None, root=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = C.run(self.parser.parse_args(argv), lambda: AccountBackend(
                root() if root else self.root, lambda: str(binary or self.binary), self.check))
        return code, json.loads(out.getvalue().splitlines()[-1])

    def login(self, account="a@example.com"):
        return self.invoke("login", account, "--readonly")

    def slot(self, account="a@example.com"):
        return self.root / "accounts" / account

    def old(self):
        self.slot().mkdir(parents=True)
        (self.slot() / "credentials.enc").write_text("synthetic-old")

    def test_login_status_accounts_logout_real_children(self):
        self.assertEqual(self.invoke("setup", "a@example.com")[0], 0)
        with patch.dict(os.environ, {"GOOGLE_WORKSPACE_CLI_TOKEN": "foreign", "GOOGLE_APPLICATION_CREDENTIALS": "foreign", "CLOUDSDK_CONFIG": "foreign"}):
            self.assertEqual(self.login()[0], 0)
            self.assertEqual(self.invoke("status", "A@example.com")[0], 0)
        self.assertEqual(self.invoke("accounts"), (0, {"ok": True, "accounts": ["a@example.com"]}))
        self.assertEqual(json.loads((self.slot() / "fake-argv.json").read_text()), ["auth", "login", "--readonly"])
        code, result = self.invoke("logout", "a@example.com")
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "logged_out")
        self.assertNotIn("synthetic-private", json.dumps(result))
        self.assertEqual(self.invoke("accounts")[1]["accounts"], [])

    def test_path_lookup_and_explicit_binary_override(self):
        with patch.dict(os.environ, {"PATH": str(self.base)}):
            self.assertEqual(self.invoke("setup", "a@example.com", binary="gws")[0], 0)
            self.assertEqual(self.invoke("login", "a@example.com", binary="gws")[0], 0)
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self.invoke("setup", "b@example.com")[0], 0)
            self.assertEqual(self.login("b@example.com")[0], 0)
        self.assertEqual(self.invoke("status", "a@example.com", binary="/missing/gws")[0], 1)

    def test_two_accounts_remain_independent(self):
        for account in ("a@example.com", "b@example.com"):
            self.assertEqual(self.invoke("setup", account)[0], 0)
        self.assertEqual(self.login()[0], 0)
        self.assertEqual(self.login("b@example.com")[0], 0)
        self.assertEqual(self.invoke("logout", "a@example.com")[0], 0)
        self.assertEqual(self.invoke("status", "b@example.com")[0], 0)

    def test_reauthorization_reuses_slot_client(self):
        self.assertEqual(self.invoke("setup", "a@example.com")[0], 0)
        self.assertEqual(self.login()[0], 0)
        self.assertEqual(self.invoke("login", "a@example.com", "--scopes", "email", "--services", "gmail")[0], 0)
        self.assertEqual(json.loads((self.slot() / "fake-argv.json").read_text()),
                         ["auth", "login", "--scopes", "email", "--services", "gmail"])

    def test_no_profile_client_fallback_or_legacy_store_cleanup(self):
        self.root.mkdir(parents=True)
        (self.root / "client_secret.json").write_text("synthetic-profile-only")
        legacy = self.root / ".pending/old"
        legacy.mkdir(parents=True)
        self.assertEqual(self.login()[0], 1)
        self.assertFalse((self.slot() / "client_secret.json").exists())
        self.assertTrue(legacy.is_dir())

    def test_wrong_identity_and_failed_in_place_reauth_block_all_routing(self):
        self.old()
        for mode in ("fail", "wrong"):
            (self.slot() / "client_secret.json").write_text(json.dumps({"installed": {"client_id": mode}}))
            self.assertEqual(self.login()[0], 1)
            self.assertEqual((self.slot() / "credentials.enc").read_text(), "synthetic-new")
            self.assertEqual(self.invoke("accounts")[1]["accounts"], [])
            with patch.object(C.R.subprocess, "run") as run:
                for call in (C.R.execute_gws, C.R.bounded_gws):
                    self.assertFalse(call(self.root, self.slot().name, ["drive", "files", "list"])["ok"])
                run.assert_not_called()
        self.assertEqual(self.invoke("logout", "a@example.com")[0], 0)
        self.assertEqual(self.invoke("setup", "a@example.com")[0], 0)
        self.assertEqual(self.login()[0], 0)

    def test_interrupted_login_fails_closed_without_deleting_native_store(self):
        self.old()
        for exc, code in ((KeyboardInterrupt(), 130), (C.subprocess.TimeoutExpired("gws", 1), 1)):
            with patch.object(C.subprocess, "run", side_effect=exc):
                self.assertEqual(self.login()[0], code)
            self.assertTrue((self.slot() / "credentials.enc").is_file())
            self.assertEqual(self.invoke("accounts")[1]["accounts"], [])

    def test_native_cache_and_plaintext_require_explicit_logout_before_replacement(self):
        self.old()
        for name in ("credentials.json", "token_cache.json", "sa_token_cache.json"):
            extra = self.slot() / name
            extra.write_text("native-only")
            for action in ("setup", "login"):
                code, result = self.invoke(action, self.slot().name)
                self.assertEqual((code, result["error_code"]), (1, "logout_required"))
            self.assertEqual(extra.read_text(), "native-only")
            extra.unlink()

    def test_logout_needs_native_success_and_credential_removal(self):
        self.old()
        for status in (2, 0):
            self.binary.write_text("#!" + sys.executable + "\nprint('private-fixture-output')\nraise SystemExit(%d)\n" % status)
            code, result = self.invoke("logout", "a@example.com")
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertNotIn("private-fixture-output", json.dumps(result))
            if not status:
                self.assertEqual(result["state"], "logout_unconfirmed")
            self.assertTrue((self.slot() / "credentials.enc").exists())

    def test_logout_does_not_claim_an_interrupted_native_temp_store_was_removed(self):
        self.old()
        residue = self.slot() / "credentials.tmp"
        residue.write_text("synthetic")
        code, result = self.invoke("logout", self.slot().name, binary="gws")
        self.assertEqual((code, result["state"]), (1, "logout_unconfirmed"))
        self.assertFalse((self.slot() / "credentials.enc").exists())
        self.assertTrue(residue.exists())
        self.assertFalse(self.invoke("accounts")[1]["accounts"])

    def test_busy_or_invalid_lifecycle_blocks_mutation(self):
        self.used = True
        for action in ("login", "logout"):
            with patch.object(C.subprocess, "run") as native:
                self.assertEqual(self.invoke(action, "a@example.com")[0], 1)
                native.assert_not_called()
        self.check = lambda args: {"ok": True, "credentials_in_use": False}
        self.assertEqual(self.login()[0], 1)

    def test_unsafe_slot_and_client_symlink_fail_before_spawn(self):
        self.slot().mkdir(parents=True)
        (self.slot() / "client_secret.json").symlink_to(self.binary)
        with patch.object(C.subprocess, "run") as run:
            for action in ("setup", "login", "logout"):
                self.assertEqual(self.invoke(action, self.slot().name)[0], 1)
            run.assert_not_called()

    def test_surviving_writer_keeps_the_fence_after_supervisor_releases_it(self):
        import subprocess
        backend = AccountBackend(self.root, lambda: str(self.binary), self.check)
        self.binary.write_text("#!" + sys.executable + "\nimport time; time.sleep(30)\n")
        with backend.auth_process("a@example.com", "login") as (cmd, options, _):
            proc = subprocess.Popen(cmd, **options)
        try:
            # The parent no longer holds the lock. Its live child still does.
            self.assertEqual(self.invoke("login", "a@example.com")[1]["error_code"], "account_busy")
            self.assertFalse(backend.registered("a@example.com"))
        finally:
            proc.kill()
            proc.wait(timeout=5)
        with backend.auth_process("a@example.com", "login"):
            pass

    def test_installed_gws_setup_reports_its_gcloud_prerequisite(self):
        import shutil
        binary = shutil.which("gws")
        self.assertIsNotNone(binary, "install native gws for this check")
        backend = AccountBackend(self.root, lambda: binary, self.check)
        with patch.dict(os.environ, {"PATH": "", "CLOUDSDK_CONFIG": "foreign"}):
            with backend.auth_process("a@example.com", "setup") as (cmd, options, _):
                # No gcloud executable or network activity; exercise the native wizard's first check.
                result = C.subprocess.run(cmd, **options, stdin=C.subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gcloud CLI not found", result.stderr + result.stdout)
        self.assertFalse(backend.registered("a@example.com"))

    def test_native_setup_offering_login_also_verifies_before_routing(self):
        self.binary.write_text(self.binary.read_text().replace('elif a[:2] == ["auth", "login"]:',
            '    a = ["auth", "login"]\nif a[:2] == ["auth", "login"]:').replace('else p.name', 'else "wrong@example.com"'))
        code, result = self.invoke("setup", "a@example.com")
        self.assertEqual((code, result["error_code"]), (1, "identity_mismatch"))
        self.assertEqual(self.invoke("accounts")[1]["accounts"], [])

    def test_invalid_account_and_scope_combinations(self):
        for argv in (["login", "../bad"], ["login", "alias"], ["login", "a@example.com", "--full", "--readonly"], []):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.parser.parse_args(argv)

    def test_missing_account_status_fails_without_creating_credentials(self):
        self.assertEqual(self.invoke("status", "missing@example.com")[0], 1)
        self.assertFalse((self.root / "accounts" / "missing@example.com").exists())

    def test_real_host_parser_and_configured_binary(self):
        from hermes_cli.main import _attach_plugin_cli_command
        from types import SimpleNamespace
        descriptors = {}
        ctx = SimpleNamespace(get_config=lambda key, default=None: str(self.binary),
            register_cli_command=lambda name, **kw: descriptors.update(name=name, **kw))
        C.register_cli(ctx, lambda: AccountBackend(self.root, lambda: str(self.binary), self.check))
        parser = argparse.ArgumentParser()
        _attach_plugin_cli_command(parser.add_subparsers(), descriptors)
        args = parser.parse_args(["gws", "setup", "a@example.com"])
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit:
            args.func(args)
        self.assertEqual(exit.exception.code, 0)
        self.assertTrue(self.slot().is_dir())

    def test_profile_guard_fails_closed(self):
        def wrong_profile():
            raise RuntimeError("wrong profile")
        with patch.object(C.subprocess, "run") as native:
            self.assertEqual(self.invoke("login", "a@example.com", root=wrong_profile)[0], 1)
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
