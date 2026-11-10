"""Persistent, project-scoped guards for this monitor's Google API attempts.

Reservations precede HTTP attempts, so rejected, failed and retried calls remain
charged. This is a local safety budget; other programs using the same project or
service account still consume Google's shared quota independently.
"""
from __future__ import annotations

import hashlib
import math
import time
from urllib.parse import urlsplit

from .google_http import GoogleServiceError

MINUTE_SECONDS = 60
DAY_SECONDS = 24 * 60 * 60
DEFAULT_MINUTE_LIMITS = {"sheets": 45, "calendar": 120}
DEFAULT_DAY_LIMITS = {"sheets": 10000, "calendar": 10000}


def _budget_error(message):
    return GoogleServiceError(message, code="usage_budget", retryable=True)


def _limits(defaults, overrides):
    values = {**defaults, **(overrides or {})}
    if set(values) != set(defaults) or any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("API 用量限制必須是已知服務的正整數。")
    return values


class ApiUsageGuard:
    def __init__(self, store, project_id, *, clock=time.time, sleep=time.sleep, minute_limits=None, day_limits=None):
        if not isinstance(project_id, str) or not project_id.strip():
            raise _budget_error("無法確認 Google API 專案識別，未送出要求。")
        self.store = store
        self._namespace = hashlib.sha256(project_id.strip().encode()).hexdigest()
        self.clock, self.sleep = clock, sleep
        self.minute_limits = _limits(DEFAULT_MINUTE_LIMITS, minute_limits)
        self.day_limits = _limits(DEFAULT_DAY_LIMITS, day_limits)
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS api_usage_clock (namespace TEXT PRIMARY KEY,highwater REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS api_usage_attempts (namespace TEXT NOT NULL,service TEXT NOT NULL,"
                       "attempted_at REAL NOT NULL,units INTEGER NOT NULL CHECK(units>0))")
            db.execute("CREATE INDEX IF NOT EXISTS api_usage_attempt_window "
                       "ON api_usage_attempts(namespace,service,attempted_at)")

    def _wall_time(self):
        try:
            now = float(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise _budget_error("無法確認本機 API 用量時間，未送出要求。") from exc
        if not math.isfinite(now):
            raise _budget_error("無法確認本機 API 用量時間，未送出要求。")
        return now

    def _effective_time(self, db):
        row = db.execute("SELECT highwater FROM api_usage_clock WHERE namespace=?", (self._namespace,)).fetchone()
        return max(self._wall_time(), row[0]) if row else self._wall_time()

    def before_attempt(self, method, url, *, checkpoint=None):
        """GoogleHTTP hook; URLs are classified but never retained or exposed."""
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname == "sheets.googleapis.com" and parsed.path.startswith("/v4/spreadsheets"):
                service = "sheets"
            elif (parsed.scheme == "https" and parsed.hostname in {"www.googleapis.com", "calendar.googleapis.com"}
                  and parsed.path.startswith("/calendar/v3/")):
                service = "calendar"
            else:
                raise _budget_error("無法辨識 Google API 用量類別，未送出要求。")
        except (TypeError, ValueError) as exc:
            raise _budget_error("無法辨識 Google API 用量類別，未送出要求。") from exc
        self.reserve(service, method, checkpoint=checkpoint)

    def reserve(self, service, method, *, checkpoint=None):
        """Atomically reserve an attempt; wait only for the rolling minute cap."""
        if service not in self.minute_limits or not isinstance(method, str):
            raise _budget_error("無法辨識 Google API 用量類別，未送出要求。")
        units = 3 if service == "calendar" and method.upper() == "PATCH" else 1
        if units > self.minute_limits[service] or units > self.day_limits[service]:
            raise _budget_error("單次 Google API 要求超過本機用量限制，未送出要求。")
        while True:
            if checkpoint:
                checkpoint()
            exhausted, wait = False, None
            with self.store.connect() as db:
                # A read followed by a separate write would race other guards.
                db.execute("BEGIN IMMEDIATE")
                now = self._effective_time(db)
                db.execute("INSERT INTO api_usage_clock VALUES(?,?) ON CONFLICT(namespace) "
                           "DO UPDATE SET highwater=excluded.highwater", (self._namespace, now))
                for name in self.minute_limits:
                    db.execute("DELETE FROM api_usage_attempts WHERE namespace=? AND service=? AND attempted_at<=?",
                               (self._namespace, name, now - DAY_SECONDS))
                day_units = db.execute("SELECT COALESCE(SUM(units),0) FROM api_usage_attempts "
                                       "WHERE namespace=? AND service=? AND attempted_at>?",
                                       (self._namespace, service, now - DAY_SECONDS)).fetchone()[0]
                if day_units + units > self.day_limits[service]:
                    exhausted = True
                else:
                    minute_units, oldest = db.execute(
                        "SELECT COALESCE(SUM(units),0),MIN(attempted_at) FROM api_usage_attempts "
                        "WHERE namespace=? AND service=? AND attempted_at>?",
                        (self._namespace, service, now - MINUTE_SECONDS),
                    ).fetchone()
                    if minute_units + units > self.minute_limits[service]:
                        wait = oldest + MINUTE_SECONDS - now
                    else:
                        db.execute("INSERT INTO api_usage_attempts VALUES(?,?,?,?)",
                                   (self._namespace, service, now, units))
            if exhausted:
                raise _budget_error("本機 Google API 用量已達最近 24 小時上限，暫停送出要求；稍後會重新核對。")
            if wait is None:
                return
            # Do not hold a DB transaction or lock while waiting. Check pause
            # and stop between short waits, including after a clock rollback.
            if checkpoint:
                checkpoint()
            self.sleep(min(1.0, max(0.001, wait)))

    def snapshot(self):
        """Read-only service totals; no project, namespace, URL or source data."""
        result = {}
        with self.store.connect() as db:
            now = self._effective_time(db)
            for service in self.minute_limits:
                minute_calls, minute_units = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(units),0) FROM api_usage_attempts "
                    "WHERE namespace=? AND service=? AND attempted_at>?",
                    (self._namespace, service, now - MINUTE_SECONDS),
                ).fetchone()
                day_calls, day_units = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(units),0) FROM api_usage_attempts "
                    "WHERE namespace=? AND service=? AND attempted_at>?",
                    (self._namespace, service, now - DAY_SECONDS),
                ).fetchone()
                result[service] = {"minute_calls": minute_calls, "minute_units": minute_units,
                                   "minute_limit": self.minute_limits[service], "day_calls": day_calls,
                                   "day_units": day_units, "day_limit": self.day_limits[service],
                                   "minute_window_seconds": MINUTE_SECONDS, "day_window_seconds": DAY_SECONDS}
        return result
