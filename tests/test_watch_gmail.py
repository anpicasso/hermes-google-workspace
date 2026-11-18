import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_gmail
from fake_google import FakeGoogle, pubsub_message
from watch_helpers import Clock, FakeRoutes, runtime, start_gmail


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        self.routes = FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)

    def tearDown(self):
        self.tmp.cleanup()

    def _watch(self, **kwargs):
        result = start_gmail(self.rt, **kwargs)
        self.assertTrue(result["ok"], result)
        return self.store.get_watch(result["watch"]["watch_id"]), result

    def _source(self, watch_row):
        return self.store.sources_for(watch_row["watch_id"])[0]

    def test_message_ids_resolve_to_deduplicated_threads(self):
        self.client.add_message("m1", "T1", internal_date="1600000000000")
        self.client.add_message("m2", "T1", internal_date="1600000000000")
        result = start_gmail(self.rt, target={"message_ids": ["m1", "m2"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["resolved"]["thread_ids"], ["T1"])
        self.assertEqual(result["resolved"]["message_ids"], ["m1", "m2"])

    def test_false_attachment_filter_is_not_ignored(self):
        from unittest.mock import patch
        self.client.scopes_value = ["https://www.googleapis.com/auth/gmail.readonly"]
        row, _ = self._watch(target={"mailbox": True}, filters={"has_attachment": False})
        event = {"message_id": "m1", "label_ids": [], "occurred_at": self.clock.now + 10}
        with patch.object(watch_gmail, "_attachment_state", return_value=True):
            self.assertFalse(watch_gmail.match(row, event, self.client))
        with patch.object(watch_gmail, "_attachment_state", return_value=False):
            self.assertTrue(watch_gmail.match(row, event, self.client))

    def test_thread_target_must_exist(self):
        result = start_gmail(self.rt, target={"thread_ids": ["MISSING"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "target_not_found")

    def test_exact_thread_sender_domain_and_label_rules(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(
            target={"thread_ids": ["T1"]},
            filters={"from_domains": ["example.com"], "subject_contains": ["invoice"],
                     "label_ids": ["INBOX"]})
        base = {"message_id": "m1", "thread_id": "T1", "from_address": "alice@example.com",
                "subject": "Invoice for June", "label_ids": ["INBOX"], "occurred_at": self.clock.now + 1}
        self.assertTrue(watch_gmail.match(watch_row, dict(base), self.client))
        self.assertFalse(watch_gmail.match(watch_row, {**base, "thread_id": "T2"}, self.client))
        self.assertFalse(watch_gmail.match(watch_row, {**base, "from_address": "alice@example.com.attacker.test"}, self.client))
        self.assertFalse(watch_gmail.match(watch_row, {**base, "from_address": "bob@sub.example.com"}, self.client))
        self.assertFalse(watch_gmail.match(watch_row, {**base, "subject": "Receipt"}, self.client))
        self.assertFalse(watch_gmail.match(watch_row, {**base, "label_ids": ["STARRED"]}, self.client))

    def test_sent_and_drafts_are_excluded_unless_enabled(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        event = {"message_id": "m1", "thread_id": "T1", "from_address": "alice@example.com",
                 "subject": "Hi", "label_ids": ["SENT"], "occurred_at": self.clock.now + 1}
        self.assertFalse(watch_gmail.match(watch_row, dict(event), self.client))
        watch_row, _ = self._watch(target={"mailbox": True}, filters={"include_sent": True})
        self.assertTrue(watch_gmail.match(watch_row, dict(event), self.client))

    def test_attachment_requires_non_empty_filename_not_subject(self):
        self.client.scopes_value = ["https://www.googleapis.com/auth/gmail.readonly"]
        self.client.add_message("m1", "T1", subject="invoice attached", internal_date="1800000000000",
                                attachments=True)
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True}, filters={"has_attachment": True})
        event = {"message_id": "m1", "thread_id": "T1", "from_address": "a@example.com",
                 "subject": "invoice attached", "label_ids": ["INBOX"], "occurred_at": self.clock.now + 1}
        self.assertTrue(watch_gmail.match(watch_row, dict(event), self.client))
        self.client.add_message("m2", "T1", subject="no file", internal_date="1800000000000",
                                attachments=False)
        event["message_id"] = "m2"
        self.assertFalse(watch_gmail.match(watch_row, dict(event), self.client))

    def test_history_pagination_is_durable_and_duplicate_safe(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        for index, message_id in enumerate(("m1", "m2", "m3"), start=101):
            self.client.add_message(message_id, "T1", internal_date="1800000000000",
                                    sender="alice@example.com", subject="new")
            self.client.add_history(index, message_id)
        self.client.gmail["page_size"] = 1
        result = watch_gmail.drain(self.client, self.store, source, [watch_row])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cursor"], "103")
        source = self.store.get_source(source["source_id"])
        self.assertEqual(source["cursor"], "103")
        self.assertIsNone(source["cursor_staged"])
        rows = self.store.outbox(watch_row["watch_id"])
        self.assertEqual(len(rows), 3)
        # A replay from the old cursor must not create a second outbox event.
        replayed = self.store.get_source(source["source_id"])
        replayed["cursor"] = "100"
        watch_gmail.drain(self.client, self.store, replayed, [watch_row])
        self.assertEqual(len(self.store.outbox(watch_row["watch_id"])), 3)

    def test_pubsub_pull_persists_hints_and_never_acks_unroutable_accounts(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        self.client.gmail["pubsub"] = [[
            pubsub_message("person@example.com", "150"),
            pubsub_message("other@example.com", "151"),
        ]]
        result = watch_gmail.pubsub_pull(self.client, self.store, source, self.rt.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["hints"], 1)
        self.assertEqual(result["unrouted"], 1)
        self.assertEqual(len(self.store.pending_hints(source["source_id"])), 1)
        ack = self.client.calls[-1]
        self.assertEqual(ack["args"][-1], "acknowledge")
        # The foreign notification belongs to another consumer; do not destroy it.
        self.assertEqual(ack["body"]["ackIds"], ["ack-150"])

    def test_pubsub_accepts_live_numeric_history_id(self):
        import base64
        row, _ = self._watch(target={"mailbox": True})
        source = self._source(row)
        # The real Gmail notification encodes historyId as a JSON integer.
        for value, expected in ((150, 1), (True, 0), (-1, 0), (1.5, 0)):
            message = pubsub_message("person@example.com", "150")
            message["message"]["data"] = base64.b64encode(json.dumps({
                "emailAddress": "person@example.com", "historyId": value}).encode()).decode()
            self.client.gmail["pubsub"] = [[message]]
            result = watch_gmail.pubsub_pull(self.client, self.store, source, self.rt.config)
            self.assertEqual(result["hints"], expected)
        self.assertEqual(len(self.store.pending_hints(source["source_id"])), 1)

    def test_pubsub_multiplexes_accounts_sharing_one_subscription(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        first, _ = self._watch(target={"mailbox": True})
        second, _ = self._watch(account="other@example.com", target={"mailbox": True})
        first_source, second_source = self._source(first), self._source(second)
        self.client.gmail["pubsub"] = [[
            pubsub_message("person@example.com", "150"),
            pubsub_message("other@example.com", "151"),
        ]]
        result = watch_gmail.pubsub_pull(self.client, self.store, first_source, self.rt.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["hints"], 2)
        self.assertEqual(result["unrouted"], 0)
        self.assertEqual(len(self.store.pending_hints(first_source["source_id"])), 1)
        self.assertEqual(len(self.store.pending_hints(second_source["source_id"])), 1)
        self.assertEqual(self.client.calls[-1]["body"]["ackIds"], ["ack-150", "ack-151"])

    def test_pubsub_pull_uses_documented_request_shape(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        watch_gmail.pubsub_pull(self.client, self.store, source, self.rt.config)
        pull = [call for call in self.client.calls if call["args"][-1] == "pull"][-1]
        self.assertEqual(pull["args"][0], "pubsub:v1")
        self.assertEqual(pull["params"], {"subscription": "projects/p/subscriptions/s"})
        self.assertEqual(pull["body"]["maxMessages"], 20)
        self.assertIs(pull["body"]["returnImmediately"], True)

    def test_pubsub_validation_failure_is_not_retried(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        self.client.fail(("pubsub:v1", "projects", "subscriptions", "pull"),
                         400, "invalid_arguments", False)
        result = watch_gmail.pubsub_pull(self.client, self.store, source, self.rt.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_arguments")
        self.assertFalse(result["retryable"])
        self.assertEqual(self.store.pending_hints(source["source_id"]), [])

    def test_transient_message_fetch_does_not_advance_cursor(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        for index, message_id in enumerate(("m1", "m2"), start=101):
            self.client.add_message(message_id, "T1", internal_date="1800000000000")
            self.client.add_history(index, message_id)
        self.client.fail(("gmail", "users", "messages", "get"), 500, "api_error", True)
        result = watch_gmail.drain(self.client, self.store, source, [watch_row])
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertEqual(self.store.get_source(source["source_id"])["cursor"], "100")
        self.assertEqual(self.store.outbox(watch_row["watch_id"]), [])
        self.client.failures.clear()
        result = watch_gmail.drain(self.client, self.store, self._source(watch_row), [watch_row])
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.store.outbox(watch_row["watch_id"])), 2)

    def test_metadata_request_is_a_header_only_projection(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        self.client.add_message("m1", "T1", internal_date="1800000000000")
        self.client.add_history(101, "m1")
        watch_gmail.drain(self.client, self.store, self._source(watch_row), [watch_row])
        get = [call for call in self.client.calls
               if call["args"][-2:] == ["messages", "get"]
               and call["params"].get("format") == "metadata"][-1]
        self.assertNotIn("format", str(get["params"].get("fields")).split(","))
        fields = get["params"]["fields"]
        self.assertIn("payload/headers", fields)
        self.assertIn("labelIds", fields)
        self.assertNotIn("snippet", fields)
        self.assertNotIn("body", fields)

    def test_attachment_fetch_failure_does_not_advance_cursor(self):
        class AttachmentFails(FakeGoogle):
            fail_attachment = True

            def call(self, account, args, params=None, body=None, timeout=30):
                if (tuple(args[-2:]) == ("messages", "get") and self.fail_attachment
                        and (params or {}).get("format") == "full"):
                    return {"ok": False, "http_status": 500, "error_code": "api_error",
                            "retryable": True}
                return super().call(account, args, params=params, body=body, timeout=timeout)

        self.client = AttachmentFails()
        self.client.scopes_value = ["https://www.googleapis.com/auth/gmail.readonly"]
        self.rt = runtime(self.store, self.client, self.routes)
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True}, filters={"has_attachment": True})
        source = self._source(watch_row)
        self.client.add_message("m1", "T1", internal_date="1800000000000", attachments=True)
        self.client.add_history(101, "m1")
        result = watch_gmail.drain(self.client, self.store, source, [watch_row])
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.get_source(source["source_id"])["cursor"], "100")
        self.assertEqual(self.store.outbox(watch_row["watch_id"]), [])
        self.client.fail_attachment = False
        result = watch_gmail.drain(self.client, self.store, self._source(watch_row), [watch_row])
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.store.outbox(watch_row["watch_id"])), 1)

    def test_barrier_uses_baseline_capture_time(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        from unittest.mock import patch
        with patch.object(watch_gmail.time, "time", return_value=1000.0):
            watch_row, _ = self._watch(target={"mailbox": True})
        event = {"message_id": "m1", "thread_id": "T1", "from_address": "a@example.com",
                 "subject": "raced arrival", "label_ids": ["INBOX"], "occurred_at": 1001.0}
        self.assertTrue(watch_gmail.match(watch_row, dict(event), self.client))

    def test_start_requires_well_formed_topic_and_subscription(self):
        rt = runtime(self.store, self.client, self.routes,
                     config={"watch_gmail_topic": "my-topic"})
        result = start_gmail(rt, target={"mailbox": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "gmail_topic_invalid")
        rt = runtime(self.store, self.client, self.routes,
                     config={"watch_gmail_subscription": "my-subscription"})
        result = start_gmail(rt, target={"mailbox": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "gmail_subscription_missing")

    def test_history_404_resyncs_without_flood_and_marks_gap(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        self.client.fail(("gmail", "users", "history", "list"), 404, "api_error", False)
        result = watch_gmail.drain(self.client, self.store, source, [watch_row])
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["resynced"])
        self.assertEqual(self.store.outbox(watch_row["watch_id"]), [])
        self.assertIn("cannot be recovered", self.store.get_watch(watch_row["watch_id"])["gap"])

    def test_watch_response_does_not_reset_committed_cursor(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        source = self._source(watch_row)
        self.store.update_source(source["source_id"], cursor="100")
        self.client.gmail["history_id"] = "555"
        # Renewal is the same users.watch call; committed history stays at 100.
        from watch_worker import Worker
        worker = Worker(self.home, self.client, FakeRoutes(), self.rt.config, store=self.store,
                        poster=lambda *args: {"ok": True})
        self.assertTrue(worker._ensure_gmail(self.store.get_source(source["source_id"])))
        self.assertEqual(self.store.get_source(source["source_id"])["cursor"], "100")

    def test_catchup_barrier_ignores_older_messages(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        watch_row, _ = self._watch(target={"mailbox": True})
        old = {"message_id": "old", "thread_id": "T1", "from_address": "a@example.com",
               "subject": "x", "label_ids": ["INBOX"], "occurred_at": self.clock.now - 10}
        self.assertFalse(watch_gmail.match(watch_row, old, self.client))
        new = dict(old, occurred_at=self.clock.now + 10)
        self.assertTrue(watch_gmail.match(watch_row, new, self.client))

    def test_start_requires_topic_and_subscription(self):
        rt = runtime(self.store, self.client, self.routes,
                     config={"watch_gmail_topic": "", "watch_gmail_subscription": ""})
        result = start_gmail(rt, target={"mailbox": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "gmail_topic_missing")

    def test_scope_gate_for_attachment(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        self.client.scopes_value = ["https://www.googleapis.com/auth/drive"]
        result = start_gmail(self.rt, target={"mailbox": True}, filters={"has_attachment": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "scope_insufficient")
        # Gmail enforces metadata restrictions whenever gmail.metadata is granted,
        # even alongside readonly, so this combination must fail closed.
        self.client.scopes_value = ["https://www.googleapis.com/auth/gmail.metadata",
                                    "https://www.googleapis.com/auth/gmail.readonly"]
        result = start_gmail(self.rt, target={"mailbox": True}, filters={"has_attachment": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "scope_insufficient")
        self.client.scopes_value = ["https://www.googleapis.com/auth/gmail.readonly"]
        result = start_gmail(self.rt, target={"mailbox": True}, filters={"has_attachment": True})
        self.assertTrue(result["ok"], result)

    def test_gmail_topic_must_match_oauth_project(self):
        self.client.project_value = "client-project"
        self.client.add_message("root", "T1", internal_date="1600000000000")
        result = start_gmail(self.rt, target={"mailbox": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "gmail_topic_project_mismatch")


if __name__ == "__main__":
    unittest.main()
