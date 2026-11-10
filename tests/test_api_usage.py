"""Persistent API reservations survive new sessions, retries and process restarts."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from vghks_sheet2cal.api_usage import ApiUsageGuard
from vghks_sheet2cal.calendar_monitor import MonitorStore
from vghks_sheet2cal.google_http import GoogleServiceError

PROJECT = "synthetic-private-project"


class Clock:
    def __init__(self, now=1000):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class IndependentConnections:
    """Exercise SQLite atomicity without MonitorStore's in-process Python lock."""
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()


class ApiUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MonitorStore(self.temp.name)
        self.addCleanup(lambda: self.store.close())
        self.clock = Clock()

    def guard(self, project=PROJECT, **kwargs):
        return ApiUsageGuard(self.store, project, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_defaults_and_url_hook_charge_patch_three_units_and_other_attempts_once(self):
        guard = self.guard()
        guard.before_attempt("GET", "https://sheets.googleapis.com/v4/spreadsheets/synthetic")
        guard.before_attempt("POST", "https://sheets.googleapis.com/v4/spreadsheets/synthetic:batchUpdate")
        for _ in range(3):
            # Retry hooks must reserve again even if the previous attempt failed.
            guard.before_attempt("PATCH", "https://www.googleapis.com/calendar/v3/calendars/synthetic/events/synthetic")
        guard.before_attempt("GET", "https://calendar.googleapis.com/calendar/v3/calendars/synthetic/events/synthetic")
        usage = guard.snapshot()
        self.assertEqual((usage["sheets"]["minute_calls"], usage["sheets"]["minute_units"]), (2, 2))
        self.assertEqual((usage["calendar"]["day_calls"], usage["calendar"]["day_units"]), (4, 10))
        self.assertEqual(usage["sheets"]["minute_limit"], 45)
        self.assertEqual(usage["calendar"]["minute_limit"], 120)
        self.assertEqual(usage["calendar"]["day_limit"], 10000)
        self.assertEqual(usage["calendar"]["minute_window_seconds"], 60)
        self.assertEqual(usage["calendar"]["day_window_seconds"], 86400)

    def test_minute_wait_is_interruptible_and_never_holds_the_sqlite_writer_lock(self):
        guard = self.guard(minute_limits={"sheets": 1})
        guard.reserve("sheets", "GET")
        checkpoints = []

        def checkpoint():
            checkpoints.append(self.clock.now)

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 1)
            with sqlite3.connect(self.store.path, timeout=0.05) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            self.clock.sleep(seconds)

        guard.sleep = sleep
        guard.reserve("sheets", "POST", checkpoint=checkpoint)
        self.assertEqual(self.clock.now, 1060)
        self.assertTrue(checkpoints)
        self.assertEqual(len(self.clock.sleeps), 60)
        self.assertEqual(guard.snapshot()["sheets"]["minute_calls"], 1)
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 2)

    def test_pause_checkpoint_aborts_wait_without_another_reservation(self):
        guard = self.guard(minute_limits={"sheets": 1})
        guard.reserve("sheets", "GET")

        def checkpoint():
            if self.clock.sleeps:
                raise RuntimeError("synthetic paused")

        with self.assertRaisesRegex(RuntimeError, "synthetic paused"):
            guard.reserve("sheets", "GET", checkpoint=checkpoint)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_usage_attempts").fetchone()[0], 1)

    def test_new_guards_and_store_restart_do_not_reset_shared_project_budget(self):
        first = self.guard(day_limits={"sheets": 3})
        second = self.guard(day_limits={"sheets": 3})
        first.reserve("sheets", "GET")
        second.reserve("sheets", "POST")
        self.store.close()
        self.store = MonitorStore(self.temp.name)
        restarted = self.guard(day_limits={"sheets": 3})
        self.assertEqual(restarted.snapshot()["sheets"]["day_calls"], 2)
        restarted.reserve("sheets", "GET")
        with self.assertRaises(GoogleServiceError) as raised:
            self.guard(day_limits={"sheets": 3}).reserve("sheets", "GET")
        self.assertEqual(raised.exception.code, "usage_budget")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(restarted.snapshot()["sheets"]["day_calls"], 3)

    def test_daily_budget_counts_units_and_is_separate_for_each_api(self):
        guard = self.guard(day_limits={"sheets": 2, "calendar": 4})
        guard.reserve("calendar", "PATCH")
        guard.reserve("calendar", "POST")
        with self.assertRaises(GoogleServiceError) as raised:
            guard.reserve("calendar", "GET")
        self.assertEqual(raised.exception.code, "usage_budget")
        self.assertTrue(raised.exception.retryable)
        guard.reserve("sheets", "GET")
        guard.reserve("sheets", "POST")
        with self.assertRaises(GoogleServiceError):
            guard.reserve("sheets", "GET")
        self.assertEqual(guard.snapshot()["calendar"]["day_units"], 4)
        self.assertEqual(guard.snapshot()["sheets"]["day_units"], 2)
        self.assertFalse(self.clock.sleeps, "The daily cap must stop instead of waiting in a loop")

    def test_rolling_day_expires_entries_only_at_their_exact_24_hour_boundary(self):
        guard = self.guard(day_limits={"sheets": 2})
        guard.reserve("sheets", "GET")
        self.clock.now += 30
        guard.reserve("sheets", "GET")
        self.clock.now = 1000 + 86400 - 1
        with self.assertRaises(GoogleServiceError):
            guard.reserve("sheets", "GET")
        self.clock.now += 1
        guard.reserve("sheets", "GET")
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 2)
        with self.store.connect() as db:
            timestamps = [row[0] for row in db.execute("SELECT attempted_at FROM api_usage_attempts ORDER BY attempted_at")]
        self.assertEqual(timestamps, [1030, 87400])

    def test_clock_rollback_keeps_reservations_and_snapshot_never_advances_highwater(self):
        guard = self.guard(day_limits={"sheets": 2})
        guard.reserve("sheets", "GET")
        self.clock.now = 100
        guard.reserve("sheets", "GET")
        self.assertEqual(guard.snapshot()["sheets"]["minute_calls"], 2)
        self.clock.now = 1000 + 86400 + 1
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT highwater FROM api_usage_clock").fetchone()[0], 1000)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_usage_attempts").fetchone()[0], 2)
        self.clock.now = 0
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 2)
        with self.assertRaises(GoogleServiceError):
            guard.reserve("sheets", "GET")
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 2)

    def test_calendar_minute_limit_is_weighted_and_cannot_wait_forever_for_an_impossible_cost(self):
        guard = self.guard(minute_limits={"calendar": 3})
        guard.reserve("calendar", "PATCH")
        guard.reserve("calendar", "GET")
        self.assertEqual(self.clock.now, 1060)
        self.assertEqual(guard.snapshot()["calendar"]["minute_units"], 1)
        self.assertEqual(guard.snapshot()["calendar"]["day_units"], 4)
        with self.assertRaises(GoogleServiceError):
            self.guard(minute_limits={"calendar": 2}).reserve("calendar", "PATCH")

    def test_project_namespaces_are_hashed_private_and_independent(self):
        first = self.guard(day_limits={"sheets": 1})
        second = self.guard("synthetic-other-project", day_limits={"sheets": 1})
        first.reserve("sheets", "GET")
        second.reserve("sheets", "GET")
        self.assertEqual(first.snapshot()["sheets"]["day_calls"], 1)
        self.assertEqual(second.snapshot()["sheets"]["day_calls"], 1)
        with self.store.connect() as db:
            namespaces = [row[0] for row in db.execute("SELECT namespace FROM api_usage_clock")]
        self.assertEqual(len(namespaces), 2)
        self.assertTrue(all(len(value) == 64 and value != PROJECT for value in namespaces))
        public = json.dumps(first.snapshot())
        self.assertNotIn(PROJECT, public)
        for namespace in namespaces:
            self.assertNotIn(namespace, public)

    def test_unknown_endpoint_or_clock_fails_closed_without_leaking_url_or_reserving(self):
        guard = self.guard()
        for url in ("https://example.invalid/PRIVATE_DETAIL", "http://sheets.googleapis.com/v4/spreadsheets/PRIVATE_DETAIL"):
            with self.subTest(url=url), self.assertRaises(GoogleServiceError) as raised:
                guard.before_attempt("GET", url)
            self.assertEqual(raised.exception.code, "usage_budget")
            self.assertNotIn("PRIVATE_DETAIL", str(raised.exception))
        self.clock.now = float("nan")
        with self.assertRaises(GoogleServiceError):
            guard.reserve("sheets", "GET")
        self.clock.now = 1000
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 0)

    def test_independent_sqlite_connections_cannot_race_past_the_daily_cap(self):
        store = IndependentConnections(self.store.path)
        guard = ApiUsageGuard(store, PROJECT, clock=self.clock, sleep=self.clock.sleep,
                              day_limits={"sheets": 3})

        def attempt(_):
            try:
                guard.reserve("sheets", "GET")
            except GoogleServiceError as exc:
                self.assertEqual(exc.code, "usage_budget")
                return False
            return True

        with ThreadPoolExecutor(max_workers=10) as executor:
            accepted = list(executor.map(attempt, range(10)))
        self.assertEqual(sum(accepted), 3)
        self.assertEqual(guard.snapshot()["sheets"]["day_calls"], 3)
