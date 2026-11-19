import asyncio
import functools
import importlib.util
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_origin import OriginBindings
from watch import WatchError


class OriginTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.bindings = OriginBindings(ttl=10, max_entries=4, clock=lambda: self.now)
        self.context: dict[str, Any] = dict(source=SimpleNamespace(user_id="actor", platform="discord", chat_id="chat",
                                                  thread_id="thread", profile="default"),
                            session_id="physical", session_key="route", platform="discord", profile="default")

    def bind(self, **kwargs):
        return asyncio.run(self.bindings.bind("", **{**self.context, **kwargs}))

    def test_real_public_command_invocation_seam(self):
        host = Path(os.environ.get("HERMES_AGENT_SRC", Path.home() / ".hermes" / "hermes-agent"))
        path = host / "hermes_cli/plugin_command_dispatch.py"
        self.assertTrue(path.is_file(), "Real host command dispatch source is required")
        spec = importlib.util.spec_from_file_location("host_command_dispatch", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = functools.partial(self.bindings.bind, profile="default")
        context = {key: value for key, value in self.context.items() if key != "profile"}
        result = asyncio.run(module.invoke_plugin_command(handler, "", **context))
        self.assertIn("origin bound", result)
        origin = self.bindings.origin("physical", "default")
        self.assertEqual(origin["principal"], "actor")
        self.assertEqual(origin["session_key"], "route")

    def test_unbound_child_empty_and_profile_refused_despite_global_fallback(self):
        self.bind()
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "physical", "HERMES_SESSION_USER_ID": "actor",
                                         "HERMES_SESSION_KEY": "route", "USER": "actor"}):
            for sid, profile in (("", "default"), ("child-rebound-id", "default"),
                                 ("physical", "other"), ("after-new", "default")):
                with self.assertRaises(WatchError):
                    self.bindings.origin(sid, profile)

    def test_no_actor_fallback_and_no_model_destination(self):
        self.context["source"].user_id = None
        with mock.patch.dict(os.environ, {"HERMES_SESSION_USER_ID": "victim", "USER": "victim"}):
            self.assertIn("refused", self.bind())
        self.context["source"].user_id = "actor"
        self.assertIn("Usage", asyncio.run(self.bindings.bind("chat=attacker", **self.context)))
        with self.assertRaises(WatchError):
            self.bindings.origin("physical", "default")

    def test_conflicting_actor_does_not_rebind_physical_session(self):
        self.bind()
        self.context["source"].user_id = "other"
        self.assertIn("refused", self.bind())
        self.assertEqual(self.bindings.origin("physical", "default")["principal"], "actor")

    def test_expiry_cleanup_capacity_and_restart(self):
        for i in range(4):
            self.assertIn("origin bound", self.bind(session_id=str(i)))
        self.assertIn("capacity", self.bind(session_id="overflow"))
        self.now += 11
        self.assertIn("origin bound", self.bind(session_id="fresh"))
        self.assertEqual(len(self.bindings._bindings), 1)
        with self.assertRaises(WatchError):
            OriginBindings().origin("fresh", "default")

    def test_concurrent_origins_and_defensive_copy(self):
        def run(i):
            source = SimpleNamespace(user_id=f"u{i}", platform="discord", chat_id=f"c{i}")
            self.bind(session_id=f"s{i}", session_key=f"k{i}", source=source)
            return self.bindings.origin(f"s{i}", "default")
        with ThreadPoolExecutor(max_workers=4) as executor:
            origins = list(executor.map(run, range(4)))
        for i, origin in enumerate(origins):
            self.assertEqual(origin["principal"], f"u{i}")
            self.assertEqual(origin["chat_id"], f"c{i}")
        origins[0]["chat_id"] = "tampered"
        self.assertEqual(self.bindings.origin("s0", "default")["chat_id"], "c0")

    def test_source_platform_profile_and_empty_session_checks(self):
        self.assertIn("refused", self.bind(session_id=""))
        self.assertIn("refused", self.bind(platform="telegram"))
        self.assertIn("refused", self.bind(profile="other"))
        self.context["source"].platform = "webhook"
        self.assertIn("refused", self.bind(platform="webhook"))


if __name__ == "__main__":
    unittest.main()
