"""Private, bounded source history records facts without patient-based matching."""
import copy
import json
import tempfile
import unittest

from vghks_sheet2cal.calendar_monitor import MonitorStore
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, MONTH_HEADERS, cell, monthly, row, workbook
from vghks_sheet2cal.source_journal import SourceJournal

SECRET = "SYNTHETIC_PRIVATE_NAME_MRN_VALUE"


class SourceJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MonitorStore(self.temp.name)
        self.addCleanup(self.store.close)
        self.configure("synthetic-source-a")
        self.journal = SourceJournal(self.store)
        self.book = workbook(monthly())
        self.at = 0

    def configure(self, source_id):
        config = self.store.get("config", "settings")
        self.store.save("config", "settings", {**config, "spreadsheet_id": source_id})

    def observe(self, *, phase="source", journal=None):
        self.at += 1
        return (journal or self.journal).observe(self.book, f"2026-11-10T18:{self.at:02}:00+08:00", phase=phase)

    def count(self, table):
        with self.store.connect() as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_baseline_persists_raw_private_cells_and_deduplicates_unchanged_reads(self):
        self.book["sheets"][0]["rows"][2][3] = cell(SECRET)
        before = copy.deepcopy(self.book)
        first = self.observe()
        self.assertTrue(first["baseline"])
        self.assertEqual(first["changed"], 0)
        self.assertTrue(first["source_read_complete"])
        self.assertFalse(first["calendar_confirmed"])
        self.assertIn(SECRET, json.dumps(self.journal.latest()))
        self.assertNotIn(SECRET, json.dumps(first))
        self.assertEqual(self.journal.recent(), [])
        second = self.observe(phase="final")
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(second["changed"], 0)
        self.assertTrue(second["unchanged"])
        self.assertEqual(self.count("source_observations"), 1)
        self.assertEqual(self.count("source_changes"), 0)
        self.assertEqual(SourceJournal(self.store).latest()["revision"], first["revision"])
        self.assertEqual(self.book, before)

    def test_new_row_and_technical_assignment_are_distinct_observations(self):
        self.observe()
        sheet = self.book["sheets"][0]
        sheet["rows"].append(row(MONTH_HEADERS, **{"病歷號": SECRET, "姓名": SECRET, "Plan": SECRET}))
        added = self.observe()
        self.assertEqual((added["added"], added["removed"]), (1, 0))
        self.assertEqual(added["diagnostic_counts"]["missing_sync_id"], 1)
        sheet["rows"][-1][MONTH_HEADERS.index("ScheduleSyncId")] = cell("f" * 32)
        assigned = self.observe(phase="identity_setup")
        self.assertEqual((assigned["added"], assigned["removed"], assigned["modified"]), (0, 0, 1))
        self.assertEqual(self.journal.recent()[0]["fields"], ["uid"])
        self.assertEqual(self.journal.recent()[0]["phase"], "identity_setup")
        sheet["rows"][-1][MONTH_HEADERS.index("CalendarEventId")] = cell("synthetic-event")
        bound = self.observe()
        self.assertEqual(bound["modified"], 1)
        self.assertEqual(self.journal.recent()[0]["fields"], ["event_id"])
        self.assertNotIn(SECRET, json.dumps(self.journal.recent()))

    def test_same_patient_deleted_and_retyped_is_not_matched_or_merged(self):
        self.observe()
        sheet = self.book["sheets"][0]
        removed = sheet["rows"].pop(2)
        removed[MONTH_HEADERS.index("ScheduleSyncId")] = {}
        removed[MONTH_HEADERS.index("CalendarEventId")] = {}
        sheet["rows"].append(removed)
        result = self.observe()
        self.assertEqual((result["added"], result["removed"]), (1, 1))
        kinds = [change["kind"] for change in self.journal.recent()]
        self.assertIn("removed", kinds)
        self.assertIn("added", kinds)

    def test_unique_uid_move_keeps_previous_and_new_date_campus_and_location(self):
        self.observe()
        sheet = self.book["sheets"][0]
        patient = sheet["rows"].pop(2)
        destination = monthly("202612", 2)
        destination["rows"] = [destination["rows"][0], row(MONTH_HEADERS, **{
            "日期／報到時間": "2026-12-23", "醫院": "聯醫", "病歷號": "◆ 刀日", "ScheduleSyncId": "e" * 32}), patient]
        self.book["sheets"].append(destination)
        self.observe()
        moved = next(item for item in self.journal.recent() if item["kind"] == "moved" and item["sheet"] == "202612")
        self.assertEqual((moved["previous_sheet"], moved["previous_row"], moved["row"]), ("202610", 3, 3))
        self.assertEqual((moved["previous_date"], moved["date"]), ("2026-10-12", "2026-12-23"))
        self.assertEqual((moved["previous_hospital"], moved["hospital"]), ("高榮", "聯醫"))
        self.assertIn("sheet", moved["fields"])
        self.assertEqual(self.journal.latest()["rows"][-1]["uid"], "b" * 32)

    def test_day_header_changes_are_recorded_on_the_patient_effective_route(self):
        self.observe()
        sheet = self.book["sheets"][0]
        unchanged_patient = copy.deepcopy(sheet["rows"][2])
        sheet["rows"][1][0], sheet["rows"][1][1] = cell("2026-11-23"), cell("聯醫")
        result = self.observe()
        self.assertEqual(result["moved"], 2)  # Header and its patient share the changed day route.
        patient = next(item for item in self.journal.recent() if item["row"] == 3)
        self.assertEqual(patient["fields"], ["date", "hospital"])
        self.assertEqual((patient["date"], patient["hospital"]), ("2026-11-23", "聯醫"))
        self.assertEqual(sheet["rows"][2], unchanged_patient)

    def test_fu_date_and_campus_changes_are_observations_without_calendar_claims(self):
        self.book = workbook({"properties": {"sheetId": 2, "title": "FU"}, "rows": [
            list(map(cell, FU_HEADERS)), row(FU_HEADERS, **{"病歷號": SECRET, "姓名": SECRET,
                "日期": "2026-11-10", "醫院": "高榮", "ScheduleSyncId": "f" * 32})]})
        self.observe()
        cells = self.book["sheets"][0]["rows"][1]
        cells[FU_HEADERS.index("日期")], cells[FU_HEADERS.index("醫院")] = cell("2026-11-23"), cell("聯醫")
        result = self.observe()
        self.assertEqual(result["moved"], 1)
        self.assertFalse(result["calendar_confirmed"])
        change = self.journal.recent()[0]
        self.assertEqual(change["fields"], ["date", "hospital"])
        self.assertEqual((change["previous_date"], change["date"]), ("2026-11-10", "2026-11-23"))
        self.assertNotIn(SECRET, json.dumps(change))

    def test_formulas_empty_mrn_notes_and_displayed_cancellation_are_preserved_privately(self):
        cells = self.book["sheets"][0]["rows"][2]
        plan = MONTH_HEADERS.index("Plan")
        cells[plan] = {"userEnteredValue": {"formulaValue": "=1+1"}, "formattedValue": "2", "note": SECRET}
        self.observe()
        cells[plan]["userEnteredValue"]["formulaValue"] = "=2*1"
        cells[2] = {"userEnteredValue": {"stringValue": ""},
                    "effectiveFormat": {"textFormat": {"strikethrough": True}}}
        # Give the name a nonempty value so clearing the MRN remains a patient row.
        cells[3] = cell(SECRET)
        self.observe()
        change = self.journal.recent()[0]
        self.assertIn("plan", change["fields"])
        self.assertIn("mrn", change["fields"])
        self.assertIn("cancelled", change["fields"])
        latest = next(row for row in self.journal.latest()["rows"] if row["uid"] == "b" * 32)
        self.assertEqual(latest["cells"][plan]["userEnteredValue"], {"formulaValue": "=2*1"})
        self.assertEqual(latest["cells"][plan]["note"], SECRET)
        self.assertNotIn(SECRET, json.dumps(self.journal.recent()))

    def test_duplicate_orphan_and_invalid_schema_diagnostics_are_not_hidden(self):
        self.observe()
        sheet = self.book["sheets"][0]
        orphan = [cell("") for _ in MONTH_HEADERS]
        orphan[MONTH_HEADERS.index("ScheduleSyncId")] = cell("b" * 32)
        orphan[MONTH_HEADERS.index("CalendarEventId")] = cell("synthetic-duplicate-event")
        sheet["rows"][2][MONTH_HEADERS.index("CalendarEventId")] = cell("synthetic-duplicate-event")
        sheet["rows"].append(orphan)
        diagnostics = self.observe()["diagnostic_counts"]
        self.assertEqual(diagnostics["orphan_sync_id"], 1)
        self.assertEqual(diagnostics["duplicate_sync_id"], 2)
        self.assertEqual(diagnostics["duplicate_event_id"], 2)
        self.assertEqual(sum(row["uid"] == "b" * 32 for row in self.journal.latest()["rows"]), 2)
        sheet["rows"][0].append(cell("病歷號"))
        result = self.observe()
        self.assertEqual(result["diagnostic_counts"]["duplicate_header"], 1)
        self.assertTrue(result["source_read_complete"])
        self.assertFalse(result["calendar_confirmed"])

    def test_untrusted_headers_values_and_hospital_never_escape_safe_summaries(self):
        sheet = self.book["sheets"][0]
        sheet["rows"][0].append(cell(SECRET))
        sheet["rows"][2].append(cell(SECRET))
        self.observe()
        sheet["rows"][2][-1] = cell(SECRET + "-changed")
        result = self.observe()
        self.assertEqual(self.journal.recent()[0]["fields"], ["custom"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(self.journal.recent()))
        self.assertIn(SECRET, json.dumps(self.journal.latest()))

    def test_retention_bounds_observations_and_deltas_without_mutating_retained_versions(self):
        journal = SourceJournal(self.store, max_observations=3, max_changes=2)
        self.observe(journal=journal)
        cells = self.book["sheets"][0]["rows"][2]
        cells[MONTH_HEADERS.index("Plan")] = cell("synthetic-one")
        self.observe(journal=journal)
        with self.store.connect() as db:
            first = db.execute("SELECT sequence,before_json,after_json FROM source_changes").fetchone()
        cells[MONTH_HEADERS.index("Plan")] = cell("synthetic-two")
        self.observe(journal=journal)
        with self.store.connect() as db:
            retained = db.execute("SELECT sequence,before_json,after_json FROM source_changes WHERE sequence=?", (first[0],)).fetchone()
        self.assertEqual(retained, first)
        for index in range(3, 7):
            cells[MONTH_HEADERS.index("Plan")] = cell(f"synthetic-{index}")
            self.observe(journal=journal)
        self.assertEqual(self.count("source_observations"), 3)
        self.assertEqual(self.count("source_changes"), 2)
        self.assertEqual(len(journal.recent(200)), 2)
        self.assertEqual(self.count("source_snapshots"), 1)

    def test_source_reconfiguration_partitions_baselines_and_recent_history(self):
        first = self.observe()
        self.book["sheets"][0]["rows"][2][MONTH_HEADERS.index("Plan")] = cell("source-a-change")
        self.observe()
        self.assertEqual(len(self.journal.recent()), 1)
        self.configure("synthetic-source-b")
        self.assertIsNone(self.journal.latest())
        other = self.observe()
        self.assertTrue(other["baseline"])
        self.assertGreater(other["revision"], first["revision"])
        self.assertEqual(self.journal.recent(), [])
        self.configure("synthetic-source-a")
        self.assertEqual(len(self.journal.recent()), 1)

    def test_incomplete_input_does_not_replace_a_complete_observation(self):
        self.observe()
        before = self.journal.latest()
        for book in (None, {}, {"sheets": None}):
            with self.subTest(book=book), self.assertRaisesRegex(ValueError, "complete workbook"):
                self.journal.observe(book, "2026-11-10T19:00:00+08:00")
        self.assertEqual(self.journal.latest(), before)
        self.assertEqual(self.count("source_observations"), 1)

    def test_store_restart_retains_revision_and_private_before_after_history(self):
        self.observe()
        self.book["sheets"][0]["rows"][2][MONTH_HEADERS.index("Plan")] = cell(SECRET)
        changed = self.observe()
        snapshot, history = self.journal.latest(), self.journal.recent()
        self.store.close()
        self.store = MonitorStore(self.temp.name)
        self.addCleanup(self.store.close)
        self.journal = SourceJournal(self.store)
        self.assertEqual(self.journal.latest(), snapshot)
        self.assertEqual(self.journal.recent(), history)
        self.assertEqual(self.observe()["revision"], changed["revision"])
        with self.store.connect() as db:
            before, after = db.execute("SELECT before_json,after_json FROM source_changes").fetchone()
        self.assertNotIn(SECRET, before)
        self.assertIn(SECRET, after)
        self.assertNotIn(SECRET, json.dumps(history))

    def test_effective_only_formula_output_rows_are_not_dropped(self):
        sheet = self.book["sheets"][0]
        projected = [{} for _ in MONTH_HEADERS]
        projected[2] = {"effectiveValue": {"stringValue": SECRET}, "formattedValue": SECRET}
        projected[3] = {"effectiveValue": {"stringValue": SECRET}, "formattedValue": SECRET}
        sheet["rows"].append(projected)
        result = self.observe()
        self.assertEqual(result["row_count"], 6)
        self.assertEqual(result["diagnostic_counts"]["missing_sync_id"], 1)
        self.assertEqual(self.journal.latest()["rows"][-1]["kind"], "patient")
        self.assertNotIn(SECRET, json.dumps(result))

