"""Synthetic-only conflict projection; the original source is never repaired."""
import copy
import json
import unittest

from vghks_sheet2cal.calendar_sync import headers, scan
from vghks_sheet2cal.selftest_calendar import MONTH_HEADERS, MemorySheets, cell, monthly, row, workbook
from vghks_sheet2cal.sheet_identity import plan_identity_setup
from vghks_sheet2cal.source_validation import project_source


def set_value(sheet, index, label, value):
    _, cols = headers(sheet)
    while len(sheet["rows"][index]) <= cols[label]:
        sheet["rows"][index].append({})
    sheet["rows"][index][cols[label]] = cell(value)


def append_patient(sheet, uid, mrn="00003", name="合成丙", eid=""):
    sheet["rows"].append(row(MONTH_HEADERS, **{
        "病歷號": mrn, "姓名": name, "ScheduleSyncId": uid, "CalendarEventId": eid}))
    return len(sheet["rows"]) - 1


class SourceValidationTests(unittest.TestCase):
    def assert_projection_plans(self, result):
        self.assertEqual(result["global_issues"], [])
        plan = plan_identity_setup(result["book"], include_archives=True)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(scan(plan["book"])["issues"], [])
        for request in plan["requests"]:
            if "updateCells" in request:
                bounds = request["updateCells"]["range"]
                self.assertFalse(any((bounds["sheetId"], index) in result["blocked_rows"]
                                     for index in range(bounds["startRowIndex"], bounds["endRowIndex"])))
        return plan

    def test_clean_source_and_unknown_tabs_are_unchanged(self):
        book = workbook()
        book["sheets"].append({"properties": {"title": "notes", "sheetId": 90},
                               "rows": [[cell("not a managed table")]]})
        before = copy.deepcopy(book)
        result = project_source(book)
        self.assertEqual(result["book"], before)
        self.assertIsNot(result["book"], book)
        self.assertEqual(result["blocked_rows"], set())
        self.assertEqual(result["row_issues"], [])
        self.assertEqual(result["active_uids"], {letter * 32 for letter in "abcd"})
        self.assert_projection_plans(result)
        self.assertEqual(book, before)

    def test_duplicate_uid_blocks_all_owners_and_keeps_healthy_physical_row(self):
        book = workbook()
        sheet = book["sheets"][0]
        set_value(sheet, 2, "CalendarEventId", "synthetic-first")
        set_value(sheet, 4, "ScheduleSyncId", "b" * 32)
        set_value(sheet, 4, "CalendarEventId", "synthetic-second")
        healthy = append_patient(sheet, "f" * 32)
        before = copy.deepcopy(book)
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, 2), (1, 4)})
        self.assertEqual(result["blocked_uids"], {"b" * 32})
        self.assertEqual(result["blocked_event_ids"], {"synthetic-first", "synthetic-second"})
        self.assertEqual(len(result["book"]["sheets"][0]["rows"]), len(sheet["rows"]))
        self.assertEqual(result["book"]["sheets"][0]["rows"][healthy], sheet["rows"][healthy])
        self.assertEqual([p["uid"] for p in scan(result["book"])["patients"]], ["f" * 32])
        self.assert_projection_plans(result)
        self.assertEqual(book, before)

    def test_duplicate_calendar_ids_do_not_choose_first_patient(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                book = workbook()
                sheet = book["sheets"][0]
                set_value(sheet, 2, "CalendarEventId", "synthetic-shared")
                set_value(sheet, 4, "CalendarEventId", "synthetic-shared")
                if reverse:
                    sheet["rows"][2], sheet["rows"][4] = sheet["rows"][4], sheet["rows"][2]
                result = project_source(book)
                self.assertEqual(result["blocked_rows"], {(1, 2), (1, 4)})
                self.assertEqual(result["blocked_uids"], {"b" * 32, "d" * 32})
                self.assertEqual(result["blocked_event_ids"], {"synthetic-shared"})
                self.assertEqual(scan(result["book"])["patients"], [])
                self.assert_projection_plans(result)

    def test_conflict_closure_connects_different_uid_and_event_groups(self):
        book = workbook()
        sheet = book["sheets"][0]
        set_value(sheet, 2, "CalendarEventId", "synthetic-one")
        set_value(sheet, 4, "ScheduleSyncId", "b" * 32)
        set_value(sheet, 4, "CalendarEventId", "synthetic-two")
        third = append_patient(sheet, "f" * 32, eid="synthetic-two")
        fourth = append_patient(sheet, "f" * 32, mrn="00004", name="合成丁", eid="synthetic-three")
        healthy = append_patient(sheet, "1" * 32, mrn="00005", name="合成戊")
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, index) for index in (2, 4, third, fourth)})
        self.assertEqual(result["blocked_event_ids"], {"synthetic-one", "synthetic-two", "synthetic-three"})
        self.assertEqual(scan(result["book"])["patients"][0]["row"], healthy)
        self.assert_projection_plans(result)

    def test_orphan_and_unidentified_destination_are_both_quarantined_without_guessing(self):
        book = workbook()
        sheet = book["sheets"][0]
        original = copy.deepcopy(sheet["rows"][2])
        _, cols = headers(sheet)
        sheet["rows"][2] = row(MONTH_HEADERS, **{"ScheduleSyncId": "b" * 32,
                                               "CalendarEventId": "synthetic-old"})
        original[cols["ScheduleSyncId"]] = {}
        original[cols["CalendarEventId"]] = {}
        sheet["rows"].append(original)
        destination = len(sheet["rows"]) - 1
        unrelated_new = append_patient(sheet, "", mrn="98765", name="另一位合成新患者")
        before = copy.deepcopy(book)
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, 2), (1, destination), (1, unrelated_new)})
        self.assertIn("b" * 32, result["active_uids"])
        self.assertIn("synthetic-old", result["blocked_event_ids"])
        self.assertEqual([p["uid"] for p in scan(result["book"])["patients"]], ["d" * 32])
        for issue in result["row_issues"]:
            if issue["row"] in (destination + 1, unrelated_new + 1):
                self.assertEqual(issue["code"], "ambiguous_move_candidate")
                self.assertEqual(issue["uid"], "")
        self.assert_projection_plans(result)
        self.assertEqual(book, before)

    def test_full_row_delete_and_retype_same_patient_remains_new_schedule(self):
        book = workbook()
        sheet = book["sheets"][0]
        old = sheet["rows"].pop(2)
        _, cols = headers(sheet)
        old[cols["ScheduleSyncId"]] = {}
        old[cols["CalendarEventId"]] = {}
        sheet["rows"].append(old)
        result = project_source(book, bound_patient_uids={"b" * 32})
        self.assertEqual(result["blocked_rows"], set())
        self.assertNotIn("b" * 32, result["active_uids"])
        plan = self.assert_projection_plans(result)
        new = next(p for p in scan(plan["book"])["patients"] if p["mrn"] == "00001")
        self.assertTrue(new["uid"])
        self.assertNotEqual(new["uid"], "b" * 32)

    def test_invalid_uid_is_quarantined_and_never_echoed_in_diagnostics(self):
        book = workbook()
        sheet = book["sheets"][0]
        bad_value = "合成敏感內容00001"
        set_value(sheet, 2, "ScheduleSyncId", bad_value)
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, 2)})
        self.assertIn(bad_value, result["blocked_uids"])
        self.assertIn(bad_value, result["active_uids"])
        self.assertEqual(result["row_issues"][0]["uid"], "")
        self.assertNotIn(bad_value, json.dumps(result["row_issues"], ensure_ascii=False))
        self.assert_projection_plans(result)

    def test_duplicate_with_archive_quarantines_both_and_preserves_archive_headers(self):
        book = workbook()
        patient = copy.deepcopy(book["sheets"][0]["rows"][2])
        archive = {"properties": {"title": "刀表封存_2026", "sheetId": 3, "gridProperties": {
                       "rowCount": 50, "columnCount": len(MONTH_HEADERS) + 1}},
                   "rows": [[], [], [], [], [], [*map(cell, MONTH_HEADERS), cell("ArchiveRowType")],
                            [*patient, cell("PATIENT")]], "column_widths": [], "protectedRanges": []}
        book["sheets"].append(archive)
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, 2), (3, 6)})
        self.assertEqual(result["book"]["sheets"][1]["rows"][:6], archive["rows"][:6])
        self.assertIn("b" * 32, result["active_uids"])
        self.assert_projection_plans(result)

    def test_bound_patient_uid_on_day_header_quarantines_complete_context_only(self):
        book = workbook()
        sheet = book["sheets"][0]
        set_value(sheet, 1, "ScheduleSyncId", "b" * 32)
        set_value(sheet, 2, "ScheduleSyncId", "e" * 32)
        result = project_source(book, bound_patient_uids={"b" * 32})
        self.assertEqual(result["blocked_rows"], {(1, 1), (1, 2)})
        self.assertIn("b" * 32, result["blocked_uids"])
        self.assertIn("e" * 32, result["blocked_uids"])
        self.assertEqual(scan(result["book"])["patients"][0]["uid"], "d" * 32)
        self.assert_projection_plans(result)

    def test_duplicate_day_header_ids_quarantine_all_affected_blocks(self):
        book = workbook()
        sheet = book["sheets"][0]
        set_value(sheet, 3, "ScheduleSyncId", "a" * 32)
        sheet["rows"].append(row(MONTH_HEADERS, **{"日期／報到時間": "2026-10-13", "醫院": "高榮",
                                                "病歷號": "◆ 刀日", "ScheduleSyncId": "c" * 32}))
        append_patient(sheet, "f" * 32)
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, index) for index in (1, 2, 3, 4)})
        self.assertEqual([p["uid"] for p in scan(result["book"])["patients"]], ["f" * 32])
        self.assert_projection_plans(result)

    def test_blank_template_rows_are_masked_but_not_counted_as_failed_schedules(self):
        book = workbook()
        sheet = book["sheets"][0]
        set_value(sheet, 1, "ScheduleSyncId", "b" * 32)
        set_value(sheet, 2, "ScheduleSyncId", "e" * 32)
        sheet["rows"][3:3] = [[], [], [], [], []]
        result = project_source(book, bound_patient_uids={"b" * 32})
        self.assertEqual(result["blocked_rows"], {(1, index) for index in range(1, 8)})
        self.assertEqual([issue["row"] for issue in result["row_issues"]], [2, 3])
        self.assertEqual(scan(result["book"])["patients"][0]["row"], 9)
        self.assert_projection_plans(result)

    def test_invalid_header_event_binding_is_local_but_real_bad_day_is_global(self):
        book = workbook()
        sheet = book["sheets"][0]
        set_value(sheet, 1, "CalendarEventId", "synthetic-patient-event")
        result = project_source(book)
        self.assertEqual(result["blocked_rows"], {(1, 1), (1, 2)})
        self.assert_projection_plans(result)
        set_value(sheet, 1, "醫院", "未識別院區")
        result = project_source(book)
        self.assertTrue(result["global_issues"])
        self.assertTrue(any("刀日結構不明" in message for message in result["global_issues"]))

    def test_bad_headers_and_patient_without_day_are_global(self):
        for case in ("duplicate", "missing", "no_day"):
            with self.subTest(case=case):
                book = workbook()
                sheet = book["sheets"][0]
                if case == "duplicate":
                    sheet["rows"][0].append(cell("病歷號"))
                elif case == "missing":
                    sheet["rows"][0][3] = cell("not a patient name header")
                else:
                    sheet["rows"].pop(1)
                self.assertTrue(project_source(book)["global_issues"])

    def test_identity_formulas_are_local_even_with_empty_display(self):
        for display in ("", "b" * 32):
            with self.subTest(display=display):
                book = workbook()
                sheet = book["sheets"][0]
                _, cols = headers(sheet)
                sheet["rows"][2][cols["ScheduleSyncId"]] = {
                    "userEnteredValue": {"formulaValue": '="synthetic"'}, "formattedValue": display}
                result = project_source(book)
                self.assertEqual(result["blocked_rows"], {(1, 2)})
                self.assertIn("formula_identity", result["row_issues"][0]["reasons"])
                self.assert_projection_plans(result)

    def test_blocked_manual_content_retains_technical_layout_and_no_row_writes(self):
        sheet = monthly(ids=False)
        sheet["rows"][0].extend([{}, {}, {}, {}, {}])
        sheet["properties"]["gridProperties"]["columnCount"] = 24
        # Add UID only; the worker will need to choose a new event-ID column.
        sheet["rows"][0].append(cell("ScheduleSyncId"))
        uid_column = len(sheet["rows"][0]) - 1
        for index in (1, 2, 3, 4):
            while len(sheet["rows"][index]) <= uid_column:
                sheet["rows"][index].append({})
            sheet["rows"][index][uid_column] = cell("a" * 32 if index == 1 else "c" * 32 if index == 3 else "b" * 32)
        sheet["rows"][2][19] = {"note": "合成人工註記，不可被新系統欄覆蓋"}
        book = workbook(sheet)
        before = copy.deepcopy(book)
        result = project_source(book)
        plan = self.assert_projection_plans(result)
        _, cols = headers(plan["book"]["sheets"][0])
        self.assertGreater(cols["CalendarEventId"], 19)
        self.assertEqual(result["blocked_rows"], {(1, 2), (1, 4)})
        client = MemorySheets(book)
        client.write(plan["requests"])
        self.assertEqual(client.book["sheets"][0]["rows"][2], before["sheets"][0]["rows"][2])
        self.assertEqual(book, before)


if __name__ == "__main__":
    unittest.main()
