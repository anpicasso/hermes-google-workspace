import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_worker
from fake_google import FakeGoogle, pubsub_message
from watch_helpers import Clock, FakeRoutes, runtime, start_drive, start_gmail


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        self.routes = FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)
        self.worker = watch_worker.Worker(self.home, self.client, self.routes, self.rt.config,
                                          store=self.store, poster=self.routes.post,
                                          clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def _gmail(self, **kwargs):
        kwargs.setdefault("batch_seconds", 0)
        result = start_gmail(self.rt, target={"mailbox": True}, **kwargs)
        self.assertTrue(result["ok"], result)
        return self.store.get_watch(result["watch"]["watch_id"])

    def test_callback_requires_exact_token_and_resource_and_persists_before_ack(self):
        self.client.add_file("FOLDER", "invoices", "application/vnd.google-apps.folder")
        result = start_drive(self.rt, target={"folder_id": "FOLDER"})
        self.assertTrue(result["ok"], result)
        watch_row = self.store.get_watch(result["watch"]["watch_id"])
        source = self.store.sources_for(watch_row["watch_id"])[0]
        self.assertTrue(self.worker._ensure_channel(source, "drive"))
        remote = self.store.remote_for_source(source["source_id"], "drive_channel")
        identity = json.loads(remote["identity_json"])
        headers = {"X-Goog-Channel-ID": identity["channel_id"],
                   "X-Goog-Channel-Token": identity["token"],
                   "X-Goog-Resource-ID": identity["resource_id"],
                   "X-Goog-Resource-State": "exists",
                   "X-Goog-Message-Number": "1"}
        status, payload = watch_worker.process_callback(self.store, headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(self.store.pending_hints(source["source_id"])), 1)
        self.assertEqual(watch_worker.process_callback(self.store, headers)[0], 200)
        self.assertEqual(len(self.store.pending_hints(source["source_id"])), 1)
        bad = dict(headers, **{"X-Goog-Channel-Token": "wrong"})
        self.assertEqual(watch_worker.process_callback(self.store, bad)[0], 403)
        unknown = dict(headers, **{"X-Goog-Channel-ID": "missing"})
        self.assertEqual(watch_worker.process_callback(self.store, unknown)[0], 404)
        self.assertEqual(watch_worker.process_callback(self.store, headers, b"x" * 1025)[0], 413)

    def test_early_sync_is_accepted_before_resource_identity(self):
        self.store.ensure_source("drive", "person@example.com", {"drive_id": ""}, {})
        source = self.store.list_sources(service="drive")[0]
        self.store.add_remote("channel:early", "drive_channel", source_id=source["source_id"],
                              identity={"token": "tok", "channel_id": "early", "resource_id": "",
                                        "source_id": source["source_id"]}, state="pending")
        status, payload = watch_worker.process_callback(self.store, {
            "X-Goog-Channel-ID": "early", "X-Goog-Channel-Token": "tok",
            "X-Goog-Resource-State": "sync", "X-Goog-Message-Number": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["sync"])

    def test_run_once_gmail_pull_history_and_native_forward(self):
        watch_row = self._gmail()
        self.client.add_message("m1", "T1", sender="alice@example.com", subject="new invoice",
                                internal_date="1800000000000")
        self.client.add_history(101, "m1")
        self.client.gmail["pubsub"] = [[pubsub_message("person@example.com", "101")]]
        self.worker.run_once()
        self.assertTrue(self.client.gmail["watch_calls"] >= 1)
        self.assertEqual(self.client.gmail["acknowledged"], [])  # fake records only calls; ack call exists
        ack_calls = [call for call in self.client.calls
                     if call["args"][-1:] == ["acknowledge"]]
        self.assertTrue(ack_calls)
        rows = self.store.outbox(watch_row["watch_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "admitted")
        self.assertEqual(len(self.routes.posts), 1)
        payload = self.routes.posts[0]["payload"]
        self.assertEqual(payload["event_type"], "gws-watch")
        self.assertEqual(payload["events"][0]["message_id"], "m1")
        self.assertEqual(self.routes.posts[0]["request_id"],
                         json.loads(rows[0]["request_id"]) if rows[0]["request_id"] and rows[0]["request_id"].startswith('"') else rows[0]["request_id"])

    def test_native_retry_is_stable_and_does_not_claim_delivery(self):
        watch_row = self._gmail()
        self.store.add_event(watch_row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.routes.post_result = {"ok": False, "error_code": "native_route_retryable",
                                   "retryable": True, "status": 503}
        self.worker.forward()
        row = self.store.outbox(watch_row["watch_id"])[0]
        self.assertEqual(row["state"], "retry")
        request_id = row["request_id"]
        self.routes.post_result = {"ok": True, "status": 202, "admission": "accepted"}
        self.clock.advance(10)
        self.worker.forward()
        row = self.store.outbox(watch_row["watch_id"])[0]
        self.assertEqual(row["state"], "admitted")
        self.assertEqual(row["request_id"], request_id)
        self.assertEqual(self.store.get_watch(watch_row["watch_id"])["delivery_state"], "admitted")

    def test_native_retry_keeps_an_immutable_batch(self):
        watch_row = self._gmail()
        self.store.add_event(watch_row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.routes.post_result = {"ok": False, "error_code": "native_route_retryable",
                                   "retryable": True, "status": 503}
        self.worker.forward()
        first = self.store.outbox(watch_row["watch_id"])[0]
        request_id = first["request_id"]
        self.assertTrue(request_id)
        # A new event arrives while the first batch is waiting to retry.
        self.store.add_event(watch_row["watch_id"], "e2", "gmail", {"message_id": "m2"})
        self.routes.post_result = {"ok": True, "status": 202, "admission": "accepted"}
        self.clock.advance(10)
        self.worker.forward()
        rows = {row["event_id"]: row for row in self.store.outbox(watch_row["watch_id"])}
        self.assertEqual({key: row["state"] for key, row in rows.items()},
                         {"e1": "admitted", "e2": "admitted"})
        self.assertEqual(rows["e1"]["request_id"], request_id)
        self.assertNotEqual(rows["e2"]["request_id"], request_id)
        retry, fresh = self.routes.posts[1], self.routes.posts[2]
        self.assertEqual(retry["payload"], self.routes.posts[0]["payload"])
        self.assertEqual(retry["request_id"], request_id)
        self.assertEqual([event["message_id"] for event in retry["payload"]["events"]], ["m1"])
        self.assertEqual([event["message_id"] for event in fresh["payload"]["events"]], ["m2"])

    def test_native_batch_never_sends_a_truncated_retry(self):
        watch_row = self._gmail()
        self.store.add_event(watch_row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.store.add_event(watch_row["watch_id"], "e2", "gmail", {"message_id": "m2"})
        self.routes.post_result = {"ok": False, "error_code": "native_route_retryable",
                                   "retryable": True, "status": 503}
        self.worker.forward()
        self.assertEqual(len(self.routes.posts), 1)
        self.assertEqual(len(self.routes.posts[0]["payload"]["events"]), 2)
        request_id = self.store.outbox(watch_row["watch_id"])[0]["request_id"]
        with self.store.tx() as conn:
            conn.execute("UPDATE outbox SET next_attempt=? WHERE event_id='e2'",
                         (self.clock.now + 600,))
        self.clock.advance(10)
        self.worker.forward()
        self.assertEqual(len(self.routes.posts), 1)  # e1 alone would change the batch
        self.routes.post_result = {"ok": True, "status": 202, "admission": "accepted"}
        with self.store.tx() as conn:
            conn.execute("UPDATE outbox SET next_attempt=0 WHERE request_id=?", (request_id,))
        self.worker.forward()
        retry = self.routes.posts[1]
        self.assertEqual(retry["request_id"], request_id)
        self.assertEqual([event["message_id"] for event in retry["payload"]["events"]],
                         ["m1", "m2"])
        self.assertEqual({row["state"] for row in self.store.outbox(watch_row["watch_id"])},
                         {"admitted"})

    def test_same_session_forward_is_opaque_bridge_payload(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        self.routes.same_session = True
        self.rt.ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None: True)
        result = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session",
                             batch_seconds=0)
        self.assertTrue(result["ok"], result)
        row = self.store.get_watch(result["watch"]["watch_id"])
        self.store.add_event(row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.worker.forward()
        self.assertEqual(len(self.routes.posts), 1)
        payload = self.routes.posts[0]["payload"]
        self.assertEqual(set(payload), {"event_type", "watch_id", "event_id", "generation"})
        self.assertNotIn("message_id", json.dumps(payload))
        out = self.store.outbox(row["watch_id"])[0]
        self.assertEqual(out["state"], "forwarding")

    def test_batch_waits_for_window_but_one_shot_bypasses(self):
        row = self._gmail(batch_seconds=10)
        self.store.add_event(row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.worker.forward()
        self.assertEqual(self.routes.posts, [])
        self.clock.advance(11)
        self.worker.forward()
        self.assertEqual(len(self.routes.posts), 1)
        one = self._gmail(stop_after_match=True, batch_seconds=10)
        self.store.add_event(one["watch_id"], "e2", "gmail", {"message_id": "m2"})
        self.worker.forward()
        self.assertTrue(any(post["payload"]["watch_id"] == one["watch_id"]
                            for post in self.routes.posts))

    def test_stop_cleanup_removes_route_and_stops_google_source(self):
        row = self._gmail()
        self.worker.run_once()  # creates the Gmail watch remote
        self.store.stop(row["watch_id"])
        self.worker.cleanup()
        self.assertEqual(self.client.gmail["stop_calls"], 1)
        self.assertEqual(self.store.get_watch(row["watch_id"])["cleanup_state"], "done")
        self.assertEqual(self.store.list_sources(service="gmail", active_only=False)[0]["state"], "stopped")
        self.assertTrue(self.routes.removes)

    def test_one_shot_native_route_is_retained_after_admission(self):
        row = self._gmail(stop_after_match=True, batch_seconds=0)
        self.store.add_event(row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.worker.forward()
        self.worker.cleanup()
        refreshed = self.store.get_watch(row["watch_id"])
        self.assertEqual(refreshed["state"], "matched")
        self.assertEqual(refreshed["cleanup_state"], "retained")
        self.assertIn("unobservable", refreshed["cleanup_reason"])
        self.assertFalse(self.routes.removes)

    def test_expiry_retains_admission_but_explicit_stop_releases_it(self):
        row = self._gmail()
        self.store.add_event(row["watch_id"], "e1", "gmail", {})
        self.worker.forward()
        self.store.add_event(row["watch_id"], "e2", "gmail", {})
        self.assertEqual(self.store.delivery_summary(row["watch_id"])["state"], "pending")
        self.store.set_event("e2", "uncertain")
        self.assertEqual(self.store.delivery_summary(row["watch_id"])["state"], "uncertain")
        self.clock.advance(3601)
        self.store.expire_due()
        self.worker.cleanup()
        self.assertFalse(self.routes.removes)
        self.store.stop(row["watch_id"])
        self.worker.cleanup()
        self.assertTrue(self.routes.removes)
        self.assertEqual(self.store.get_watch(row["watch_id"])["cleanup_state"], "done")

    def test_cleanup_retries_remote_and_route_failures(self):
        row = self._gmail(stop_after_match=True)
        self.store.add_event(row["watch_id"], "e1", "gmail", {})
        self.store.set_event("e1", "failed")
        self.store.stop(row["watch_id"])
        with mock.patch.object(self.worker, "_stop_source", return_value=False), \
             mock.patch.object(self.routes, "remove", return_value={"removed": False, "state": "conflict"}):
            self.worker.cleanup()
        self.assertEqual(self.store.get_watch(row["watch_id"])["cleanup_state"], "pending")
        self.worker.cleanup()
        self.assertEqual(self.store.get_watch(row["watch_id"])["cleanup_state"], "done")

    def test_channel_stop_identity_is_in_json_body(self):
        for service in ("drive", "calendar"):
            source = self.store.ensure_source(service, "person@example.com", {}, {})
            self.store.add_remote(service, service + "_channel", source_id=source["source_id"],
                                  identity={"channel_id": "channel", "resource_id": "resource"})
            with mock.patch.object(self.client, "call", return_value={"ok": True}) as call:
                self.assertTrue(self.worker._stop_source(source))
            call.assert_called_once_with("person@example.com", [service, "channels", "stop"], {},
                                         body={"id": "channel", "resourceId": "resource"})

    def test_bridge_render_preserves_owner_instructions(self):
        from watch_routes import _render_bridge
        row = self._gmail(instructions="Summarize the changed invoice.")
        self.assertIn("Owner instructions: Summarize the changed invoice.", _render_bridge(row, {}))

    def test_stale_forwarding_is_recovered_without_blind_replay(self):
        row = self._gmail()
        self.store.add_event(row["watch_id"], "e1", "gmail", {"message_id": "m1"})
        self.store.set_event("e1", "forwarding")
        with self.store.tx() as conn:
            conn.execute("UPDATE outbox SET updated_at=? WHERE event_id=?", (self.clock.now - 120, "e1"))
        self.worker.run_once()
        self.assertIn(self.store.outbox(row["watch_id"])[0]["state"], ("retry", "admitted"))
        # same-session uncertainty never becomes a second attempt
        self.client.add_message("root", "T1", internal_date="1600000000000")
        self.routes.same_session = True
        self.rt.ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None: True)
        result = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session")
        self.assertTrue(result["ok"], result)
        same = self.store.get_watch(result["watch"]["watch_id"])
        self.store.add_event(same["watch_id"], "e2", "gmail", {"message_id": "m2"})
        self.store.set_event("e2", "forwarding")
        with self.store.tx() as conn:
            conn.execute("UPDATE outbox SET updated_at=? WHERE event_id=?", (self.clock.now - 120, "e2"))
        self.store.recover_stale_outbox(self.clock.now)
        self.assertEqual(self.store.outbox(same["watch_id"])[0]["state"], "uncertain")

    def _same_session_watch(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        self.routes.same_session = True
        self.rt.ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None: True)
        result = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session")
        self.assertTrue(result["ok"], result)
        return self.store.get_watch(result["watch"]["watch_id"])

    def test_bridge_scheduled_state_survives_a_late_worker_failure(self):
        watch = self._same_session_watch()
        self.store.add_event(watch["watch_id"], "e1", "gmail", {"message_id": "m1"})

        def poster(name, payload, request_id, timeout=10):
            self.store.finish_bridge_event(
                "e1", "scheduled", expected_states=("forwarding",),
                detail="injected by hook")
            return {"ok": False, "error_code": "bridge_timeout", "retryable": True}

        self.worker.poster = poster
        self.worker.forward()
        row = self.store.outbox(watch["watch_id"])[0]
        self.assertEqual(row["state"], "scheduled")
        self.assertEqual(self.store.get_watch(watch["watch_id"])["delivery_state"], "scheduled")
        self.assertFalse(self.store.bridge_accepted("e1", "late ack"))
        self.assertEqual(self.store.outbox(watch["watch_id"])[0]["detail"], "injected by hook")

    def test_stale_injecting_is_uncertain_and_never_replayed(self):
        watch = self._same_session_watch()
        self.store.add_event(watch["watch_id"], "e1", "gmail", {"message_id": "m1"})
        claimed_watch, claimed = self.store.claim_outbox(
            ["e1"], state="injecting", expected_states=("pending",), generation=watch["generation"])
        self.assertIsNotNone(claimed_watch)
        self.assertEqual(claimed[0]["state"], "injecting")
        with self.store.tx() as conn:
            conn.execute("UPDATE outbox SET updated_at=? WHERE event_id='e1'",
                         (self.clock.now - 120,))
        self.store.recover_stale_outbox(self.clock.now)
        row = self.store.outbox(watch["watch_id"])[0]
        self.assertEqual(row["state"], "uncertain")
        self.assertIn("not replayed", row["detail"])
        self.worker.forward()
        self.assertEqual(self.routes.posts, [])

    def test_single_worker_lock(self):
        path = self.home / "worker.lock"
        with watch_worker.SingleInstance(path):
            second = watch_worker.SingleInstance(path)
            with self.assertRaises(RuntimeError):
                second.__enter__()
            self.assertIsNone(second.handle)  # no leaked descriptor on refusal
        with watch_worker.SingleInstance(path):
            pass  # the lock is released with the first instance
        with mock.patch.object(watch_worker, "fcntl", None):
            with self.assertRaises(RuntimeError) as raised:
                watch_worker.SingleInstance(path).__enter__()
        self.assertIn("unsupported", str(raised.exception))

    def test_channel_renewal_overlaps_and_keeps_old_remote(self):
        self.client.add_calendar("cal-1", primary=True)
        result = start_drive(self.rt, target={"all_files": True})
        self.assertTrue(result["ok"], result)
        row = self.store.get_watch(result["watch"]["watch_id"])
        source = self.store.sources_for(row["watch_id"])[0]
        self.assertTrue(self.worker._ensure_channel(source, "drive"))
        first = self.store.remote_for_source(source["source_id"], "drive_channel")
        self.store.update_remote(first["remote_id"], expires_at=self.clock.now + 10)
        self.assertTrue(self.worker._ensure_channel(self.store.get_source(source["source_id"]), "drive"))
        self.assertEqual(len(self.client.drive["watch_calls"]), 2)
        active = self.store.remotes(source_id=source["source_id"])
        self.assertGreaterEqual(len(active), 2)


if __name__ == "__main__":
    unittest.main()
