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

from vghks_sheet2cal.calendar_client import CalendarError, CalendarUncertain
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

    def test_past_schedule_resumes_original_event_after_reschedule_and_restart(self):
        for day in ("2026-10-13", "2026-10-20"):
            for frozen in (False, True):
                with self.subTest(day=day, frozen=frozen), tempfile.TemporaryDirectory() as directory:
                    monitor, sheets, calendar = prepared(directory)
                    try:
                        self.assertTrue(monitor.run_once()["ok"])
                        patient = scan(sheets.book)["patients"][0]
                        uid, eid = patient["uid"], patient["event_id"]
                        before = copy.deepcopy(calendar.events)
                        if frozen:
                            old = monitor.store.get("event", uid)
                            monitor.store.save("event", uid, {**old, "status": "historical", "frozen_past": True})
                        monitor.close()
                        monitor = CalendarMonitor(directory, clients=lambda _, s=sheets, c=calendar: (s, c), background=False,
                                                  today=lambda: date(2026, 10, 13))
                        _, cols = headers(sheets.book["sheets"][0])
                        sheets.book["sheets"][0]["rows"][1][cols["日期／報到時間"]] = cell(day)
                        with patch.object(calendar, "get", wraps=calendar.get) as get:
                            result = monitor.run_once()
                        self.assertTrue(result["ok"], result)
                        self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 0))
                        self.assertEqual(set(calendar.events), set(before))
                        self.assertTrue(calendar.events[eid]["start"]["dateTime"].startswith(day))
                        other = next(key for key in before if key != eid)
                        self.assertEqual(calendar.events[other], before[other])
                        self.assertNotIn(other, [call.args[0] for call in get.call_args_list])
                        after = scan(sheets.book)["patients"][0]
                        self.assertEqual((after["uid"], after["event_id"]), (uid, eid))
                        self.assertEqual(len(sheets.book["sheets"][0]["rows"]), 5)
                        saved = monitor.store.get("event", uid)
                        self.assertEqual(saved["status"], "ready")
                        self.assertNotIn("frozen_past", saved)
                        self.assertTrue(monitor.run_once()["ok"])
                        self.assertEqual((calendar.inserts, calendar.updates), (2, 1))
                    finally:
                        monitor.close()

    def test_reschedule_uses_original_sheet_binding_without_local_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(Path(directory) / "original")
            other = None
            try:
                self.assertTrue(monitor.run_once()["ok"])
                patient = scan(sheets.book)["patients"][0]
                original_ids = set(calendar.events)
                other = CalendarMonitor(Path(directory) / "empty-cache", clients=lambda _: (sheets, calendar),
                                        background=False, today=lambda: date(2026, 10, 13))
                other.store.save("config", "settings", monitor.store.get("config", "settings"))
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][1][cols["日期／報到時間"]] = cell("2026-10-20")
                result = other.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual(set(calendar.events), original_ids)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 0))
                self.assertEqual(other.store.get("event", patient["uid"])["event_id"], patient["event_id"])
            finally:
                if other:
                    other.close()
                monitor.close()

    def test_resumed_historical_event_reenters_normal_future_deletion_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                patient = scan(sheets.book)["patients"][0]
                old = monitor.store.get("event", patient["uid"])
                monitor.store.save("event", patient["uid"], {**old, "status": "historical", "frozen_past": True})
                monitor.today = lambda: date(2026, 10, 13)
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][1][cols["日期／報到時間"]] = cell("2026-10-20")
                self.assertTrue(monitor.run_once()["ok"])
                sheets.book["sheets"][0]["rows"].pop(2)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertIn(patient["event_id"], calendar.events)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertNotIn(patient["event_id"], calendar.events)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 1))
            finally:
                monitor.close()

    def test_past_fu_resumes_same_all_day_event(self):
        fu = {"properties": {"title": "FU", "sheetId": 2,
              "gridProperties": {"rowCount": 20, "columnCount": len(FU_HEADERS)}},
              "rows": [list(map(cell, FU_HEADERS)), row(FU_HEADERS, **{
                  "日期": "2026-10-12", "病歷號": "00003", "姓名": "合成追蹤", "ScheduleSyncId": "e" * 32})],
              "column_widths": []}
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, workbook(monthly(), fu))
            try:
                self.assertTrue(monitor.run_once()["ok"])
                original = next(p for p in scan(sheets.book)["patients"] if p["kind"] == "FU")
                before = copy.deepcopy(calendar.events)
                monitor.today = lambda: date(2026, 10, 13)
                _, cols = headers(sheets.book["sheets"][1])
                sheets.book["sheets"][1]["rows"][1][cols["日期"]] = cell("2026-10-13")
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (3, 1, 0))
                self.assertEqual(set(calendar.events), set(before))
                self.assertEqual(calendar.events[original["event_id"]]["start"], {"date": "2026-10-13"})
                self.assertEqual(next(p for p in scan(sheets.book)["patients"] if p["kind"] == "FU")["uid"], original["uid"])
            finally:
                monitor.close()

    def test_active_schedule_updates_original_even_if_calendar_was_moved_to_past(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                eid = scan(sheets.book)["patients"][0]["event_id"]
                calendar.events[eid]["start"]["dateTime"] = "2026-09-30T08:30:00+08:00"
                calendar.events[eid]["end"]["dateTime"] = "2026-09-30T09:00:00+08:00"
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 0))
                self.assertTrue(calendar.events[eid]["start"]["dateTime"].startswith("2026-10-12"))
            finally:
                monitor.close()

    def test_removed_schedule_still_protects_actual_past_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                patient = scan(sheets.book)["patients"][0]
                eid = patient["event_id"]
                calendar.events[eid]["start"]["dateTime"] = "2026-09-30T08:30:00+08:00"
                calendar.events[eid]["end"]["dateTime"] = "2026-09-30T09:00:00+08:00"
                before = copy.deepcopy(calendar.events[eid])
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

    def test_rescheduled_historical_identity_is_revalidated_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                before = copy.deepcopy(calendar.events)
                monitor.today = lambda: date(2026, 10, 13)
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][2][cols["病歷號"]] = cell("00999")
                self.assertTrue(monitor.run_once()["ok"])
                sheets.book["sheets"][0]["rows"][1][cols["日期／報到時間"]] = cell("2026-10-20")
                with patch.object(calendar, "get", wraps=calendar.get) as get:
                    result = monitor.run_once()
                    self.assertFalse(result["ok"])
                    self.assertEqual(get.call_count, 0)
                self.assertEqual(calendar.events, before)
                self.assertEqual((calendar.updates, calendar.deletes), (0, 0))
            finally:
                monitor.close()

    def test_reschedule_uncertain_patch_recovers_same_event_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                patient = scan(sheets.book)["patients"][0]
                uid, eid = patient["uid"], patient["event_id"]
                monitor.today = lambda: date(2026, 10, 13)
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][1][cols["日期／報到時間"]] = cell("2026-10-20")
                update = calendar.update

                def uncertain(eid, body):
                    update(eid, body)
                    raise CalendarUncertain("synthetic uncertain reschedule")

                with patch.object(calendar, "update", side_effect=uncertain):
                    self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(monitor.store.get("event", uid)["status"], "pending")
                monitor.close()
                monitor = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                                          today=lambda: date(2026, 10, 13))
                result = monitor.run_once()
                self.assertTrue(result["ok"], result)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (2, 1, 0))
                self.assertEqual(monitor.store.get("event", uid)["event_id"], eid)
                self.assertEqual(monitor.store.get("event", uid)["status"], "ready")
                self.assertTrue(calendar.events[eid]["start"]["dateTime"].startswith("2026-10-20"))
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
