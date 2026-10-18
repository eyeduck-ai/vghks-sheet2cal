"""Synthetic Google transport and patch behavior without live patient data."""
import unittest
from copy import deepcopy
from unittest.mock import Mock

import requests
from google.auth.exceptions import RefreshError

from vghks_sheet2cal.calendar_client import CalendarClient, CalendarError, CalendarUncertain
from vghks_sheet2cal.google_http import GoogleHTTP
from vghks_sheet2cal.google_sheets import SheetError, SheetsClient, SheetUncertain


def response(status, reason=""):
    return Mock(status_code=status, ok=status < 400,
                json=Mock(return_value={"error": {"errors": [{"reason": reason}]}} if reason else {}))


class GoogleHTTPTests(unittest.TestCase):
    def client(self, cls):
        session, sleep = Mock(), Mock()
        client = cls("synthetic", session)
        client.http = GoogleHTTP(session, sleep=sleep, jitter=lambda: 0)
        return client, session, sleep

    def test_calendar_403_rate_limit_retries_without_permission_error(self):
        client, session, sleep = self.client(CalendarClient)
        session.request.side_effect = [response(403, "rateLimitExceeded"), response(429), response(200)]
        self.assertEqual(client.request("GET", "/events"), {})
        self.assertEqual(client.http.requests, 3)
        self.assertEqual(client.http.retries, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.3, 0.6])

    def test_quota_exhaustion_and_real_permission_failure_stay_distinct(self):
        for cls, error in ((CalendarClient, CalendarError), (SheetsClient, SheetError)):
            with self.subTest(client=cls.__name__):
                client, session, _ = self.client(cls)
                session.request.return_value = response(403, "userRateLimitExceeded")
                with self.assertRaises(error) as failed:
                    client.request("GET", "")
                self.assertEqual(failed.exception.code, "rate_limit")
                self.assertTrue(failed.exception.retryable)
                self.assertNotIn("授權失敗", str(failed.exception))
                session.request.reset_mock()
                session.request.return_value = response(403, "forbidden")
                with self.assertRaises(error) as failed:
                    client.request("GET", "")
                self.assertEqual(failed.exception.code, "permission")
                session.request.assert_called_once()

    def test_mutation_retries_only_a_definite_quota_rejection(self):
        client, session, _ = self.client(SheetsClient)
        session.request.side_effect = [response(403, "rateLimitExceeded"), response(200)]
        client.write([])
        self.assertEqual(session.request.call_count, 2)
        session.request.reset_mock()
        session.request.side_effect = [response(503)]
        with self.assertRaises(SheetUncertain):
            client.write([])
        session.request.assert_called_once()

    def test_timeout_on_mutation_is_not_replayed_and_reads_are_bounded(self):
        client, session, _ = self.client(CalendarClient)
        session.request.side_effect = requests.Timeout()
        with self.assertRaises(CalendarUncertain):
            client.insert("stable-id", {})
        session.request.assert_called_once()
        session.request.reset_mock()
        with self.assertRaises(CalendarError):
            client.get("stable-id")
        self.assertEqual(session.request.call_count, 3)

    def test_refresh_permission_failure_is_not_reported_as_retryable_transport(self):
        client, session, _ = self.client(CalendarClient)
        session.request.side_effect = RefreshError("synthetic invalid credential")
        with self.assertRaises(CalendarError) as failed:
            client.get("stable-id")
        self.assertEqual(failed.exception.code, "permission")
        self.assertFalse(failed.exception.retryable)
        session.request.assert_called_once()

    def test_calendar_patch_converts_event_time_type_and_preserves_unrelated_fields(self):
        all_day = {"start": {"date": "2026-10-01"}, "end": {"date": "2026-10-02"}}
        timed = {"start": {"dateTime": "2026-10-01T08:00:00+08:00", "timeZone": "Asia/Taipei"},
                 "end": {"dateTime": "2026-10-01T08:30:00+08:00", "timeZone": "Asia/Taipei"}}

        def merge(target, patch):
            for key, value in patch.items():
                if value is None:
                    target.pop(key, None)
                elif isinstance(value, dict):
                    merge(target.setdefault(key, {}), value)
                else:
                    target[key] = deepcopy(value)

        for initial in (all_day, timed):
            for expected in (all_day, timed):
                with self.subTest(initial=list(initial["start"]), expected=list(expected["start"])):
                    client, session, _ = self.client(CalendarClient)
                    event = {**deepcopy(initial), "reminders": {"useDefault": False},
                             "extendedProperties": {"private": {"unrelated": "keep"}}}
                    request = {**deepcopy(expected), "summary": "synthetic",
                               "extendedProperties": {"private": {"vghksSyncId": "synthetic-id"}}}
                    original = deepcopy(request)

                    def patch(method, url, *, target=event, **kwargs):
                        self.assertEqual(method, "PATCH")
                        self.assertTrue(url.endswith("/events/stable-id"))
                        self.assertEqual(kwargs["params"], {"sendUpdates": "none"})
                        candidate = deepcopy(target)
                        merge(candidate, kwargs["json"])
                        for field in ("start", "end"):
                            if ("date" in candidate[field]) == ("dateTime" in candidate[field]):
                                return response(400, "invalid")
                        target.update(candidate)
                        return Mock(status_code=200, ok=True, json=Mock(return_value=deepcopy(target)))

                    session.request.side_effect = patch
                    result = client.update("stable-id", request)
                    self.assertEqual(result["start"], expected["start"])
                    self.assertEqual(result["end"], expected["end"])
                    self.assertEqual(result["reminders"], {"useDefault": False})
                    self.assertEqual(result["extendedProperties"]["private"],
                                     {"unrelated": "keep", "vghksSyncId": "synthetic-id"})
                    self.assertEqual(request, original)
                    session.request.assert_called_once()

    def test_pause_checkpoint_stops_a_pending_read_retry(self):
        client, session, _ = self.client(CalendarClient)
        session.request.return_value = response(503)
        client.http.checkpoint = Mock(side_effect=[None, CalendarError("paused", code="paused")])
        with self.assertRaises(CalendarError) as failed:
            client.get("stable-id")
        self.assertEqual(failed.exception.code, "paused")
        session.request.assert_called_once()

    def test_light_monitor_read_preserves_identity_and_cancellation_only(self):
        client, session, _ = self.client(SheetsClient)
        session.request.side_effect = [Mock(status_code=200, ok=True, json=Mock(return_value={"sheets": [
            {"properties": {"sheetId": 1, "title": "202610", "gridProperties": {"columnCount": 24, "rowCount": 10}},
             "conditionalFormats": [{}]}]})), response(200)]
        client.read_for_monitor()
        fields = dict(session.request.call_args.kwargs["params"])["fields"]
        self.assertIn("userEnteredValue", fields)
        self.assertIn("effectiveFormat(textFormat(strikethrough))", fields)
        self.assertNotIn("dataValidation", fields)
        self.assertIn("note", fields)  # Unnamed columns with notes must not be reused for IDs.
        self.assertIn("textFormatRuns", fields)

    def test_monitor_batches_multiple_tabs_and_restores_sparse_grid_offsets(self):
        client, session, _ = self.client(SheetsClient)
        meta = {"sheets": [{"properties": {"sheetId": sid, "title": f"2026{sid:02d}",
                  "gridProperties": {"columnCount": 24, "rowCount": 800}}} for sid in (1, 2)]}
        body = {"sheets": [{"properties": {"sheetId": sid}, "data": [
            {"rowData": [{"values": [{"userEnteredValue": {"stringValue": "header"}}]}],
             "columnMetadata": [{"hiddenByUser": True}]},
            {"startRow": 400, "rowData": [{"values": [{"note": "keep"}]}]}]} for sid in (2, 1)]}
        session.request.side_effect = [Mock(status_code=200, ok=True, json=Mock(return_value=value)) for value in (meta, body)]
        book = client.read_for_monitor()
        self.assertEqual(session.request.call_count, 2)  # Metadata + four grid ranges in one request.
        for sheet in book["sheets"]:
            self.assertEqual(sheet["rows"][400][0], {"note": "keep"})
            self.assertEqual(sheet["column_widths"], [{"hiddenByUser": True}])
        self.assertEqual(sum(key == "ranges" for key, _ in session.request.call_args.kwargs["params"]), 4)
