"""Real HTTP/client wiring with synthetic responses and durable local budgets."""
import tempfile
import unittest
from unittest.mock import Mock, patch

from vghks_sheet2cal.api_usage import ApiUsageGuard
from vghks_sheet2cal.calendar_client import CalendarClient, CalendarError
from vghks_sheet2cal.calendar_monitor import CalendarMonitor, MonitorStore
from vghks_sheet2cal.google_http import GoogleHTTP, GoogleServiceError
from vghks_sheet2cal.google_sheets import SheetError, SheetsClient


class ApiUsageIntegrationTests(unittest.TestCase):
    def test_every_patch_retry_reserves_three_units_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(directory)
            try:
                guard = ApiUsageGuard(store, "synthetic-project")
                session = Mock()
                session.request.side_effect = [Mock(status_code=429), Mock(status_code=429), Mock(status_code=200)]
                http = GoogleHTTP(session, sleep=Mock(), jitter=lambda: 0)
                http.before_attempt = guard.before_attempt
                http.request("PATCH", "https://www.googleapis.com/calendar/v3/calendars/synthetic/events/one")
                self.assertEqual((http.requests, http.retries, http.quota_units), (3, 2, 9))
                self.assertEqual((http.read_requests, http.write_requests), (0, 3))
                self.assertEqual(guard.snapshot()["calendar"]["day_calls"], 3)
                self.assertEqual(guard.snapshot()["calendar"]["day_units"], 9)
                self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in session.request.call_args_list))
            finally:
                store.close()

    def test_budget_or_storage_rejection_sends_no_http_and_does_not_count_as_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(directory)
            try:
                guard = ApiUsageGuard(store, "synthetic-project", day_limits={"sheets": 1})
                session = Mock()
                session.request.return_value = Mock(status_code=200)
                http = GoogleHTTP(session)
                http.before_attempt = guard.before_attempt
                url = "https://sheets.googleapis.com/v4/spreadsheets/synthetic"
                http.request("GET", url)
                with self.assertRaises(GoogleServiceError) as failed:
                    http.request("GET", url)
                self.assertEqual(failed.exception.code, "usage_budget")
                with patch.object(store, "connect", side_effect=OSError("synthetic storage failure")), \
                        self.assertRaises(OSError):
                    http.request("GET", url)
                session.request.assert_called_once()
                self.assertEqual(http.requests, 1)
            finally:
                store.close()

    def test_minute_wait_can_be_cancelled_without_another_http_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(directory)
            try:
                cancelled = False

                def pause(_seconds):
                    nonlocal cancelled
                    cancelled = True

                def checkpoint():
                    if cancelled:
                        raise ValueError("synthetic pause")

                guard = ApiUsageGuard(store, "synthetic-project", minute_limits={"sheets": 1}, sleep=pause)
                session = Mock()
                session.request.return_value = Mock(status_code=200)
                http = GoogleHTTP(session)
                http.checkpoint = checkpoint
                http.before_attempt = lambda method, url: guard.before_attempt(method, url, checkpoint=http.checkpoint)
                url = "https://sheets.googleapis.com/v4/spreadsheets/synthetic"
                http.request("GET", url)
                with self.assertRaisesRegex(ValueError, "synthetic pause"):
                    http.request("GET", url)
                session.request.assert_called_once()
                self.assertEqual(http.requests, 1)
            finally:
                store.close()

    def test_production_client_sessions_share_guard_and_disable_hidden_401_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = CalendarMonitor(directory, background=False)
            key = {"project_id": "synthetic-project", "client_email": "synthetic@example.invalid"}
            try:
                def session_factory(_credentials, *, max_refresh_attempts):
                    self.assertEqual(max_refresh_attempts, 0)
                    session = Mock()
                    session.request.return_value = Mock(status_code=200, ok=True, json=Mock(return_value={}))
                    return session

                with patch.object(monitor.store, "key", return_value=key), \
                        patch("vghks_sheet2cal.calendar_monitor.service_account.Credentials.from_service_account_info"), \
                        patch("vghks_sheet2cal.calendar_monitor.AuthorizedSession", side_effect=session_factory):
                    with monitor.clients() as (sheets, calendar):
                        sheets.read_metadata()
                        calendar.request("GET", "/events")
                    first_guard = monitor.api_usage
                    with monitor.clients() as (sheets, _calendar):
                        sheets.read_metadata()
                    self.assertIs(monitor.api_usage, first_guard)
                    usage = monitor.public()["api_usage"]
                    self.assertEqual(usage["sheets"]["day_calls"], 2)
                    self.assertEqual(usage["calendar"]["day_calls"], 1)
                    self.assertNotIn("synthetic-project", str(usage))
            finally:
                monitor.close()

    def test_redirects_are_not_followed_or_interpreted_as_success(self):
        for client_type, error_type in ((SheetsClient, SheetError), (CalendarClient, CalendarError)):
            with self.subTest(client=client_type.__name__):
                session = Mock()
                session.request.return_value = Mock(status_code=302, ok=True, json=Mock(return_value={}))
                client = client_type("synthetic", session)
                with self.assertRaises(error_type):
                    client.request("GET", "")
                session.request.assert_called_once()
                self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_normal_google_account_domain_keeps_budget_when_project_field_is_added(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = CalendarMonitor(directory, background=False)
            try:
                email = "synthetic@synthetic-project.iam.gserviceaccount.com"
                first = monitor._usage_guard({"client_email": email})
                first.reserve("sheets", "GET")
                later = monitor._usage_guard({"client_email": email, "project_id": "synthetic-project"})
                self.assertIs(later, first)
                self.assertEqual(later.snapshot()["sheets"]["day_calls"], 1)
            finally:
                monitor.close()
