"""Real monitor integration for synthetic Google Sheets native editing flows."""
import copy
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from vghks_sheet2cal.calendar_client import CalendarError
from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_sync import headers, matches, resource, scan
from vghks_sheet2cal.selftest_calendar import MONTH_HEADERS, cell, prepared, row, workbook

TODAY = date(2026, 11, 10)
SOURCE = "synthetic-spreadsheet-00000000000"


def native_book(*, ids=True):
    def identity(letter):
        return letter * 32 if ids else ""

    def patient(mrn, name, uid):
        cells = row(MONTH_HEADERS, **{"日期／報到時間": "08:15", "病歷號": mrn, "姓名": name,
                                    "術式": "CATA", "側別": "OD", "ScheduleSyncId": identity(uid)})
        cells[MONTH_HEADERS.index("Plan")]["note"] = "合成人工註記"
        return cells

    return workbook({"properties": {"title": "202611", "sheetId": 1, "gridProperties": {
        "rowCount": 100, "columnCount": len(MONTH_HEADERS)}}, "rows": [list(map(cell, MONTH_HEADERS)),
        row(MONTH_HEADERS, **{"日期／報到時間": "2026-11-10", "醫院": "高榮", "病歷號": "◆ 刀日",
                             "ScheduleSyncId": identity("a")}),
        patient("00001", "合成甲", "b"), patient("00002", "合成乙", "d"),
        row(MONTH_HEADERS, **{"日期／報到時間": "2026-11-23", "醫院": "聯醫", "病歷號": "◆ 刀日",
                             "ScheduleSyncId": identity("c")}),
        patient("00003", "合成丙", "f")], "column_widths": [], "protectedRanges": []})


def located(sheets, uid):
    found = [p for p in scan(sheets.book)["patients"] if p["uid"] == uid]
    if len(found) != 1:
        raise AssertionError("Test requires one uniquely identified synthetic source row")
    item = found[0]
    sheet = next(s for s in sheets.book["sheets"] if s["properties"]["sheetId"] == item["sheet_id"])
    return item, sheet["rows"][item["row"]]


def set_field(sheets, uid, label, value):
    item, cells = located(sheets, uid)
    cells[item["cols"][label]] = cell(value)


def clinical(book):
    result = []
    for sheet in book["sheets"]:
        _, cols = headers(sheet)
        excluded = {cols.get("ScheduleSyncId"), cols.get("CalendarEventId")}
        result.append([[copy.deepcopy(value) for index, value in enumerate(cells) if index not in excluded]
                       for cells in sheet["rows"]])
    return result


def fresh_manual_row(mrn="", name="合成新患者"):
    return row(MONTH_HEADERS, **{"日期／報到時間": "09:45", "病歷號": mrn, "姓名": name,
                                "術式": "CATA", "側別": "OS", "Plan": "合成手填"})


