"""Offline request and race regressions for conditional technical writes.

The tiny executor models the emitted operations; it is not evidence about
Google's empty-cell search behavior.  That prerequisite is covered separately
by a real synthetic Google Sheet format+findReplace check.
"""
import copy
import re
import unittest

from vghks_sheet_core.identity import plan_identity_setup

from vghks_sheet2cal.calendar_sync import scan
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, MemorySheets, cell, monthly, row, workbook
from vghks_sheet2cal.technical_writes import TechnicalWriteError, guard_technical_writes


def value_request(col, value, row_index=2, sheet_id=1):
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index,
            "endRowIndex": row_index + 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
            "fields": "userEnteredValue"}}


def _java_literal(value):
    result, index = [], 0
    while index < len(value):
        if value[index] == "\\":
            index += 1
            if index >= len(value) or value[index] not in {"\\", "$"}:
                raise AssertionError("Unexpected nonliteral Java replacement")
        elif value[index] == "$":
            raise AssertionError("Unescaped Java replacement group")
        result.append(value[index])
        index += 1
    return "".join(result)


def execute(book, requests):
    """Apply emitted requests to synthetic values, skipping all formula cells."""
    sheets = MemorySheets(book)
    changed = 0
    for request in requests:
        kind, body = next(iter(request.items()))
        if kind not in {"repeatCell", "findReplace"}:
            sheets.write([request])
            continue
        bounds = body["range"]
        tab = next(s for s in sheets.book["sheets"] if s["properties"]["sheetId"] == bounds["sheetId"])
        row_index, col = bounds["startRowIndex"], bounds["startColumnIndex"]
        while len(tab["rows"]) <= row_index:
            tab["rows"].append([])
        while len(tab["rows"][row_index]) <= col:
            tab["rows"][row_index].append({})
        target = tab["rows"][row_index][col]
        if kind == "repeatCell":
            assert body["fields"] == "userEnteredFormat.numberFormat"
            target.setdefault("userEnteredFormat", {})["numberFormat"] = copy.deepcopy(
                body["cell"]["userEnteredFormat"]["numberFormat"])
            continue
        entered = target.get("userEnteredValue", {})
        if "formulaValue" in entered:
            continue
        if re.fullmatch(body["find"], entered.get("stringValue", "")):
            target["userEnteredValue"] = {"stringValue": _java_literal(body["replacement"])}
            changed += 1
    return sheets.book, changed


