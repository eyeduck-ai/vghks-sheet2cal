"""Independent row deletion and new drafts must not halt unrelated schedules."""
import copy
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_sync import headers, matches, resource, scan
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, MONTH_HEADERS, cell, prepared, row, workbook

REMOVED_UID = "b" * 32
HEALTHY_UID = "d" * 32
TODAY = date(2026, 11, 10)


def tab(kind, sid=1, *, title=None, patients=True, day="2026-11-11"):
    labels = MONTH_HEADERS if kind == "MONTHLY" else FU_HEADERS
    title = title or ("202611" if kind == "MONTHLY" else "FU")
    rows = [list(map(cell, labels))]
    if kind == "MONTHLY":
        rows.append(row(labels, **{"日期／報到時間": day, "醫院": "高榮", "病歷號": "◆ 刀日",
                                  "ScheduleSyncId": ("a" if sid == 1 else "e") * 32}))
    if patients:
        for mrn, name, uid in (("00001", "合成原排程", REMOVED_UID), ("00002", "合成保留", HEALTHY_UID)):
            rows.append(patient_row(kind, mrn, name, uid=uid, day=day))
    return {"properties": {"title": title, "sheetId": sid,
                           "gridProperties": {"rowCount": 100, "columnCount": len(labels)}},
            "rows": rows, "column_widths": [], "protectedRanges": []}


def patient_row(kind, mrn="00003", name="合成新增", *, uid="", day="2026-11-11"):
    labels = MONTH_HEADERS if kind == "MONTHLY" else FU_HEADERS
    values = {"病歷號": mrn, "姓名": name, "ScheduleSyncId": uid, "Plan": "人工內容保留"}
    values.update({"日期／報到時間": "09:45", "側別": "OD", "術式": "CATA"}
                  if kind == "MONTHLY" else {"日期": day, "醫院": "高榮", "Condition": "合成追蹤"})
    cells = row(labels, **values)
    cells[labels.index("Plan")]["note"] = "人工註記保留"
    return cells


def locate(sheets, uid):
    item = next(item for item in scan(sheets.book)["patients"] if item["uid"] == uid)
    sheet = next(sheet for sheet in sheets.book["sheets"] if sheet["properties"]["sheetId"] == item["sheet_id"])
    return item, sheet, sheet["rows"][item["row"]]


def remove_and_edit(sheets):
    removed, sheet, _ = locate(sheets, REMOVED_UID)
    sheet["rows"].pop(removed["row"])
    healthy, _, cells = locate(sheets, HEALTHY_UID)
    cells[healthy["cols"]["Plan"]] = cell("合成正常更新")
    return removed


def clinical_projection(book):
    result = []
    for sheet in book["sheets"]:
        _, cols = headers(sheet)
        technical = {cols["ScheduleSyncId"], cols["CalendarEventId"]}
        result.append((sheet["properties"]["sheetId"], [
            [copy.deepcopy(value) for index, value in enumerate(cells) if index not in technical]
            for cells in sheet["rows"]]))
    return result


def restart(monitor, directory, sheets, calendar):
    host_id = monitor.host_id
    monitor.close()
    return CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                           today=lambda: TODAY, host_id=host_id)


