"""Audit regressions: calendar start barrier, subscription/stop backoff, stop race,
retention and worker health. Fake clock and fake Google only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_calendar
import watch_routes
import watch_worker
from fake_google import FakeGoogle
from watch_helpers import Clock, FakeRoutes, runtime, start_calendar, start_drive
from test_watch_pubsub import runtime as push_runtime, FakeGoogle as PushGoogle
from watch_helpers import start_gmail

CAL = "cal-1"


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        self.routes = FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)
        self.client.add_calendar(CAL, primary=True)
        self.client.add_event(CAL, "E1", summary="Client meeting")
        self.worker = self._worker()

    def tearDown(self):
        self.tmp.cleanup()

    def _worker(self, store=None):
        return watch_worker.Worker(self.home, self.client, self.routes, self.rt.config,
                                   store=store or self.store, poster=self.routes.post,
                                   clock=self.clock)

    def _calendar(self):
        result = start_calendar(self.rt, target={"calendar_ids": [CAL]})
        self.assertTrue(result["ok"], result)
        row = self.store.get_watch(result["watch"]["watch_id"])
        return row, self.store.sources_for(row["watch_id"])[0]

    def test_gmail_fallback_is_periodic_not_every_worker_tick(self):
        self.client = PushGoogle()
        self.rt = push_runtime(self.store, self.client, self.routes)
        result = start_gmail(self.rt)
        self.assertTrue(result["ok"], result)
        self.worker = self._worker()
        self.worker.run_once()
        self.assertFalse(any(c["args"][-2:] == ["history", "list"] for c in self.client.calls))
        self.clock.advance(301)
        self.worker.run_once()
        count = sum(c["args"][-2:] == ["history", "list"] for c in self.client.calls)
        self.assertEqual(count, 1)
        self.worker.run_once()
        self.assertEqual(sum(c["args"][-2:] == ["history", "list"] for c in self.client.calls), count)

    def test_gmail_racing_and_staged_hints_survive_until_reconciled(self):
        from unittest.mock import patch
        self.client = PushGoogle()
        self.rt = push_runtime(self.store, self.client, self.routes)
        result = start_gmail(self.rt)
        self.assertTrue(result["ok"], result)
        source = self.store.sources_for(result["watch"]["watch_id"])[0]
        self.worker = self._worker()
        self.store.add_hint("old", source["source_id"], {"history_id": "100"})
        def race(*args, **kwargs):
            self.store.add_hint("new", source["source_id"], {"history_id": "102"})
            return {"ok": True, "cursor": "101"}
        with patch.object(watch_worker.watch_gmail, "drain", side_effect=race):
            self.worker.drain_source(source)
        self.assertEqual([h["hint_id"] for h in self.store.pending_hints()], ["new"])
        with patch.object(watch_worker.watch_gmail, "drain", return_value={"ok": True, "staged": True}):
            self.worker.drain_source(source)
        self.assertEqual(len(self.store.pending_hints()), 1)
        with patch.object(watch_worker.watch_gmail, "drain", return_value={"ok": True, "cursor": "101"}):
            self.worker.drain_source(source)
        self.assertEqual(len(self.store.pending_hints()), 1)
        with patch.object(watch_worker.watch_gmail, "drain", return_value={"ok": True, "cursor": "102"}):
            self.worker.drain_source(source)
        self.assertEqual(self.store.pending_hints(), [])

    def test_calendar_drain_honours_start_barrier(self):
        row, source = self._calendar()
        old = dict(self.client.calendar["events"][CAL]["E1"], summary="edited",
                   updated="2020-01-01T00:00:00Z")  # stamped before the watch started
        self.client.calendar["deltas"] = [old]
        result = watch_calendar.drain(self.client, self.store, source, [row])
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])
        # The before-state still advances so a later real edit diffs correctly.
        self.assertIn("edited", self.store.get_watch(row["watch_id"])["baseline_json"])
        fresh = dict(old, summary="edited again", updated="2026-06-02T00:00:00Z")
        self.client.calendar["deltas"] = [fresh]
        source = self.store.get_source(source["source_id"])
        watch_calendar.drain(self.client, self.store, source, [self.store.get_watch(row["watch_id"])])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)

    def test_failed_registration_backs_off_durably_without_row_growth(self):
        row, source = self._calendar()
        calls = []
        original = self.client.call
        def denied(account, args, params=None, body=None, timeout=None):
            if args[-1] == "watch":
                calls.append(args)
                return {"ok": False, "error": "denied", "http_status": 403, "retryable": False}
            return original(account, args, params, body=body, timeout=timeout)
        self.client.call = denied
        for _ in range(5):
            self.worker.run_once()
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.store.get_source(source["source_id"])["last_reconcile"])
        self.clock.advance(11)  # first delay is 10s
        self.worker.run_once()
        self.assertEqual(len(calls), 2)
        # Restart: a fresh worker on the same DB keeps the backoff (now 20s).
        restarted = self._worker(watch.WatchStore(self.home, clock=self.clock))
        restarted.run_once()
        self.assertEqual(len(calls), 2)
        remotes = self.store.remotes(source_id=source["source_id"])
        self.assertEqual([r["state"] for r in remotes], ["error"])
        for _ in range(30):  # bounded: delay never exceeds the configured cap
            self.clock.advance(901)
            restarted.run_once()
        state = json.loads(self.store.get_meta("backoff:sub:" + source["source_id"]))
        self.assertLessEqual(state["next"] - self.clock.now, 900)
        self.assertEqual(len(self.store.remotes(source_id=source["source_id"])), 1)
        # A fresh explicit start on the source clears the backoff.
        self.store.stop(row["watch_id"])
        self.client.call = original
        self._calendar()
        self.assertIsNone(self.store.get_meta("backoff:sub:" + source["source_id"]))

    def test_source_stop_does_not_clobber_concurrent_rearm(self):
        row, source = self._calendar()
        self.store.stop(row["watch_id"])
        self.store.add_remote("channel:old", "calendar_channel", source_id=source["source_id"],
                              state="active", identity={"token": "t", "channel_id": "old",
                                                        "resource_id": "rid"})
        rearmed = {}
        original = self.client.call
        def call(account, args, params=None, body=None, timeout=None):
            if list(args[1:]) == ["channels", "stop"] and not rearmed:
                rearmed["row"] = self._calendar()[0]  # user re-arms while stop is in flight
            return original(account, args, params, body=body, timeout=timeout)
        self.client.call = call
        self.assertTrue(self.worker._stop_source(source))
        self.assertEqual(self.store.get_source(source["source_id"])["state"], "active")
        self.assertEqual(self.store.get_watch(rearmed["row"]["watch_id"])["state"], "active")
        # Without a concurrent consumer the source is stopped as before.
        self.store.stop(rearmed["row"]["watch_id"])
        self.assertTrue(self.worker._stop_source(self.store.get_source(source["source_id"])))
        self.assertEqual(self.store.get_source(source["source_id"])["state"], "stopped")

    def test_failed_remote_stop_backs_off(self):
        row, source = self._calendar()
        self.store.add_remote("channel:x", "calendar_channel", source_id=source["source_id"],
                              state="active", identity={"channel_id": "x", "resource_id": "r"})
        calls = []
        def failing(account, args, params=None, body=None, timeout=None):
            calls.append(args)
            return {"ok": False, "error_code": "unavailable", "http_status": 503}
        self.client.call = failing
        self.store.stop(row["watch_id"])
        for _ in range(4):
            self.worker.cleanup()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get_watch(row["watch_id"])["cleanup_state"], "pending")

    def test_retention_prunes_only_cleaned_terminal_records(self):
        done, _ = self._calendar()
        self.store.stop(done["watch_id"])
        self.worker.cleanup()
        self.assertEqual(self.store.get_watch(done["watch_id"])["cleanup_state"], "done")
        self.client.add_file("F", "f", "application/vnd.google-apps.folder")
        kept = start_drive(self.rt, target={"folder_id": "F"}, stop_after_match=True)
        kept = self.store.get_watch(kept["watch"]["watch_id"])
        self.store.add_event(kept["watch_id"], "e1", "drive", {})
        self.store.finish_outbox(["e1"], "admitted", expected_states=("pending",))
        self.worker.cleanup()  # matched + admitted -> route retained
        self.assertEqual(self.store.get_watch(kept["watch_id"])["cleanup_state"], "retained")
        uncertain, _ = self._calendar_other()
        self.store.add_event(uncertain["watch_id"], "u1", "calendar", {})
        self.store.finish_outbox(["u1"], "uncertain", expected_states=("pending",))
        # Even explicit route cleanup must retain ambiguous delivery evidence.
        self.store.update_watch(uncertain["watch_id"], state="expired", cleanup_state="done")

        self.store.prune(retention_seconds=86400)  # too young: nothing goes
        self.assertIsNotNone(self.store.get_watch(done["watch_id"]))
        self.clock.advance(3 * 86400)
        result = self.store.prune(retention_seconds=86400)
        self.assertEqual(result["watches"], 1)
        self.assertIsNone(self.store.get_watch(done["watch_id"]))
        self.assertIsNotNone(self.store.get_watch(kept["watch_id"]))
        self.assertEqual(len(self.store.outbox(kept["watch_id"])), 1)
        self.assertIsNotNone(self.store.get_watch(uncertain["watch_id"]))
        self.assertIsNotNone(self.store.get_remote("route:" + kept["watch_id"]))

    def _calendar_other(self):
        self.client.add_calendar("cal-2")
        result = start_calendar(self.rt, target={"calendar_ids": ["cal-2"]})
        self.assertTrue(result["ok"], result)
        row = self.store.get_watch(result["watch"]["watch_id"])
        return row, self.store.sources_for(row["watch_id"])[0]

    def test_worker_health_reports_heartbeat_lock_and_retries(self):
        lock = self.home / "worker.lock"
        report = watch_worker.health(self.store, lock)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["heartbeat"])
        self._calendar()
        self.worker.run_once()
        with watch_worker.SingleInstance(lock):
            report = watch_worker.health(self.store, lock)
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["worker_running"])
            self.clock.advance(120)
            self.assertFalse(watch_worker.health(self.store, lock)["ok"])
        self.assertFalse(watch_worker.health(self.store, lock)["worker_running"])
        self.assertEqual(report["watches"], {"active": 1})
        self.assertNotIn("token", json.dumps(report))

    def test_status_cli_exit_code(self):
        self.assertEqual(watch_worker.main(["status", "--home", str(self.home)]), 1)

    def test_same_session_preflight_uses_only_public_capability(self):
        class PublicContext:
            def inject_message(self, *args, **kwargs):
                return False  # actual admission, not capability discovery, decides consent
            @property
            def _gateway_injection_allowed(self):
                raise AssertionError("Private host API must not be inspected")
        self.assertTrue(watch_routes.same_session_capability(PublicContext())["supported"])
        unavailable = SimpleNamespace(inject_message=None)
        self.assertFalse(watch_routes.same_session_capability(unavailable)["supported"])
        self.rt.ctx = unavailable
        self.routes.same_session = True
        result = start_calendar(self.rt, target={"calendar_ids": [CAL]}, mode="same_session")
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.list_watches(), [])


if __name__ == "__main__":
    unittest.main()
