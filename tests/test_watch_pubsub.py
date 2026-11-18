"""Offline transport checks: fake credentials and HTTP only, no Google calls."""
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import watch_pubsub as P
from watch_worker import GwsClient


class PubSubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.client = GwsClient(self.home)
        for name in ("one@example.com", "two@example.com"):
            path = self.home / "gws-oauth" / "accounts" / name
            path.mkdir(parents=True)
            (path / "credentials.enc").write_bytes(b"fake-encrypted-fixture")
        self.credentials = {"type": "authorized_user", "client_id": "fixture-id",
                            "client_secret": "fixture-secret", "refresh_token": "fixture-refresh"}
        self.export = patch.object(P.subprocess, "run", side_effect=self.exported).start()
        self.real_post = P._post
        self.http = patch.object(P, "_post", side_effect=self.response).start()
        self.addCleanup(patch.stopall)
        self.requests = []

    def exported(self, args, **kwargs):
        self.assertEqual(args, ["gws", "auth", "export", "--unmasked"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"], "file")
        self.assertNotIn("GOOGLE_WORKSPACE_CLI_TOKEN", kwargs["env"])
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", kwargs["env"])
        return subprocess.CompletedProcess(args, 0, json.dumps(self.credentials).encode())

    def response(self, url, data, headers, timeout, *args):
        self.requests.append((url, data, headers))
        if url == P._TOKEN_URL:
            return {"access_token": "fixture-access", "expires_in": 3600}
        return {}

    def call(self, operation="pull", body=None, account="one@example.com", subscription=None):
        return self.client.call(account, ["pubsub:v1", "projects", "subscriptions", operation],
                                {"subscription": subscription or "projects/example/subscriptions/watch"},
                                body=body or {"maxMessages": 20, "returnImmediately": True})

    def test_real_worker_dispatch_cache_and_account_isolation(self):
        with patch.dict(os.environ,
                        {"GOOGLE_WORKSPACE_CLI_TOKEN": "wrong", "GOOGLE_APPLICATION_CREDENTIALS": "wrong"}):
            self.assertTrue(self.call()["ok"])
            self.assertTrue(self.call("acknowledge", {"ackIds": ["ack-1"]})["ok"])
            self.assertTrue(self.call(account="two@example.com")["ok"])
        self.assertEqual(sum(url == P._TOKEN_URL for url, _, _ in self.requests), 2)
        self.assertEqual(json.loads(self.requests[2][1]), {"ackIds": ["ack-1"]})
        self.assertTrue(self.requests[2][0].endswith(":acknowledge"))
        self.assertEqual(self.requests[1][2]["Authorization"], "Bearer fixture-access")
        paths = [call.kwargs["env"]["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] for call in self.export.call_args_list]
        self.assertTrue(paths[0].endswith("one@example.com"))
        self.assertTrue(paths[-1].endswith("two@example.com"))

    def test_reauthorization_and_expiry_refresh(self):
        self.assertTrue(self.call()["ok"])
        self.credentials["refresh_token"] = "fixture-replaced"
        self.assertTrue(self.call()["ok"])
        key = "one@example.com"
        fp, access, _ = self.client.pubsub._tokens[key]
        self.client.pubsub._tokens[key] = (fp, access, 0)
        self.assertTrue(self.call()["ok"])
        self.assertEqual(sum(url == P._TOKEN_URL for url, _, _ in self.requests), 3)

    def test_invalid_destination_operation_and_account_never_export(self):
        for sub in ("projects/p/subscriptions/../bad", "https://evil.test", "projects/p/subscriptions/x?token=x"):
            self.assertFalse(self.call(subscription=sub)["ok"])
        self.assertFalse(self.call(operation="delete")["ok"])
        self.assertFalse(self.call(account="../escape")["ok"])
        self.export.assert_not_called()
        self.http.assert_not_called()

    def test_failures_are_secret_free_and_401_clears_cache(self):
        self.assertTrue(self.call()["ok"])
        self.http.side_effect = urllib.error.HTTPError("https://pubsub.googleapis.com", 401,
                                                       "fixture-secret", {}, io.BytesIO(b"fixture-refresh"))
        result = self.call()
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn("fixture", json.dumps(result))
        self.assertEqual(self.client.pubsub._tokens, {})
        self.export.side_effect = subprocess.TimeoutExpired("fixture-secret", 30, output=b"fixture-refresh")
        self.assertNotIn("fixture", json.dumps(self.call()))

    def test_bad_export_and_untrusted_token_uri(self):
        self.credentials["token_uri"] = "https://evil.test"
        self.assertTrue(self.call()["ok"])
        self.assertEqual(self.requests[0][0], P._TOKEN_URL)
        del self.credentials["refresh_token"]
        self.assertEqual(self.call()["error_code"], "pubsub_credentials")

    def test_redirects_refused_and_response_bounded(self):
        handler = P._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://evil.test"))
        # Exercise the real HTTP JSON reader with an in-memory response.
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b"x" * 11
        with patch.object(P.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                self.real_post("https://pubsub.googleapis.com", b"{}", {}, 1, limit=10)


if __name__ == "__main__":
    unittest.main()