class NativeEditFlowTests(unittest.TestCase):
    def setup_monitor(self, directory, *, ids=True):
        monitor, sheets, calendar = prepared(directory, native_book(ids=ids))
        monitor.today = lambda: TODAY
        return monitor, sheets, calendar

    def assert_event(self, sheets, calendar, uid, eid):
        item = located(sheets, uid)[0]
        self.assertEqual(item["event_id"], eid)
        self.assertTrue(matches(calendar.events[eid], resource(item, SOURCE)))
        return item

    def assert_no_cell_writes(self, requests, blocked):
        for request in requests:
            if "updateCells" not in request:
                continue
            bounds = request["updateCells"]["range"]
            self.assertFalse(any((bounds["sheetId"], index) in blocked
                                 for index in range(bounds["startRowIndex"], bounds["endRowIndex"])))

    def test_manual_add_and_missing_mrn_get_ids_without_manual_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            original = native_book(ids=False)
            monitor, sheets, calendar = self.setup_monitor(directory, ids=False)
            try:
                self.assertEqual(clinical(sheets.book), clinical(original))
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 3)
                for item in scan(sheets.book)["patients"]:
                    self.assertRegex(item["uid"], r"^[a-f0-9]{32}$")
                    self.assertTrue(item["event_id"])
                    self.assertEqual(item["fields"]["醫院"], "")
                sheets.book["sheets"][0]["rows"].append(fresh_manual_row())
                before = clinical(sheets.book)
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["updated"], 1)
                new = next(p for p in scan(sheets.book)["patients"] if p["name"] == "合成新患者")
                self.assertEqual(new["mrn"], "")
                self.assertEqual(new["fields"]["醫院"], "")
                self.assertTrue(new["uid"] and new["event_id"])
                self.assertEqual(clinical(sheets.book), before)
                self.assertEqual(calendar.inserts, 4)
                self.assert_event(sheets, calendar, new["uid"], new["event_id"])
                self.assertEqual(monitor.run_once()["updated"], 0)
            finally:
                monitor.close()

    def test_full_row_drag_to_new_day_campus_with_blank_mrn_keeps_original_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                old, _ = located(sheets, "b" * 32)
                eid, uid = old["event_id"], old["uid"]
                sheet = sheets.book["sheets"][0]
                moved = sheet["rows"].pop(old["row"])
                moved[old["cols"]["病歷號"]] = {}
                _, cols = headers(sheet)
                destination = next(index for index, cells in enumerate(sheet["rows"])
                                   if len(cells) > cols["ScheduleSyncId"]
                                   and cells[cols["ScheduleSyncId"]] == cell("c" * 32))
                sheet["rows"].insert(destination + 1, moved)
                expected_source = copy.deepcopy(sheets.book)
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual((result["updated"], calendar.inserts, calendar.deletes), (1, 3, 0))
                item = self.assert_event(sheets, calendar, uid, eid)
                self.assertEqual((item["date"], item["hospital"], item["time"], item["mrn"]),
                                 ("2026-11-23", "聯醫", "08:15", ""))
                self.assertEqual(item["fields"]["醫院"], "")
                self.assertEqual(sheets.book, expected_source)
                host = monitor.host_id
                monitor.close()
                monitor = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                                          today=lambda: TODAY, host_id=host)
                set_field(sheets, uid, "病歷號", "88888")
                self.assertTrue(monitor.run_once()["ok"])
                self.assert_event(sheets, calendar, uid, eid)
                self.assertEqual(calendar.inserts, 3)
            finally:
                monitor.close()

    def test_full_delete_and_same_patient_retype_uses_new_binding_and_retires_old(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                old, _ = located(sheets, "b" * 32)
                sheet = sheets.book["sheets"][0]
                sheet["rows"].pop(old["row"])
                sheet["rows"].append(fresh_manual_row("00001", "合成甲"))
                set_field(sheets, "d" * 32, "Plan", "合成正常更新")
                before = clinical(sheets.book)
                first = monitor.run_once()
                self.assertTrue(first["ok"], first)
                self.assertEqual((first["updated"], first["deleted"]), (2, 0))
                new = next(p for p in scan(sheets.book)["patients"] if p["name"] == "合成甲")
                self.assertNotEqual(new["uid"], old["uid"])
                self.assertNotEqual(new["event_id"], old["event_id"])
                self.assertIn(old["event_id"], calendar.events)
                self.assertEqual(monitor.store.get("event", old["uid"])["missing"], 1)
                self.assertEqual(clinical(sheets.book), before)
                self.assertEqual(monitor.run_once()["deleted"], 1)
                self.assertNotIn(old["event_id"], calendar.events)
                self.assert_event(sheets, calendar, new["uid"], new["event_id"])
                self.assertEqual((calendar.inserts, calendar.deletes), (4, 1))
                self.assertEqual(monitor.run_once()["updated"], 0)
            finally:
                monitor.close()

    def test_visible_range_move_quarantines_orphan_and_all_unidentified_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                old, cells = located(sheets, "b" * 32)
                old_event = copy.deepcopy(calendar.events[old["event_id"]])
                moved = copy.deepcopy(cells)
                moved[old["cols"]["ScheduleSyncId"]] = {}
                moved[old["cols"]["CalendarEventId"]] = {}
                sheets.book["sheets"][0]["rows"][old["row"]] = row(MONTH_HEADERS, **{
                    "ScheduleSyncId": old["uid"], "CalendarEventId": old["event_id"]})
                sheets.book["sheets"][0]["rows"].extend([moved, fresh_manual_row("99999", "合成無關新增")])
                set_field(sheets, "d" * 32, "Plan", "合成健康更新")
                before, writes = copy.deepcopy(sheets.book), len(sheets.writes)
                first = monitor.run_once()
                self.assertTrue(first["partial"], first)
                self.assertEqual(first["updated"], 1)
                self.assertEqual(first["failed_rows"], 3)
                self.assertEqual((calendar.inserts, calendar.deletes), (3, 0))
                self.assertEqual(calendar.events[old["event_id"]], old_event)
                self.assertEqual(sheets.book, before)
                blocked = {(1, old["row"]), (1, len(before["sheets"][0]["rows"]) - 1),
                           (1, len(before["sheets"][0]["rows"]) - 2)}
                self.assert_no_cell_writes(sheets.writes[writes:], blocked)
                inspection = monitor.inspect()
                self.assertFalse(inspection["ok"])
                self.assertEqual(len(inspection["row_issues"]), 3)
                self.assertEqual(monitor.run_once()["updated"], 0)
                self.assertEqual((calendar.inserts, calendar.deletes), (3, 0))
            finally:
                monitor.close()

    def test_duplicate_rows_are_untouched_while_healthy_and_new_rows_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                old, cells = located(sheets, "b" * 32)
                before_event = copy.deepcopy(calendar.events[old["event_id"]])
                source_copy = copy.deepcopy(cells)
                sheet = sheets.book["sheets"][0]
                sheet["rows"].append(copy.deepcopy(cells))
                duplicated = len(sheet["rows"]) - 1
                sheet["rows"].append(fresh_manual_row("00004", "合成健康新增"))
                set_field(sheets, "d" * 32, "Plan", "合成健康更新")
                before_clinical, writes = clinical(sheets.book), len(sheets.writes)
                with patch.object(calendar, "get", wraps=calendar.get) as get:
                    result = monitor.run_once()
                self.assertTrue(result["partial"], result)
                self.assertEqual((result["updated"], result["failed_rows"]), (2, 2))
                self.assertNotIn(old["event_id"], [call.args[0] for call in get.call_args_list])
                self.assertEqual(calendar.events[old["event_id"]], before_event)
                self.assertEqual(sheet["rows"][old["row"]], source_copy)
                self.assertEqual(sheet["rows"][duplicated], source_copy)
                self.assertEqual(clinical(sheets.book), before_clinical)
                self.assert_no_cell_writes(sheets.writes[writes:], {(1, old["row"]), (1, duplicated)})
                new = next(p for p in scan(sheets.book)["patients"] if p["name"] == "合成健康新增")
                self.assertTrue(new["uid"] and new["event_id"])
                self.assert_event(sheets, calendar, new["uid"], new["event_id"])
                self.assertEqual((calendar.inserts, calendar.deletes), (4, 0))
            finally:
                monitor.close()

    def test_foreign_missing_event_blocks_all_retirements_after_healthy_update(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                first, _ = located(sheets, "b" * 32)
                later, _ = located(sheets, "f" * 32)
                for item in sorted((first, later), key=lambda item: item["row"], reverse=True):
                    sheets.book["sheets"][0]["rows"].pop(item["row"])
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(monitor.store.get("event", first["uid"])["missing"], 1)
                self.assertEqual(monitor.store.get("event", later["uid"])["missing"], 1)
                scan_before = monitor.store.get("scan", "last")
                calendar.events[later["event_id"]]["extendedProperties"]["private"]["vghksSyncId"] = "e" * 32
                foreign_before = copy.deepcopy(calendar.events[later["event_id"]])
                set_field(sheets, "d" * 32, "Plan", "合成健康更新")
                result = monitor.run_once()
                self.assertTrue(result["partial"], result)
                self.assertEqual(result["updated"], 1)
                self.assertEqual(calendar.deletes, 0)
                self.assertIn(first["event_id"], calendar.events)
                self.assertEqual(calendar.events[later["event_id"]], foreign_before)
                self.assertEqual(monitor.store.get("scan", "last"), scan_before)
                for item in (first, later):
                    self.assertEqual(monitor.store.get("event", item["uid"])["missing"], 0)
                calendar.events[later["event_id"]]["extendedProperties"]["private"]["vghksSyncId"] = later["uid"]
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 0)
                self.assertEqual(monitor.run_once()["deleted"], 2)
            finally:
                monitor.close()

    def test_source_edit_during_calendar_write_is_detected_and_reconciled_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                first, _ = located(sheets, "b" * 32)
                set_field(sheets, first["uid"], "Plan", "合成第一輪值")
                set_field(sheets, "d" * 32, "Plan", "合成健康更新")
                update = calendar.update

                def edit_after_update(eid, body):
                    result = update(eid, body)
                    if eid == first["event_id"]:
                        set_field(sheets, first["uid"], "Plan", "合成同步中第二輪值")
                    return result

                with patch.object(calendar, "update", side_effect=edit_after_update):
                    result = monitor.run_once()
                self.assertTrue(result["partial"], result)
                self.assertEqual(result["updated"], 2)
                self.assertTrue(any(issue["code"] == "source_changed" for issue in result["row_issues"]))
                self.assertIn("Plan: 合成第一輪值", calendar.events[first["event_id"]]["description"])
                self.assertEqual(located(sheets, first["uid"])[0]["fields"]["Plan"], "合成同步中第二輪值")
                self.assertEqual(monitor.run_once()["updated"], 1)
                self.assert_event(sheets, calendar, first["uid"], first["event_id"])
                self.assertEqual((calendar.inserts, calendar.deletes), (3, 0))
            finally:
                monitor.close()

    def test_injected_identity_request_cannot_write_a_quarantined_source_row(self):
        from vghks_sheet2cal.sheet_identity import plan_identity_setup

        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                old, cells = located(sheets, "b" * 32)
                sheets.book["sheets"][0]["rows"].append(copy.deepcopy(cells))
                sheets.book["sheets"][0]["rows"].append(fresh_manual_row("00004", "合成新增"))
                before, writes = copy.deepcopy(sheets.book), len(sheets.writes)

                def unsafe_plan(book, **kwargs):
                    plan = plan_identity_setup(book, **kwargs)
                    plan["requests"].append({"updateCells": {"range": {"sheetId": old["sheet_id"],
                        "startRowIndex": old["row"], "endRowIndex": old["row"] + 1,
                        "startColumnIndex": old["cols"]["ScheduleSyncId"],
                        "endColumnIndex": old["cols"]["ScheduleSyncId"] + 1},
                        "rows": [{"values": [cell("e" * 32)]}], "fields": "userEnteredValue"}})
                    return plan

                with patch("vghks_sheet2cal.sheet_identity.plan_identity_setup", side_effect=unsafe_plan):
                    result = monitor.run_once()
                self.assertFalse(result["ok"])
                self.assertIn("碰到待核對列", result["message"])
                self.assertEqual(sheets.book, before)
                self.assertEqual(len(sheets.writes), writes)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 0, 0))
            finally:
                monitor.close()

    def test_existing_bound_sheet_identity_header_loss_stops_before_cloud_operations(self):
        for label in ("ScheduleSyncId", "CalendarEventId"):
            for action in ("rename", "blank", "delete_column"):
                with self.subTest(label=label, action=action), tempfile.TemporaryDirectory() as directory:
                    monitor, sheets, calendar = self.setup_monitor(directory)
                    try:
                        self.assertTrue(monitor.run_once()["ok"])
                        sheet = sheets.book["sheets"][0]
                        _, cols = headers(sheet)
                        column = cols[label]
                        if action == "delete_column":
                            for cells in sheet["rows"]:
                                if len(cells) > column:
                                    cells.pop(column)
                            sheet["properties"]["gridProperties"]["columnCount"] -= 1
                        else:
                            sheet["rows"][0][column] = cell("其他標題" if action == "rename" else "")
                        before, writes = copy.deepcopy(sheets.book), len(sheets.writes)
                        events = copy.deepcopy(calendar.events)
                        with patch.object(calendar, "get", wraps=calendar.get) as get:
                            result = monitor.run_once()
                        self.assertFalse(result["ok"], result)
                        self.assertEqual(get.call_count, 0)
                        self.assertEqual(sheets.book, before)
                        self.assertEqual(len(sheets.writes), writes)
                        self.assertEqual(calendar.events, events)
                        self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 0, 0))
                    finally:
                        monitor.close()

    def test_new_month_without_technical_headers_is_prepared_without_rebinding_old_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                originals = {p["uid"]: p["event_id"] for p in scan(sheets.book)["patients"]}
                labels = MONTH_HEADERS[:-2]
                sheets.book["sheets"].append({"properties": {"title": "202612", "sheetId": 2,
                    "gridProperties": {"rowCount": 100, "columnCount": len(labels)}},
                    "rows": [list(map(cell, labels)), row(labels, **{
                        "日期／報到時間": "2026-12-01", "醫院": "高榮", "病歷號": "◆ 刀日"}),
                        row(labels, **{"日期／報到時間": "09:30", "病歷號": "00004", "姓名": "合成新月份",
                                       "術式": "CATA", "側別": "OS"})],
                    "column_widths": [], "protectedRanges": []})
                before = clinical(sheets.book)
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["updated"], 1)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (4, 0, 0))
                new = next(p for p in scan(sheets.book)["patients"] if p["name"] == "合成新月份")
                self.assertTrue(new["uid"] and new["event_id"])
                self.assertEqual(new["fields"]["醫院"], "")
                self.assertEqual(clinical(sheets.book), before)
                for uid, eid in originals.items():
                    self.assert_event(sheets, calendar, uid, eid)
            finally:
                monitor.close()

    def test_legacy_event_header_alias_is_recognized_on_existing_bound_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                original = {p["uid"]: p["event_id"] for p in scan(sheets.book)["patients"]}
                sheet = sheets.book["sheets"][0]
                _, cols = headers(sheet)
                sheet["rows"][0][cols["CalendarEventId"]] = cell("日曆apiEventID")
                set_field(sheets, "d" * 32, "Plan", "合成正常更新")
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["updated"], 1)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 1, 0))
                for uid, eid in original.items():
                    self.assert_event(sheets, calendar, uid, eid)
            finally:
                monitor.close()

    def test_event_becoming_past_or_foreign_after_retirement_preparation_is_not_deleted(self):
        for change_kind in ("past", "foreign_uid", "foreign_source"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = self.setup_monitor(directory)
                try:
                    # One active row produces the first GET, retirement planning
                    # the second, and the immediate pre-delete recheck the third.
                    absent, _ = located(sheets, "f" * 32)
                    sheets.book["sheets"][0]["rows"].pop(absent["row"])
                    self.assertTrue(monitor.run_once()["ok"])
                    removed, _ = located(sheets, "b" * 32)
                    sheets.book["sheets"][0]["rows"].pop(removed["row"])
                    self.assertTrue(monitor.run_once()["ok"])
                    self.assertEqual(monitor.store.get("event", removed["uid"])["missing"], 1)
                    get = calendar.get
                    calls = 0
                    raced_event = None

                    def change_before_delete(eid, removed=removed, calendar=calendar, change_kind=change_kind, get=get):
                        nonlocal calls, raced_event
                        calls += 1
                        if calls == 3:
                            self.assertEqual(eid, removed["event_id"])
                            actual = calendar.events[eid]
                            if change_kind == "past":
                                actual["start"], actual["end"] = {"date": "2026-11-09"}, {"date": "2026-11-10"}
                            else:
                                key = "vghksSyncId" if change_kind == "foreign_uid" else "vghksSource"
                                actual["extendedProperties"]["private"][key] = "e" * 32
                            raced_event = copy.deepcopy(actual)
                        return get(eid)

                    with patch.object(calendar, "get", side_effect=change_before_delete), \
                            patch.object(calendar, "delete", wraps=calendar.delete) as delete:
                        result = monitor.run_once()
                    self.assertEqual(calls, 3)
                    delete.assert_not_called()
                    self.assertEqual(calendar.deletes, 0)
                    self.assertEqual(calendar.events[removed["event_id"]], raced_event)
                    if change_kind == "past":
                        self.assertTrue(result["ok"], result)
                        self.assertTrue(monitor.store.get("event", removed["uid"])["frozen_past"])
                    else:
                        self.assertTrue(result["partial"], result)
                        self.assertEqual(monitor.store.get("event", removed["uid"])["missing"], 0)
                finally:
                    monitor.close()

    def test_conditional_patch_refusal_is_local_and_keeps_confirmed_calendar_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.setup_monitor(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                affected, _ = located(sheets, "b" * 32)
                actual_before = copy.deepcopy(calendar.events[affected["event_id"]])
                set_field(sheets, affected["uid"], "Plan", "合成待條件核對")
                set_field(sheets, "d" * 32, "Plan", "合成健康更新")
                update = calendar.update

                def reject_one_version(eid, body):
                    if eid == affected["event_id"]:
                        raise CalendarError("synthetic stale Calendar version", code="event_changed", retryable=True)
                    return update(eid, body)

                with patch.object(calendar, "update", side_effect=reject_one_version):
                    result = monitor.run_once()
                self.assertTrue(result["partial"], result)
                self.assertEqual(result["updated"], 1)
                self.assertEqual([issue["code"] for issue in result["row_issues"]], ["event_changed"])
                self.assertEqual(calendar.events[affected["event_id"]], actual_before)
                cached = monitor.store.get("event", affected["uid"])
                self.assertEqual(cached["status"], "pending")
                self.assertTrue(matches(cached["resource"], actual_before))
                self.assertNotIn("合成待條件核對", cached["resource"]["description"])
                self.assertIn("合成待條件核對", cached["desired_resource"]["description"])
                self.assertEqual(monitor.run_once()["updated"], 1)
                self.assert_event(sheets, calendar, affected["uid"], affected["event_id"])
                self.assertEqual((calendar.inserts, calendar.deletes), (3, 0))
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
