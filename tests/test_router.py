import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router import account_dir, account_health, account_identity, bounded_gws, classify_gws_failure, execute_gws, files_dir, list_accounts


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
    def test_native_dotenv_ancestor_injection_is_stopped_by_empty_sentinel(self):
        import subprocess
        from router import _account_env
        binary = shutil.which("gws")
        if not binary:
            self.skipTest("native gws is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            slot = _account(root, content="not-encrypted-synthetic")
            # All files are synthetic; auth status cannot refresh these credentials.
            (root / ".env").write_text("GOOGLE_WORKSPACE_CLI_TOKEN=synthetic-dotenv-only\n")
            unguarded = root / "unguarded"
            unguarded.mkdir()
            result = subprocess.run([binary, "auth", "status"], env=_account_env(slot),
                                    cwd=unguarded, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["credential_source"], "token_env_var")
            result = bounded_gws(root, slot.name, ["auth", "status"], gws_bin=binary)
            self.assertTrue(result["ok"])
            self.assertNotEqual(result["data"]["credential_source"], "token_env_var")
            self.assertFalse(result["data"]["encryption_valid"])
            sentinel = root / "files/.env"
            self.assertEqual(sentinel.stat().st_size, 0)
            self.assertFalse(account_health(root, slot.name, gws_bin=binary)["valid"])

    @unittest.skipUnless(os.name == "posix", "POSIX sentinel safety")
    def test_unsafe_dotenv_sentinels_never_spawn_or_truncate(self):
        for kind in ("content", "symlink", "hardlink", "directory", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                slot = _account(tmp)
                directory = Path(tmp) / "files"
                directory.mkdir()
                sentinel = directory / ".env"
                foreign = Path(tmp) / "foreign"
                foreign.write_text("synthetic-do-not-truncate")
                if kind == "content":
                    sentinel.write_text("GOOGLE_WORKSPACE_CLI_TOKEN=synthetic\n")
                elif kind == "symlink":
                    sentinel.symlink_to(foreign)
                elif kind == "hardlink":
                    os.link(foreign, sentinel)
                elif kind == "directory":
                    sentinel.mkdir()
                else:
                    os.mkfifo(sentinel)
                with mock.patch("router.subprocess.run") as run:
                    for call in (execute_gws, bounded_gws):
                        self.assertFalse(call(tmp, slot.name, ["drive", "files", "list"])["ok"])
                    self.assertIsNone(account_health(tmp, slot.name)["valid"])
                    run.assert_not_called()
                self.assertEqual(foreign.read_text(), "synthetic-do-not-truncate")

    def test_relative_binary_and_files_keep_documented_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = _account(tmp)
            directory = files_dir(tmp)
            (directory / "input.txt").write_text("relative input")
            absolute = Path(tmp) / "absolute.txt"
            absolute.write_text("absolute input")
            fake = _fake_gws(tmp, """import json, sys
from pathlib import Path
args = sys.argv[1:]
if args == ['auth', 'status']:
    print(json.dumps({'token_valid': Path('.env').stat().st_size == 0}))
else:
    source = Path(args[args.index('--upload') + 1])
    target = Path(args[args.index('--output') + 1])
    target.write_text(source.read_text())
    print(json.dumps({'cwd': str(Path.cwd()), 'binary': sys.argv[0]}))
""")
            binary = os.path.relpath(fake)
            for call in (execute_gws, bounded_gws):
                for source, output in (("input.txt", "output.txt"),
                                       (str(absolute), str(Path(tmp) / "absolute-out.txt"))):
                    result = call(tmp, slot.name, ["drive", "+upload", "--upload", source,
                                                   "--output", output], gws_bin=binary)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["data"]["cwd"], str(directory))
                    self.assertTrue(Path(result["data"]["binary"]).is_absolute())
                    self.assertEqual((directory / output).read_text(),
                                     (directory / source).read_text())
            self.assertTrue(account_health(tmp, slot.name, gws_bin=binary)["valid"])

    def test_explicit_identity_native_user_gmail_fallback_and_no_scope_limit(self):
        cases = [
            ({"token_valid": True, "user": "PERSON@example.com"}, {}, True, None, 1),
            ({"token_valid": True, "user": "foreign@example.com"}, {}, False, "identity_mismatch", 1),
            ({"token_valid": True, "scopes": ["https://www.googleapis.com/auth/drive"]}, {},
             False, "identity_unconfirmed", 1),
            ({"token_valid": True, "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
             {"emailAddress": "person@example.com"}, True, None, 2),
            ({"token_valid": True, "scopes": ["https://www.googleapis.com/auth/gmail.metadata"]},
             {"emailAddress": "foreign@example.com"}, False, "identity_mismatch", 2),
            ({"token_valid": True, "scopes": ["https://www.googleapis.com/auth/gmail.send"]}, {},
             False, "identity_unconfirmed", 1),
            ({"token_valid": True, "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
             {}, False, "identity_unconfirmed", 2),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            slot = _account(tmp)
            for status, profile, ok, code, count in cases:
                with self.subTest(status=status, profile=profile):
                    replies = [mock.Mock(returncode=0, stdout=json.dumps(data), stderr="")
                               for data in (status, profile)]
                    with mock.patch("router.subprocess.run", side_effect=replies) as run:
                        result = account_identity(tmp, slot.name)
                    self.assertEqual(result["ok"], ok)
                    self.assertEqual(result.get("error_code"), code)
                    self.assertEqual(run.call_count, count)
                    self.assertNotIn("foreign", json.dumps(result))
                    if count == 2:
                        self.assertEqual(run.call_args.args[0][1:4], ["gmail", "users", "getProfile"])
                        self.assertLessEqual(run.call_args.kwargs["timeout"], 20)
            with mock.patch("router.subprocess.run", return_value=mock.Mock(returncode=0,
                    stdout='{"token_valid":true,"user":"foreign@example.com"}', stderr="")):
                self.assertEqual(account_health(tmp, slot.name)["error_code"], "identity_mismatch")

    def test_minimal_environment_is_shared_by_all_native_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            slot = _account(root)
            foreign = Path(tmp) / "foreign"
            adc = foreign / ".config/gcloud/application_default_credentials.json"
            adc.parent.mkdir(parents=True)
            adc.write_text('{"type":"authorized_user","refresh_token":"synthetic-only"}')
            injected = {name: "synthetic-only" for name in (
                "GOOGLE_WORKSPACE_CLI_TOKEN", "GOOGLE_WORKSPACE_CLI_CLIENT_SECRET",
                "GOOGLE_WORKSPACE_CLI_LOG", "GOOGLE_WORKSPACE_PROJECT_ID", "OPENAI_API_KEY",
                "RUST_LOG", "PYTHONPATH", "HTTPS_PROXY", "CLOUDSDK_CONFIG", "XDG_CONFIG_HOME")}
            injected.update(HOME=str(foreign), GOOGLE_APPLICATION_CREDENTIALS=str(adc),
                            GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=str(adc))
            fake = _fake_gws(tmp, """import json, os
from pathlib import Path
allowed = {'PATH', 'HOME', 'GOOGLE_WORKSPACE_CLI_CONFIG_DIR', 'GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND'}
if os.name == 'nt':
    allowed.update({'SYSTEMROOT', 'USERPROFILE'})
home = Path(os.environ['HOME'])
isolated = (set(os.environ) <= allowed | {'LC_CTYPE'} and
            not (home / '.config/gcloud/application_default_credentials.json').exists())
print(json.dumps({'token_valid': isolated, 'isolated': isolated, 'home': str(home),
                  'config': os.environ['GOOGLE_WORKSPACE_CLI_CONFIG_DIR'],
                  'backend': os.environ['GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND']}))
""")
            with mock.patch.dict(os.environ, injected):
                for call in (execute_gws, bounded_gws):
                    result = call(root, slot.name, ["drive", "files", "list"], gws_bin=str(fake))
                    self.assertTrue(result["ok"])
                    self.assertTrue(result["data"]["isolated"])
                    self.assertEqual(result["data"]["config"], str(slot.resolve()))
                    self.assertEqual(result["data"]["backend"], "file")
                    home = Path(result["data"]["home"])
                    self.assertTrue(home.is_relative_to(slot))
                    self.assertEqual(home.stat().st_mode & 0o777, 0o700)
                self.assertTrue(account_health(root, slot.name, gws_bin=str(fake))["valid"])
                self.assertEqual(os.environ["HOME"], str(foreign))

    def test_concurrent_accounts_have_distinct_homes_and_absolute_stores(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            names = ("one@example.com", "two@example.com")
            for name in names:
                _account(root, name)
            fake = _fake_gws(tmp, "import json, os\nprint(json.dumps({k: os.environ[k] for k in "
                             "('HOME', 'GOOGLE_WORKSPACE_CLI_CONFIG_DIR')}))\n")
            relative_root = os.path.relpath(root)
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda name: execute_gws(
                    relative_root, name, ["drive", "files", "list"], gws_bin=str(fake)), names))
            homes = set()
            for name, result in zip(names, results):
                self.assertTrue(result["ok"])
                config = Path(result["data"]["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"])
                self.assertEqual(config, root / "accounts" / name)
                home = Path(result["data"]["HOME"])
                self.assertTrue(home.is_relative_to(config))
                homes.add(home)
            self.assertEqual(len(homes), 2)

    def test_missing_slots_and_malformed_aliases_never_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts/empty@example.com").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"GOOGLE_WORKSPACE_CLI_TOKEN": "synthetic-only",
                    "GOOGLE_APPLICATION_CREDENTIALS": str(root / "foreign-adc.json")}), \
                    mock.patch("router.subprocess.run") as run:
                for name in ("missing@example.com", "empty@example.com", "../escape", ".", "..",
                             "/absolute", "bad/name", "bad\\name", "", None, "a" * 129):
                    with self.subTest(account=name):
                        with self.assertRaises(ValueError):
                            execute_gws(root, name, ["drive", "files", "list"])
                        self.assertFalse(bounded_gws(root, name, ["auth", "status"])["ok"])
                run.assert_not_called()

    @unittest.skipIf(os.name != "posix", "POSIX symlink regression")
    def test_symlinked_store_paths_and_native_files_are_rejected(self):
        for part in ("root", "ancestor", "accounts", "slot", "credentials.enc", "credentials.json",
                     ".encryption_key", "client_secret.json", "token_cache.json", "sa_token_cache.json"):
            with self.subTest(part=part), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "profile"
                slot = _account(root)
                target = {"root": root, "ancestor": root, "accounts": slot.parent,
                          "slot": slot}.get(part, slot / part)
                moved = Path(tmp) / "foreign"
                if target.exists():
                    target.rename(moved)
                target.symlink_to(moved)  # Also test dangling native-file symlinks.
                if part == "ancestor":
                    root = root / "nested"
                    _account(moved / "nested")
                with self.assertRaises(ValueError):
                    account_dir(root, slot.name)
                self.assertEqual(list_accounts(root), [])
                with mock.patch("router.subprocess.run") as run:
                    self.assertFalse(bounded_gws(root, slot.name, ["auth", "status"])["ok"])
                    run.assert_not_called()

    @unittest.skipIf(os.name != "posix", "POSIX native-file metadata regression")
    def test_nonregular_and_hardlinked_native_files_fail_closed(self):
        for kind in ("directory", "fifo", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                slot = _account(tmp)
                target = slot / "token_cache.json"
                if kind == "directory":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    os.link(slot / "credentials.enc", target)
                with self.assertRaises(ValueError):
                    account_dir(tmp, slot.name)
                with mock.patch("router.subprocess.run") as run:
                    self.assertFalse(bounded_gws(tmp, slot.name, ["auth", "status"])["ok"])
                    run.assert_not_called()

    @unittest.skipIf(os.name != "posix", "POSIX symlink regression")
    def test_isolated_home_and_lock_symlinks_fail_closed(self):
        import hashlib
        from router import _account_env
        for part in ("home", "adc", "swap_lock", "account_lock", "lock_dir"):
            with self.subTest(part=part), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "profile"
                slot = _account(root)
                home = Path(_account_env(slot)["HOME"])
                self.assertTrue(home.is_relative_to(slot))  # Never mutate a real HOME on failure.
                foreign = Path(tmp) / "foreign"
                foreign.mkdir()
                if part == "home":
                    home.rmdir()
                    home.symlink_to(foreign)
                elif part == "adc":
                    (home / ".config").symlink_to(foreign)
                else:
                    name = hashlib.sha256(slot.name.encode()).hexdigest()[:32]
                    target = {"swap_lock": slot.parent / ".lock", "lock_dir": root / ".locks",
                              "account_lock": root / ".locks" / f"{name}.lock"}[part]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(foreign if part == "lock_dir" else foreign / "missing")
                with mock.patch("router.subprocess.run") as run:
                    self.assertFalse(bounded_gws(root, slot.name, ["auth", "status"])["ok"])
                    run.assert_not_called()
                self.assertEqual(list(foreign.iterdir()), [])

    def test_native_corrupt_store_status_is_sanitized(self):
        binary = shutil.which("gws")
        if not binary:
            self.skipTest("native gws is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            slot = _account(root, content="not-an-encrypted-credential")
            # No valid refresh material: native status cannot make an OAuth request.
            foreign = Path(tmp) / "foreign"
            adc = foreign / ".config/gcloud/application_default_credentials.json"
            adc.parent.mkdir(parents=True)
            adc.write_text("not-json-synthetic-adc")
            with mock.patch.dict(os.environ, {"HOME": str(foreign),
                    "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                    "GOOGLE_WORKSPACE_CLI_TOKEN": "synthetic-only"}):
                health = account_health(root, slot.name, gws_bin=binary)
            self.assertEqual(health["state"], "unreadable")
            self.assertFalse(health["valid"])
            self.assertTrue(health["reauth_required"])
            self.assertNotIn("synthetic", json.dumps(health))
            self.assertNotIn(str(tmp), json.dumps(health))

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
            (files / ".env").unlink()
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
        self.assertIn("gws auth login", result["error"])
        self.assertNotIn("gws_login", result["error"])
        rendered = json.dumps(result)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("refresh_token", rendered)

    def test_health_reports_valid_stored_and_unreadable_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            valid = {"auth_method": "oauth2", "token_valid": True, "email": "person@example.com",
                     "access_token": "ya29.secret", "refresh_token": "secret-refresh"}
            fake = _fake_gws(tmp, "print(" + repr(json.dumps(valid)) + ")\n")
            health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["state"], "valid")
            self.assertTrue(health["stored"])
            self.assertTrue(health["valid"])
            self.assertNotIn("ya29", json.dumps(health))
            self.assertNotIn("secret", json.dumps(health))

            invalid = {"auth_method": "oauth2", "token_valid": False,
                       "access_token": "ya29.secret", "refresh_token": "secret-refresh"}
            fake = _fake_gws(tmp, "print(" + repr(json.dumps(invalid)) + ")\n")
            health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["state"], "invalid")
            self.assertFalse(health["valid"])
            self.assertTrue(health["reauth_required"])
            self.assertNotIn("secret", json.dumps(health))

            unreadable = {"auth_method": "none", "credentials_readable": False}
            fake = _fake_gws(tmp, "print(" + repr(json.dumps(unreadable)) + ")\n")
            health = account_health(root, "person@example.com", gws_bin=str(fake))
            self.assertEqual(health["state"], "unreadable")
            self.assertTrue(health["reauth_required"])

    def test_auth_is_blocked_except_internal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            _account(root)
            fake = _fake_gws(tmp, "print('{\"auth_method\": \"none\"}')\n")
            for argv in (["auth", "export", "--unmasked"], ["--format", "json", "auth", "export"],
                         ["help", "auth", "export"], ["auth", "login"], ["auth", "logout"]):
                with mock.patch("router.subprocess.run") as run:
                    for call in (execute_gws, bounded_gws):
                        result = call(root, "person@example.com", argv, gws_bin=str(fake))
                        self.assertEqual(result["error_code"], "auth_blocked")
                        self.assertNotIn("gws_login", json.dumps(result))
                    run.assert_not_called()
            internal = bounded_gws(root, "person@example.com", ["auth", "status"], gws_bin=str(fake))
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
