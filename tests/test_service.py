import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from vghks_sheet2cal.calendar_monitor import MonitorStore
from vghks_sheet2cal.selftest_calendar import prepared
from vghks_sheet2cal.sheet_mutex import sheet_mutex


class ServiceTests(unittest.TestCase):
    def test_data_directory_lock_excludes_a_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(directory)
            try:
                result = subprocess.run([sys.executable, "-c",
                    "from vghks_sheet2cal.calendar_monitor import MonitorStore; import sys; MonitorStore(sys.argv[1])", directory],
                    capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("已有程式使用", result.stderr)
            finally:
                store.close()
            MonitorStore(directory).close()

    def test_sheet_lock_excludes_another_process_and_is_reentrant(self):
        process = subprocess.Popen([sys.executable, "-c",
            "from vghks_sheet2cal.sheet_mutex import sheet_mutex; import time\n"
            "with sheet_mutex('synthetic-lock-check'):\n print('locked', flush=True); time.sleep(20)"],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(TimeoutError), sheet_mutex("synthetic-lock-check", timeout=0.1):
                self.fail("Second process acquired the sheet lock")
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        with sheet_mutex("synthetic-lock-check", timeout=1), sheet_mutex("synthetic-lock-check", timeout=1):
            pass

    def test_archive_transaction_blocks_calendar_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                sheets.book["developerMetadata"].append({"metadataKey": "vghks.sheet.archive.lock",
                    "metadataId": 2147483646, "metadataValue": "synthetic-archive"})
                writes = len(sheets.writes)
                result = monitor.run_once()
                self.assertFalse(result["ok"])
                self.assertEqual(result["failure_code"], "archive_in_progress")
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (0, 0, 0))
                self.assertEqual(len(sheets.writes), writes)
            finally:
                monitor.close()

    def test_preflight_does_not_write_cloud_or_event_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = prepared(directory)
            try:
                writes = len(sheets.writes)
                config = monitor.store.get("config", "settings")
                result = monitor.inspect()
                self.assertTrue(result["ok"])
                self.assertEqual(len(sheets.writes), writes)
                self.assertEqual((calendar.inserts, calendar.updates, calendar.deletes), (0, 0, 0))
                self.assertEqual(monitor.store.all("event"), [])
                self.assertEqual(monitor.store.get("config", "settings"), config)
            finally:
                monitor.close()

    def test_service_health_and_sigterm_cleanup_without_google(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / ".runtime.json"
            process = subprocess.Popen([sys.executable, "-m", "vghks_sheet2cal", "--data-dir", directory, "serve", "--port", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 8
                while not runtime.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(runtime.exists())
                from urllib.parse import urlsplit
                address = urlsplit(json.loads(runtime.read_text())["url"])
                self.assertEqual(os.stat(runtime).st_mode & 0o777, 0o600)
                connection = http.client.HTTPConnection("127.0.0.1", address.port)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                health = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(health["alive"])
                self.assertEqual(health["state"], "setup_required")
                self.assertFalse(health["ready"])
                connection.request("GET", "/readyz")
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                connection.request("GET", "/api/status", headers={"Host": "untrusted.example"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertNotIn(address.fragment, stdout + stderr)
                self.assertFalse(runtime.exists())
                MonitorStore(directory).close()
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=8)

    def test_runtime_has_no_hospital_sdk(self):
        self.assertFalse(any(name.startswith("vghks_sdk") for name in sys.modules))
