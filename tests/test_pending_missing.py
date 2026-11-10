"""An uncertain move to the past must not leave a deleted row's future event behind."""
import copy
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from vghks_sheet2cal.calendar_client import CalendarUncertain
from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_sync import calendar_day, headers, scan
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, MONTH_HEADERS, cell, prepared, row, workbook

TODAY = date(2026, 11, 10)
FUTURE = "2026-11-23"
PAST = "2026-11-09"
UID = "b" * 32


def future_schedule(directory, kind):
    labels = MONTH_HEADERS if kind == "MONTHLY" else FU_HEADERS
    rows = [list(map(cell, labels))]
    if kind == "MONTHLY":
        rows.append(row(labels, **{"日期／報到時間": FUTURE, "醫院": "高榮", "病歷號": "◆ 刀日",
                                  "ScheduleSyncId": "a" * 32}))
    values = {"病歷號": "00001", "姓名": "合成排程", "ScheduleSyncId": UID}
    values.update({"日期／報到時間": "09:30", "側別": "OD", "術式": "CATA"}
                  if kind == "MONTHLY" else {"日期": FUTURE, "醫院": "高榮", "Condition": "FU"})
    rows.append(row(labels, **values))
    book = workbook({"properties": {"title": "202611" if kind == "MONTHLY" else "FU", "sheetId": 1,
                                   "gridProperties": {"rowCount": 100, "columnCount": len(labels)}},
                     "rows": rows, "column_widths": [], "protectedRanges": []})
    monitor, sheets, calendar = prepared(directory, book)
    monitor.today = lambda: TODAY
    result = monitor.run_once()
    if not result["ok"]:
        monitor.close()
        raise AssertionError(result)
    patient = scan(sheets.book)["patients"][0]
    _, cols = headers(sheets.book["sheets"][0])
    date_cell = sheets.book["sheets"][0]["rows"][1]
    date_col = cols["日期／報到時間" if kind == "MONTHLY" else "日期"]
    return monitor, sheets, calendar, patient, date_cell, date_col


def restart(monitor, directory, sheets, calendar):
    host_id = monitor.host_id
    monitor.close()
    return CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                           today=lambda: TODAY, host_id=host_id)


def fail_reverse_move(monitor, calendar, date_cell, date_col, *, applied, legacy):
    date_cell[date_col] = cell(PAST)
    original_update = calendar.update

    def uncertain(event_id, expected):
        if applied:
            original_update(event_id, expected)
        raise CalendarUncertain("synthetic uncertain move to the past")

    with patch.object(calendar, "update", side_effect=uncertain):
        result = monitor.run_once()
    if result["ok"]:
        raise AssertionError("An uncertain Calendar update was reported as successful")
    pending = monitor.store.get("event", UID)
    if legacy:
        # v1.3.1 saved the unconfirmed target directly as resource, so its
        # past date cannot be trusted until the actual event is read again.
        pending["resource"] = pending.pop("desired_resource")
        pending.pop("confirmed_at", None)
        monitor.store.save("event", UID, pending)
    return pending


