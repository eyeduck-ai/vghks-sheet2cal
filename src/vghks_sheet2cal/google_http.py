"""Bounded Google REST retries; ambiguous mutations are never replayed here."""
from __future__ import annotations

import random
import time

import requests
from google.auth.exceptions import TransportError

RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})


class GoogleServiceError(Exception):
    def __init__(self, message, *, code="", retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def response_kind(response):
    """Inspect Google's reason as well as HTTP status, without logging payloads."""
    reasons = set()
    if response.status_code == 403:
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            reasons = {entry.get("reason") for entry in error.get("errors", []) if isinstance(entry, dict)}
        except (ValueError, TypeError, AttributeError):
            pass
    if response.status_code == 429 or response.status_code == 403 and reasons & RATE_REASONS:
        return "rate_limit"
    if response.status_code >= 500:
        return "transient"
    if response.status_code in {401, 403}:
        return "permission"
    return "rejected"


class GoogleHTTP:
    def __init__(self, session, *, attempts=3, sleep=time.sleep, jitter=random.random):
        self.session, self.attempts = session, attempts
        self.sleep, self.jitter = sleep, jitter
        self.requests = self.retries = 0
        self.checkpoint = lambda: None

    def request(self, method, url, **kwargs):
        for attempt in range(self.attempts):
            self.checkpoint()
            self.requests += 1
            try:
                response = self.session.request(method, url, timeout=(10, 55), **kwargs)
            except (requests.RequestException, TransportError):
                if method != "GET" or attempt + 1 == self.attempts:
                    raise
            else:
                kind = response_kind(response)
                # Rate-limit responses reject the operation. 5xx and network
                # failures on a mutation may have committed it: reconcile first.
                retry = kind == "rate_limit" or method == "GET" and kind == "transient"
                if not retry or attempt + 1 == self.attempts:
                    return response
            self.retries += 1
            self.sleep(0.3 * 2**attempt + 0.15 * self.jitter())
