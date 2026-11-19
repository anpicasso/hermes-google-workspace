"""Exercise the real host ContextVars with synthetic routes, not live delivery."""
import contextvars
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

HOST = Path(os.environ.get("HERMES_AGENT_SRC", Path.home() / ".hermes/hermes-agent"))
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import session_context as S
from agent.delegation_context import delegated_child_context
from session_origin import current_origin
from watch import WatchError


class OriginTests(unittest.TestCase):
    def setUp(self):
        self.context = contextvars.Context()
        self.fields: dict[str, Any] = dict(platform="discord", chat_id="chat", thread_id="thread",
                           user_id="actor", actor_id="actor", profile="default",
                           session_id="physical", session_key="route", cron_session="")
        self.context.run(S.set_session_vars, **self.fields)

    def origin(self, sid="physical", profile="default"):
        return self.context.run(current_origin, sid, profile)

    def test_automatic_snapshot_without_command(self):
        origin = self.origin()
        self.assertEqual(origin["principal"], "actor")
        self.assertEqual(origin["session_key"], "route")
        self.assertEqual(origin["session_id"], "physical")
        self.assertEqual(origin["thread_id"], "thread")
        origin["chat_id"] = "tampered"
        self.assertEqual(self.origin()["chat_id"], "chat")

    def test_unbound_context_never_borrows_global_environment(self):
        env = {"HERMES_SESSION_" + k: v for k, v in {
            "ID": "physical", "KEY": "route", "PROFILE": "default",
            "PLATFORM": "discord", "CHAT_ID": "chat", "ACTOR_ID": "actor"}.items()}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(WatchError):
                contextvars.Context().run(current_origin, "physical", "default")

    def test_id_profile_and_required_fields_fail_closed(self):
        for sid, profile in (("", "default"), ("child", "default"), ("physical", "other")):
            with self.assertRaises(WatchError):
                self.origin(sid, profile)
        for field in ("actor_id", "chat_id", "session_key", "profile"):
            self.context.run(S.set_session_vars, **{**self.fields, field: ""})
            with self.assertRaises(WatchError):
                self.origin()

    def test_child_inherited_route_refused_even_after_id_rebind(self):
        def child(sid):
            with delegated_child_context(sid):
                self.assertEqual(S.get_session_env("HERMES_SESSION_ID"), sid)
                with self.assertRaises(WatchError):
                    current_origin(sid, "default")
        for sid in ("physical", "child-rebound"):
            self.context.run(child, sid)
        self.assertEqual(self.origin()["session_id"], "physical")

    def test_delegated_process_marker_refused(self):
        with mock.patch.dict(os.environ, {"HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
            with self.assertRaises(WatchError):
                self.origin()

    def test_actor_clearing_never_falls_back_to_route_user(self):
        event = SimpleNamespace(user_id="actor")
        self.context.run(S.set_session_vars, **self.fields, turn_event=event)
        self.assertEqual(self.origin()["principal"], "actor")
        event.user_id = ""
        with self.assertRaises(WatchError):
            self.origin()

    def test_nonconversation_surfaces_and_cron_refused(self):
        for platform in ("webhook", "api_server", "cron", "cli", "kanban"):
            self.context.run(S.set_session_vars, **{**self.fields, "platform": platform})
            with self.assertRaises(WatchError):
                self.origin()
        self.context.run(S.set_session_vars, **{**self.fields, "cron_session": "1"})
        with self.assertRaises(WatchError):
            self.origin()

    def test_concurrent_contexts_do_not_mix_routes(self):
        def run(i):
            def resolve():
                S.set_session_vars(**{**self.fields, "actor_id": f"u{i}", "chat_id": f"c{i}",
                                      "session_id": f"s{i}", "session_key": f"k{i}"})
                return current_origin(f"s{i}", "default")
            return contextvars.Context().run(resolve)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        for i, origin in enumerate(results):
            self.assertEqual((origin["principal"], origin["chat_id"], origin["session_key"]),
                             (f"u{i}", f"c{i}", f"k{i}"))

    def test_missing_host_adapter_refused(self):
        with mock.patch.dict(sys.modules, {"gateway.session_context": None}):
            with self.assertRaises(WatchError):
                self.origin()


if __name__ == "__main__":
    unittest.main()