class PendingMissingTests(unittest.TestCase):
    def test_deleted_pending_row_reconciles_actual_scope_before_retirement(self):
        for kind in ("MONTHLY", "FU"):
            for applied in (False, True):
                for reboot in (False, True):
                    with self.subTest(kind=kind, applied=applied, restart=reboot), \
                            tempfile.TemporaryDirectory() as directory:
                        monitor, sheets, calendar, patient, target, col = future_schedule(directory, kind)
                        eid = patient["event_id"]
                        try:
                            pending = fail_reverse_move(monitor, calendar, target, col, applied=applied, legacy=False)
                            self.assertEqual(pending["status"], "pending")
                            self.assertEqual(calendar_day(pending["resource"]), date.fromisoformat(FUTURE))
                            self.assertEqual(calendar_day(pending["desired_resource"]), date.fromisoformat(PAST))
                            sheets.book["sheets"][0]["rows"].pop(patient["row"])
                            if reboot:
                                monitor = restart(monitor, directory, sheets, calendar)
                            before = copy.deepcopy(calendar.events[eid])
                            with patch.object(calendar, "get", wraps=calendar.get) as get:
                                first = monitor.run_once()
                            self.assertTrue(first["ok"], first)
                            self.assertIn(eid, [call.args[0] for call in get.call_args_list])
                            self.assertEqual(first["deleted"], 0)
                            self.assertEqual(calendar.events[eid], before)
                            self.assertEqual(calendar.updates, int(applied))
                            self.assertEqual(calendar.inserts, 1)
                            if applied:
                                saved = monitor.store.get("event", UID)
                                self.assertEqual(saved["status"], "historical")
                                self.assertTrue(saved["frozen_past"])
                                with patch.object(calendar, "get", wraps=calendar.get) as get:
                                    for _ in range(3):
                                        self.assertTrue(monitor.run_once()["ok"])
                                self.assertEqual(get.call_count, 0)
                                self.assertEqual(calendar.events[eid], before)
                                self.assertEqual(calendar.deletes, 0)
                            else:
                                self.assertEqual(monitor.store.get("event", UID)["missing"], 1)
                                second = monitor.run_once()
                                self.assertTrue(second["ok"], second)
                                self.assertEqual(second["deleted"], 1)
                                self.assertNotIn(eid, calendar.events)
                                self.assertEqual(calendar.deletes, 1)
                                self.assertEqual(monitor.store.get("event", UID)["status"], "deleted")
                        finally:
                            monitor.close()

    def test_legacy_unconfirmed_past_resource_is_read_after_restart_when_row_is_gone(self):
        for kind in ("MONTHLY", "FU"):
            for applied in (False, True):
                with self.subTest(kind=kind, applied=applied), tempfile.TemporaryDirectory() as directory:
                    monitor, sheets, calendar, patient, target, col = future_schedule(directory, kind)
                    eid = patient["event_id"]
                    try:
                        pending = fail_reverse_move(monitor, calendar, target, col, applied=applied, legacy=True)
                        self.assertNotIn("desired_resource", pending)
                        self.assertEqual(calendar_day(pending["resource"]), date.fromisoformat(PAST))
                        sheets.book["sheets"][0]["rows"].pop(patient["row"])
                        monitor = restart(monitor, directory, sheets, calendar)
                        actual_before = copy.deepcopy(calendar.events[eid])
                        with patch.object(calendar, "get", wraps=calendar.get) as get:
                            first = monitor.run_once()
                        self.assertTrue(first["ok"], first)
                        self.assertIn(eid, [call.args[0] for call in get.call_args_list])
                        self.assertEqual(calendar.events[eid], actual_before)
                        self.assertEqual(calendar.deletes, 0)
                        second = monitor.run_once()
                        self.assertTrue(second["ok"], second)
                        if applied:
                            self.assertIn(eid, calendar.events)
                            self.assertEqual(calendar.events[eid], actual_before)
                            self.assertTrue(monitor.store.get("event", UID)["frozen_past"])
                        else:
                            self.assertNotIn(eid, calendar.events)
                            self.assertEqual(calendar.deletes, 1)
                        self.assertEqual(calendar.updates, int(applied))
                        self.assertEqual(calendar.inserts, 1)
                    finally:
                        monitor.close()

    def test_pending_row_still_present_heals_same_event_after_restart(self):
        for kind in ("MONTHLY", "FU"):
            for applied in (False, True):
                for legacy in (False, True):
                    with self.subTest(kind=kind, applied=applied, legacy=legacy), \
                            tempfile.TemporaryDirectory() as directory:
                        monitor, sheets, calendar, patient, target, col = future_schedule(directory, kind)
                        eid = patient["event_id"]
                        try:
                            fail_reverse_move(monitor, calendar, target, col, applied=applied, legacy=legacy)
                            monitor = restart(monitor, directory, sheets, calendar)
                            result = monitor.run_once()
                            self.assertTrue(result["ok"], result)
                            self.assertEqual(set(calendar.events), {eid})
                            self.assertEqual(calendar_day(calendar.events[eid]), date.fromisoformat(PAST))
                            self.assertEqual(calendar.inserts, 1)
                            self.assertEqual(calendar.updates, 1)
                            saved = monitor.store.get("event", UID)
                            self.assertEqual(saved["status"], "ready")
                            self.assertEqual(saved["event_id"], eid)
                            self.assertNotIn("desired_resource", saved)
                            with patch.object(calendar, "get", wraps=calendar.get) as get:
                                self.assertTrue(monitor.run_once()["ok"])
                            self.assertEqual(get.call_count, 0)
                            self.assertEqual(calendar.deletes, 0)
                        finally:
                            monitor.close()
