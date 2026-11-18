"""Local logout only: synthetic account slots, no Google calls."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router import logout_account
from api_policy import make_pre_tool_call_hook


class LogoutTests(unittest.TestCase):
    def test_exact_slot_removed_and_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = root / "client_secret.json"
            client.write_text("synthetic-client")
            for account in ("a@example.com", "b@example.com"):
                slot = root / "accounts" / account
                slot.mkdir(parents=True)
                (slot / "credentials.enc").write_text("synthetic")
            result = logout_account(root, "a@example.com")
            self.assertTrue(result["removed"])
            self.assertFalse(result["remote_revoked"])
            self.assertFalse((root / "accounts/a@example.com").exists())
            self.assertTrue((root / "accounts/b@example.com/credentials.enc").is_file())
            self.assertEqual(client.read_text(), "synthetic-client")
            self.assertFalse(logout_account(root, "a@example.com")["removed"])

    def test_empty_native_logout_slot_can_be_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = Path(tmp) / "accounts/a@example.com"
            slot.mkdir(parents=True)
            (slot / "client_secret.json").write_text("{}")
            self.assertTrue(logout_account(tmp, "a@example.com")["removed"])
            self.assertFalse(slot.exists())

    def test_path_traversal_and_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in (None, "", ".", "..", "../a", "/a", "a\n"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    logout_account(root, bad)
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep").write_text("keep")
            base = root / "accounts"
            base.mkdir()
            (base / "a@example.com").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                logout_account(root, "a@example.com")
            self.assertTrue((outside / "keep").is_file())

    def test_logout_requires_native_approval_not_model_flag(self):
        hook = make_pre_tool_call_hook()
        result = hook(tool_name="gws_logout", args={"account": "a@example.com", "approved": True})
        self.assertEqual(result["action"], "approve")
        self.assertEqual(result["rule_key"], "gws:a@example.com:logout")


if __name__ == "__main__":
    unittest.main()
