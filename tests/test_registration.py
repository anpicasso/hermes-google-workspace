"""The distributed package registers all three agent tools."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gws_oauth_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class RegistrationTests(unittest.TestCase):
    def test_plugin_exposes_login_and_profile_tools(self):
        tools = {}
        starts = []
        finishes = []
        actions = []
        manager = SimpleNamespace(
            status=lambda *a, **kw: actions.append(("status", a, kw)) or {"ok": True},
            cancel=lambda *a, **kw: actions.append(("cancel", a, kw)) or {"ok": True},
            logout=lambda root, account, remove: remove(),
            close=lambda: None,
            start=lambda *args, **kwargs: starts.append((args, kwargs)) or {"ok": True},
            finish=lambda *args, **kwargs: finishes.append((args, kwargs)) or {"ok": True},
        )
        ctx = SimpleNamespace(
            get_config=lambda key, default=None: default,
            register_tool=lambda **kwargs: tools.setdefault(kwargs["name"], kwargs),
            register_hook=lambda name, callback: None,
            on_unload=lambda callback: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_constants = ModuleType("hermes_constants")
            setattr(hermes_constants, "get_hermes_home", lambda: home)
            gateway = ModuleType("gateway")
            setattr(gateway, "__path__", [])
            session_context = ModuleType("gateway.session_context")
            session = {
                "HERMES_SESSION_PLATFORM": "discord",
                "HERMES_SESSION_CHAT_ID": "thread-1",
                "HERMES_SESSION_USER_ID": "user-1",
                "HERMES_SESSION_CHAT_TYPE": "thread",
                "HERMES_SESSION_PROFILE": "default",
            }
            setattr(session_context, "get_session_env", session.get)
            with patch.object(plugin, "LoginManager", return_value=manager):
                with patch.dict(sys.modules, {
                    "hermes_constants": hermes_constants,
                    "gateway": gateway,
                    "gateway.session_context": session_context,
                }):
                    plugin.register(ctx)
            state_dir = home / "gws-oauth"
            self.assertTrue(state_dir.is_dir())
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertFalse((state_dir / "client_secret.json").exists())
            from gws_oauth_test.workspace_tools import SCHEMAS
            self.assertEqual(set(tools), {"gws_login", "gws_logout", "gws_accounts", "gws_api",
                                          "gws_gmail_watch", "gws_drive_watch",
                                          "gws_calendar_watch"} | set(SCHEMAS))
            self.assertEqual(starts, [])
            self.assertEqual(tools["gws_login"]["schema"]["parameters"]["properties"]
                             ["scope_mode"]["default"], "full")
            for name in ("gws_gmail_watch", "gws_drive_watch", "gws_calendar_watch"):
                schema = tools[name]["schema"]
                self.assertEqual(schema["parameters"]["properties"]["action"]["enum"],
                                 ["start", "status", "stop"])
                self.assertFalse(schema["parameters"]["additionalProperties"])
            self.assertNotIn("gws_watch", tools)
            result = json.loads(tools["gws_login"]["handler"]({"account": "user@example.com"}))
            self.assertTrue(result["ok"])
            self.assertEqual(starts[0][0][1:5], ("discord", "thread-1", "user-1", "user@example.com"))
            for action in ("status", "cancel"):
                result = json.loads(tools["gws_login"]["handler"]({"account": "user@example.com", "action": action}))
                self.assertTrue(result["ok"])
                self.assertEqual(actions[-1], (action, starts[0][0][:5], {"profile": "default"}))
            self.assertEqual(len(starts), 1)
            for invalid in ({"action": "bad"}, {"action": "cancel", "callback_url": "localhost:1/?code=fixture"}):
                self.assertEqual(json.loads(tools["gws_login"]["handler"](
                    {"account": "user@example.com", **invalid}))["reason"], "invalid_arguments")
            for platform in ("cli", "tui", "desktop", "api_server", "custom_ui"):
                session.clear()
                session.update(HERMES_SESSION_SOURCE=platform, HERMES_SESSION_ID="terminal-1")
                handler = tools["gws_login"]["handler"]
                self.assertTrue(json.loads(handler({"account": "user@example.com"}))["ok"])
                self.assertEqual(starts[-1][0][1:5], (platform, "terminal-1", "local", "user@example.com"))
                self.assertIsNone(starts[-1][0][6], "non-gateway sessions must not register an input hook")
                callback = "localhost:1234/?code=fixture"
                self.assertTrue(json.loads(handler({"account": "user@example.com", "callback_url": callback}))["ok"])
                self.assertEqual(finishes[-1][0][1:], (platform, "terminal-1", "local", "user@example.com", callback))
                self.assertTrue(json.loads(handler({"account": "user@example.com"}, session_id="terminal-2"))["ok"])
                self.assertEqual(starts[-1][0][2], "terminal-2")
                self.assertTrue(json.loads(handler({"account": "user@example.com", "callback_url": None}))["ok"])
                self.assertEqual(starts[-1][0][2], "terminal-1")
            session.update(HERMES_SESSION_SOURCE="desktop", HERMES_SESSION_USER_ID="browser-user")
            handler({"account": "user@example.com"}, session_id="desktop-session")
            self.assertEqual(starts[-1][0][1:4], ("desktop", "desktop-session", "browser-user"))
            session.update(HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_ID="shared-chat",
                           HERMES_SESSION_USER_ID="")
            handler({"account": "user@example.com"}, session_id="do-not-substitute")
            self.assertEqual(starts[-1][0][1:4], ("telegram", "shared-chat", ""))
            self.assertIn("callback_url", tools["gws_login"]["schema"]["parameters"]["properties"])
            self.assertIn("reauthorize", tools["gws_login"]["schema"]["parameters"]["properties"])
            self.assertIn("check_health", tools["gws_accounts"]["schema"]["parameters"]["properties"])

            handler = tools["gws_login"]["handler"]
            self.assertTrue(json.loads(handler({"account": "user@example.com", "reauthorize": True}))["ok"])
            self.assertIs(starts[-1][1]["reauthorize"], True)

            account = state_dir / "accounts" / "user@example.com"
            account.mkdir(parents=True)
            (account / "credentials.enc").write_text("fixture")
            plain = json.loads(tools["gws_accounts"]["handler"]({}))
            self.assertEqual(plain, {"ok": True, "accounts": ["user@example.com"]})
            with patch.object(plugin, "account_health", return_value={"state": "valid", "stored": True,
                                                                      "valid": True, "reauth_required": False}):
                checked = json.loads(tools["gws_accounts"]["handler"]({"check_health": True}))
            self.assertEqual(checked["accounts"], ["user@example.com"])
            self.assertEqual(checked["health"]["user@example.com"]["state"], "valid")
            from gws_oauth_test.watch import WatchStore
            store = WatchStore(home)
            store.add_watch({"watch_id": "logout-test", "service": "gmail",
                "account": "user@example.com", "profile": "default", "principal": "u",
                "origin": {}, "mode": "new_session", "target": {}, "filters": {},
                "instructions": "", "skills": [], "minutes": 1, "batch_seconds": 0,
                "stop_after_match": False, "created_at": 1, "not_before": 1,
                "expires_at": 2, "fingerprint": "logout-test"})
            logout = tools["gws_logout"]["handler"]
            for state, cleanup in (("active", "none"), ("matched", "done"), ("stopped", "pending")):
                store.update_watch("logout-test", state=state, cleanup_state=cleanup)
                self.assertEqual(json.loads(logout({"account": "user@example.com"}))["error_code"], "watches_active")
                self.assertEqual(json.loads(logout({"account": "User@Example.com"}))["error_code"], "watches_active")
                self.assertTrue((account / "credentials.enc").exists())
            store.update_watch("logout-test", state="stopped", cleanup_state="done")
            result = json.loads(logout({"account": "user@example.com"}))
            self.assertTrue(result["removed"])
            self.assertFalse(result["remote_revoked"])
            self.assertFalse(account.exists())
            self.assertFalse(json.loads(logout({"account": "user@example.com"}))["removed"])
            self.assertEqual(json.loads(logout({"account": "../oops"}))["error_code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
