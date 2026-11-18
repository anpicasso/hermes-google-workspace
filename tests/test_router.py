import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router import account_health, classify_gws_failure, execute_gws, list_accounts


def _account(root, name="person@example.com", content="fixture-value"):
    path = Path(root) / "accounts" / name
    path.mkdir(parents=True)
    (path / "credentials.enc").write_text(content)
    return path


def _fake_gws(tmp, body):
    path = Path(tmp) / "fake-gws"
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o700)
    return path


class RouterTests(unittest.TestCase):
    def test_large_success_is_saved_complete_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            _account(tmp)
            output = json.dumps({"data": "A" * 210_000, "label": "dólares"}, ensure_ascii=False)
            with mock.patch("router.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=output, stderr="ignored"
            )):
                result = execute_gws(tmp, "person@example.com", ["gmail", "users", "messages", "attachments", "get"])
            self.assertTrue(result["ok"])
            saved = Path(result["output_file"])
            self.assertEqual(saved.read_text(encoding="utf-8"), output)
            self.assertEqual(result["output_bytes"], len(output.encode("utf-8")))
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue(saved.is_relative_to(Path(tmp)))
            self.assertNotIn("data", result)

    def test_public_calls_run_in_private_files_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            _account(tmp)
            with mock.patch("router.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout="{}", stderr="")) as run:
                execute_gws(tmp, "person@example.com", ["drive", "files", "list"])
            files = Path(tmp).resolve() / "files"
            self.assertEqual(run.call_args.kwargs["cwd"], files)
            self.assertEqual(files.stat().st_mode & 0o777, 0o700)
            files.rmdir()
            files.symlink_to(tmp)
            with mock.patch("router.subprocess.run") as run:
                self.assertFalse(execute_gws(tmp, "person@example.com", ["drive", "files", "list"])["ok"])
                run.assert_not_called()

    def test_large_failure_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            _account(tmp)
            with mock.patch("router.subprocess.run", return_value=mock.Mock(
                returncode=2, stdout="secret" * 40_000, stderr="invalid_grant"
            )):
                result = execute_gws(tmp, "person@example.com", ["anything"])
            self.assertFalse(result["ok"])
            self.assertNotIn("output_file", result)
            self.assertFalse(list(Path(tmp).glob("response-*")))

    def test_large_response_write_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            _account(tmp)
            with mock.patch("router.subprocess.run", return_value=mock.Mock(
                returncode=0, stdout="x" * 210_000, stderr=""
            )), mock.patch("tempfile.mkdtemp", side_effect=OSError("private-details")):
                result = execute_gws(tmp, "person@example.com", ["anything"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "output_write_failed")
            self.assertNotIn("private-details", str(result))

    @unittest.skipIf(os.name != "posix", "POSIX flock check")
    def test_native_calls_and_other_processes_share_swap_lock(self):
        import fcntl
        import select
        import subprocess
        from router import _commit_lock
        with tempfile.TemporaryDirectory() as tmp:
            _account(tmp)
            accounts = Path(tmp) / "accounts"
            def run_locked(*args, **kwargs):
                with (accounts / ".lock").open("r+") as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return mock.Mock(returncode=0, stdout='{"token_valid": true}', stderr="")
            with mock.patch("router.subprocess.run", side_effect=run_locked):
                self.assertTrue(execute_gws(tmp, "person@example.com", ["drive", "files", "list"])["ok"])
                self.assertTrue(account_health(tmp, "person@example.com")["valid"])
            code = ("from pathlib import Path\nfrom router import _commit_lock\n"
                    "import sys\nprint('ready', flush=True)\n"
                    "with _commit_lock(Path(sys.argv[1])):\n    print('acquired', flush=True)\n")
            with _commit_lock(accounts):
                proc = subprocess.Popen([sys.executable, "-c", code, str(accounts)],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, text=True)
                assert proc.stdout is not None
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                self.assertFalse(select.select([proc.stdout], [], [], 0.15)[0])
            out, _ = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(out.strip(), "acquired")

    @unittest.skipIf(os.name != "posix", "POSIX threaded flock regression")
    def test_waiting_swap_does_not_block_reader_release(self):
        import subprocess
        code = """
import sys, threading, time
from pathlib import Path
from router import _commit_lock
accounts = Path(sys.argv[1])
waiting = threading.Event()
acquired = threading.Event()
def swap():
    waiting.set()
    with _commit_lock(accounts):
        acquired.set()
with _commit_lock(accounts, shared=True):
    thread = threading.Thread(target=swap, daemon=True)
    thread.start()
    assert waiting.wait(1)
    time.sleep(0.1)
    assert not acquired.is_set()
thread.join(2)
assert acquired.is_set()
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-c", code, tmp], timeout=5,
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_health_inconclusive_does_not_request_reauthorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            _account(tmp)
            for status in ({}, {"token_valid": False, "token_error": "network timeout secret-fixture"}):
                with mock.patch("router.subprocess.run", return_value=mock.Mock(
                    returncode=0, stdout=json.dumps(status), stderr=""
                )):
                    health = account_health(tmp, "person@example.com")
                self.assertIsNone(health["valid"])
                self.assertFalse(health["reauth_required"])
                self.assertNotIn("secret-fixture", json.dumps(health))
        self.assertFalse(classify_gws_failure(2, "network unavailable")["reauth_required"])

    def test_native_failures_are_classified_without_leaking_output(self):
        cases = [
            (2, '{"error":{"code":400,"message":"invalid_grant: Token has been expired or revoked secret-token"}}',
             "refresh_token=secret-refresh", "invalid_grant", "auth", True),
            (2, '{"error":{"code":400,"message":"reauth related error (invalid_rapt)"}}',
             "access_token=ya29.secret", "invalid_rapt", "auth", True),
            (1, '{"error":{"code":403,"message":"PERMISSION_DENIED secret-body"}}',
             "client_secret=secret-client", "permission_denied", "permission", False),
            (3, '{"error":{"code":400,"reason":"validationError"}}',
             "error: unexpected argument '--secret-arg' found", "invalid_arguments", "syntax", False),
            (1, '{"error":{"code":404,"message":"not found secret-body"}}',
             "request_id=secret-request", "api_error", "api", False),
        ]
        for returncode, stdout, stderr, code, category, reauth in cases:
            with self.subTest(code=code):
                result = classify_gws_failure(returncode, stdout, stderr)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["category"], category)
                self.assertEqual(result["reauth_required"], reauth)
                rendered = json.dumps(result).lower()
                for secret in ("secret-token", "secret-refresh", "ya29", "secret-body",
                               "secret-client", "secret-arg", "secret-request"):
                    self.assertNotIn(secret, rendered)
                for raw_field in ("stdout", "stderr", "output"):
                    self.assertNotIn(raw_field, result)

    def test_execute_gws_returns_sanitized_actionable_auth_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            fake = _fake_gws(tmp, (
                "import sys\n"
                "sys.stderr.write('refresh_token=secret-refresh invalid_grant: expired\\n')\n"
                "sys.stdout.write('{\"error\":{\"code\":400,\"message\":\"secret-body\"}}\\n')\n"
                "sys.exit(2)\n"
            ))
            result = execute_gws(root, "person@example.com", ["drive", "files", "list"], gws_bin=str(fake))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_grant")
        self.assertTrue(result["reauth_required"])
        self.assertEqual(result["action"], "reauthorize")
        self.assertIn("reauthorize=true", result["error"])
        rendered = json.dumps(result)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("refresh_token", rendered)

    def test_health_reports_valid_stored_and_unreadable_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            fake = _fake_gws(tmp, (
                "import json, os, sys\n"
                "if sys.argv[1:3] != ['auth', 'status']:\n"
                "    sys.exit(3)\n"
                "print(os.environ['FAKE_STATUS_JSON'])\n"
            ))
            valid = {"auth_method": "oauth2", "token_valid": True, "email": "person@example.com",
                     "access_token": "ya29.secret", "refresh_token": "secret-refresh"}
            with mock.patch.dict(os.environ, {"FAKE_STATUS_JSON": json.dumps(valid)}):
                health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["state"], "valid")
            self.assertTrue(health["stored"])
            self.assertTrue(health["valid"])
            self.assertNotIn("ya29", json.dumps(health))
            self.assertNotIn("secret", json.dumps(health))

            invalid = {"auth_method": "oauth2", "token_valid": False,
                       "access_token": "ya29.secret", "refresh_token": "secret-refresh"}
            with mock.patch.dict(os.environ, {"FAKE_STATUS_JSON": json.dumps(invalid)}):
                health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["state"], "invalid")
            self.assertFalse(health["valid"])
            self.assertTrue(health["reauth_required"])
            self.assertNotIn("secret", json.dumps(health))

            unreadable = {"auth_method": "none", "credentials_readable": False}
            with mock.patch.dict(os.environ, {"FAKE_STATUS_JSON": json.dumps(unreadable)}):
                health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["state"], "unreadable")
            self.assertTrue(health["reauth_required"])

    def test_public_execute_blocks_auth_but_internal_bounded_keeps_it(self):
        from router import bounded_gws
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            fake = _fake_gws(tmp, "print('{\"refresh_token\": \"secret-refresh\"}')\n")
            for argv in (["auth", "export", "--unmasked"], ["--format", "json", "auth", "export"],
                         ["help", "auth", "export"]):
                result = execute_gws(root, "person@example.com", argv, gws_bin=str(fake))
                self.assertEqual(result["error_code"], "auth_blocked")
                self.assertNotIn("secret", json.dumps(result))
            internal = bounded_gws(root, "person@example.com", ["auth", "export"], gws_bin=str(fake))
            self.assertTrue(internal["ok"])

    def test_health_reports_only_sanitized_granted_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            status = {"token_valid": True, "access_token": "ya29.secret", "scopes": [
                "https://www.googleapis.com/auth/drive", "openid", "https://www.googleapis.com/auth/drive",
                "ya29.secret-token", "https://evil.example/auth?x=secret", 7]}
            fake = _fake_gws(tmp, "print(" + repr(json.dumps(status)) + ")\n")
            health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["scopes"], ["https://www.googleapis.com/auth/drive", "openid"])
            self.assertNotIn("secret", json.dumps(health))
            fake = _fake_gws(tmp, "print('{\"token_valid\": true}')\n")
            self.assertNotIn("scopes", account_health(root, "person@example.com", gws_bin=str(fake)))

    def test_health_failure_is_sanitized_and_marks_account_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            fake = _fake_gws(tmp, (
                "import sys\n"
                "sys.stderr.write('invalid_grant refresh_token=secret-refresh\\n')\n"
                "sys.exit(2)\n"
            ))
            health = account_health(root, "person@example.com", gws_bin=str(fake))

        self.assertEqual(health["state"], "invalid")
        self.assertEqual(health["error_code"], "invalid_grant")
        self.assertTrue(health["reauth_required"])
        self.assertNotIn("secret", json.dumps(health))

    def test_only_current_profile_account_is_exposed_to_gws(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            other = Path(tmp) / "other"
            for base in (root, other):
                account = base / "accounts" / "person@example.com"
                account.mkdir(parents=True)
                (account / "credentials.enc").write_text("fake")
            fake = Path(tmp) / "gws"
            fake.write_text("#!" + sys.executable + "\nimport json,os,sys\nprint(json.dumps({'config':os.getenv('GOOGLE_WORKSPACE_CLI_CONFIG_DIR'),'token':os.getenv('GOOGLE_WORKSPACE_CLI_TOKEN'),'args':sys.argv[1:]}))\n")
            fake.chmod(0o700)
            previous = os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN")
            os.environ["GOOGLE_WORKSPACE_CLI_TOKEN"] = "fixture-value"
            try:
                self.assertEqual(list_accounts(root), ["person@example.com"])
                raw_args = ["drive", "+upload", "/tmp/report.pdf", "--output", "out.bin"]
                result = execute_gws(root, "person@example.com", raw_args, gws_bin=str(fake))
            finally:
                if previous is None:
                    os.environ.pop("GOOGLE_WORKSPACE_CLI_TOKEN", None)
                else:
                    os.environ["GOOGLE_WORKSPACE_CLI_TOKEN"] = previous
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["config"], str(root / "accounts" / "person@example.com"))
            self.assertIsNone(result["data"]["token"])
            self.assertEqual(result["data"]["args"], raw_args)
            with self.assertRaises(ValueError):
                execute_gws(root, "../other", ["drive", "files", "list"], gws_bin=str(fake))

    @unittest.skipIf(os.name != "posix", "POSIX flock check")
    def test_different_accounts_run_in_parallel_while_same_account_serializes(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            for name in ("one@example.com", "two@example.com"):
                _account(root, name)
            fake = _fake_gws(tmp, "import time\ntime.sleep(0.45)\nprint('{}')\n")

            def run(name):
                return execute_gws(root, name, ["drive", "files", "list"], gws_bin=str(fake))

            started = time.monotonic()
            threads = [threading.Thread(target=run, args=(name,)) for name in
                       ("one@example.com", "two@example.com")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            parallel = time.monotonic() - started
            self.assertLess(parallel, 0.78)

            started = time.monotonic()
            threads = [threading.Thread(target=run, args=("one@example.com",)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            serialized = time.monotonic() - started
            self.assertGreater(serialized, 0.78)

    def test_bounded_gws_accepts_empty_success_but_not_malformed_json(self):
        from router import bounded_gws
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            for output, ok in (("", True), ("not json", False)):
                fake = _fake_gws(tmp, "import sys\nsys.stdout.write(" + repr(output) + ")\n")
                result = bounded_gws(root, "person@example.com", ["gmail", "users", "stop"],
                                     gws_bin=str(fake))
                self.assertEqual(result["ok"], ok)
                if ok:
                    self.assertEqual(result["data"], {})

    def test_bounded_gws_exposes_status_and_sanitizes_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            fake = _fake_gws(tmp, (
                "import sys\n"
                "sys.stdout.write('{\"error\":{\"code\":429,\"message\":\"secret-body\"}}')\n"
                "sys.exit(1)\n"
            ))
            from router import bounded_gws
            result = bounded_gws(root, "person@example.com", ["drive", "files", "list"],
                                 gws_bin=str(fake))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 429)
        self.assertTrue(result["retryable"])
        self.assertNotIn("secret-body", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
