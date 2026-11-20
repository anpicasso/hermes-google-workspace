"""Exercise real host context binding and watch dispatch without live delivery."""
import asyncio
import contextvars
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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


class GatewayDispatchTests(unittest.TestCase):
    def test_primary_profile_watch_status_through_tool_call(self):
        from gateway.config import GatewayConfig, Platform
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionEntry, SessionSource, build_session_context
        from gateway.session_identity import clear_identity
        from hermes_cli.plugins_manifest import parse_manifest_file

        repo = Path(__file__).resolve().parents[1]
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": tmp, "HERMES_HOME": str(Path(tmp) / ".hermes"), "HERMES_DISABLE_LAZY_INSTALLS": "1",
        }))
        from gateway.run import GatewayRunner
        from hermes_cli.plugins import get_plugin_manager
        from hermes_cli.profiles import get_active_profile_name
        from model_tools import handle_function_call

        manager = get_plugin_manager()
        manifest = parse_manifest_file(repo / "plugin.yaml", repo, "user", "")
        assert manifest is not None
        manager._load_plugin(manifest)
        self.addCleanup(manager.unload, manifest)
        runner = GatewayRunner.__new__(GatewayRunner)  # No credentials or listeners.
        runner.config = GatewayConfig(multiplex_profiles=True)
        profile = runner._primary_profile_name = get_active_profile_name()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="actor")
        identity = runner._canonicalize(source)
        assert identity is not None
        self.assertEqual(identity.runtime_profile, profile)
        self.assertIsNone(source.profile)
        event = MessageEvent(text="status", source=source, user_id="actor")
        now = datetime.now(timezone.utc)
        entry = SessionEntry(runner._session_key_for_source(source), "physical", now, now)

        def dispatch(service="calendar", sid="physical"):
            args = {"calls": [{"name": f"gws_{service}_watch", "arguments": {"action": "status"}}]}
            def call():
                return handle_function_call("tool_call", args, session_id=sid, enabled_toolsets=["google_workspace"])
            return json.loads(asyncio.run(runner._run_in_executor_with_context(call)))

        def exercise():
            with runner._session_env_scope(build_session_context(source, runner.config, entry, event)):
                self.assertEqual(S._bound_session_value("HERMES_SESSION_PROFILE"), "")
                for service in ("gmail", "drive", "calendar"):
                    result = dispatch(service)
                    self.assertTrue(result.get("ok"), result)
                    self.assertEqual(result["watches"], [])
                self.assertEqual(dispatch(sid="other")["error_code"], "no_trusted_origin")
                with delegated_child_context("child"):
                    self.assertEqual(dispatch(sid="child")["error_code"], "no_trusted_origin")
                clear_identity(source)
                self.assertEqual(dispatch()["error_code"], "no_trusted_origin")
                runner._canonicalize(source, primary_home=Path(tmp) / "other-home")
                self.assertEqual(dispatch()["error_code"], "no_trusted_origin")
                clear_identity(source)
                runner._primary_profile_name = "foreign"
                runner._canonicalize(source)
                self.assertEqual(dispatch()["error_code"], "no_trusted_origin")
                clear_identity(source)
                runner._primary_profile_name = profile
                runner._canonicalize(source)
                event.absorb_turn_input(MessageEvent(text="another sender", user_id="other"))
                self.assertEqual(dispatch()["error_code"], "no_trusted_origin")

        contextvars.Context().run(exercise)


if __name__ == "__main__":
    unittest.main()
