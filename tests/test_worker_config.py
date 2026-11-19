"""Standalone worker uses the same profile settings as the gateway."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import watch_worker as worker


class WorkerConfigTests(unittest.TestCase):
    def test_exact_profile_shape_precedence_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(worker.profile_settings(home), {})
            path = home / "config.yaml"
            path.write_text(json.dumps({"plugins": {"entries": {"google-workspace": {
                "config": {"gws_bin": "old", "watch_retention_days": 4},
                "settings": {"gws_bin": "new"}}}}}))
            self.assertEqual(worker.profile_settings(home), {"gws_bin": "new", "watch_retention_days": 4})
            for invalid in ("[]", "plugins: []", "plugins: {entries: {google-workspace: {settings: []}}}", "[bad"):
                path.write_text(invalid)
                with self.assertRaisesRegex(ValueError, "Invalid google-workspace"):
                    worker.profile_settings(home)

    def test_cli_overrides_and_routes_are_reread(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
            path = Path(tmp) / "config.yaml"
            settings = {"gws_bin": "configured-gws", "watch_gmail_topic": "configured-topic",
                        "watch_reconcile_seconds": 42, "watch_retention_days": 3,
                        "watch_subscription_backoff_max": 80, "watch_routes": {"routes": []}}
            def save():
                path.write_text(json.dumps({"plugins": {"entries": {"google-workspace": {"settings": settings}}}}))
            save()
            instance = mock.Mock(run_forever=mock.AsyncMock())
            with mock.patch.object(worker, "Worker", return_value=instance) as make, \
                 mock.patch.object(worker, "GwsClient") as client, \
                 mock.patch.object(worker.watch_routes, "RouteManager") as routes:
                self.assertEqual(worker.main(["run", "--home", tmp, "--profile", "work",
                                              "--gmail-topic", "cli-topic", "--callback-port", "0"]), 0)
                config = make.call_args.args[3]
                self.assertEqual(config["watch_gmail_topic"], "cli-topic")
                self.assertEqual(config["watch_reconcile_seconds"], 42)
                self.assertEqual(config["watch_retention_days"], 3)
                self.assertEqual(config["watch_subscription_backoff_max"], 80)
                client.assert_called_once_with(tmp, gws_bin="configured-gws")
                self.assertEqual(routes.call_args.kwargs["profile"], "work")
                reread = routes.call_args.kwargs["config"]
                self.assertEqual(reread(), {"routes": []})
                settings["watch_routes"] = {"routes": [{"name": "changed"}]}
                save()
                self.assertEqual(reread(), settings["watch_routes"])
                path.write_text("[]")
                with self.assertRaises(ValueError):
                    reread()
