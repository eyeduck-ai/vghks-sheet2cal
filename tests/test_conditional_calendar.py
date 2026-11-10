"""Offline Calendar conditional mutations; stale versions are definite refusals."""
import copy
import unittest
from unittest.mock import Mock

from vghks_sheet2cal.calendar_client import CalendarClient, CalendarError, CalendarUncertain
from vghks_sheet2cal.google_http import GoogleHTTP


def response(status, value=None):
    return Mock(status_code=status, ok=status < 400, json=Mock(return_value=value or {}))


class ConditionalCalendarTests(unittest.TestCase):
    def client(self, status=200):
        session, delay = Mock(), Mock()
        session.request.return_value = response(status, {"id": "synthetic-event"})
        client = CalendarClient("synthetic-calendar", session)
        client.http = GoogleHTTP(session, sleep=delay, jitter=lambda: 0)
        return client, session, delay

    def test_patch_sends_exact_etag_without_notifications_and_preserves_input(self):
        for when in ({"date": "2026-11-23"}, {"dateTime": "2026-11-23T08:30:00+08:00", "timeZone": "Asia/Taipei"}):
            with self.subTest(when=when):
                client, session, _ = self.client()
                body = {"summary": "合成條件更新", "start": copy.deepcopy(when), "end": copy.deepcopy(when)}
                original = copy.deepcopy(body)
                client.update("synthetic/event", body, etag='"synthetic-version-1"')
                call = session.request.call_args
                self.assertEqual(call.args[0], "PATCH")
                self.assertTrue(call.args[1].endswith("/events/synthetic%2Fevent"))
                self.assertEqual(call.kwargs["headers"], {"If-Match": '"synthetic-version-1"'})
                self.assertEqual(call.kwargs["params"], {"sendUpdates": "none"})
                self.assertEqual(body, original)
                if "date" in when:
                    self.assertIsNone(call.kwargs["json"]["start"]["dateTime"])
                    self.assertIsNone(call.kwargs["json"]["start"]["timeZone"])
                else:
                    self.assertIsNone(call.kwargs["json"]["start"]["date"])
                session.request.assert_called_once()

    def test_delete_sends_exact_etag_without_notifications(self):
        client, session, _ = self.client(204)
        self.assertEqual(client.delete("synthetic/event", etag='"synthetic-version-2"'), {})
        call = session.request.call_args
        self.assertEqual(call.args[0], "DELETE")
        self.assertTrue(call.args[1].endswith("/events/synthetic%2Fevent"))
        self.assertEqual(call.kwargs["headers"], {"If-Match": '"synthetic-version-2"'})
        self.assertEqual(call.kwargs["params"], {"sendUpdates": "none"})
        self.assertNotIn("json", call.kwargs)
        session.request.assert_called_once()

    def test_precondition_failure_is_typed_definite_rejection_and_never_replayed(self):
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                client, session, delay = self.client(412)
                with self.assertRaises(CalendarError) as raised:
                    if operation == "update":
                        client.update("synthetic-event", {"summary": "合成更新"}, etag='"stale-version"')
                    else:
                        client.delete("synthetic-event", etag='"stale-version"')
                self.assertNotIsInstance(raised.exception, CalendarUncertain)
                self.assertEqual(raised.exception.code, "event_changed")
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(session.request.call_args.kwargs["headers"], {"If-Match": '"stale-version"'})
                self.assertEqual(session.request.call_args.kwargs["params"], {"sendUpdates": "none"})
                session.request.assert_called_once()
                delay.assert_not_called()
                self.assertEqual((client.http.requests, client.http.retries), (1, 0))

    def test_calls_without_etag_do_not_send_a_placeholder_if_match(self):
        client, session, _ = self.client()
        client.update("synthetic-event", {"summary": "合成更新"})
        self.assertNotIn("headers", session.request.call_args.kwargs)
        client.delete("synthetic-event")
        self.assertNotIn("headers", session.request.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
