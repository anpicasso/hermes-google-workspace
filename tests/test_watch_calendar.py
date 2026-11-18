import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_calendar
from fake_google import FakeGoogle
from watch_helpers import Clock, FakeRoutes, runtime, start_calendar

CAL = "cal-1"


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        self.routes = FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)
        self.client.add_calendar(CAL, primary=True)
        self.client.add_event(CAL, "E1", summary="Client meeting",
                              attendees=[{"email": "alice@example.com", "responseStatus": "accepted"}])

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, **kwargs):
        result = start_calendar(self.rt, **kwargs)
        self.assertTrue(result["ok"], result)
        return self.store.get_watch(result["watch"]["watch_id"]), result

    def _source(self, row):
        return self.store.sources_for(row["watch_id"])[0]

    def _drain(self, row, events):
        self.client.calendar["deltas"] = events
        return watch_calendar.drain(self.client, self.store, self._source(row), [row])

    def test_primary_resolves_to_concrete_id_and_no_initial_flood(self):
        row, result = self._start(target={"calendar_ids": ["primary"]})
        self.assertEqual(result["resolved"]["calendar_ids"], [CAL])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])
        self.assertIn("E1", self.store.get_watch(row["watch_id"])["baseline_json"])

    def test_unrelated_event_and_calendar_are_ignored(self):
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["E1"]})
        unrelated = self.client.add_event(CAL, "E2", summary="Other")
        self._drain(row, [unrelated])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])
        self.client.add_calendar("cal-2")
        self.client.add_event("cal-2", "E1", summary="Same id other calendar")
        result = start_calendar(self.rt, target={"calendar_ids": ["cal-2"]}, filters={})
        self.assertTrue(result["ok"])

    def test_reschedule_matches_only_requested_fields(self):
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["E1"]},
                             filters={"change_types": ["updated"],
                                      "changed_fields": ["start", "end"]})
        moved = self.client.add_event(CAL, "E1", summary="Client meeting",
                                      start="2026-06-01T12:00:00Z", end="2026-06-01T13:00:00Z",
                                      updated="2026-06-01T10:00:00Z")
        self._drain(row, [moved])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)
        self.assertIn("updated", self.store.outbox(row["watch_id"])[0]["payload_json"])
        # A description/other-field-only edit is invisible to this field filter.
        row = self.store.get_watch(row["watch_id"])
        self.client.calendar["next_sync"] = "sync-3"
        self.store.update_source(self._source(row)["source_id"], cursor="sync-2")
        same = dict(moved, location="Room 2", updated="2026-06-01T11:00:00Z")
        self.client.calendar["deltas"] = [same]
        watch_calendar.drain(self.client, self.store, self._source(row), [row])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)

    def test_selected_attendee_rsvp_change_matches(self):
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["E1"]},
                             filters={"attendee_addresses": ["alice@example.com"],
                                      "changed_fields": ["attendee_responses"]})
        changed = self.client.add_event(CAL, "E1", summary="Client meeting",
                                        attendees=[{"email": "alice@example.com",
                                                    "responseStatus": "declined"}],
                                        updated="2026-06-01T10:00:00Z")
        self._drain(row, [changed])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)
        self.assertIn("declined", self.store.outbox(row["watch_id"])[0]["payload_json"])

    def test_omitted_attendees_are_unknown_not_declined(self):
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["E1"]},
                             filters={"attendee_addresses": ["alice@example.com"],
                                      "changed_fields": ["attendee_responses"]})
        changed = self.client.add_event(CAL, "E1", summary="Client meeting", attendees=[],
                                        updated="2026-06-01T10:00:00Z")
        self._drain(row, [changed])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])

    def test_all_day_end_is_exclusive(self):
        self.client.add_event(CAL, "ALLDAY", summary="Holiday", date_start="2026-06-01",
                              date_end="2026-06-02", updated="2026-05-01T00:00:00Z")
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["ALLDAY"]},
                             filters={"date_window": {"start": "2026-06-02T00:00:00Z",
                                                      "end": "2026-06-03T00:00:00Z"}})
        self.assertEqual(self.store.outbox(row["watch_id"]), [])
        row = self.store.get_watch(row["watch_id"])
        changed = dict(self.client.calendar["events"][CAL]["ALLDAY"], updated="2026-06-01T10:00:00Z")
        self.client.calendar["deltas"] = [changed]
        watch_calendar.drain(self.client, self.store, self._source(row), [row])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])

    def test_moved_out_of_window_still_matches(self):
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["E1"]},
                             filters={"date_window": {"start": "2026-06-01T00:00:00Z",
                                                      "end": "2026-06-02T00:00:00Z"}})
        moved = self.client.add_event(CAL, "E1", summary="Client meeting",
                                      start="2026-07-01T10:00:00Z", end="2026-07-01T11:00:00Z",
                                      updated="2026-06-01T10:00:00Z")
        self._drain(row, [moved])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)

    def test_series_master_and_exception_identity(self):
        self.client.add_event(CAL, "SERIES", summary="Weekly", recurring=None,
                              updated="2026-05-01T00:00:00Z")
        self.client.calendar["events"][CAL]["SERIES"]["recurrence"] = ["RRULE:FREQ=WEEKLY"]
        row, _ = self._start(target={"calendar_ids": [CAL], "recurring_event_ids": ["SERIES"]})
        exception = self.client.add_event(CAL, "EXC1", summary="Weekly", recurring="SERIES",
                                          original_start={"dateTime": "2026-06-08T10:00:00Z"},
                                          updated="2026-06-01T10:00:00Z")
        self._drain(row, [exception])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)
        self.assertIn("SERIES", self.store.outbox(row["watch_id"])[0]["payload_json"])

    def test_series_with_date_window_is_rejected(self):
        self.client.calendar["events"][CAL]["SERIES"] = dict(
            self.client.add_event(CAL, "SERIES", summary="Weekly"),
            recurrence=["RRULE:FREQ=WEEKLY"])
        result = start_calendar(self.rt, target={"calendar_ids": [CAL],
                                                 "recurring_event_ids": ["SERIES"]},
                                filters={"date_window": {"start": "2026-06-01T00:00:00Z",
                                                         "end": "2026-06-02T00:00:00Z"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unsupported_combination")

    def test_series_lookup_requests_recurrence_field(self):
        event = self.client.add_event(CAL, "SERIES", summary="Weekly")
        event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
        row, _ = self._start(target={"calendar_ids": [CAL], "recurring_event_ids": ["SERIES"]})
        self.assertTrue(row["watch_id"])
        gets = [call for call in self.client.calls if call["args"][-2:] == ["events", "get"]]
        self.assertTrue(gets)
        self.assertIn("recurrence", gets[-1]["params"]["fields"])

    def test_master_series_with_date_window_is_rejected(self):
        event = self.client.add_event(CAL, "SERIES", summary="Weekly")
        event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
        result = start_calendar(self.rt, target={"calendar_ids": [CAL], "event_ids": ["SERIES"]},
                                filters={"date_window": {"start": "2026-06-01T00:00:00Z",
                                                         "end": "2026-06-02T00:00:00Z"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unsupported_combination")

    def test_master_series_with_date_window_does_not_false_match(self):
        event = self.client.add_event(CAL, "SERIES", summary="Weekly",
                                      start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z")
        event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
        row, _ = self._start(target={"calendar_ids": [CAL]},
                             filters={"date_window": {"start": "2026-06-01T00:00:00Z",
                                                      "end": "2026-06-02T00:00:00Z"}})
        changed = dict(event, summary="Weekly renamed", updated="2026-06-01T10:00:00Z")
        self._drain(row, [changed])
        # The first occurrence overlaps, but the series' later occurrences are
        # unbounded; fail closed instead of claiming an unsupported match.
        self.assertEqual(self.store.outbox(row["watch_id"]), [])

    def test_all_day_uses_calendar_timezone(self):
        try:
            import zoneinfo
            zoneinfo.ZoneInfo("America/Lima")
        except Exception:
            self.skipTest("system tzdata is unavailable")
        self.client.calendar["calendars"][CAL]["timeZone"] = "America/Lima"
        event = self.client.add_event(CAL, "ALLDAY", summary="Holiday", date_start="2026-06-01",
                                      date_end="2026-06-02", updated="2026-05-01T00:00:00Z")
        event["start"].pop("timeZone", None)
        event["end"].pop("timeZone", None)
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["ALLDAY"]},
                             filters={"date_window": {"start": "2026-06-02T00:00:00Z",
                                                      "end": "2026-06-02T04:00:00Z"}})
        baseline = json.loads(self.store.get_watch(row["watch_id"])["baseline_json"])
        self.assertEqual(baseline["timezones"][CAL], "America/Lima")
        changed = dict(event, summary="Holiday observed", updated="2026-06-01T10:00:00Z")
        self._drain(row, [changed])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)

    def test_event_lookup_transient_failure_is_not_target_not_found(self):
        self.client.fail(("calendar", "events", "get"), 500, "api_error", True)
        result = start_calendar(self.rt, target={"calendar_ids": [CAL], "event_ids": ["E1"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "calendar_access")

    def test_sparse_cancellation_uses_prior_metadata(self):
        row, _ = self._start(target={"calendar_ids": [CAL], "event_ids": ["E1"]},
                             filters={"summary_contains": ["client"]})
        cancelled = {"id": "E1", "status": "cancelled", "summary": "",
                     "start": {}, "end": {}, "organizer": {}, "attendees": [],
                     "recurringEventId": "", "originalStartTime": {},
                     "updated": "2026-06-01T10:00:00Z"}
        self._drain(row, [cancelled])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)
        self.assertIn("cancelled", self.store.outbox(row["watch_id"])[0]["payload_json"])

    def test_410_rebuilds_without_flood_and_marks_gap(self):
        row, _ = self._start(target={"calendar_ids": [CAL]})
        self.client.calendar["gone"] = True
        result = watch_calendar.drain(self.client, self.store, self._source(row), [row])
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["resynced"])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])
        self.assertIn("cannot be recovered", self.store.get_watch(row["watch_id"])["gap"])

    def test_same_event_id_in_two_target_calendars_is_ambiguous(self):
        self.client.add_calendar("cal-2")
        self.client.add_event("cal-2", "E1", summary="Other")
        result = start_calendar(self.rt, target={"calendar_ids": [CAL, "cal-2"],
                                                 "event_ids": ["E1"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ambiguous_event")

    def test_start_requires_callback_url(self):
        rt = runtime(self.store, self.client, self.routes, config={"watch_callback_url": ""})
        result = start_calendar(rt, target={"calendar_ids": [CAL]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "calendar_callback_missing")


if __name__ == "__main__":
    unittest.main()
