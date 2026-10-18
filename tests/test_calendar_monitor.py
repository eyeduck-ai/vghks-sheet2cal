import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_sheet2cal.calendar_monitor_server import MonitorServer
from vghks_sheet2cal.calendar_sync import headers, resource, scan
from vghks_sheet2cal.selftest_calendar import FU_HEADERS, cell, checks, monthly, prepared, row, workbook


class MonitorTests(unittest.TestCase):
    def test_unchanged_workbook_read_budget_is_constant_with_patients_and_months(self):
        from vghks_sheet2cal.selftest_calendar import MONTH_HEADERS
        for count, months in ((10, 1), (50, 12), (200, 24)):
            with self.subTest(patients=count, months=months), tempfile.TemporaryDirectory() as directory:
                tabs = []
                for index in range(months):
                    year, month = 2026 + (9 + index) // 12, (9 + index) % 12 + 1
                    tab = monthly(f"{year}{month:02d}", index + 1)
                    tab["rows"] = tab["rows"][:2]
                    tab["rows"][1] = row(MONTH_HEADERS, **{"日期／報到時間": f"{year}-{month:02d}-12", "醫院": "高榮",
                        "病歷號": "◆ 刀日", "ScheduleSyncId": f"{10000 + index:032x}"})
                    tabs.append(tab)
                tabs[0]["rows"].extend(row(MONTH_HEADERS, **{"醫院": "高榮", "病歷號": f"{index + 1:05d}",
                    "姓名": "合成測試", "ScheduleSyncId": f"{index + 1:032x}"}) for index in range(count))
                tabs[0]["properties"]["gridProperties"]["rowCount"] = count + 10
                monitor, sheets, calendar = prepared(directory, workbook(*tabs))
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    writes = len(sheets.writes)
                    with patch.object(sheets, "read_all", wraps=sheets.read_all) as reads:
                        self.assertTrue(monitor.run_once()["ok"])
                    self.assertEqual(reads.call_count, 2)
                    self.assertEqual(len(sheets.writes), writes)
                    self.assertEqual(calendar.inserts, count)
                    self.assertEqual(calendar.updates, 0)
                finally:
                    monitor.close()

    def test_pause_during_a_request_preserves_binding_and_resumes_same_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            insert = calendar.insert

            def paused_insert(eid, resource):
                result = insert(eid, resource)
                monitor.pause()
                return result

            try:
                with patch.object(calendar, "insert", side_effect=paused_insert):
                    result = monitor.run_once()
                self.assertFalse(result["ok"])
                self.assertEqual(result["failure_code"], "paused")
                self.assertFalse(monitor.public()["enabled"])
                self.assertEqual(calendar.inserts, 1)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 2)
                self.assertTrue(all(item["event_id"] for item in scan(sheets.book)["patients"]))
                self.assertTrue(monitor.public()["last_success"])
                self.assertEqual(monitor.public()["consecutive_failures"], 0)
            finally:
                monitor.close()

    def test_failure_breaks_consecutive_deletion_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                sheets.book["sheets"][0]["rows"].pop(2)
                self.assertTrue(monitor.run_once()["ok"])
                with patch.object(sheets, "read_all", side_effect=ValueError("synthetic failed scan")):
                    self.assertFalse(monitor.run_once()["ok"])
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 0)
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 1)
            finally:
                monitor.close()

    def test_storage_failure_never_leaks_busy_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _, _ = prepared(directory)
            try:
                with patch.object(monitor.store, "get", side_effect=RuntimeError("synthetic disk failure")):
                    with self.assertRaises(RuntimeError):
                        monitor.run_once()
                self.assertFalse(monitor.busy.locked())
                self.assertTrue(monitor.run_once()["ok"])
            finally:
                monitor.close()

    def test_patient_uid_changed_to_day_header_is_pending_and_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                sheet = sheets.book["sheets"][0]
                _, cols = headers(sheet)
                target = sheet["rows"][2]
                uid = copy.deepcopy(target[cols["ScheduleSyncId"]])
                target[:] = [{} for _ in target]
                for label, value in {"日期／報到時間": "2026-10-13", "醫院": "高榮", "病歷號": "◆ 刀日"}.items():
                    target[cols[label]] = cell(value)
                target[cols["ScheduleSyncId"]] = uid
                self.assertEqual(scan(sheets.book)["issues"], [])
                before = len(sheets.writes)
                for _ in range(3):
                    result = monitor.run_once()
                    self.assertFalse(result["ok"])
                    self.assertIn("變成刀日標題", result["message"])
                self.assertEqual(len(sheets.writes), before)
                self.assertEqual(calendar.deletes, 0)
                self.assertEqual(calendar.updates, 0)
                self.assertEqual(len(calendar.events), 2)
            finally:
                monitor.close()

    def test_empty_header_hospital_blocks_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][1][cols["醫院"]] = {}
                before = len(sheets.writes)
                self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(len(sheets.writes), before)
                self.assertEqual(calendar.updates, 0)
                self.assertEqual(calendar.deletes, 0)
            finally:
                monitor.close()

    def test_local_patient_binding_is_checked_before_hospital_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                _, cols = headers(sheets.book["sheets"][0])
                target = sheets.book["sheets"][0]["rows"][2]
                target[cols["病歷號"]] = cell("00999")
                target[cols["醫院"]] = cell("聯醫")
                before = len(sheets.writes)
                self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(len(sheets.writes), before)
                self.assertEqual(target[cols["醫院"]], cell("聯醫"))
                self.assertEqual(calendar.updates, 0)
            finally:
                monitor.close()

    def test_first_adoption_checks_shared_uid_patient_hash_without_changing_baseline(self):
        from vghks_sheet2cal.calendar_monitor import CalendarMonitor
        from vghks_sheet2cal.selftest_calendar import MemoryCalendar, MemorySheets
        from vghks_sheet2cal.sheet_sync_state import field_hash, plan_sync_state
        book = plan_sync_state(workbook(), {"b" * 32: {"kind": "surgery", "mrn_hash": field_hash("mrn", "00999")}})["book"]
        state = copy.deepcopy(book["sheets"][-1])
        with tempfile.TemporaryDirectory() as directory:
            sheets, calendar = MemorySheets(book), MemoryCalendar()
            monitor = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False)
            try:
                monitor.configure({"spreadsheet_id": "synthetic-spreadsheet-00000000000", "calendar_id": "synthetic-calendar"})
                with self.assertRaisesRegex(ValueError, "原病歷號綁定不符"):
                    monitor.prepare()
                self.assertEqual(sheets.writes, [])
                self.assertEqual(calendar.inserts, 0)
                self.assertEqual(sheets.book["sheets"][-1], state)
            finally:
                monitor.close()

    def test_monitor_credential_envelope_is_private_and_survives_relocation(self):
        import shutil

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from vghks_sheet2cal.calendar_monitor import CalendarMonitor
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        raw = {"type": "service_account", "private_key": pem, "client_email": "synthetic@example.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            monitor = CalendarMonitor(first, background=False)
            monitor.configure({"spreadsheet_id": "synthetic-spreadsheet-00000000000", "calendar_id": "synthetic-calendar", "key": raw})
            self.assertNotIn("private_key", json.dumps(monitor.public()))
            self.assertNotIn(pem.encode(), monitor.store.path.read_bytes())
            monitor.close()
            shutil.copytree(first, second)
            restored = CalendarMonitor(second, background=False)
            try:
                self.assertEqual(restored.store.key(), raw)
                self.assertFalse(restored.public()["enabled"])
            finally:
                restored.close()

    def test_offline_core_migration_moves_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(len(checks(directory)), 7)

    def test_uncertain_creation_reuses_stable_id_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            calendar.timeout_insert = True
            self.assertFalse(monitor.run_once()["ok"])
            self.assertEqual(calendar.inserts, 1)
            monitor.close()
            from vghks_sheet2cal.calendar_monitor import CalendarMonitor
            monitor = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 2)
                self.assertEqual(len(calendar.events), 2)
            finally:
                monitor.close()

    def test_uid_cannot_silently_change_patient(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                _, cols = headers(sheets.book["sheets"][0])
                sheets.book["sheets"][0]["rows"][2][cols["病歷號"]] = cell("00999")
                result = monitor.run_once()
                self.assertFalse(result["ok"])
                self.assertIn("病人已變更", result["message"])
                self.assertEqual(calendar.updates, 0)
                self.assertEqual(calendar.deletes, 0)
            finally:
                monitor.close()

    def test_visible_only_move_and_duplicate_ids_hold_events(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, calendar = prepared(directory)
                try:
                    self.assertTrue(monitor.run_once()["ok"])
                    source = sheets.book["sheets"][0]["rows"][2]
                    moved = copy.deepcopy(source)
                    if not duplicate:
                        moved[-2:] = [{}, {}]
                        source[:len(source) - 2] = [{} for _ in source[:-2]]
                    sheets.book["sheets"][0]["rows"].append(moved)
                    self.assertFalse(monitor.run_once()["ok"])
                    self.assertEqual(calendar.inserts, 2)
                    self.assertEqual(calendar.deletes, 0)
                finally:
                    monitor.close()

    def test_archive_ids_protect_existing_event(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                self.assertTrue(monitor.run_once()["ok"])
                moved = sheets.book["sheets"][0]["rows"].pop(2)
                archive_headers = [*monthly()["rows"][0], cell("ArchiveRowType")]
                sheets.book["sheets"].append({"properties": {"title": "刀表封存_2026", "sheetId": 3,
                    "gridProperties": {"rowCount": 50, "columnCount": len(archive_headers)}},
                    "rows": [[], [], [], [], [], archive_headers, [*moved, cell("PATIENT")]], "column_widths": []})
                for _ in range(3):
                    self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 0)
            finally:
                monitor.close()

    def test_fu_draft_has_no_event_cleared_date_retains_binding(self):
        fu = {"properties": {"title": "FU", "sheetId": 2, "gridProperties": {"rowCount": 50, "columnCount": len(FU_HEADERS)}},
              "rows": [list(map(cell, FU_HEADERS)), row(FU_HEADERS, **{"病歷號": "00003", "姓名": "追蹤甲", "醫院": "高榮", "Tag": "FU", "ScheduleSyncId": "f" * 32})], "column_widths": []}
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory, workbook(monthly(), fu))
            try:
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 2)
                target = sheets.book["sheets"][1]
                _, cols = headers(target)
                target["rows"][1][cols["日期"]] = cell("2026-11-01")
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 3)
                self.assertEqual(calendar.events[scan(sheets.book)["patients"][-1]["event_id"]]["start"], {"date": "2026-11-01"})
                target["rows"][1][cols["日期"]] = {}
                self.assertFalse(monitor.run_once()["ok"])
                self.assertEqual(calendar.deletes, 0)
            finally:
                monitor.close()

    def test_legacy_icaluid_is_adopted_without_duplicate_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                target = sheets.book["sheets"][0]
                _, cols = headers(target)
                target["rows"][2][cols["CalendarEventId"]] = cell("legacy@google.com")
                calendar.events["legacyid"] = {"id": "legacyid", "iCalUID": "legacy@google.com", "summary": "00001 | 合成甲 | Old"}
                self.assertTrue(monitor.run_once()["ok"])
                self.assertEqual(calendar.inserts, 1)
                self.assertEqual(scan(sheets.book)["patients"][0]["event_id"], "legacyid")
                self.assertTrue(monitor.run_once()["ok"])
            finally:
                monitor.close()

    def test_ga_and_cancelled_presentation(self):
        sheet = monthly()
        _, cols = headers(sheet)
        sheet["rows"][2][cols["GA"]] = cell("GA")
        item = scan(workbook(sheet))["patients"][0]
        event = resource(item, "synthetic")
        self.assertEqual(event["start"], {"date": "2026-10-12"})
        self.assertEqual(event["colorId"], "4")
        self.assertIn("原報到時間: 08:30", event["description"])
        item["cancelled"] = True
        self.assertEqual(resource(item, "synthetic")["colorId"], "8")

    def test_loopback_session_csrf_and_blank_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            from vghks_sheet2cal.calendar_monitor import CalendarMonitor
            monitor = CalendarMonitor(directory, background=False)
            with MonitorServer(0, monitor) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                try:
                    connection.request("GET", "/api/status")
                    self.assertEqual(connection.getresponse().status, 401)
                    connection.close()
                    connection.request("POST", "/api/session", json.dumps({"token": monitor.launch_token}), {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                    self.assertEqual(response.status, 200)
                    csrf = json.loads(response.read())["csrf"]
                    connection.request("GET", "/api/status", headers={"Cookie": cookie})
                    response = connection.getresponse()
                    state = json.loads(response.read())
                    self.assertEqual(state["spreadsheet_id"], "")
                    self.assertFalse(state["configured"])
                    self.assertNotIn("private_key", json.dumps(state))
                    connection.request("POST", "/api/remove-key", "{}", {"Content-Type": "application/json", "Cookie": cookie})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    connection.request("POST", "/api/remove-key", "{}", {"Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": csrf})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join()
                    monitor.close()
            self.assertFalse((Path(directory) / "clinical.sqlite3").exists())
