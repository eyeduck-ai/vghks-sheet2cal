import copy
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from vghks_sheet2cal.calendar_client import CalendarError, CalendarUncertain
from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_sync import calendar_day, headers, scan
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, cell, monthly, prepared, row, workbook


class PastRescheduleTests(unittest.TestCase):
    def future_schedule(self, directory, kind="MONTHLY", day="2026-10-20"):
        fu = {"properties": {"title": "FU", "sheetId": 2,
              "gridProperties": {"rowCount": 20, "columnCount": len(FU_HEADERS)}},
              "rows": [list(map(cell, FU_HEADERS)), row(FU_HEADERS, **{
                  "日期": day, "病歷號": "00003", "姓名": "合成追蹤", "ScheduleSyncId": "e" * 32})],
              "column_widths": []}
        book = workbook(monthly(), fu) if kind == "FU" else workbook()
        monitor, sheets, calendar = prepared(directory, book)
        monitor.today = lambda: date(2026, 10, 19)
        sheet = sheets.book["sheets"][1 if kind == "FU" else 0]
        _, cols = headers(sheet)
        index, label = (1, "日期") if kind == "FU" else (1, "日期／報到時間")
        sheet["rows"][index][cols[label]] = cell(day)
        try:
            self.assertTrue(monitor.run_once()["ok"])
            patient = next(p for p in scan(sheets.book)["patients"] if p["kind"] == kind and p["date"] == day)
            self.assertEqual(calendar.inserts, 1)
        except Exception:
            monitor.close()
            raise
        return monitor, sheets, calendar, patient, sheet["rows"][index], cols[label]

    def restart(self, monitor, directory, sheets, calendar):
        monitor.close()
        return CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                               today=lambda: date(2026, 10, 19))

    def assert_idle(self, monitor, calendar, expected):
        with patch.object(calendar, "get", wraps=calendar.get) as get:
            for _ in range(3):
                self.assertTrue(monitor.run_once()["ok"])
            self.assertEqual(get.call_count, 0)
        self.assertEqual(calendar.events, expected)
        self.assertEqual(calendar.deletes, 0)

    def test_today_and_future_moves_to_past_once_then_stays_idle_after_restart(self):
        for kind in ("MONTHLY", "FU"):
            for day in ("2026-10-19", "2026-10-20"):
                with self.subTest(kind=kind, day=day), tempfile.TemporaryDirectory() as directory:
                    monitor, sheets, calendar, patient, target, col = self.future_schedule(directory, kind, day)
                    try:
                        before_ids = set(calendar.events)
                        before_rows = [len(s["rows"]) for s in sheets.book["sheets"]]
                        target[col] = cell("2026-10-18")
                        self.assertEqual(monitor.inspect()["eligible_rows"], 1)
                        monitor = self.restart(monitor, directory, sheets, calendar)
                        result = monitor.run_once()
                        self.assertTrue(result["ok"], result)
                        self.assertEqual(result["updated"], 1)
                        self.assertEqual(calendar_day(calendar.events[patient["event_id"]]), date(2026, 10, 18))
                        self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (1, 1, 0))
                        self.assertEqual(set(calendar.events), before_ids)
                        current = next(p for p in scan(sheets.book)["patients"] if p["uid"] == patient["uid"])
                        self.assertEqual(current["event_id"], patient["event_id"])
                        self.assertEqual([len(s["rows"]) for s in sheets.book["sheets"]], before_rows)
                        self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "ready")
                        self.assertEqual(monitor.inspect()["eligible_rows"], 0)
                        monitor = self.restart(monitor, directory, sheets, calendar)
                        self.assert_idle(monitor, calendar, copy.deepcopy(calendar.events))
                        self.assertEqual(calendar.updates, 1)
                    finally:
                        monitor.close()

    def test_uncertain_patch_after_commit_is_verified_after_restart_without_second_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, patient, target, col = self.future_schedule(directory)
            try:
                target[col] = cell("2026-10-18")
                update = calendar.update

                def committed(eid, body):
                    update(eid, body)
                    raise CalendarUncertain("synthetic response lost after patch")

                with patch.object(calendar, "update", side_effect=committed):
                    self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "pending")
                monitor = self.restart(monitor, directory, sheets, calendar)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual((calendar.inserts, calendar.updates), (1, 1))
                self.assert_idle(monitor, calendar, copy.deepcopy(calendar.events))
            finally:
                monitor.close()

    def test_failed_patch_before_commit_retries_same_event_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, patient, target, col = self.future_schedule(directory)
            try:
                target[col] = cell("2026-10-18")
                with patch.object(calendar, "update", side_effect=CalendarUncertain("synthetic request lost")):
                    self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(calendar_day(calendar.events[patient["event_id"]]), date(2026, 10, 20))
                self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "pending")
                monitor = self.restart(monitor, directory, sheets, calendar)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar_day(calendar.events[patient["event_id"]]), date(2026, 10, 18))
                self.assertEqual((calendar.inserts, calendar.updates), (1, 1))
                self.assert_idle(monitor, calendar, copy.deepcopy(calendar.events))
            finally:
                monitor.close()

    def test_failed_readback_does_not_retire_unconfirmed_past_move(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, patient, target, col = self.future_schedule(directory)
            try:
                target[col] = cell("2026-10-18")
                update = calendar.update

                def readback_lost(eid, body):
                    result = update(eid, body)
                    def failed_get(_):
                        raise CalendarError("synthetic readback lost")

                    calendar.get = failed_get
                    return result

                original_get = calendar.get
                with patch.object(calendar, "update", side_effect=readback_lost):
                    self.assertFalse(monitor.run_once()["ok"])
                calendar.get = original_get
                self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "pending")
                monitor = self.restart(monitor, directory, sheets, calendar)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.updates, 1)
                self.assert_idle(monitor, calendar, copy.deepcopy(calendar.events))
            finally:
                monitor.close()

    def test_failed_binding_write_keeps_past_move_pending_until_restart_verifies_it(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, patient, target, col = self.future_schedule(directory)
            try:
                target[col] = cell("2026-10-18")
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][2][cols["CalendarEventId"]] = cell("")
                with patch.object(monitor, "_write_bindings", side_effect=ValueError("synthetic binding failure")):
                    self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "pending")
                self.assertEqual(calendar_day(calendar.events[patient["event_id"]]), date(2026, 10, 18))
                monitor = self.restart(monitor, directory, sheets, calendar)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(scan(sheets.book)["patients"][0]["event_id"], patient["event_id"])
                self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "ready")
                self.assertEqual((calendar.inserts, calendar.updates), (1, 1))
                self.assert_idle(monitor, calendar, copy.deepcopy(calendar.events))
            finally:
                monitor.close()

    def test_later_past_edits_are_skipped_but_moving_back_to_future_resumes_same_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, patient, target, col = self.future_schedule(directory)
            try:
                target[col] = cell("2026-10-18")
                self.assertTrue(monitor.run_once()["ok"])
                before = copy.deepcopy(calendar.events)
                target[col] = cell("2026-10-17")
                self.assert_idle(monitor, calendar, before)
                target[col] = cell("2026-10-21")
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar_day(calendar.events[patient["event_id"]]), date(2026, 10, 21))
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (1, 2, 0))
            finally:
                monitor.close()

    def test_original_missing_does_not_create_past_event_and_stops_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, patient, target, col = self.future_schedule(directory)
            try:
                calendar.events.pop(patient["event_id"])
                target[col] = cell("2026-10-18")
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (1, 0, 0))
                self.assertEqual(monitor.store.get("event", patient["uid"])["status"], "historical")
                self.assertEqual(scan(sheets.book)["patients"][0]["event_id"], patient["event_id"])
                monitor = self.restart(monitor, directory, sheets, calendar)
                self.assert_idle(monitor, calendar, {})
            finally:
                monitor.close()

    def test_natural_passage_into_past_does_not_query_or_update(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _, calendar, _, _, _ = self.future_schedule(directory, day="2026-10-19")
            try:
                monitor.today = lambda: date(2026, 10, 20)
                self.assert_idle(monitor, calendar, copy.deepcopy(calendar.events))
                self.assertEqual(calendar.updates, 0)
            finally:
                monitor.close()

    def test_reassigned_patient_is_rejected_before_reverse_move(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar, _, target, col = self.future_schedule(directory)
            try:
                before = copy.deepcopy(calendar.events)
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][2][cols["病歷號"]] = cell("00999")
                sheets.book["sheets"][0]["rows"][2][cols["姓名"]] = cell("另一位合成患者")
                target[col] = cell("2026-10-18")
                with patch.object(calendar, "get", wraps=calendar.get) as get:
                    result = monitor.run_once()
                    self.assertFalse(result["ok"])
                    self.assertEqual(get.call_count, 0)
                self.assertEqual(calendar.events, before)
            finally:
                monitor.close()

    def test_foreign_event_is_not_changed_by_reverse_move(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _, calendar, patient, target, col = self.future_schedule(directory)
            try:
                calendar.events[patient["event_id"]]["extendedProperties"]["private"]["vghksSyncId"] = "f" * 32
                before = copy.deepcopy(calendar.events)
                target[col] = cell("2026-10-18")
                self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(calendar.events, before)
                self.assertEqual(calendar.updates, 0)
            finally:
                monitor.close()
