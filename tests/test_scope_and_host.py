import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from unittest.mock import patch

from vghks_sheet2cal.calendar_client import CalendarError
from vghks_sheet2cal.calendar_monitor import CalendarMonitor, metadata_owner
from vghks_sheet2cal.calendar_sync import OWNER_METADATA_ID, headers, scan
from vghks_sheet2cal.selftest_calendar import (
    FU_HEADERS,
    MemoryCalendar,
    MemorySheets,
    cell,
    monthly,
    prepared,
    row,
    workbook,
)


class ScopeTests(unittest.TestCase):
    def test_yesterday_today_tomorrow_and_undated_fu(self):
        fu = {"properties": {"title": "FU", "sheetId": 2,
              "gridProperties": {"rowCount": 20, "columnCount": len(FU_HEADERS)}},
              "rows": [list(map(cell, FU_HEADERS))], "column_widths": []}
        for index, day in enumerate(("2026-10-18", "2026-10-19", "2026-10-20", ""), 10):
            fu["rows"].append(row(FU_HEADERS, **{"日期": day, "病歷號": f"{index:05}",
                "姓名": "合成追蹤", "ScheduleSyncId": f"{index:032x}"}))
        with tempfile.TemporaryDirectory() as directory:
            monitor, _, calendar = prepared(directory, workbook(monthly(), fu))
            monitor.today = lambda: date(2026, 10, 19)
            try:
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual(calendar.inserts, 2)
                self.assertEqual({e["start"]["date"] for e in calendar.events.values()},
                                 {"2026-10-19", "2026-10-20"})
                self.assertEqual(result["past_skipped"], 3)
            finally:
                monitor.close()

    def test_past_events_survive_sheet_edits_and_row_removal_without_calendar_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                before = copy.deepcopy(calendar.events)
                monitor.today = lambda: date(2026, 10, 13)
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][2][cols["病歷號"]] = cell("00999")
                sheets.book["sheets"][0]["rows"].pop(4)
                with patch.object(calendar, "get", wraps=calendar.get) as get:
                    for _ in range(3):
                        self.assertTrue(monitor.run_once()["ok"])
                    self.assertEqual(get.call_count, 0)
                self.assertEqual(calendar.events, before)
                self.assertEqual((calendar.updates, calendar.deletes), (0, 0))
            finally:
                monitor.close()

    def test_past_event_is_not_moved_when_same_schedule_is_reused_for_future_date(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                before = copy.deepcopy(calendar.events)
                monitor.today = lambda: date(2026, 10, 13)
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][1][cols["日期／報到時間"]] = cell("2026-10-20")
                with patch.object(calendar, "get", wraps=calendar.get) as get:
                    result = monitor.run_once()
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(get.call_count, 0)
                self.assertEqual(result["protected_past"], 1)
                self.assertEqual(calendar.events, before)
            finally:
                monitor.close()

    def test_actual_event_moved_to_past_is_frozen_before_update_or_delete(self):
        for removed in (False, True):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory)
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    patient = scan(sheets.book)["patients"][0]
                    eid = patient["event_id"]
                    calendar.events[eid]["start"]["dateTime"] = "2026-09-30T08:30:00+08:00"
                    calendar.events[eid]["end"]["dateTime"] = "2026-09-30T09:00:00+08:00"
                    before = copy.deepcopy(calendar.events[eid])
                    if removed:
                        sheets.book["sheets"][0]["rows"].pop(2)
                    for _ in range(3):
                        self.assertTrue(monitor.run_once()["ok"])
                    self.assertEqual(calendar.events[eid], before)
                    self.assertTrue(monitor.store.get("event", patient["uid"])["frozen_past"])
                    self.assertEqual((calendar.updates, calendar.deletes), (0, 0))
                    with patch.object(calendar, "get", wraps=calendar.get) as get:
                        self.assertTrue(monitor.run_once()["ok"])
                        self.assertNotIn(eid, [call.args[0] for call in get.call_args_list])
                finally:
                    monitor.close()


class HostTests(unittest.TestCase):
    def test_another_instance_cannot_register_or_overwrite_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(Path(directory) / "first")
            other = CalendarMonitor(Path(directory) / "second", background=False,
                                    clients=lambda _: (sheets, calendar))
            try:
                other.configure({"spreadsheet_id": monitor.public()["spreadsheet_id"],
                                 "calendar_id": monitor.public()["calendar_id"]})
                before = copy.deepcopy(sheets.book["developerMetadata"])
                with self.assertRaises(CalendarError):
                    other.prepare()
                self.assertEqual(sheets.book["developerMetadata"], before)
                self.assertTrue(monitor.run_once()["ok"])
            finally:
                other.close()
                monitor.close()

    def test_copied_database_identity_on_another_machine_is_disabled_before_any_calendar_request(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(Path(directory) / "first")
            other = CalendarMonitor(Path(directory) / "copy", background=False, host_id="different-machine",
                                    clients=lambda _: (sheets, calendar))
            try:
                config = monitor.public()
                other.store.save("config", "settings", {**monitor.store.get("config", "settings"), "enabled": True})
                writes = len(sheets.writes)
                with patch.object(calendar, "get", wraps=calendar.get) as get:
                    result = other.run_once()
                    self.assertEqual(result["failure_code"], "host_conflict")
                    self.assertFalse(other.public()["enabled"])
                    self.assertEqual(get.call_count, 0)
                self.assertEqual(len(sheets.writes), writes)
                self.assertEqual(other.public()["worker_id"], config["worker_id"])
                self.assertEqual(calendar.inserts, 0)
            finally:
                other.close()
                monitor.close()

    def test_concurrent_first_registration_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            sheets, calendar = MemorySheets(workbook()), MemoryCalendar()
            monitors = [CalendarMonitor(Path(directory) / str(i), background=False, host_id=f"host-{i}",
                        clients=lambda _: (sheets, calendar)) for i in range(2)]
            for monitor in monitors:
                monitor.configure({"spreadsheet_id": "synthetic-spreadsheet-00000000000", "calendar_id": "synthetic-calendar"})
            barrier = threading.Barrier(2)
            write = sheets.write

            def simultaneous_claim(requests):
                if any("createDeveloperMetadata" in request for request in requests):
                    barrier.wait(timeout=5)
                return write(requests)

            def prepare(monitor):
                try:
                    monitor.prepare()
                    return True
                except CalendarError as error:
                    self.assertEqual(error.code, "host_conflict")
                    return False

            try:
                with patch("vghks_sheet2cal.sheet_mutex.sheet_mutex", return_value=nullcontext()), \
                        patch.object(sheets, "write", side_effect=simultaneous_claim), ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(prepare, monitors))
                self.assertEqual(sum(results), 1)
                metadata, _ = metadata_owner(sheets.book)
                self.assertEqual(metadata["metadataId"], OWNER_METADATA_ID)
                self.assertEqual(len(sheets.book["developerMetadata"]), 1)
            finally:
                for monitor in monitors:
                    monitor.close()

    def test_owner_change_after_read_is_caught_before_calendar_write(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            get = calendar.get

            def changed_owner(eid):
                metadata, owner = metadata_owner(sheets.book)
                owner["host_id"] = "another-machine"
                metadata["metadataValue"] = json.dumps(owner)
                return get(eid)

            try:
                with patch.object(calendar, "get", side_effect=changed_owner):
                    result = monitor.run_once()
                self.assertEqual(result["failure_code"], "host_conflict")
                self.assertFalse(monitor.public()["enabled"])
                self.assertEqual(calendar.inserts, 0)
            finally:
                monitor.close()
