import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_drive
from fake_google import FakeGoogle
from watch_helpers import Clock, FakeRoutes, runtime, start_drive

FOLDER = "application/vnd.google-apps.folder"


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        self.routes = FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)
        self.client.add_file("FOLDER", "invoices", FOLDER)
        self.client.add_file("EXISTING", "old.pdf", "application/pdf", ["FOLDER"],
                             modified="2025-01-01T00:00:00Z")
        self.client.add_file("OTHER", "elsewhere.txt", parents=["OTHER_FOLDER"])

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, **kwargs):
        result = start_drive(self.rt, **kwargs)
        self.assertTrue(result["ok"], result)
        return self.store.get_watch(result["watch"]["watch_id"]), result

    def _source(self, row):
        return self.store.sources_for(row["watch_id"])[0]

    def test_folder_baseline_does_not_announce_existing_children(self):
        row, result = self._start(target={"folder_id": "FOLDER"})
        self.assertTrue(result["resolved"]["direct_children_only"])
        self.assertEqual(result["resolved"]["folder_id"], "FOLDER")
        self.assertEqual(self.store.outbox(row["watch_id"]), [])
        self.assertIn("EXISTING", self.store.get_watch(row["watch_id"])["baseline_json"])

    def test_pdf_arrival_matches_and_nested_child_does_not(self):
        row, _ = self._start(target={"folder_id": "FOLDER"},
                             filters={"mime_types": ["application/pdf"], "name_contains": ["contract"]})
        self.client.add_file("SUB", "nested", FOLDER, ["FOLDER"])
        self.client.add_file("NEW", "contract.pdf", "application/pdf", ["FOLDER"],
                             modified="2026-06-01T00:00:00Z")
        self.client.add_file("NESTED", "contract-nested.pdf", "application/pdf", ["SUB"],
                             modified="2026-06-01T00:00:00Z")
        self.client.set_change_pages([
            {"fileId": "NEW", "removed": False, "file": self.client.drive["files"]["NEW"]},
            {"fileId": "NESTED", "removed": False, "file": self.client.drive["files"]["NESTED"]},
        ])
        result = watch_drive.drain(self.client, self.store, self._source(row), [row])
        self.assertTrue(result["ok"], result)
        rows = self.store.outbox(row["watch_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload_json"].count("contract.pdf"), 1)

    def test_rename_away_matches_prior_name_and_is_not_lost(self):
        self.client.add_file("DOC", "budget.xlsx", parents=["FOLDER"])
        row, _ = self._start(target={"folder_id": "FOLDER"},
                             filters={"name_contains": ["budget"]})
        moved = dict(self.client.drive["files"]["DOC"], name="memo.txt",
                     modifiedTime="2026-06-02T00:00:00Z", version="2")
        self.client.set_change_pages([{"fileId": "DOC", "removed": False, "file": moved}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        rows = self.store.outbox(row["watch_id"])
        self.assertEqual(len(rows), 1)
        self.assertIn("renamed", rows[0]["payload_json"])

    def test_move_out_is_reported_as_moved_not_access_loss(self):
        self.client.add_file("DOC", "budget.xlsx", parents=["FOLDER"])
        row, _ = self._start(target={"folder_id": "FOLDER"},
                             filters={"name_contains": ["budget"]})
        moved = dict(self.client.drive["files"]["DOC"], parents=["OTHER_FOLDER"],
                     modifiedTime="2026-06-02T00:00:00Z", version="2")
        self.client.set_change_pages([{"fileId": "DOC", "removed": False, "file": moved}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        rows = self.store.outbox(row["watch_id"])
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload_json"]
        self.assertIn("moved", payload)
        self.assertIn('"removed": false', payload)
        self.assertNotIn("no longer accessible", payload)
        # The child is no longer a member of the watched folder.
        baseline = self.store.get_watch(row["watch_id"])["baseline_json"]
        self.assertNotIn("DOC", baseline)

    def test_access_loss_removal_uses_prior_metadata(self):
        self.client.add_file("DOC", "budget.xlsx", parents=["FOLDER"])
        row, _ = self._start(target={"folder_id": "FOLDER"},
                             filters={"name_contains": ["budget"]})
        self.client.set_change_pages([{"fileId": "DOC", "removed": True, "file": None,
                                       "time": "2026-06-02T00:00:00Z"}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        rows = self.store.outbox(row["watch_id"])
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload_json"]
        self.assertIn("removed or no longer accessible", payload)
        self.assertIn("budget.xlsx", payload)

    def test_all_files_removal_is_not_dropped_without_prior_baseline(self):
        row, _ = self._start(target={"all_files": True})
        self.client.set_change_pages([{"fileId": "GONE", "removed": True, "file": None,
                                       "time": "2026-06-02T00:00:00Z"}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        rows = self.store.outbox(row["watch_id"])
        self.assertEqual(len(rows), 1)
        self.assertIn("removed or no longer accessible", rows[0]["payload_json"])

    def test_old_queued_change_time_does_not_fire_new_watch(self):
        row, _ = self._start(target={"folder_id": "FOLDER"})
        self.client.add_file("NEW", "new.pdf", "application/pdf", ["FOLDER"],
                             modified="2026-06-01T00:00:00Z")
        self.client.set_change_pages([{
            "fileId": "NEW", "removed": False, "time": "2023-11-13T00:00:00Z",
            "file": self.client.drive["files"]["NEW"]}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])

    def test_staged_pagination_persists_membership(self):
        self.client.add_file("DOC", "budget.xlsx", parents=["FOLDER"])
        self.client.add_file("SECOND", "other.txt", parents=["FOLDER"])
        row, _ = self._start(target={"folder_id": "FOLDER"})
        moved = dict(self.client.drive["files"]["DOC"], parents=["OTHER_FOLDER"],
                     modifiedTime="2026-06-02T00:00:00Z", version="2")
        self.client.set_change_pages([
            {"fileId": "DOC", "removed": False, "file": moved, "time": "2026-06-02T00:00:00Z"},
            {"fileId": "SECOND", "removed": False, "time": "2026-06-02T00:00:00Z",
             "file": dict(self.client.drive["files"]["SECOND"], version="2",
                          modifiedTime="2026-06-02T00:00:00Z")},
        ], page_size=1)
        source = self._source(row)
        first = watch_drive.drain(self.client, self.store, source, [row], max_pages=1)
        self.assertTrue(first["staged"])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)
        self.assertNotIn("DOC", self.store.get_watch(row["watch_id"])["baseline_json"])
        second = watch_drive.drain(self.client, self.store,
                                   self.store.get_source(source["source_id"]), [row])
        self.assertTrue(second["ok"], second)
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 2)

    def test_trash_and_restore_are_distinct(self):
        self.client.add_file("DOC", "budget.xlsx", parents=["FOLDER"])
        row, _ = self._start(target={"folder_id": "FOLDER"},
                             filters={"name_contains": ["budget"]})
        trashed = dict(self.client.drive["files"]["DOC"], trashed=True,
                       modifiedTime="2026-06-02T00:00:00Z", version="2")
        self.client.set_change_pages([{"fileId": "DOC", "removed": False, "file": trashed}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        self.assertIn("trashed", self.store.outbox(row["watch_id"])[-1]["payload_json"])
        restored = dict(trashed, trashed=False, modifiedTime="2026-06-03T00:00:00Z", version="3")
        self.client.set_change_pages([{"fileId": "DOC", "removed": False, "file": restored}])
        self.store.update_source(self._source(row)["source_id"], cursor="20")
        row = self.store.get_watch(row["watch_id"])
        watch_drive.drain(self.client, self.store, self.store.get_source(self._source(row)["source_id"]), [row])
        self.assertIn("restored", self.store.outbox(row["watch_id"])[-1]["payload_json"])

    def test_file_target_removed_uses_prior_metadata(self):
        self.client.add_file("A", "proposal.docx", parents=["FOLDER"])
        row, _ = self._start(target={"file_ids": ["A"]})
        self.client.set_change_pages([{"fileId": "A", "removed": True, "file": None}])
        watch_drive.drain(self.client, self.store, self._source(row), [row])
        rows = self.store.outbox(row["watch_id"])
        self.assertEqual(len(rows), 1)
        self.assertIn("removed or no longer accessible", rows[0]["payload_json"])

    def test_duplicate_change_replay_does_not_duplicate_outbox(self):
        row, _ = self._start(target={"folder_id": "FOLDER"})
        self.client.add_file("NEW", "new.pdf", "application/pdf", ["FOLDER"])
        self.client.set_change_pages([{"fileId": "NEW", "removed": False,
                                       "file": self.client.drive["files"]["NEW"]}])
        source = self._source(row)
        watch_drive.drain(self.client, self.store, source, [row])
        replay = dict(source, cursor="10", cursor_staged=None)
        watch_drive.drain(self.client, self.store, replay, [row])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 1)

    def test_cursor_continuation_across_slices(self):
        row, _ = self._start(target={"folder_id": "FOLDER"})
        self.client.add_file("N1", "one.txt", parents=["FOLDER"])
        self.client.add_file("N2", "two.txt", parents=["FOLDER"])
        self.client.set_change_pages([
            {"fileId": "N1", "removed": False, "file": self.client.drive["files"]["N1"]},
            {"fileId": "N2", "removed": False, "file": self.client.drive["files"]["N2"]},
        ], page_size=1)
        source = self._source(row)
        first = watch_drive.drain(self.client, self.store, source, [row], max_pages=1)
        self.assertTrue(first["staged"])
        source = self.store.get_source(source["source_id"])
        self.assertEqual(source["cursor"], "10")
        self.assertIsNotNone(source["cursor_staged"])
        second = watch_drive.drain(self.client, self.store, source, [row], max_pages=1)
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.store.outbox(row["watch_id"])), 2)
        self.assertEqual(self.store.get_source(source["source_id"])["cursor"], "20")

    def test_shared_drive_mismatch_is_refused(self):
        self.client.add_file("A", "x.txt", parents=[], drive_id="drive-1")
        result = start_drive(self.rt, target={"file_ids": ["A"]}, shared_drive_id="drive-2")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "drive_mismatch")

    def test_folder_target_must_be_a_folder(self):
        result = start_drive(self.rt, target={"folder_id": "EXISTING"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_a_folder")

    def test_rejected_change_token_is_reported_not_invented_as_resync(self):
        row, _ = self._start(target={"folder_id": "FOLDER"})
        self.client.fail(("drive", "changes", "list"), 410, "gone", False)
        result = watch_drive.drain(self.client, self.store, self._source(row), [row])
        self.assertFalse(result["ok"])
        self.assertTrue(result["resync_required"])
        self.assertEqual(self.store.outbox(row["watch_id"]), [])

    def test_start_requires_callback_url(self):
        rt = runtime(self.store, self.client, self.routes, config={"watch_callback_url": ""})
        result = start_drive(rt, target={"folder_id": "FOLDER"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "drive_callback_missing")


if __name__ == "__main__":
    unittest.main()
