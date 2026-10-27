"""Google Calendar REST operations owned exclusively by the calendar monitor."""
from __future__ import annotations

from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError, TransportError

from .google_http import GoogleHTTP, GoogleServiceError, response_kind


class CalendarError(GoogleServiceError):
    pass


class CalendarUncertain(CalendarError):
    """A mutation may have reached Google; reconcile its stable ID before retrying."""

    def __init__(self, message, *, code="uncertain", retryable=True):
        super().__init__(message, code=code, retryable=retryable)


class CalendarClient:
    def __init__(self, calendar_id, session):
        self.calendar_id, self.session = calendar_id, session
        self.http = GoogleHTTP(session)
        self.base = "https://www.googleapis.com/calendar/v3/calendars/" + quote(calendar_id, safe="")

    def close(self):
        self.session.close()

    def request(self, method, suffix, **kwargs):
        try:
            response = self.http.request(method, self.base + suffix, **kwargs)
        except (requests.RequestException, TransportError) as exc:
            error = CalendarUncertain if method != "GET" else CalendarError
            raise error("無法確認 Google 日曆回應；下一輪會先核對既有事件。", code="transport", retryable=True) from exc
        except GoogleAuthError as exc:
            raise CalendarError("日曆服務帳戶授權失敗，請檢查金鑰及系統時間。", code="permission") from exc
        if response.status_code in {404, 410}:
            return None
        if response.status_code == 409:
            return {"conflict": True}
        kind = response_kind(response)
        if kind == "rate_limit":
            raise CalendarError("Google 日曆流量限制，已暫停本輪；稍後會重新核對並重試。", code=kind, retryable=True)
        if kind == "permission":
            raise CalendarError("日曆授權失敗：請啟用 Calendar API 並將日曆的事件編輯權限分享給服務帳戶。", code=kind)
        if response.status_code == 429 or response.status_code >= 500:
            error = CalendarUncertain if method != "GET" else CalendarError
            raise error("Google 日曆暫時無法完成請求；稍後會核對並重試。", code="transient", retryable=True)
        if not response.ok:
            raise CalendarError("Google 日曆拒絕這筆事件資料；原綁定保留，請核對日期與內容。",
                                code="row_rejected" if response.status_code in {400, 422} else "rejected")
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            error = CalendarUncertain if method != "GET" else CalendarError
            raise error("無法辨識 Google 日曆回應；下一輪會重新核對。", code="response", retryable=True) from exc

    def probe(self):
        result = self.request("GET", "/events", params={"maxResults": 1})
        if result is None:
            raise CalendarError("找不到目標日曆，請確認 ID 與分享權限。")
        return {"ok": True, "message": "日曆讀取成功；新增與更新仍需要事件編輯權限。"}

    def get(self, event_id):
        if "@" in event_id:
            result = self.request("GET", "/events", params={"iCalUID": event_id, "maxResults": 10})
            matches = [e for e in (result or {}).get("items", []) if e.get("status") != "cancelled"]
            if len(matches) > 1:
                raise CalendarError("舊 iCalUID 對應多個日曆事件，請人工核對。", code="row_rejected")
            return matches[0] if matches else None
        result = self.request("GET", "/events/" + quote(event_id, safe=""))
        return None if result and result.get("status") == "cancelled" else result

    def insert(self, event_id, resource):
        result = self.request("POST", "/events", json={**resource, "id": event_id}, params={"sendUpdates": "none"})
        if result and result.get("conflict"):
            return self.get(event_id)
        return result

    def update(self, event_id, resource):
        # PATCH preserves unrelated Calendar fields such as reminders and attachments.
        # Nested fields also retain omitted values. Explicitly clear the other
        # time representation when converting an all-day event to/from timed.
        payload = dict(resource)
        for field in ("start", "end"):
            when = resource.get(field, {})
            if "dateTime" in when:
                payload[field] = {**when, "date": None}
            elif "date" in when:
                payload[field] = {**when, "dateTime": None, "timeZone": None}
        return self.request("PATCH", "/events/" + quote(event_id, safe=""), json=payload,
                            params={"sendUpdates": "none"})

    def delete(self, event_id):
        return self.request("DELETE", "/events/" + quote(event_id, safe=""), params={"sendUpdates": "none"})
