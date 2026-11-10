"""Five-minute source observations are read-only and independent of Calendar runs."""
import copy
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_monitor_server import health
from vghks_sheet2cal.selftest_calendar import MONTH_HEADERS, prepared, row


class ObservationLoopTests(unittest.TestCase):
    def enabled(self, directory):
        monitor, sheets, calendar = prepared(directory)
        monitor.configure({"enabled": True})
        config = monitor.store.get("config", "settings")
        config.update(next_check="2099-01-01T00:00:00+08:00", last_success="2026-11-10T12:00:00+08:00")
        monitor.store.save("config", "settings", config)
        return monitor, sheets, calendar

    def assert_calendar_not_called(self, stack, calendar):
        spies = [stack.enter_context(patch.object(calendar, name, wraps=getattr(calendar, name)))
                 for name in ("probe", "get", "insert", "update", "delete")]
        return spies

    def test_observation_records_new_unidentified_row_without_any_cloud_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.enabled(directory)
            try:
                before_revision = monitor.journal.latest()["revision"]
                sheets.book["sheets"][0]["rows"].append(row(MONTH_HEADERS, **{
                    "病歷號": "00003", "姓名": "合成新增", "Plan": "人工內容保留"}))
                before_book = copy.deepcopy(sheets.book)
                before_config = monitor.store.get("config", "settings")
                before_writes = len(sheets.writes)
                with ExitStack() as stack:
                    spies = self.assert_calendar_not_called(stack, calendar)
                    write = stack.enter_context(patch.object(sheets, "write", wraps=sheets.write))
                    read = stack.enter_context(patch.object(sheets, "read_all", wraps=sheets.read_all))
                    stack.enter_context(patch("vghks_sheet2cal.calendar_monitor.time.monotonic", return_value=1000))
                    result = monitor.observe_once()
                self.assertTrue(result["ok"], result)
                self.assertGreater(result["revision"], before_revision)
                self.assertEqual(result["added"], 1)
                self.assertFalse(result["calendar_confirmed"])
                self.assertEqual(monitor.next_observation, 1300)
                self.assertEqual(monitor.public()["observation_interval_seconds"], 300)
                read.assert_called_once()
                write.assert_not_called()
                for spy in spies:
                    spy.assert_not_called()
                self.assertEqual(sheets.book, before_book)
                self.assertEqual(len(sheets.writes), before_writes)
                self.assertEqual(monitor.store.get("config", "settings"), before_config)
                self.assertEqual(monitor.store.all("event"), [])
                cells = sheets.book["sheets"][0]["rows"][-1]
                self.assertFalse(cells[MONTH_HEADERS.index("ScheduleSyncId")])
                self.assertFalse(cells[MONTH_HEADERS.index("CalendarEventId")])
                self.assertEqual(monitor.public()["source_observation"]["revision"], result["revision"])
            finally:
                monitor.close()

    def test_paused_or_unprepared_observer_does_not_open_clients_or_read_sheet(self):
        for enabled, setup_done in ((False, True), (True, False)):
            with self.subTest(enabled=enabled, setup_done=setup_done), tempfile.TemporaryDirectory() as directory:
                monitor, sheets, _ = self.enabled(directory)
                try:
                    config = monitor.store.get("config", "settings")
                    config.update(enabled=enabled, setup_done=setup_done)
                    monitor.store.save("config", "settings", config)
                    before_revision = monitor.journal.latest()["revision"]
                    with patch.object(monitor, "clients", side_effect=AssertionError("Observer opened cloud clients")), \
                            patch.object(sheets, "read_all", wraps=sheets.read_all) as read:
                        result = monitor.observe_once()
                    self.assertEqual(result, {"ok": False, "paused": True})
                    read.assert_not_called()
                    self.assertEqual(monitor.journal.latest()["revision"], before_revision)
                    self.assertFalse(monitor.busy.locked())
                finally:
                    monitor.close()

    def test_observation_read_error_preserves_calendar_failure_and_retries_after_one_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.enabled(directory)
            try:
                config = monitor.store.get("config", "settings")
                config["consecutive_failures"] = 9
                monitor.store.save("config", "settings", config)
                monitor._status(message="合成 Calendar 綁定需核對", failure_code="conflict", partial=False,
                                updated=2, deleted=0)
                previous = {key: monitor.public().get(key) for key in (
                    "message", "failure_code", "partial", "last_success", "next_check", "consecutive_failures")}
                before_revision = monitor.journal.latest()["revision"]
                with ExitStack() as stack:
                    spies = self.assert_calendar_not_called(stack, calendar)
                    stack.enter_context(patch.object(sheets, "read_all", side_effect=ValueError("PRIVATE_GOOGLE_DETAIL")))
                    stack.enter_context(patch("vghks_sheet2cal.calendar_monitor.time.monotonic", return_value=1000))
                    result = monitor.observe_once()
                self.assertEqual(result, {"ok": False})
                self.assertEqual(monitor.next_observation, 1060)
                state = monitor.public()
                self.assertEqual({key: state.get(key) for key in previous}, previous)
                self.assertTrue(state["source_observation_error"])
                self.assertNotIn("PRIVATE_GOOGLE_DETAIL", str(state))
                self.assertEqual(monitor.journal.latest()["revision"], before_revision)
                with patch.object(monitor.store, "configured", return_value=True), \
                        patch.object(monitor.thread, "is_alive", return_value=True):
                    self.assertEqual(health(monitor)["state"], "blocked")
                for spy in spies:
                    spy.assert_not_called()
                self.assertFalse(monitor.busy.locked())
                # A later read success clears only the observation error.
                self.assertTrue(monitor.observe_once()["ok"])
                self.assertEqual(monitor.public()["source_observation_error"], "")
                self.assertEqual({key: monitor.public().get(key) for key in previous}, previous)
            finally:
                monitor.close()

    def test_hourly_due_cycle_precedes_source_observation_without_changing_its_schedule(self):
        for hourly_due in (True, False):
            with self.subTest(hourly_due=hourly_due), tempfile.TemporaryDirectory() as directory:
                monitor, _, _ = self.enabled(directory)
                try:
                    config = monitor.store.get("config", "settings")
                    config["next_check"] = "2026-11-10T11:00:00+08:00" if hourly_due else "2026-11-10T13:00:00+08:00"
                    monitor.store.save("config", "settings", config)
                    monitor.next_observation = 0
                    with patch.object(monitor.stop, "wait", side_effect=[False, True]), \
                            patch.object(monitor, "run_once") as hourly, \
                            patch.object(monitor, "observe_once") as observer, \
                            patch("vghks_sheet2cal.calendar_monitor.timestamp", return_value="2026-11-10T12:00:00+08:00"), \
                            patch("vghks_sheet2cal.calendar_monitor.time.monotonic", return_value=1000):
                        monitor._loop()
                    if hourly_due:
                        hourly.assert_called_once()
                        observer.assert_not_called()
                    else:
                        hourly.assert_not_called()
                        observer.assert_called_once()
                    self.assertEqual(monitor.store.get("config", "settings")["next_check"], config["next_check"])
                finally:
                    monitor.close()

    def test_source_observation_and_change_history_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, sheets, calendar = self.enabled(directory)
            try:
                sheets.book["sheets"][0]["rows"].append(row(MONTH_HEADERS, **{
                    "病歷號": "00003", "姓名": "合成新增"}))
                result = monitor.observe_once()
                self.assertTrue(result["ok"])
                saved_observation = monitor.public()["source_observation"]
                saved_changes = monitor.public()["source_changes"]
                host_id = monitor.host_id
                monitor.close()
                monitor = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                                          host_id=host_id)
                self.assertEqual(monitor.public()["source_observation"], saved_observation)
                self.assertEqual(monitor.public()["source_changes"], saved_changes)
                self.assertEqual(monitor.journal.latest()["revision"], saved_observation["revision"])
                repeated = monitor.observe_once()
                self.assertTrue(repeated["ok"])
                self.assertTrue(repeated["unchanged"])
                self.assertEqual(repeated["revision"], saved_observation["revision"])
                self.assertEqual(monitor.public()["source_changes"], saved_changes)
            finally:
                monitor.close()