class TechnicalWriteTests(unittest.TestCase):
    def book(self):
        return workbook(monthly())

    def test_actual_core_initial_setup_and_second_noop_preserve_clinical_cells(self):
        book = workbook(monthly(ids=False))
        original = copy.deepcopy(book)
        plan = plan_identity_setup(book)
        self.assertFalse(plan["issues"])
        requests = guard_technical_writes(book, plan["requests"])
        self.assertEqual(book, original)
        self.assertNotIn("updateCells", {kind for request in requests for kind in request})
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 6)  # Two headers, two day IDs, two patient IDs.
        self.assertFalse(scan(updated)["issues"])
        self.assertEqual({p["uid"] for p in scan(updated)["patients"]},
                         {p["uid"] for p in scan(plan["book"])["patients"]})
        for before, after in zip(original["sheets"][0]["rows"], updated["sheets"][0]["rows"], strict=True):
            self.assertEqual(before, after[:16])
        second = plan_identity_setup(updated)
        self.assertFalse(second["issues"])
        self.assertEqual(guard_technical_writes(updated, second["requests"]), [])

    def test_missing_uid_condition_preserves_uid_inserted_after_snapshot(self):
        book = self.book()
        book["sheets"][0]["rows"][2][17] = {}
        requests = guard_technical_writes(book, [value_request(17, "e" * 32)])
        book["sheets"][0]["rows"][2][17] = cell("f" * 32)
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 0)
        self.assertEqual(updated["sheets"][0]["rows"][2][17]["userEnteredValue"], {"stringValue": "f" * 32})

    def test_actual_core_fu_and_archive_header_offsets_are_preserved(self):
        fu_labels = FU_HEADERS[:-2]
        archive_labels = ["ArchiveRowType", "病歷號", "姓名"]
        fu = {"properties": {"title": "FU", "sheetId": 2, "gridProperties": {
            "rowCount": 20, "columnCount": len(fu_labels)}}, "rows": [list(map(cell, fu_labels)),
            row(fu_labels, **{"病歷號": "synthetic-fu", "姓名": "Synthetic followup", "日期": "2026-11-23"})],
            "protectedRanges": [], "column_widths": []}
        archive = {"properties": {"title": "刀表封存_2025", "sheetId": 3, "gridProperties": {
            "rowCount": 20, "columnCount": len(archive_labels)}}, "rows": [[], [], [], [], [],
            list(map(cell, archive_labels)),
            row(archive_labels, **{"ArchiveRowType": "PATIENT", "病歷號": "synthetic-archive", "姓名": "Synthetic archive"})],
            "protectedRanges": [], "column_widths": []}
        book = workbook(fu, archive)
        plan = plan_identity_setup(book)
        self.assertFalse(plan["issues"])
        requests = guard_technical_writes(book, plan["requests"])
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 6)  # Two headers and one UID in each tab.
        self.assertFalse(scan(updated)["issues"])
        self.assertEqual(updated["sheets"][1]["rows"][:5], book["sheets"][1]["rows"][:5])
        self.assertEqual(updated["sheets"][1]["rows"][6][:3], book["sheets"][1]["rows"][6][:3])

    def test_whole_row_move_cannot_replace_existing_uid_at_old_address(self):
        book = self.book()
        book["sheets"][0]["rows"][4][17] = {}
        requests = guard_technical_writes(book, [value_request(17, "e" * 32, row_index=4)])
        rows = book["sheets"][0]["rows"]
        rows[2], rows[4] = rows[4], rows[2]
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 0)
        self.assertEqual(updated["sheets"][0]["rows"][4][17], cell("b" * 32) | {
            "userEnteredFormat": {"numberFormat": {"type": "TEXT"}}})
        self.assertFalse(updated["sheets"][0]["rows"][2][17])

    def test_blank_format_then_conditional_write_does_not_change_notes_or_other_format(self):
        book = self.book()
        target = book["sheets"][0]["rows"][2][16]
        target.update(note="synthetic technical note", userEnteredFormat={"textFormat": {"bold": True}})
        requests = guard_technical_writes(book, [value_request(16, "synthetic-event")])
        self.assertEqual([next(iter(r)) for r in requests], ["repeatCell", "findReplace"])
        self.assertEqual(requests[1]["findReplace"]["find"], r"^\s*$")
        self.assertFalse(requests[1]["findReplace"]["includeFormulas"])
        updated, changed = execute(book, requests)
        actual = updated["sheets"][0]["rows"][2][16]
        self.assertEqual(changed, 1)
        self.assertEqual(actual["note"], target["note"])
        self.assertTrue(actual["userEnteredFormat"]["textFormat"]["bold"])
        self.assertEqual(actual["userEnteredValue"], {"stringValue": "synthetic-event"})

    def test_exact_old_event_condition_and_replacement_are_literal_java_strings(self):
        book = self.book()
        old = r"legacy.$[1]\value@example.invalid"
        desired = r"canonical$2\path"
        book["sheets"][0]["rows"][2][16] = cell(old)
        requests = guard_technical_writes(book, [value_request(16, desired)])
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 1)
        self.assertEqual(updated["sheets"][0]["rows"][2][16]["userEnteredValue"], {"stringValue": desired})
        book["sheets"][0]["rows"][2][16] = cell("another-existing-event")
        raced, changed = execute(book, requests)
        self.assertEqual(changed, 0)
        self.assertEqual(raced["sheets"][0]["rows"][2][16]["userEnteredValue"],
                         {"stringValue": "another-existing-event"})

    def test_formula_added_after_snapshot_is_skipped_and_original_formula_is_refused(self):
        book = self.book()
        requests = guard_technical_writes(book, [value_request(16, "synthetic-event")])
        formula = {"userEnteredValue": {"formulaValue": '=IF(TRUE,"","")'}}
        book["sheets"][0]["rows"][2][16] = copy.deepcopy(formula)
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 0)
        self.assertEqual(updated["sheets"][0]["rows"][2][16]["userEnteredValue"], formula["userEnteredValue"])
        with self.assertRaises(TechnicalWriteError):
            guard_technical_writes(book, [value_request(16, "synthetic-event")])

    def test_legacy_event_header_is_conditionally_canonicalized(self):
        book = self.book()
        book["sheets"][0]["rows"][0][16] = cell("日曆eventID")
        plan = plan_identity_setup(book)
        requests = guard_technical_writes(book, plan["requests"])
        updated, _ = execute(book, requests)
        self.assertEqual(updated["sheets"][0]["rows"][0][16]["userEnteredValue"],
                         {"stringValue": "CalendarEventId"})

    def test_existing_uid_cannot_be_changed_even_by_an_unexpected_plan(self):
        book = self.book()
        with self.assertRaises(TechnicalWriteError):
            guard_technical_writes(book, [value_request(17, "e" * 32)])
        self.assertEqual(guard_technical_writes(book, [value_request(17, "b" * 32)]), [])

    def test_patient_or_shared_state_writes_and_unknown_coordinate_changes_fail_closed(self):
        for request in (
            value_request(2, "synthetic-clinical-change"),
            {"moveDimension": {"source": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": 16, "endIndex": 17},
                               "destinationIndex": 18}},
            {"appendDimension": {"sheetId": 1, "dimension": "ROWS", "length": 1}},
            {"updateDimensionProperties": {"range": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": 2,
                                                      "endIndex": 3},
                                           "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
        ):
            with self.subTest(request=next(iter(request))), self.assertRaises(TechnicalWriteError):
                guard_technical_writes(self.book(), [request])
        shared = self.book()
        shared["sheets"][0]["properties"]["title"] = "__VGHKS_SYNC_STATE"
        with self.assertRaises(TechnicalWriteError):
            guard_technical_writes(shared, [value_request(16, "synthetic-event")])

    def test_invalid_later_request_never_returns_a_partially_converted_batch(self):
        book = self.book()
        requests = [value_request(16, "synthetic-event"), value_request(3, "synthetic clinical name")]
        original = copy.deepcopy(requests)
        with self.assertRaises(TechnicalWriteError):
            guard_technical_writes(book, requests)
        self.assertEqual(requests, original)
        self.assertFalse(book["sheets"][0]["rows"][2][16])

    def test_known_limit_another_blank_cell_at_same_address_still_requires_readback(self):
        book = self.book()
        requests = guard_technical_writes(book, [value_request(16, "synthetic-event")])
        rows = book["sheets"][0]["rows"]
        rows[2], rows[4] = rows[4], rows[2]
        updated, changed = execute(book, requests)
        self.assertEqual(changed, 1)
        indexed = {p["uid"]: p["event_id"] for p in scan(updated)["patients"]}
        self.assertFalse(indexed["b" * 32])
        self.assertEqual(indexed["d" * 32], "synthetic-event")


if __name__ == "__main__":
    unittest.main()