class DeletedAndNewRowsTests(unittest.TestCase):
    def assert_preflight_read_only(self, monitor, sheets):
        before = copy.deepcopy(sheets.book)
        before_writes = len(sheets.writes)
        inspection = monitor.inspect()
        self.assertTrue(inspection["ok"], inspection)
        self.assertEqual(sheets.book, before)
        self.assertEqual(len(sheets.writes), before_writes)
        return inspection

    def test_same_patient_with_lost_ids_gets_distinct_binding_and_old_event_retires_after_two_scans(self):
        for kind in ("MONTHLY", "FU"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, workbook(tab(kind)))
                monitor.today = lambda: TODAY
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    removed, sheet, original = locate(sheets, REMOVED_UID)
                    moved = copy.deepcopy(original)
                    remove_and_edit(sheets)
                    moved[removed["cols"]["ScheduleSyncId"]] = {}
                    moved[removed["cols"]["CalendarEventId"]] = {}
                    if kind == "MONTHLY":
                        sheet["rows"].append(row(MONTH_HEADERS, **{
                            "日期／報到時間": "2026-11-23", "醫院": "聯醫", "病歷號": "◆ 刀日"}))
                    else:
                        moved[removed["cols"]["日期"]] = cell("2026-11-23")
                        moved[removed["cols"]["醫院"]] = cell("聯醫")
                    sheet["rows"].append(moved)
                    clinical = clinical_projection(sheets.book)
                    self.assert_preflight_read_only(monitor, sheets)
                    first = monitor.run_once()
                    self.assertTrue(first["ok"], first)
                    replacement = next(item for item in scan(sheets.book)["patients"]
                                       if item["name"] == removed["name"])
                    self.assertEqual(replacement["date"], "2026-11-23")
                    self.assertEqual(replacement["hospital"], "聯醫")
                    self.assertNotEqual(replacement["uid"], removed["uid"])
                    self.assertNotEqual(replacement["event_id"], removed["event_id"])
                    self.assertIn(removed["event_id"], calendar.events)
                    self.assertIn(replacement["event_id"], calendar.events)
                    self.assertTrue(monitor.run_once()["ok"])
                    self.assertNotIn(removed["event_id"], calendar.events)
                    self.assertIn(replacement["event_id"], calendar.events)
                    self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 1, 1))
                    self.assertEqual(clinical_projection(sheets.book), clinical)
                finally:
                    monitor.close()

    def test_independent_delete_and_new_row_sync_and_confirm_deletion_after_restart(self):
        for kind in ("MONTHLY", "FU"):
            for reboot in (False, True):
                with self.subTest(kind=kind, restart=reboot), tempfile.TemporaryDirectory() as directory:
                    monitor, sheets, calendar = prepared(directory, workbook(tab(kind)))
                    monitor.today = lambda: TODAY
                    try:
                        self.assertTrue(monitor.run_once()["ok"])
                        removed = remove_and_edit(sheets)
                        healthy_eid = locate(sheets, HEALTHY_UID)[0]["event_id"]
                        sheets.book["sheets"][0]["rows"].append(patient_row(kind))
                        clinical = clinical_projection(sheets.book)
                        inspection = self.assert_preflight_read_only(monitor, sheets)
                        self.assertGreater(inspection["planned_technical_requests"], 0)
                        result = monitor.run_once()
                        self.assertTrue(result["ok"], result)
                        self.assertEqual((result["updated"], result["deleted"]), (2, 0))
                        self.assertEqual(clinical_projection(sheets.book), clinical)
                        new = next(item for item in scan(sheets.book)["patients"] if item["name"] == "合成新增")
                        self.assertTrue(new["uid"] and new["event_id"])
                        self.assertNotIn(new["uid"], {REMOVED_UID, HEALTHY_UID})
                        self.assertNotIn(new["event_id"], {removed["event_id"], healthy_eid})
                        self.assertTrue(matches(calendar.events[new["event_id"]], resource(new, "synthetic-spreadsheet-00000000000")))
                        self.assertEqual(locate(sheets, HEALTHY_UID)[0]["event_id"], healthy_eid)
                        self.assertIn("Plan: 合成正常更新", calendar.events[healthy_eid]["description"])
                        self.assertIn(removed["event_id"], calendar.events)
                        self.assertEqual(monitor.store.get("event", REMOVED_UID)["missing"], 1)
                        if reboot:
                            monitor = restart(monitor, directory, sheets, calendar)
                            self.assertTrue(monitor.run_once()["ok"])
                            self.assertIn(removed["event_id"], calendar.events)
                            self.assertEqual(monitor.store.get("event", REMOVED_UID)["missing"], 1)
                        self.assert_preflight_read_only(monitor, sheets)
                        self.assertEqual(monitor.store.get("event", REMOVED_UID)["missing"], 1)
                        second = monitor.run_once()
                        self.assertTrue(second["ok"], second)
                        self.assertEqual(second["deleted"], 1)
                        self.assertNotIn(removed["event_id"], calendar.events)
                        for _ in range(2):
                            self.assertTrue(monitor.run_once()["ok"])
                        self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 1, 1))
                        self.assertEqual(clinical_projection(sheets.book), clinical)
                    finally:
                        monitor.close()

    def test_deleted_row_does_not_block_new_month_or_unrelated_undated_fu_draft(self):
        for kind, title, day in (("MONTHLY", "202612", "2026-12-11"), ("FU", "FU", "")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                target = tab(kind, 2, title=title, patients=False, day=day)
                monitor, sheets, calendar = prepared(directory, workbook(tab("MONTHLY"), target))
                monitor.today = lambda: TODAY
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    removed = remove_and_edit(sheets)
                    sheets.book["sheets"][1]["rows"].append(patient_row(kind, day=day))
                    clinical = clinical_projection(sheets.book)
                    self.assert_preflight_read_only(monitor, sheets)
                    first = monitor.run_once()
                    self.assertTrue(first["ok"], first)
                    self.assertEqual(first["updated"], 2 if kind == "MONTHLY" else 1)
                    new = next(item for item in scan(sheets.book)["patients"] if item["name"] == "合成新增")
                    self.assertTrue(new["uid"])
                    self.assertEqual(bool(new["event_id"]), kind == "MONTHLY")
                    self.assertIn(removed["event_id"], calendar.events)
                    self.assertTrue(monitor.run_once()["ok"])
                    self.assertNotIn(removed["event_id"], calendar.events)
                    self.assertEqual(calendar.inserts, 3 if kind == "MONTHLY" else 2)
                    self.assertEqual((calendar.updates, calendar.deletes), (1, 1))
                    self.assertEqual(clinical_projection(sheets.book), clinical)
                finally:
                    monitor.close()

    def test_new_fu_outside_selected_scope_gets_identity_without_calendar_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, workbook(
                tab("MONTHLY"), tab("FU", 2, patients=False)))
            monitor.today = lambda: TODAY
            monitor.configure({"include_fu": False})
            try:
                self.assertTrue(monitor.run_once()["ok"])
                removed = remove_and_edit(sheets)
                sheets.book["sheets"][1]["rows"].append(patient_row("FU"))
                clinical = clinical_projection(sheets.book)
                self.assert_preflight_read_only(monitor, sheets)
                first = monitor.run_once()
                self.assertTrue(first["ok"], first)
                self.assertEqual(first["updated"], 1)
                new = next(item for item in scan(sheets.book)["patients"] if item["kind"] == "FU")
                self.assertTrue(new["uid"])
                self.assertFalse(new["event_id"])
                self.assertIsNone(monitor.store.get("event", new["uid"]))
                self.assertIn(removed["event_id"], calendar.events)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertNotIn(removed["event_id"], calendar.events)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 1))
                self.assertEqual(clinical_projection(sheets.book), clinical)
            finally:
                monitor.close()

    def test_missing_past_event_is_preserved_while_healthy_and_new_future_rows_sync(self):
        for kind in ("MONTHLY", "FU"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, workbook(tab(kind, day="2026-11-09")))
                monitor.today = lambda: date(2026, 11, 8)
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    removed = remove_and_edit(sheets)
                    past_event = copy.deepcopy(calendar.events[removed["event_id"]])
                    healthy, sheet, cells = locate(sheets, HEALTHY_UID)
                    if kind == "MONTHLY":
                        sheet["rows"][1][healthy["cols"]["日期／報到時間"]] = cell("2026-11-11")
                    else:
                        cells[healthy["cols"]["日期"]] = cell("2026-11-11")
                    sheet["rows"].append(patient_row(kind))
                    monitor.today = lambda: TODAY
                    clinical = clinical_projection(sheets.book)
                    self.assert_preflight_read_only(monitor, sheets)
                    with patch.object(calendar, "get", wraps=calendar.get) as get:
                        for _ in range(3):
                            self.assertTrue(monitor.run_once()["ok"])
                    self.assertNotIn(removed["event_id"], [call.args[0] for call in get.call_args_list])
                    self.assertEqual(calendar.events[removed["event_id"]], past_event)
                    self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 1, 0))
                    self.assertEqual(clinical_projection(sheets.book), clinical)
                finally:
                    monitor.close()

    def test_orphan_or_duplicate_ids_hold_related_rows_while_healthy_row_updates(self):
        for kind in ("MONTHLY", "FU"):
            for conflict in ("orphan", "duplicate_uid", "duplicate_event"):
                with self.subTest(kind=kind, conflict=conflict), tempfile.TemporaryDirectory() as directory:
                    monitor, sheets, calendar = prepared(directory, workbook(tab(kind)))
                    monitor.today = lambda: TODAY
                    try:
                        self.assertTrue(monitor.run_once()["ok"])
                        original, sheet, source = locate(sheets, REMOVED_UID)
                        bad_event = copy.deepcopy(calendar.events[original["event_id"]])
                        copied = copy.deepcopy(source)
                        uid_col, eid_col = original["cols"]["ScheduleSyncId"], original["cols"]["CalendarEventId"]
                        if conflict == "orphan":
                            for index in range(len(source)):
                                if index not in {uid_col, eid_col}:
                                    source[index] = {}
                            copied[uid_col], copied[eid_col] = {}, {}
                        elif conflict == "duplicate_uid":
                            copied[eid_col] = {}
                        else:
                            copied[uid_col] = cell("f" * 32)
                        sheet["rows"].append(copied)
                        healthy, _, healthy_cells = locate(sheets, HEALTHY_UID)
                        healthy_cells[healthy["cols"]["Plan"]] = cell("合成正常更新")
                        clinical = clinical_projection(sheets.book)
                        with patch.object(calendar, "get", wraps=calendar.get) as get:
                            self.assertFalse(monitor.inspect()["ok"])
                            result = monitor.run_once()
                        self.assertFalse(result["ok"], result)
                        self.assertTrue(result["partial"], result)
                        self.assertTrue(result["row_issues"])
                        requested = [call.args[0] for call in get.call_args_list]
                        self.assertIn(healthy["event_id"], requested)
                        self.assertNotIn(original["event_id"], requested)
                        self.assertEqual(calendar.events[original["event_id"]], bad_event)
                        self.assertIn("Plan: 合成正常更新", calendar.events[healthy["event_id"]]["description"])
                        self.assertEqual(set(calendar.events), {original["event_id"], healthy["event_id"]})
                        self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 0))
                        self.assertEqual(clinical_projection(sheets.book), clinical)
                    finally:
                        monitor.close()
