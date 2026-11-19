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
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from gws_cli_test import account_cli as C

FAKE = '''import json, os, sys
from pathlib import Path
p = Path(os.environ["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"])
assert p.is_absolute()
assert Path(os.environ["HOME"]).parent == p
assert os.environ["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] == "file"
assert not any(k in os.environ for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_WORKSPACE_CLI_TOKEN", "CLOUDSDK_CONFIG"))
assert Path(".env").read_bytes() == b""
a = sys.argv[1:]
if a[:2] == ["auth", "login"]:
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
        self.client = self.base / "client.json"
        self.client.write_text(json.dumps({"installed": {"client_id": "ok", "client_secret": "synthetic"}}))
        self.parser = argparse.ArgumentParser()
        C.setup_parser(self.parser)
        self.used = False
        self.check = lambda args: {"ok": True, "contract": "gws-account-lifecycle-v1",
            "account": args["account"], "credentials_in_use": self.used}

    def invoke(self, *argv, binary=None, root=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = C.run(self.parser.parse_args(argv), root=root or (lambda: self.root),
                         gws_bin=lambda: str(binary or self.binary), check_lifecycle=self.check)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def login(self, account="a@example.com"):
        return self.invoke("login", account, "--client-file", str(self.client), "--readonly")

    def slot(self, account="a@example.com"):
        return self.root / "accounts" / account

    def old(self):
        self.slot().mkdir(parents=True)
        (self.slot() / "credentials.enc").write_text("synthetic-old")

    def test_login_status_accounts_logout_real_children(self):
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
            self.assertEqual(self.invoke("login", "a@example.com", "--client-file", str(self.client), binary="gws")[0], 0)
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self.login("b@example.com")[0], 0)
        self.assertEqual(self.invoke("status", "a@example.com", binary="/missing/gws")[0], 1)

    def test_two_accounts_remain_independent(self):
        self.assertEqual(self.login()[0], 0)
        self.assertEqual(self.login("b@example.com")[0], 0)
        self.assertEqual(self.invoke("logout", "a@example.com")[0], 0)
        self.assertEqual(self.invoke("status", "b@example.com")[0], 0)

    def test_reauthorization_reuses_slot_client(self):
        self.assertEqual(self.login()[0], 0)
        self.client.unlink()
        self.assertEqual(self.invoke("login", "a@example.com", "--scopes", "email", "--services", "gmail")[0], 0)
        self.assertEqual(json.loads((self.slot() / "fake-argv.json").read_text()),
                         ["auth", "login", "--scopes", "email", "--services", "gmail"])

    def test_profile_client_fallback(self):
        self.root.mkdir(parents=True)
        (self.root / "client_secret.json").write_bytes(self.client.read_bytes())
        self.assertEqual(self.invoke("login", "a@example.com", "--full")[0], 0)

    def test_native_failure_and_wrong_identity_preserve_old_slot(self):
        self.old()
        for mode in ("fail", "wrong"):
            self.client.write_text(json.dumps({"installed": {"client_id": mode, "client_secret": "synthetic"}}))
            self.assertEqual(self.login()[0], 1)
            self.assertEqual((self.slot() / "credentials.enc").read_text(), "synthetic-old")
            self.assertEqual(list(self.root.glob(".native-login-*")), [])

    def test_interrupt_and_timeout_preserve_old_slot(self):
        import subprocess
        self.old()
        for exc, code in ((KeyboardInterrupt(), 130), (subprocess.TimeoutExpired("gws", 1), 1)):
            with patch.object(C.R, "_run_gws", side_effect=exc):
                self.assertEqual(self.login()[0], code)
            self.assertEqual((self.slot() / "credentials.enc").read_text(), "synthetic-old")
            self.assertEqual(list(self.root.glob(".native-login-*")), [])

    def test_swap_failure_restores_old_slot(self):
        self.old()
        rename = os.rename
        def fail(source, destination):
            if ".native-login-" in str(source) and Path(destination) == self.slot():
                raise OSError("synthetic swap failure")
            return rename(source, destination)
        with patch.object(C.os, "rename", side_effect=fail):
            self.assertEqual(self.login()[0], 1)
        self.assertEqual((self.slot() / "credentials.enc").read_text(), "synthetic-old")

    def test_busy_or_invalid_lifecycle_blocks_mutation(self):
        self.used = True
        for action in ("login", "logout"):
            with patch.object(C.R, "_run_gws") as native:
                self.assertEqual(self.invoke(action, "a@example.com")[0], 1)
                native.assert_not_called()
        self.check = lambda args: {"ok": True, "credentials_in_use": False}
        self.assertEqual(self.login()[0], 1)

    def test_invalid_clients_symlinks_and_unsafe_slot_fail(self):
        self.client.write_text('{"refresh_token":"synthetic"}')
        self.assertEqual(self.login()[0], 1)
        self.client.unlink()
        self.client.symlink_to(self.binary)
        self.assertEqual(self.login()[0], 1)
        self.slot().parent.mkdir(parents=True, exist_ok=True)
        self.slot().symlink_to(self.base, target_is_directory=True)
        self.assertEqual(self.login()[0], 1)

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
        C.register_cli(ctx, lambda: self.root, self.check)
        parser = argparse.ArgumentParser()
        _attach_plugin_cli_command(parser.add_subparsers(), descriptors)
        args = parser.parse_args(["gws", "login", "a@example.com", "--client-file", str(self.client), "--readonly"])
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit:
            args.func(args)
        self.assertEqual(exit.exception.code, 0)
        self.assertTrue(self.slot().is_dir())

    def test_profile_guard_fails_closed(self):
        def wrong_profile():
            raise RuntimeError("wrong profile")
        with patch.object(C.R, "_run_gws") as native:
            self.assertEqual(self.invoke("login", "a@example.com", root=wrong_profile)[0], 1)
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
