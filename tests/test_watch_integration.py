"""Real-host integration paths, isolated from the active profile.

These tests exercise the installed Hermes webhook CAS helpers in a temporary
home. They do not prove live Google delivery or native session execution.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch_routes


class HostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patch = mock.patch("hermes_constants.get_hermes_home", return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_real_host_webhook_cas_round_trip(self):
        manager = watch_routes.RouteManager(SimpleNamespace())
        self.assertTrue(watch_routes.native_available())
        watch = {"watch_id": "1234567890abcdef", "service": "gmail", "account": "person@example.com",
                 "generation": 1}
        spec = {"instructions": "Summarize the matching mail metadata", "skills": [],
                "mode": "new_session", "account": "person@example.com"}
        origin = {"profile": "default", "platform": "discord", "chat_id": "chat",
                  "thread_id": "thread", "session_key": "k", "session_id": "s", "principal": "u"}
        public = manager.create(watch, spec, origin)
        path = self.home / "webhook_subscriptions.json"
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(path.read_text())
        route = stored[public["name"]]
        self.assertEqual(route["description"], "gws-watch owned route watch=1234567890abcdef mode=new_session")
        self.assertEqual(route["deliver"], "discord")
        self.assertEqual(route["deliver_extra"]["chat_id"], "chat")
        self.assertEqual(route["signature"], "generic")
        self.assertEqual(route["watch_digest"], watch_routes._digest(route))
        self.assertEqual(manager.remove(public["name"], watch["watch_id"])["state"], "removed")
        self.assertEqual(json.loads(path.read_text()), {})

    def test_real_host_route_disabled_is_not_recreated_or_removed(self):
        manager = watch_routes.RouteManager(SimpleNamespace())
        watch = {"watch_id": "abcdef1234567890", "service": "drive", "account": "person@example.com",
                 "generation": 1}
        spec = {"instructions": "x", "skills": [], "mode": "new_session",
                "account": "person@example.com"}
        origin = {"profile": "default", "platform": "discord", "chat_id": "chat",
                  "thread_id": None, "session_key": "k", "session_id": "s", "principal": "u"}
        public = manager.create(watch, spec, origin)
        path = self.home / "webhook_subscriptions.json"
        data = json.loads(path.read_text())
        data[public["name"]]["enabled"] = False
        path.write_text(json.dumps(data))
        with self.assertRaises(watch_routes.RouteError) as raised:
            manager.create(watch, spec, origin)
        self.assertEqual(raised.exception.code, "native_route_disabled")
        self.assertEqual(manager.remove(public["name"], watch["watch_id"])["state"],
                         "preserved_disabled")
        self.assertIn(public["name"], json.loads(path.read_text()))

    def test_installed_host_public_injection_uses_existing_session_key_api(self):
        from hermes_cli.plugins_context import PluginContext
        calls = {}

        class FakeManager:
            home_path = self.home
            has_gateway_message_injector = True
            has_tui_message_injector = False
            _cli_ref = None
            _plugins = {}

            def inject_gateway_message(self, **kwargs):
                calls.update(kwargs)
                return True

        fake_ctx = SimpleNamespace(
            plugin_id="gws-oauth",
            _manager=FakeManager(),
            _gateway_injection_allowed=lambda: True,
        )
        self.assertTrue(PluginContext.inject_message(
            fake_ctx, "hello", session_key="agent:default:discord:chat:thread"))
        self.assertNotIn("expected_session_id", calls)
        self.assertEqual(calls["session_key"], "agent:default:discord:chat:thread")
        self.assertNotIn("origin", calls)

    def test_registration_does_not_spawn_worker(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gws_oauth_int", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        tools, hooks = {}, []
        ctx = SimpleNamespace(
            get_config=lambda key, default=None: default,
            register_tool=lambda **kwargs: tools.setdefault(kwargs["name"], kwargs),
            register_hook=lambda name, callback: hooks.append(name),
            on_unload=lambda callback: None,
        )
        with mock.patch("hermes_constants.get_hermes_home", return_value=self.home), \
                mock.patch("gateway.session_context.get_session_env", return_value=None), \
                mock.patch("gateway.session_context.bound_session_key", return_value=""):
            plugin.register(ctx)
        self.assertEqual(set(tools), {"gws_login", "gws_accounts", "gws_api",
                                      "gws_gmail_watch", "gws_drive_watch", "gws_calendar_watch"})
        self.assertIn("pre_gateway_dispatch", hooks)
        self.assertFalse((self.home / "plugin-data" / "gws-oauth" / "worker.lock").exists())
        # Registration never starts the supervised worker; its lock only exists after `run`.
        self.assertTrue(all("watch-worker" not in thread.name for thread in __import__("threading").enumerate()))


if __name__ == "__main__":
    unittest.main()
