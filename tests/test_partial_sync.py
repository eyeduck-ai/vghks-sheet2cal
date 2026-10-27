"""Synthetic regressions for mutable patient details and isolated row failures."""
import copy
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from vghks_sheet2cal.calendar_client import CalendarClient, CalendarError, CalendarUncertain
from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_monitor_server import health
from vghks_sheet2cal.calendar_sync import matches, resource, scan
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, MONTH_HEADERS, cell, prepared, row, workbook


def sample_book(kind="MONTHLY"):
    labels = MONTH_HEADERS if kind == "MONTHLY" else FU_HEADERS
    title = "202611" if kind == "MONTHLY" else "FU"
    rows = [list(map(cell, labels))]
    if kind == "MONTHLY":
        rows.append(row(labels, **{"日期／報到時間": "2026-11-11", "醫院": "聯醫",
                                  "病歷號": "◆ 刀日", "ScheduleSyncId": "a" * 32}))
    for index, (name, uid) in enumerate((("合成甲", "b"), ("合成乙", "d"), ("合成丙", "f")), 1):
        values = {"病歷號": f"{index:05}", "姓名": name, "ScheduleSyncId": uid * 32}
        values.update({"日期／報到時間": "08:30", "術式": "CATA", "側別": "OD"}
                      if kind == "MONTHLY" else {"日期": "2026-11-11", "醫院": "聯醫", "Condition": "FU"})
        rows.append(row(labels, **values))
    return workbook({"properties": {"title": title, "sheetId": 1,
                     "gridProperties": {"rowCount": 100, "columnCount": len(labels)}},
                     "rows": rows, "column_widths": [], "protectedRanges": []})


def source_row(sheets, uid):
    item = next(item for item in scan(sheets.book)["patients"] if item["uid"] == uid)
    sheet = next(sheet for sheet in sheets.book["sheets"]
                 if sheet["properties"]["sheetId"] == item["sheet_id"])
    return item, sheet["rows"][item["row"]], item["cols"]


def change(sheets, uid, **values):
    _, target, cols = source_row(sheets, uid)
    for label, value in values.items():
        target[cols[label]] = cell(value)


def restart(monitor, directory, sheets, calendar):
    host_id = monitor.host_id
    monitor.close()
    return CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                           today=lambda: date(2026, 10, 1), host_id=host_id)


