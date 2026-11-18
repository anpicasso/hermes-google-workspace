import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_policy import ALLOW, APPROVE, BLOCK, classify, make_pre_tool_call_hook

HOST = Path(os.environ.get("HERMES_AGENT_SRC", Path.home() / ".hermes" / "hermes-agent"))


class ClassifyTests(unittest.TestCase):
    def test_reads_schema_and_help_run_directly(self):
        for argv in (["drive", "files", "list", "--params", '{"q":"name contains \'delete\'"}'],
                     ["gmail", "users", "messages", "get", "--params={\"id\":\"1\"}", "--format", "json"],
                     ["drive", "--format", "table", "files", "list", "--page-all", "--page-limit", "3"],
                     ["gmail", "+triage"], ["calendar", "freebusy", "query", "--json", "{}"], ["schema", "drive.files.delete"],
                     ["drive", "files", "delete", "--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(classify(argv)[0], ALLOW)

    def test_mutations_outputs_and_unknowns_need_approval(self):
        for argv in (["drive", "files", "delete", "--params", '{"fileId":"x","method":"list"}'],
                     ["drive", "files", "list", "--output", "/tmp/x"], ["drive", "files", "get", "-o", "x"],
                     ["drive", "files", "update", "--json", "{}"], ["drive", "files", "create", "--upload", "a"],
                     ["drive", "permissions", "create"], ["gmail", "users", "messages", "send"],
                     ["gmail", "+send", "--subject", "list"], ["gmail", "+forward"], ["drive", "+upload", "a"],
                     ["chat", "+send"], ["gmail", "+new-helper"], ["drive", "files", "list", "--weird"],
                     ["drive", "files", "list", "--json={}"], ["drive", "files", "list", "--dry-run"],
                     ["drive", "files"], [], ["calendar", "freebusy", "query", "--output", "x"], ["drive", "files", "list", "--format"],
                     ["gmail", "+read", "--id", "1", "--output", "x"],
                     ["gmail", "+triage", "--unknown-mutation"]):
            with self.subTest(argv=argv):
                self.assertEqual(classify(argv)[0], APPROVE)

    def test_auth_namespace_and_bad_input_are_blocked(self):
        for argv in (["auth", "export", "--unmasked"], ["AUTH", "status"], ["--format", "json", "auth", "export"],
                     ["--format=json", "auth", "status"], ["help", "auth", "export"], ["auth", "--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(classify(argv)[0], BLOCK)
        self.assertEqual(classify("drive files list")[0], BLOCK)
        self.assertEqual(classify([1])[0], BLOCK)

    def test_hook_ignores_model_supplied_approval_and_covers_typed_tools(self):
        hook = make_pre_tool_call_hook(lambda name, args: ["gmail", "+send", "--to", args["to"]],
                                       typed_names=["gws_gmail_send"])
        result = hook(tool_name="gws_api", args={"account": "a@example.com", "approved": True, "confirm": True,
                                                  "args": ["drive", "files", "delete"]})
        self.assertEqual(result["action"], "approve")
        self.assertEqual(result["rule_key"], "gws:a@example.com:drive.files.delete")
        self.assertEqual(hook(tool_name="gws_gmail_send", args={"to": "x@y"})["action"], "approve")
        self.assertIsNone(hook(tool_name="gws_api", args={"args": ["drive", "files", "list"]}))
        self.assertIsNone(hook(tool_name="terminal", args={"command": "gws auth export"}))
        self.assertEqual(hook(tool_name="gws_api", args={"args": ["auth", "export"]})["action"], "block")
        broken = make_pre_tool_call_hook(lambda *_: 1 / 0, typed_names=["gws_x"])
        self.assertEqual(broken(tool_name="gws_x", args={})["action"], "block")


@unittest.skipUnless((HOST / "model_tools.py").is_file(), "installed Hermes host source not found")
class HostDispatchTests(unittest.TestCase):
    """Real model_tools.handle_function_call -> pre_tool_call -> request_tool_approval."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Clean env: a parent gateway/cron exports HERMES_EXEC_ASK/HERMES_CRON_SESSION.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
        env = mock.patch.dict(os.environ, {**clean, "HERMES_HOME": tmp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        if str(HOST) not in sys.path:
            sys.path.insert(0, str(HOST))
        import hermes_cli.plugins as plugins
        from tools import approval, approval_context, approval_prompt
        manager = plugins.PluginManager()
        from workspace_tools import SCHEMAS, build_args
        manager._hooks["pre_tool_call"] = [make_pre_tool_call_hook(build_args, SCHEMAS)]
        self.calls, self.prompts = [], []
        registry = mock.MagicMock()
        registry.dispatch.side_effect = lambda name, args, **kw: self.calls.append(args) or json.dumps({"ok": True})
        self.choice = "deny"

        def prompt(*args, **kwargs):
            self.prompts.append(args)
            return self.choice
        for patcher in (mock.patch.object(plugins, "_plugin_manager", manager),
                        mock.patch("model_tools.registry", registry),
                        mock.patch.object(approval, "is_approved", lambda *a: False),
                        mock.patch.object(approval, "_yolo_active", lambda: False),
                        mock.patch.object(approval_context, "_get_approval_mode", lambda: "manual"),
                        mock.patch.object(approval, "_is_interactive_cli", lambda: True),
                        mock.patch.object(approval, "_is_gateway_approval_context", lambda: False),
                        mock.patch.object(approval_context, "_is_gateway_approval_context", lambda: False),
                        mock.patch.object(approval, "prompt_dangerous_approval", prompt),
                        mock.patch.object(approval_prompt, "prompt_dangerous_approval", prompt)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, argv, **extra):
        from model_tools import handle_function_call
        return handle_function_call("gws_api", {"account": "a@example.com", "args": argv, **extra},
                                    task_id="t", session_id="s")

    def test_logout_denial_never_reaches_handler(self):
        from model_tools import handle_function_call
        result = handle_function_call("gws_logout", {"account": "a@example.com", "approved": True},
                                      task_id="t", session_id="s")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("denied", result.lower())

    def test_destructive_call_is_denied_before_handler(self):
        result = self.call(["drive", "files", "delete", "--params", '{"fileId":"x"}'], approved=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("drive.files.delete", json.dumps(self.prompts[0]))
        self.assertIn("denied", result.lower())

    def test_human_approval_runs_handler_once(self):
        self.choice = "once"
        self.call(["gmail", "+send", "--to", "x@example.com"])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.calls), 1)

    def test_typed_upload_share_and_download_are_gated(self):
        from model_tools import handle_function_call
        cases = {
            "gws_drive_upload": {"path": "report.pdf"},
            "gws_drive_share": {"file_id": "f1", "role": "reader", "type": "user",
                                "email_address": "reader@example.com"},
            "gws_drive_read": {"file_id": "f1", "mode": "content", "output_path": "download.bin"},
        }
        for name, args in cases.items():
            with self.subTest(name=name):
                self.calls.clear()
                self.prompts.clear()
                result = handle_function_call(name, {"account": "a@example.com", **args},
                                              task_id="t", session_id="s")
                self.assertEqual(self.calls, [])
                self.assertEqual(len(self.prompts), 1)
                self.assertIn("denied", result.lower())

    def test_read_runs_without_prompt_and_auth_never_runs(self):
        self.call(["drive", "files", "list"])
        self.assertEqual((len(self.calls), self.prompts), (1, []))
        result = self.call(["auth", "export", "--unmasked"])
        self.assertEqual((len(self.calls), self.prompts), (1, []))
        self.assertIn("auth", result)


if __name__ == "__main__":
    unittest.main()