class PartialSyncTests(unittest.TestCase):
    def test_blank_then_replacement_mrn_survives_campus_date_change_and_restart(self):
        for kind in ("MONTHLY", "FU"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, sample_book(kind))
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    uid = "b" * 32
                    item, target, cols = source_row(sheets, uid)
                    original_eid = item["event_id"]
                    # Existing v1.2.x stores have no explicit patient projection.
                    old = monitor.store.get("event", uid)
                    old.pop("patient", None)
                    monitor.store.save("event", uid, old)
                    target[cols["病歷號"]] = cell("")
                    if kind == "MONTHLY":
                        sheet = sheets.book["sheets"][0]
                        sheet["rows"][1][cols["日期／報到時間"]] = cell("2026-11-09")
                        sheet["rows"][1][cols["醫院"]] = cell("高榮")
                    else:
                        target[cols["日期"]] = cell("2026-11-09")
                        target[cols["醫院"]] = cell("高榮")
                    untouched = copy.deepcopy(target)
                    self.assertTrue(monitor.run_once()["ok"])
                    self.assertEqual(target, untouched)
                    item = source_row(sheets, uid)[0]
                    self.assertEqual(item["event_id"], original_eid)
                    self.assertEqual(item["mrn"], "")
                    self.assertTrue(matches(calendar.events[original_eid], resource(item, "synthetic-spreadsheet-00000000000")))
                    self.assertTrue(calendar.events[original_eid]["summary"].startswith(" | 合成甲 | "))
                    self.assertIn("醫院: 高榮", calendar.events[original_eid]["description"])
                    if kind == "MONTHLY":
                        self.assertEqual(target[cols["醫院"]], {})
                    monitor = restart(monitor, directory, sheets, calendar)
                    change(sheets, uid, **{"病歷號": "77777"})
                    before = copy.deepcopy(source_row(sheets, uid)[1])
                    self.assertTrue(monitor.run_once()["ok"])
                    item = source_row(sheets, uid)[0]
                    self.assertEqual(item["event_id"], original_eid)
                    self.assertEqual(source_row(sheets, uid)[1], before)
                    self.assertTrue(calendar.events[original_eid]["summary"].startswith("77777 | 合成甲 | "))
                    self.assertEqual(calendar.inserts, 3)
                    self.assertEqual(monitor.store.get("event", uid)["patient"], {"mrn": "77777", "name": "合成甲"})
                finally:
                    monitor.close()

    def test_missing_name_with_same_mrn_updates_existing_event(self):
        for kind in ("MONTHLY", "FU"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, sample_book(kind))
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    uid = "b" * 32
                    eid = source_row(sheets, uid)[0]["event_id"]
                    change(sheets, uid, **{"姓名": "", "Plan": "合成備註"})
                    self.assertTrue(monitor.run_once()["ok"])
                    self.assertTrue(calendar.events[eid]["summary"].startswith("00001 |  | "))
                    self.assertEqual(source_row(sheets, uid)[0]["name"], "")
                    self.assertEqual(calendar.inserts, 3)
                finally:
                    monitor.close()

    def test_first_or_middle_identity_problem_does_not_block_updates_or_new_binding(self):
        for bad_uid in ("b" * 32, "d" * 32):
            with self.subTest(bad_uid=bad_uid), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, sample_book())
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    bad_eid = source_row(sheets, bad_uid)[0]["event_id"]
                    old_event = copy.deepcopy(calendar.events[bad_eid])
                    old_state = monitor.store.get("event", bad_uid)
                    change(sheets, bad_uid, **{"病歷號": "99999", "姓名": "合成他人"})
                    for uid in ("b" * 32, "d" * 32, "f" * 32):
                        if uid != bad_uid:
                            change(sheets, uid, **{"Plan": "合成更新"})
                    sheets.book["sheets"][0]["rows"].append(row(MONTH_HEADERS, **{
                        "病歷號": "00004", "姓名": "合成丁", "日期／報到時間": "09:30"}))
                    with patch.object(calendar, "get", wraps=calendar.get) as get:
                        result = monitor.run_once()
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["partial"])
                    self.assertEqual(result["updated"], 3)
                    self.assertEqual(result["failed_rows"], 1)
                    self.assertEqual(result["row_issues"][0]["uid"], bad_uid)
                    self.assertNotIn(bad_eid, [call.args[0] for call in get.call_args_list])
                    self.assertEqual(calendar.events[bad_eid], old_event)
                    self.assertEqual(monitor.store.get("event", bad_uid), old_state)
                    self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (4, 2, 0))
                    patients = scan(sheets.book)["patients"]
                    new = next(item for item in patients if item["name"] == "合成丁")
                    self.assertTrue(new["uid"] and new["event_id"])
                    self.assertEqual(monitor.store.get("event", new["uid"])["status"], "ready")
                    for item in patients:
                        if item["uid"] != bad_uid:
                            self.assertTrue(matches(calendar.events[item["event_id"]], resource(item, "synthetic-spreadsheet-00000000000")))
                finally:
                    monitor.close()

    def test_invalid_time_on_first_or_middle_row_is_isolated(self):
        for bad_uid in ("b" * 32, "d" * 32):
            with self.subTest(bad_uid=bad_uid), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, sample_book())
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    eid = source_row(sheets, bad_uid)[0]["event_id"]
                    before = copy.deepcopy(calendar.events[eid])
                    for uid in ("b" * 32, "d" * 32, "f" * 32):
                        change(sheets, uid, **{"Plan": "合成更新"})
                    change(sheets, bad_uid, **{"日期／報到時間": "25:70"})
                    result = monitor.run_once()
                    self.assertTrue(result["partial"])
                    self.assertEqual((result["updated"], result["failed_rows"]), (2, 1))
                    self.assertEqual(calendar.events[eid], before)
                finally:
                    monitor.close()

    def test_partial_round_resets_deletion_confirmation_and_retains_success_time(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, sample_book())
            try:
                self.assertTrue(monitor.run_once()["ok"])
                deleted_uid = "f" * 32
                removed = source_row(sheets, deleted_uid)[0]
                sheets.book["sheets"][0]["rows"].pop(removed["row"])
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(monitor.store.get("event", deleted_uid)["missing"], 1)
                before_scan = monitor.store.get("scan", "last")
                before_success = monitor.store.get("config", "settings")["last_success"]
                change(sheets, "b" * 32, **{"病歷號": "99999", "姓名": "合成他人"})
                change(sheets, "d" * 32, **{"Plan": "合成更新"})
                for _ in range(2):
                    result = monitor.run_once()
                    self.assertTrue(result["partial"])
                    self.assertEqual(calendar.deletes, 0)
                    self.assertFalse(monitor.last_scan_ok)
                    self.assertEqual(monitor.store.get("event", deleted_uid)["missing"], 0)
                    self.assertEqual(monitor.store.get("scan", "last"), before_scan)
                    self.assertEqual(monitor.store.get("config", "settings")["last_success"], before_success)
                monitor = restart(monitor, directory, sheets, calendar)
                change(sheets, "b" * 32, **{"病歷號": "00001", "姓名": "合成甲"})
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 0)
                self.assertEqual(monitor.store.get("event", deleted_uid)["missing"], 1)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 1)
                self.assertNotIn(removed["event_id"], calendar.events)
            finally:
                monitor.close()

    def test_event_data_rejection_is_local_but_transport_failure_stops_remaining_rows(self):
        for code in ("row_rejected", "transport", "permission", "rate_limit"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, sample_book())
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    first = source_row(sheets, "b" * 32)[0]
                    before = copy.deepcopy(calendar.events)
                    for uid in ("b" * 32, "d" * 32, "f" * 32):
                        change(sheets, uid, **{"Plan": "合成更新"})
                    update = calendar.update

                    def reject(eid, body, first=first, code=code, update=update):
                        if eid == first["event_id"]:
                            error = CalendarUncertain if code == "transport" else CalendarError
                            raise error("synthetic event failure", code=code, retryable=code in {"transport", "rate_limit"})
                        return update(eid, body)

                    with patch.object(calendar, "update", side_effect=reject):
                        result = monitor.run_once()
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["partial"], code == "row_rejected")
                    self.assertEqual(result["updated"], 2 if code == "row_rejected" else 0)
                    self.assertEqual(calendar.events[first["event_id"]], before[first["event_id"]])
                    self.assertEqual(monitor.store.get("event", first["uid"])["status"], "pending")
                    if code != "row_rejected":
                        self.assertEqual(calendar.events, before)
                        self.assertEqual(result["failure_code"], code)
                finally:
                    monitor.close()

    def test_concurrent_change_before_binding_write_keeps_only_that_row_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, sample_book())
            try:
                insert = calendar.insert

                def mutate_first_row_after_last_insert(eid, body):
                    result = insert(eid, body)
                    if calendar.inserts == 3:
                        change(sheets, "b" * 32, **{"病歷號": "99999", "姓名": "合成他人"})
                    return result

                with patch.object(calendar, "insert", side_effect=mutate_first_row_after_last_insert):
                    result = monitor.run_once()
                self.assertTrue(result["partial"])
                self.assertEqual(result["failed_rows"], 1)
                self.assertEqual(calendar.inserts, 3)
                self.assertEqual(monitor.store.get("event", "b" * 32)["status"], "pending")
                self.assertEqual(source_row(sheets, "b" * 32)[0]["event_id"], "")
                for uid in ("d" * 32, "f" * 32):
                    state = monitor.store.get("event", uid)
                    self.assertEqual(state["status"], "ready")
                    self.assertEqual(source_row(sheets, uid)[0]["event_id"], state["event_id"])
                change(sheets, "b" * 32, **{"病歷號": "00001", "姓名": "合成甲"})
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 3)
                self.assertEqual(monitor.store.get("event", "b" * 32)["status"], "ready")
            finally:
                monitor.close()

    def test_inspect_reports_identity_and_invalid_fu_date_rows_without_writes(self):
        for kind in ("MONTHLY", "FU"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory, sample_book(kind))
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    change(sheets, "b" * 32, **{"病歷號": "99999", "姓名": "合成他人"})
                    if kind == "FU":
                        change(sheets, "d" * 32, **{"日期": "not-a-date"})
                    before_sheet = copy.deepcopy(sheets.book)
                    before_events = copy.deepcopy(calendar.events)
                    write_count = len(sheets.writes)
                    result = monitor.inspect()
                    self.assertFalse(result["ok"])
                    self.assertEqual({issue["uid"] for issue in result["row_issues"]},
                                     {"b" * 32, "d" * 32} if kind == "FU" else {"b" * 32})
                    self.assertEqual(sheets.book, before_sheet)
                    self.assertEqual(calendar.events, before_events)
                    self.assertEqual(len(sheets.writes), write_count)
                finally:
                    monitor.close()

    def test_invalid_fu_date_does_not_block_other_rows_or_delete_bound_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, sample_book("FU"))
            try:
                self.assertTrue(monitor.run_once()["ok"])
                bad = source_row(sheets, "b" * 32)[0]
                before = copy.deepcopy(calendar.events[bad["event_id"]])
                change(sheets, bad["uid"], **{"日期": "not-a-date"})
                change(sheets, "d" * 32, **{"Plan": "合成更新"})
                result = monitor.run_once()
                self.assertTrue(result["partial"])
                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["failed_rows"], 1)
                self.assertEqual(calendar.events[bad["event_id"]], before)
                self.assertEqual(calendar.deletes, 0)
            finally:
                monitor.close()

    def test_calendar_bad_request_is_distinguished_from_global_errors(self):
        client = CalendarClient("synthetic-calendar", None)
        for status, expected_code in ((400, "row_rejected"), (422, "row_rejected"),
                                      (401, "permission"), (403, "permission"),
                                      (429, "rate_limit"), (500, "transient")):
            with self.subTest(status=status):
                response = SimpleNamespace(status_code=status, ok=False, json=lambda: {})
                with patch.object(client.http, "request", return_value=response):
                    with self.assertRaises(CalendarError) as raised:
                        client.request("PATCH", "/events/synthetic", json={})
                self.assertEqual(raised.exception.code, expected_code)

    def test_partial_health_is_not_ready_despite_recent_success_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, _ = prepared(directory, sample_book())
            try:
                self.assertTrue(monitor.run_once()["ok"])
                monitor.configure({"enabled": True})
                change(sheets, "b" * 32, **{"病歷號": "99999", "姓名": "合成他人"})
                self.assertTrue(monitor.run_once()["partial"])
                with patch.object(monitor.store, "configured", return_value=True), \
                        patch.object(monitor.thread, "is_alive", return_value=True):
                    state = health(monitor)
                    self.assertTrue(state["alive"])
                    self.assertTrue(state["enabled"])
                    self.assertIsNotNone(state["last_success_age_seconds"])
                    self.assertFalse(state["ready"])
                    self.assertEqual(state["state"], "partial")
                    self.assertEqual(state["failed_rows"], 1)
                    self.assertNotIn("row_issues", state)
                    change(sheets, "b" * 32, **{"病歷號": "00001", "姓名": "合成甲"})
                    self.assertTrue(monitor.run_once()["ok"])
                    state = health(monitor)
                    self.assertTrue(state["ready"])
                    self.assertEqual(state["state"], "ready")
                    self.assertEqual(state["failed_rows"], 0)
            finally:
                monitor.close()

    def test_first_adoption_baseline_conflict_stays_local_even_for_owned_calendar_event(self):
        from vghks_sheet2cal.sheet_sync_state import field_hash, plan_sync_state

        uid = "b" * 32
        book = plan_sync_state(sample_book(), {uid: {
            "kind": "surgery", "mrn_hash": field_hash("mrn", "00999")}})["book"]
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, book)
            try:
                change(sheets, uid, **{"CalendarEventId": "synthetic-existing-event"})
                item = copy.deepcopy(source_row(sheets, uid)[0])
                item["mrn"] = "00999"
                event = {"id": "synthetic-existing-event", **resource(item, "synthetic-spreadsheet-00000000000")}
                calendar.events[event["id"]] = copy.deepcopy(event)
                shared_before = copy.deepcopy(sheets.book["sheets"][-1])
                for _ in range(2):
                    with patch.object(calendar, "get", wraps=calendar.get) as get:
                        result = monitor.run_once()
                    self.assertTrue(result["partial"])
                    self.assertEqual(result["failed_rows"], 1)
                    self.assertEqual(result["row_issues"][0]["uid"], uid)
                    self.assertIsNone(monitor.store.get("event", uid))
                    self.assertNotIn(event["id"], [call.args[0] for call in get.call_args_list])
                    self.assertEqual(calendar.events[event["id"]], event)
                    self.assertEqual(sheets.book["sheets"][-1], shared_before)
                self.assertEqual(calendar.inserts, 2)
                self.assertEqual(calendar.updates, 0)
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
