"""Independent, durable Sheets-to-Calendar worker; never loads hospital credentials."""
from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from vghks_sheet_core.cells import fingerprint
from vghks_sheet_core.common import norm

from . import __version__
from .api_usage import ApiUsageGuard
from .binding_writes import cleanup_binding_anchors, marker_key, plan_binding_anchors, resolve_binding_anchors
from .calendar_client import CalendarClient, CalendarError
from .calendar_sync import (
    OWNER_KEY,
    OWNER_METADATA_ID,
    calendar_binding_compatible,
    digest,
    event_id,
    headers,
    in_calendar_range,
    matches,
    resource,
    scan,
    schedule_in_calendar_range,
    schedule_requires_reconciliation,
)
from .google_http import GoogleServiceError
from .google_sheets import GoogleSettings, SheetError, SheetsClient, spreadsheet_id
from .host_registration import current_host_id
from .locking import lock_file
from .monitor_policy import DELETION_CONFIRMATION_CYCLES, OBSERVATION_SECONDS, POLICY_VERSION
from .portable_credentials import seal, unseal
from .settings import TAIPEI, timestamp
from .source_journal import SourceJournal
from .source_validation import project_source
from .technical_writes import guard_technical_writes

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/calendar.events"]


class RowSyncError(ValueError):
    """A bounded row needs review; other rows can still be reconciled."""


def row_issue(item, exc, code="row_conflict"):
    return {"uid": item["uid"], "sheet": item["sheet"], "row": item["row"] + 1,
            "code": code, "message": str(exc)}


class MonitorStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "calendar.sqlite3"
        self.lock = threading.RLock()
        self.handle = (self.directory / ".lock").open("a+b")
        try:
            lock_file(self.handle)
        except OSError as exc:
            self.handle.close()
            raise ValueError("這個監控資料目錄已有程式使用。") from exc
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS documents (kind TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(kind,id))")
            db.execute("CREATE TABLE IF NOT EXISTS credential (id INTEGER PRIMARY KEY, secret BLOB)")
            db.execute("INSERT OR IGNORE INTO credential VALUES(1,NULL)")
        if self.get("config", "settings") is None:
            self.save("config", "settings", {"spreadsheet_id": "", "calendar_id": "", "email": "",
                "enabled": False, "include_fu": True, "hours": 1, "setup_done": False,
                "worker_id": uuid.uuid4().hex, "next_check": "", "last_attempt": ""})

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.execute("PRAGMA synchronous=FULL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def get(self, kind, key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM documents WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM documents WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recent(self, kind, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM documents WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, kind, key, payload):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?)", (kind, key, json.dumps(payload, ensure_ascii=False)))
            if kind == "history":
                db.execute("DELETE FROM documents WHERE kind='history' AND id NOT IN (SELECT id FROM documents WHERE kind='history' ORDER BY id DESC LIMIT 200)")

    def key(self, supplied=None, remove=False):
        with self.connect() as db:
            if supplied is not None:
                GoogleSettings.validate_key(supplied)
                db.execute("UPDATE credential SET secret=? WHERE id=1", (seal(db, json.dumps(supplied).encode()),))
            if remove:
                db.execute("UPDATE credential SET secret=NULL WHERE id=1")
            row = db.execute("SELECT secret FROM credential WHERE id=1").fetchone()
            return json.loads(unseal(db, row[0])) if row[0] is not None else None

    def configured(self):
        with self.connect() as db:
            return db.execute("SELECT secret IS NOT NULL FROM credential WHERE id=1").fetchone()[0] == 1

    def close(self):
        self.handle.close()


def metadata_owner(book):
    owners = [m for m in book.get("developerMetadata", []) if m.get("metadataKey") == OWNER_KEY]
    if len(owners) > 1:
        raise CalendarError("刀表有重複監控主機登記，已停止同步。", code="host_conflict")
    if not owners:
        return None, None
    if owners[0].get("metadataId") != OWNER_METADATA_ID:
        raise CalendarError("刀表的主機登記需由管理員更新，已停止同步。", code="host_conflict")
    try:
        return owners[0], json.loads(owners[0]["metadataValue"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CalendarError("監控主機登記格式無法辨識，已停止同步。", code="host_conflict") from exc


def registration(config, host_id):
    return {**{k: config[k] for k in ("worker_id", "spreadsheet_id", "calendar_id")}, "host_id": host_id}


def owner_matches(owner, config, host_id=None):
    return owner == registration(config, host_id or current_host_id())


class CalendarMonitor:
    def __init__(self, directory, *, clients=None, background=True, today=None, host_id=None):
        self.host_id = host_id or current_host_id()
        self.today = today or (lambda: datetime.now(TAIPEI).date())
        self.store = MonitorStore(directory)
        self.journal = SourceJournal(self.store)
        self.api_usage = None
        self.usage_project = ""
        raw_key = self.store.key()
        if raw_key:
            self._usage_guard(raw_key)
        self.next_observation = 0.0
        self.clients_factory = clients
        self.stop = threading.Event()
        self.busy = threading.Lock()
        self.state_lock = threading.RLock()
        self.status = {"running": False, "message": "監控尚未啟用。", "issues": [], "updated": 0, "deleted": 0}
        self.status.update(self.store.get("health", "last") or {})
        self.status["running"] = False
        self.pause_epoch = self.active_epoch = 0
        self.last_scan_ok = False
        self.launch_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.thread = threading.Thread(target=self._loop, name="calendar-monitor", daemon=True)
        if background:
            self.thread.start()

    def public(self):
        with self.state_lock:
            return {**self.store.get("config", "settings"), **self.status,
                    "configured": self.store.configured(), "data_dir": str(self.store.directory),
                    "scope_from": self.today().isoformat(),
                    "worker_alive": self.thread.is_alive(), "platform": sys.platform, "history": self.store.recent("history"),
                    "source_observation": self.status.get("source_observation", {}),
                    "source_changes": self.journal.recent(20), "observation_interval_seconds": OBSERVATION_SECONDS,
                    "api_usage": self.api_usage.snapshot() if self.api_usage else {},
                    "monitor_version": __version__, "monitor_policy_version": POLICY_VERSION}

    def configure(self, values):
        if not self.busy.acquire(blocking=False):
            raise ValueError("同步正在執行，請完成後再變更設定。")
        try:
            config = self.store.get("config", "settings")
            changed = dict(config)
            for field in ("spreadsheet_id", "calendar_id", "include_fu", "hours", "enabled"):
                if field in values:
                    changed[field] = values[field]
            changed["spreadsheet_id"] = spreadsheet_id(changed["spreadsheet_id"])
            if not isinstance(changed["calendar_id"], str) or not changed["calendar_id"].strip() or len(changed["calendar_id"]) > 512:
                raise ValueError("請輸入目標 Google Calendar ID。")
            changed["calendar_id"] = changed["calendar_id"].strip()
            if type(changed["hours"]) is not int or not 1 <= changed["hours"] <= 720:
                raise ValueError("監控頻率需為 1–720 小時。")
            if any(type(changed[k]) is not bool for k in ("enabled", "include_fu")):
                raise ValueError("監控選項格式不正確。")
            target_changed = any(changed[k] != config[k] for k in ("spreadsheet_id", "calendar_id"))
            if target_changed and self.store.all("event"):
                raise ValueError("此資料目錄已有事件綁定；更換刀表或日曆請建立新的監控資料目錄。")
            if target_changed or values.get("key"):
                changed.update(enabled=False, setup_done=False)
            if changed["enabled"] and not changed["setup_done"]:
                raise ValueError("請先執行「檢查並準備刀表」，完成後再啟用。")
            if values.get("key"):
                self.store.key(values["key"])
                self._usage_guard(values["key"])
                changed["email"] = values["key"]["client_email"]
            changed.pop("autorun", None)
            if changed["enabled"] and not config["enabled"]:
                changed["next_check"] = timestamp()
            self.store.save("config", "settings", changed)
            return self.public()
        finally:
            self.busy.release()

    def _usage_guard(self, raw):
        domain = raw["client_email"].split("@", 1)[-1]
        suffix = ".iam.gserviceaccount.com"
        project = domain[:-len(suffix)] if domain.endswith(suffix) else raw.get("project_id") or domain
        if not self.api_usage or self.usage_project != project:
            self.api_usage = ApiUsageGuard(self.store, project)
            self.usage_project = project
        return self.api_usage

    @contextmanager
    def clients(self):
        config = self.store.get("config", "settings")
        if self.clients_factory:
            yield self.clients_factory(config)
            return
        raw = self.store.key()
        if not raw:
            raise ValueError("請先匯入服務帳戶金鑰。")
        credentials = service_account.Credentials.from_service_account_info(raw, scopes=SCOPES)
        guard = self._usage_guard(raw)
        # Token refresh before a request is automatic. Do not let an internal
        # 401 replay bypass durable per-attempt accounting or mutation recovery.
        sheets = SheetsClient(config["spreadsheet_id"], AuthorizedSession(credentials, max_refresh_attempts=0))
        calendar = CalendarClient(config["calendar_id"], AuthorizedSession(credentials, max_refresh_attempts=0))
        def stop_checkpoint():
            if self.stop.is_set():
                raise CalendarError("監控正在停止，待核對狀態已保留。", code="paused", retryable=True)

        for client in (sheets, calendar):
            transport = client.http
            transport.checkpoint = stop_checkpoint
            transport.before_attempt = lambda method, url, http=transport: guard.before_attempt(
                method, url, checkpoint=http.checkpoint)
        try:
            yield sheets, calendar
        finally:
            sheets.close()
            calendar.close()

    def _source_view(self, book):
        from .sheet_sync_state import read_sync_state

        previous = self.store.all("event")
        historical = {old["uid"] for old in previous if old.get("status") not in {"pending", "deleting"}
                      and (old.get("frozen_past") or not in_calendar_range(old.get("resource"), self.today()))}
        bound = {old["uid"] for old in previous if old.get("status") != "deleted"}
        bound.update(uid for uid, state in read_sync_state(book)["states"].items() if state.get("mrn_hash"))
        view = project_source(book, bound_patient_uids=bound - historical)
        bound_sheets = {old["sheet_id"] for old in previous if old.get("status") != "deleted"}
        for sheet in book.get("sheets", []):
            if sheet["properties"]["sheetId"] not in bound_sheets:
                continue
            try:
                _, cols = headers(sheet)
            except ValueError:
                continue
            if {"ScheduleSyncId", "CalendarEventId"} - cols.keys():
                view["global_issues"].append(f"{sheet['properties']['title']} 既有綁定分頁的系統欄標題遺失，原事件保留。")
        return view

    def _scan_source(self, book, include_fu=True):
        view = self._source_view(book)
        result = scan(view["book"], include_fu)
        result["issues"] = list(dict.fromkeys(view["global_issues"] + result["issues"]))
        result["row_issues"].extend(view["row_issues"])
        result["identities"].update(view["active_uids"])
        result["blocked_uids"] = view["blocked_uids"]
        return result

    def _observe(self, book, *, phase="source"):
        observed = self.journal.observe(book, timestamp(), phase=phase)
        self.next_observation = time.monotonic() + OBSERVATION_SECONDS
        self._status(source_observation=observed, source_observation_error="")
        return observed

    def _migrate(self, sheets, book):
        from .sheet_archive_lock import assert_archive_available
        from .sheet_identity import plan_identity_setup

        assert_archive_available(book)
        # Structural identity faults are quarantined in an in-memory view.
        # No quarantined source values are cleared or selected as a winner.
        view = self._source_view(book)
        if view["global_issues"]:
            raise ValueError("；".join(view["global_issues"][:8]))
        plan = plan_identity_setup(view["book"], include_archives=True)
        if plan["issues"]:
            raise ValueError("；".join(i.get("reason") or f"{i.get('sheet', '')} {i['code']}" for i in plan["issues"][:8]))
        expected = scan(plan["book"])
        if expected["issues"]:
            raise ValueError("；".join(expected["issues"][:8]))
        self._validate_bindings(plan["book"], expected)
        requests_ = plan["requests"]
        for request in requests_:
            bounds = request.get("updateCells", {}).get("range")
            if bounds and any(sid == bounds["sheetId"] and bounds.get("startRowIndex", 0) <= row < bounds["endRowIndex"]
                              for sid, row in view["blocked_rows"]):
                raise ValueError("識別欄調整會碰到待核對列；請先核對欄位位置，原資料保留。")
        if not requests_:
            return book
        fresh = self._read_monitor(sheets)
        if fingerprint(fresh) != fingerprint(book):
            raise ValueError("刀表在準備期間有變動，請重新執行；未寫入。")
        self._assert_owner(sheets)
        sheets.write(guard_technical_writes(book, requests_))
        result = self._read_monitor(sheets)
        actual = self._scan_source(result)
        if actual["issues"]:
            raise ValueError("系統欄回讀需核對：" + "；".join(actual["issues"][:6]))
        pairs = {p["uid"]: p["identity"] for p in actual["patients"]}
        if any(pairs.get(p["uid"]) != p["identity"] for p in expected["patients"]):
            raise ValueError("系統欄寫入時病人列已移動，請人工核對；未操作日曆。")
        self._validate_bindings(result, actual)
        self._observe(result, phase="identity_setup")
        return result

    def _validate_bindings(self, book, snapshot):
        from .sheet_sync_state import field_hash, read_sync_state
        shared = read_sync_state(book)
        if shared["issues"]:
            raise ValueError("主程式同步基準格式需核對，未變更刀表或日曆。")
        config = self.store.get("config", "settings")
        patient_ids = {item["uid"] for item in snapshot["patients"]}
        header_ids = snapshot["identities"] - patient_ids - snapshot["archive_ids"] - snapshot.get("blocked_uids", set())
        historical_ids = {old["uid"] for old in self.store.all("event")
                          if old.get("status") != "pending"
                          and (old.get("frozen_past") or not in_calendar_range(old.get("resource"), self.today()))}
        bound_ids = {old["uid"] for old in self.store.all("event") if old.get("status") != "deleted"}
        bound_ids.update(uid for uid, state in shared["states"].items() if state.get("mrn_hash"))
        bound_ids -= historical_ids
        if header_ids & bound_ids:
            raise ValueError("已綁定患者的排程 ID 變成刀日標題，請核對列類型與識別；未變更刀表或日曆。")
        issues = list(snapshot.get("row_issues", []))
        quarantined = {issue["uid"] for issue in issues}
        for item in snapshot["patients"]:
            old = self.store.get("event", item["uid"])
            if item["date"] and not schedule_requires_reconciliation(item, self.today(), old):
                continue
            if item["uid"] in quarantined:
                continue
            try:
                self._binding(item, old, config)
                try:
                    expected = resource(item, config["spreadsheet_id"])
                except ValueError as exc:
                    raise RowSyncError(str(exc)) from exc
                if expected is None and (old or item["event_id"]):
                    raise RowSyncError("已綁定追蹤列清空日期，請人工確認；事件保留。")
                state = shared["states"].get(item["uid"], {})
                bound_mrn = state.get("mrn_hash") or state.get("fields", {}).get("mrn")
                # A trusted local Calendar binding can survive a campus MRN
                # change. First adoption still requires the shared baseline.
                if not old and bound_mrn and bound_mrn != field_hash("mrn", item["mrn"]):
                    raise RowSyncError("排程 ID 與主程式原病歷號綁定不符，請先核對首次綁定。")
            except RowSyncError as exc:
                issues.append(row_issue(item, exc))
        return issues

    def pause(self):
        # Pausing must also work before the first configuration is complete.
        with self.state_lock:
            config = self.store.get("config", "settings")
            config["enabled"] = False
            self.store.save("config", "settings", config)
            self.pause_epoch += 1
            if self.busy.locked():
                self.status["message"] = "已要求暫停，將在目前操作核對後停止；未完成綁定會保留。"
            return self.public()

    def _checkpoint(self):
        if self.stop.is_set() or self.pause_epoch != self.active_epoch:
            raise CalendarError("已暫停同步，未完成操作已保留，恢復後會先核對原事件。", code="paused")

    def _require_owner(self, book, config):
        _, owner = metadata_owner(book)
        matched = owner_matches(owner, config, self.host_id)
        self._status(host_state="registered" if matched else "conflict" if owner else "unregistered",
                     host_check_at=timestamp())
        if not matched:
            raise CalendarError("刀表已由另一個監控實例或主機登記，本機已停止同步。", code="host_conflict")

    def _assert_owner(self, sheets):
        reader = getattr(sheets, "read_metadata", None)
        self._require_owner(reader() if reader else self._read_monitor(sheets), self.store.get("config", "settings"))

    def prepare(self):
        if not self.busy.acquire(blocking=False):
            raise ValueError("同步正在執行。")
        try:
            config = self.store.get("config", "settings")
            config["enabled"] = False
            self.store.save("config", "settings", config)
            from .sheet_mutex import sheet_mutex
            with self.clients() as (sheets, calendar), sheet_mutex(config["spreadsheet_id"]):
                calendar.probe()
                book = self._read_monitor(sheets)
                from .sheet_archive_lock import assert_archive_available

                assert_archive_available(book)
                _, owner = metadata_owner(book)
                from .sheet_identity import plan_identity_setup

                view = self._source_view(book)
                preview = plan_identity_setup(view["book"], include_archives=True)
                if preview["issues"] or view["global_issues"]:
                    raise ValueError("刀表格式或識別需核對，尚未登記主機。")
                self._validate_bindings(preview["book"], scan(preview["book"]))
                if owner:
                    self._require_owner(book, config)
                else:
                    # The fixed spreadsheet-scoped ID makes concurrent claims
                    # atomic: at most one first-time registration can succeed.
                    try:
                        sheets.write([{"createDeveloperMetadata": {"developerMetadata": {
                            "metadataId": OWNER_METADATA_ID, "metadataKey": OWNER_KEY,
                            "metadataValue": json.dumps(registration(config, self.host_id), sort_keys=True),
                            "visibility": "DOCUMENT", "location": {"spreadsheet": True}}}}])
                    except SheetError:
                        self._assert_owner(sheets)
                    self._assert_owner(sheets)
                    book = self._read_monitor(sheets)
                self._observe(book)
                book = self._migrate(sheets, book)
                prepared_snapshot = self._scan_source(book)
                if prepared_snapshot["issues"]:
                    raise ValueError("；".join(prepared_snapshot["issues"][:8]))
                self._assert_owner(sheets)
            config["setup_done"] = True
            self.store.save("config", "settings", config)
            issues = self._validate_bindings(book, self._scan_source(book, config["include_fu"]))
            message = "準備完成。啟用後每小時同步；目前仍停用。"
            if issues:
                message += f"有 {len(issues)} 列待核對，啟用後其餘列仍可同步。"
            self._status(message=message, issues=[], row_issues=issues, partial=False)
            return self.public()
        finally:
            self.busy.release()

    def remove_key(self):
        if not self.busy.acquire(blocking=False):
            raise ValueError("同步正在執行。")
        try:
            config = self.store.get("config", "settings")
            config.update(enabled=False, setup_done=False, email="")
            self.store.key(remove=True)
            self.store.save("config", "settings", config)
            return self.public()
        finally:
            self.busy.release()

    def inspect(self):
        """Check live input and proposed technical setup without any cloud writes."""
        from .sheet_archive_lock import assert_archive_available
        from .sheet_identity import plan_identity_setup

        if not self.busy.acquire(blocking=False):
            raise ValueError("同步正在執行。")
        try:
            config = self.store.get("config", "settings")
            with self.clients() as (sheets, calendar):
                calendar.probe()
                book = self._read_monitor(sheets)
                assert_archive_available(book)
                _, owner = metadata_owner(book)
                matched = owner_matches(owner, config, self.host_id)
                self._status(host_state="registered" if matched else "conflict" if owner else "unregistered",
                             host_check_at=timestamp())
                view = self._source_view(book)
                plan = plan_identity_setup(view["book"], include_archives=True)
                snapshot = scan(plan["book"], config["include_fu"])
                snapshot["issues"].extend(view["global_issues"])
                snapshot["row_issues"].extend(view["row_issues"])
                row_issues = self._validate_bindings(plan["book"], snapshot) if not snapshot["issues"] else []
                cutoff = self.today()
                today = cutoff.isoformat()
                return {"ok": not plan["issues"] and not snapshot["issues"] and not row_issues and (not owner or matched),
                        "issues": plan["issues"] + snapshot["issues"],
                        "row_issues": row_issues,
                        "planned_technical_requests": len(plan["requests"]),
                        "monthly_rows": sum(p["kind"] == "MONTHLY" for p in snapshot["patients"]),
                        "fu_rows": sum(p["kind"] == "FU" for p in snapshot["patients"]),
                        "scope_from": today,
                        "eligible_rows": sum(schedule_requires_reconciliation(
                            p, cutoff, self.store.get("event", p["uid"])) for p in snapshot["patients"]),
                        "owner_registered": bool(owner), "owner_matches": matched,
                        "calendar_readable": True}
        finally:
            self.busy.release()

    def _status(self, **values):
        with self.state_lock:
            self.status.update(values)

    def _binding(self, item, old, config):
        if not calendar_binding_compatible(item, old):
            raise RowSyncError("排程 ID 對應病人已變更，病歷號與姓名均無法核對；原事件保留。")
        sheet_event = item["event_id"]
        if old and sheet_event and sheet_event not in {old["event_id"], old.get("legacy_event_id")}:
            raise RowSyncError("事件綁定與監控紀錄不一致，請核對；原事件保留。")
        return (old or {}).get("event_id") or sheet_event or event_id(config["spreadsheet_id"], item["uid"])

    @staticmethod
    def _read_monitor(sheets):
        reader = getattr(sheets, "read_for_monitor", None) or sheets.read_all
        return reader()

    def _write_bindings(self, sheets, bindings):
        # Short-lived native row anchors follow whole-row moves. Conditions on
        # the old address alone cannot distinguish two blank event-ID cells.
        succeeded, issues = set(), []
        worker_id = self.store.get("config", "settings")["worker_id"]
        pending = (self.store.get("binding", "anchors_pending") or {}).get("pending")
        if not bindings and not pending:
            return succeeded, issues
        self._cleanup_binding_anchors(sheets, worker_id)
        for start in range(0, len(bindings), 100):
            self._checkpoint()
            batch = bindings[start:start + 100]
            current_book = self._read_monitor(sheets)
            current = self._scan_source(current_book)
            if current["issues"]:
                raise ValueError("回寫事件 ID 前刀表有衝突，事件已保存在監控紀錄，稍後重試。")
            indexed = {p["uid"]: p for p in current["patients"]}
            valid = []
            for item, eid in batch:
                target = indexed.get(item["uid"])
                if not target or target["identity"] != item["identity"]:
                    issues.append(row_issue(item, "回寫事件 ID 前病人列已移動或變更，待下輪核對。"))
                    continue
                if target["event_id"] and target["event_id"] not in {eid, item["event_id"]}:
                    issues.append(row_issue(target, "既有事件 ID 已由其他操作變更，未覆寫。"))
                    continue
                valid.append((item, eid))
            needs_write = [(indexed[item["uid"]], eid) for item, eid in valid if indexed[item["uid"]]["event_id"] != eid]
            if needs_write:
                plan = plan_binding_anchors(needs_write, worker_id)
                # A crash or ambiguous create/cleanup must be recoverable even
                # when the next cycle has no event IDs left to write.
                self.store.save("binding", "anchors_pending", {"pending": True})
                self._assert_owner(sheets)
                sheets.write(plan["requests"])
                markers = sheets.search_developer_metadata(plan["key"])
                anchored_book = self._read_monitor(sheets)
                anchored = self._scan_source(anchored_book)
                if anchored["issues"]:
                    raise ValueError("事件綁定前刀表結構有變動，保留事件並待下輪核對。")
                resolved = resolve_binding_anchors(plan, anchored_book, anchored["patients"], markers)
                issues.extend(resolved["row_issues"])
                deferred = {issue["uid"] for issue in resolved["row_issues"]}
                valid = [(item, eid) for item, eid in valid if item["uid"] not in deferred]
                if resolved["body"]["data"]:
                    self._assert_owner(sheets)
                    try:
                        sheets.write_filtered_values(resolved["body"])
                    except SheetError as exc:
                        if exc.code != "binding_changed":
                            raise
                        # A row may be deleted, taking its metadata with it.
                        # Re-resolve and isolate definite per-filter rejections;
                        # network/5xx/permission failures are never replayed here.
                        latest_markers = sheets.search_developer_metadata(plan["key"])
                        latest_book = self._read_monitor(sheets)
                        latest = self._scan_source(latest_book)
                        if latest["issues"]:
                            raise ValueError("事件綁定核對時刀表結構有變動；原事件保留。") from exc
                        retried = resolve_binding_anchors(plan, latest_book, latest["patients"], latest_markers)
                        issues.extend(retried["row_issues"])
                        for value_range in retried["body"]["data"]:
                            self._assert_owner(sheets)
                            try:
                                sheets.write_filtered_values({**retried["body"], "data": [value_range]})
                            except SheetError as failed:
                                if failed.code != "binding_changed":
                                    raise
                                uid = value_range["dataFilter"]["developerMetadataLookup"]["metadataValue"].split(":")[1]
                                item = next(item for item, _ in valid if item["uid"] == uid)
                                issues.append(row_issue(item, "此列的事件綁定尚未確認；其他正常列繼續。", "binding_anchor"))
                        deferred = {issue["uid"] for issue in issues}
                        valid = [(item, eid) for item, eid in valid if item["uid"] not in deferred]
            verified = self._scan_source(self._read_monitor(sheets))
            if verified["issues"]:
                raise ValueError("事件 ID 回讀發現刀表結構衝突；監控已保留事件綁定。")
            indexed = {p["uid"]: p for p in verified["patients"]}
            for item, eid in valid:
                target = indexed.get(item["uid"])
                if not target or target["event_id"] != eid or target["identity"] != item["identity"]:
                    issues.append(row_issue(target or item, "事件 ID 回讀不一致；監控已保留事件綁定。"))
                else:
                    succeeded.add(item["uid"])
            if needs_write:
                self._cleanup_binding_anchors(sheets, worker_id)
        return succeeded, issues

    def _cleanup_binding_anchors(self, sheets, worker_id):
        markers = sheets.search_developer_metadata(marker_key(worker_id))
        requests_ = cleanup_binding_anchors(worker_id, markers)
        if requests_:
            self._assert_owner(sheets)
            sheets.write(requests_)
        self.store.save("binding", "anchors_pending", {"pending": False})

    def _sync_item(self, sheets, calendar, item, config, now):
        old = self.store.get("event", item["uid"])
        eid = self._binding(item, old, config)
        try:
            expected = resource(item, config["spreadsheet_id"])
        except ValueError as exc:
            raise RowSyncError(str(exc)) from exc
        if expected is None:
            if old or item["event_id"]:
                raise RowSyncError("已綁定追蹤列清空日期，請人工確認；事件保留。")
            return None, None, 0
        confirmed = (old or {}).get("resource")
        if old and old.get("status") == "pending" and "desired_resource" not in old:
            # Older pending records used resource for an unconfirmed target.
            confirmed = None
        pending = {"uid": item["uid"], "identity": (old or {}).get("identity", item["identity"]), "event_id": eid,
            "patient": (old or {}).get("patient") or {"mrn": item["mrn"], "name": item["name"]},
            "sheet_id": item["sheet_id"], "source": {"sheet": item["sheet"], "row": item["row"] + 1},
            "hash": digest(expected), "resource": confirmed, "desired_resource": expected,
            "status": "pending", "missing": 0, "updated_at": now}
        if old and old.get("legacy_event_id"):
            pending["legacy_event_id"] = old["legacy_event_id"]
        self.store.save("event", item["uid"], pending)
        actual = calendar.get(eid)
        if not schedule_in_calendar_range(item, self.today()) and actual is None:
            self.store.save("event", item["uid"], {**pending, "resource": None, "status": "historical",
                                                   "confirmed_absent": True, "confirmed_at": timestamp()})
            return None, None, 0
        if "@" in eid and not actual:
            raise RowSyncError("刀表的舊 iCalUID 查無事件，請人工核對；未建立另一事件。")
        if actual and "@" in eid:
            pending["legacy_event_id"] = eid
            eid = actual["id"]
            pending["event_id"] = eid
            self.store.save("event", item["uid"], pending)
        private = (actual or {}).get("extendedProperties", {}).get("private", {})
        if actual and private.get("vghksSyncId") and private["vghksSyncId"] != item["uid"]:
            raise RowSyncError("日曆事件已綁定另一筆排程，未覆寫。")
        if actual and private.get("vghksSource") and private["vghksSource"] != config["spreadsheet_id"]:
            raise RowSyncError("日曆事件屬於另一份刀表，未覆寫。")
        if actual and not item["event_id"] and not private.get("vghksSyncId"):
            raise RowSyncError("固定日曆 ID 已被未識別事件使用，未覆寫。")
        if (actual and not private.get("vghksSyncId")
                and norm(actual.get("summary", "").split(" | ")[0]) != norm(item["mrn"])):
            raise RowSyncError("既有日曆事件的病歷號與刀表不同，請先核對綁定。")
        if actual:
            pending.update(resource=actual, confirmed_at=timestamp())
            self.store.save("event", item["uid"], pending)
        updated = 0
        if not matches(actual, expected):
            if actual and isinstance(calendar, CalendarClient) and not actual.get("etag"):
                raise RowSyncError("日曆事件缺少版本資訊，保留事件並待下輪核對。")
            self._assert_owner(sheets)
            options = {"etag": actual["etag"]} if actual and actual.get("etag") else {}
            response = calendar.update(eid, expected, **options) if actual else calendar.insert(eid, expected)
            if response is None:
                raise RowSyncError("日曆事件未能寫入，將於下一輪核對。")
            if not matches(calendar.get(eid), expected):
                raise RowSyncError("日曆事件回讀與刀表預期不符，已保留待核對狀態。")
            updated = 1
        # Calendar confirmation survives an independent Sheet binding failure.
        # Keep pending until both sides agree, without losing confirmed truth.
        pending.update(resource=expected, confirmed_at=timestamp(), identity=item["identity"],
                       patient={"mrn": item["mrn"], "name": item["name"]})
        self.store.save("event", item["uid"], pending)
        binding = (item, eid) if item["event_id"] != eid else None
        ready = {key: value for key, value in pending.items() if key != "desired_resource"}
        ready.update(event_id=eid, identity=item["identity"], patient={"mrn": item["mrn"], "name": item["name"]},
                     resource=expected, confirmed_at=timestamp(), status="ready")
        return binding, ready, updated

    def _prepare_retirement(self, calendar, old, snapshot, deletion_safe):
        """Resolve uncertain mutations before using dates to protect/delete an absent row."""
        uid = old["uid"]
        if uid in snapshot["identities"]:
            if old.get("missing"):
                self.store.save("event", uid, {**old, "missing": 0})
            return None, 0
        if old.get("status") == "deleted":
            return None, 0
        actual = None
        if old.get("status") in {"pending", "deleting"}:
            actual = calendar.get(old["event_id"])
            if actual is None:
                self.store.save("event", uid, {**old, "resource": None, "status": "deleted", "missing": 0,
                                               "confirmed_at": timestamp()})
                return None, 0
            self._require_event_owner(actual, uid)
            # The disappeared source must not cause its old desired update to
            # replay. Only the actual confirmed Calendar state decides scope.
            old = {**old, "resource": actual, "status": "missing", "confirmed_at": timestamp()}
            self.store.save("event", uid, old)
        if old.get("frozen_past") or not in_calendar_range(old.get("resource"), self.today()):
            if actual is not None:
                self.store.save("event", uid, {**old, "status": "historical", "frozen_past": True, "missing": 0})
                return None, 1
            return None, 0
        previous = self.store.get("scan", "last") or {}
        source_present = old["sheet_id"] in snapshot["sheet_ids"]
        # A real row delete can happen before/after a cross-tab move; never
        # infer removal while any formerly observed sheet is absent.
        all_sheets_present = set(previous.get("sheet_ids", [])) <= snapshot["sheet_ids"]
        prior = old.get("missing", 0) if self.last_scan_ok else 0
        count = prior + 1 if deletion_safe and source_present and all_sheets_present else 0
        self.store.save("event", uid, {**old, "missing": count})
        if count < DELETION_CONFIRMATION_CYCLES:
            return None, 0
        actual = calendar.get(old["event_id"])
        if actual and not in_calendar_range(actual, self.today()):
            self.store.save("event", uid, {**old, "resource": actual, "status": "historical",
                                           "frozen_past": True, "missing": 0, "confirmed_at": timestamp()})
            return None, 1
        if actual:
            self._require_event_owner(actual, uid)
        else:
            self.store.save("event", uid, {**old, "missing": count, "status": "deleted", "confirmed_at": timestamp()})
            return None, 0
        return {"old": old, "missing": count}, 0

    def _apply_retirement(self, sheets, calendar, plan, final_book):
        old, count = plan["old"], plan["missing"]
        uid = old["uid"]
        if fingerprint(self._read_monitor(sheets)) != fingerprint(final_book):
            raise ValueError("刪除前刀表有新變動，本輪保留事件，待下輪重新確認。")
        actual = calendar.get(old["event_id"])
        if actual is None:
            self.store.save("event", uid, {**old, "status": "deleted", "confirmed_at": timestamp()})
            return 0
        self._require_event_owner(actual, uid)
        if not in_calendar_range(actual, self.today()):
            self.store.save("event", uid, {**old, "resource": actual, "status": "historical",
                                           "frozen_past": True, "missing": 0, "confirmed_at": timestamp()})
            return 0
        if isinstance(calendar, CalendarClient) and not actual.get("etag"):
            raise RowSyncError("日曆事件缺少版本資訊，保留事件並待下輪核對。")
        self.store.save("event", uid, {**old, "missing": count, "status": "deleting"})
        self._assert_owner(sheets)
        options = {"etag": actual["etag"]} if actual.get("etag") else {}
        calendar.delete(old["event_id"], **options)
        if calendar.get(old["event_id"]) is not None:
            raise CalendarError("刪除事件尚未確認，下輪會先核對。")
        self.store.save("event", uid, {**old, "missing": count, "status": "deleted", "confirmed_at": timestamp()})
        return 1

    def _require_event_owner(self, actual, uid):
        config = self.store.get("config", "settings")
        private = actual.get("extendedProperties", {}).get("private", {})
        if private.get("vghksSyncId") != uid or private.get("vghksSource") != config["spreadsheet_id"]:
            raise RowSyncError("待核對事件無法確認擁有權；原事件保留，請核對綁定。")

    def _reset_missing(self):
        for old in self.store.all("event"):
            if old.get("missing"):
                self.store.save("event", old["uid"], {**old, "missing": 0})

    def run_once(self):
        with self.state_lock:
            epoch = self.pause_epoch
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "busy": True}
        updated = deleted = past_skipped = protected_past = 0
        row_issues = []
        now = timestamp()
        started = time.monotonic()
        self.active_epoch = epoch
        config, result, metrics, sheets, calendar = {}, {"ok": False, "at": now}, {}, None, None
        self._status(running=True, message="正在讀取並核對刀表…", issues=[], row_issues=[], partial=False, failed_rows=0)
        try:
            config = self.store.get("config", "settings")
            self._checkpoint()
            if not config["setup_done"]:
                raise ValueError("請先檢查並準備刀表。")
            from .sheet_mutex import sheet_mutex
            with self.clients() as (sheets, calendar), sheet_mutex(config["spreadsheet_id"]):
                for client in (sheets, calendar):
                    if hasattr(client, "http"):
                        client.http.checkpoint = self._checkpoint
                book = self._read_monitor(sheets)
                self._require_owner(book, config)
                observation = self._observe(book)
                book = self._migrate(sheets, book)
                snapshot = self._scan_source(book, config["include_fu"])
                if snapshot["issues"]:
                    raise ValueError("；".join(snapshot["issues"][:8]))
                previous = self.store.get("scan", "last")
                deletion_safe = bool(previous)
                # Any absent sheet disables deletion; archived IDs remain protective.
                # Presence anywhere protects an existing event, even if the
                # row's type changed and it is no longer an eligible patient.
                row_issues = self._validate_bindings(book, snapshot)
                quarantined = {issue["uid"] for issue in row_issues}
                bindings, completed = [], []
                for item in snapshot["patients"]:
                    self._checkpoint()
                    if item["uid"] in quarantined:
                        continue
                    old = self.store.get("event", item["uid"])
                    if item["date"] and not schedule_requires_reconciliation(item, self.today(), old):
                        past_skipped += 1
                        continue
                    try:
                        binding, ready, changed = self._sync_item(sheets, calendar, item, config, now)
                    except RowSyncError as exc:
                        row_issues.append(row_issue(item, exc))
                        continue
                    except CalendarError as exc:
                        # Transport, quota, authorization and uncertain mutations
                        # affect the whole connection. Only explicit rejections
                        # of one event's data are isolated.
                        if exc.code not in {"row_rejected", "event_changed"}:
                            raise
                        row_issues.append(row_issue(item, exc, exc.code))
                        continue
                    updated += changed
                    if binding:
                        bindings.append(binding)
                    if ready:
                        completed.append((item["uid"], ready))
                        if not binding:
                            self.store.save("event", item["uid"], ready)
                verified, binding_issues = self._write_bindings(sheets, bindings)
                row_issues.extend(binding_issues)
                needs_binding = {item["uid"] for item, _ in bindings}
                for uid, ready in completed:
                    if uid not in needs_binding or uid in verified:
                        self.store.save("event", uid, ready)
                self._checkpoint()
                # Re-read the complete workbook before considering any disappearance.
                final_book = self._read_monitor(sheets)
                self._observe(final_book, phase="verified_source")
                final = self._scan_source(final_book, config["include_fu"])
                if final["issues"] or final["identities"] != snapshot["identities"]:
                    raise ValueError("同步期間刀表結構或識別有變動，本輪不刪除事件。")
                final_row_issues = self._validate_bindings(final_book, final)
                known = {(issue["uid"], issue["sheet"], issue["row"]) for issue in row_issues}
                row_issues.extend(issue for issue in final_row_issues
                                  if (issue["uid"], issue["sheet"], issue["row"]) not in known)
                already_failed = {issue["uid"] for issue in row_issues}
                completed_ids = {uid for uid, _ in completed}
                for item in final["patients"]:
                    if item["uid"] not in completed_ids or item["uid"] in already_failed:
                        continue
                    try:
                        current_expected = resource(item, config["spreadsheet_id"])
                    except ValueError:
                        continue  # Already represented by the row diagnostics.
                    confirmed = self.store.get("event", item["uid"])
                    if current_expected and not matches((confirmed or {}).get("resource"), current_expected):
                        row_issues.append(row_issue(item, "同步期間排程有新變動；其他完成項目保留，此列待下輪核對。", "source_changed"))
                if row_issues:
                    self._reset_missing()
                else:
                    retirements = []
                    for old in self.store.all("event"):
                        self._checkpoint()
                        try:
                            retirement, protected = self._prepare_retirement(calendar, old, final, deletion_safe)
                        except RowSyncError as exc:
                            source = old.get("source", {})
                            row_issues.append({"uid": old["uid"], "sheet": source.get("sheet", ""),
                                               "row": source.get("row", 0), "code": "event_owner", "message": str(exc)})
                            self._reset_missing()
                            break
                        if retirement:
                            retirements.append(retirement)
                        protected_past += protected
                    if not row_issues:
                        for retirement in retirements:
                            self._checkpoint()
                            try:
                                deleted += self._apply_retirement(sheets, calendar, retirement, final_book)
                            except (RowSyncError, CalendarError) as exc:
                                if isinstance(exc, CalendarError) and exc.code != "event_changed":
                                    raise
                                old = retirement["old"]
                                source = old.get("source", {})
                                row_issues.append({"uid": old["uid"], "sheet": source.get("sheet", ""),
                                                   "row": source.get("row", 0), "code": "event_changed", "message": str(exc)})
                                self._reset_missing()
                                break
                        if not row_issues:
                            self.store.save("scan", "last", {"sheet_ids": sorted(snapshot["sheet_ids"]), "at": now})
            result = {"ok": not row_issues, "partial": bool(row_issues), "row_issues": row_issues,
                      "failed_rows": len(row_issues), "updated": updated, "deleted": deleted, "at": now,
                      "scope_from": self.today().isoformat(), "past_skipped": past_skipped,
                      "protected_past": protected_past, "source_revision": observation["revision"],
                      "message": f"同步完成：更新 {updated} 筆，刪除 {deleted} 筆。"}
            if past_skipped:
                result["message"] += f"略過過去排程 {past_skipped} 筆。"
            if protected_past:
                result["message"] += f"保留已無排程且已過去或日期不明的事件 {protected_past} 筆。"
            if row_issues:
                result["failure_code"] = "row_issues"
                result["message"] = f"部分完成：更新 {updated} 筆，{len(row_issues)} 列待核對；本輪不刪除事件。"
                if deleted:
                    result["message"] = f"部分完成：更新 {updated} 筆、已確認刪除 {deleted} 筆；偵測到新問題後停止後續刪除，{len(row_issues)} 列待核對。"
            self._status(**{k: result[k] for k in ("updated", "deleted", "message", "partial", "row_issues", "failed_rows")}, issues=[])
            self.last_scan_ok = not row_issues
        except Exception as exc:
            self.last_scan_ok = False
            message = str(exc) if isinstance(exc, (ValueError, GoogleServiceError)) else "同步未完成，已保存待核對狀態；請檢查網路與設定。"
            result = {"ok": False, "at": now, "message": message, "updated": updated, "deleted": deleted,
                      "failure_code": getattr(exc, "code", "") or "conflict", "retryable": bool(getattr(exc, "retryable", False)),
                      "partial": False, "row_issues": row_issues, "failed_rows": len(row_issues)}
            self._status(message=message, issues=[message], updated=updated, deleted=deleted, partial=False,
                         row_issues=row_issues, failed_rows=len(row_issues))
            if result["failure_code"] == "host_conflict":
                config = self.store.get("config", "settings")
                config["enabled"] = False
                self.store.save("config", "settings", config)
                self._status(host_state="conflict")
            # Errors break consecutive-success deletion confirmation.
            self._reset_missing()
        finally:
            try:
                metrics = self._api_metrics(sheets, calendar)
                # Pause can update settings while this round finishes a request.
                config = self.store.get("config", "settings")
                failures = 0 if result["ok"] else config.get("consecutive_failures", 0) + 1
                delay = timedelta(hours=config["hours"])
                if result.get("retryable"):
                    delay = min(delay, timedelta(minutes=min(60, 5 * 2**min(failures - 1, 4))))
                config.update(last_attempt=now, consecutive_failures=failures,
                              next_check=(datetime.now(TAIPEI) + delay).isoformat())
                if result["ok"]:
                    config["last_success"] = timestamp()
                result.update(duration_seconds=round(time.monotonic() - started, 3), api=metrics)
                self.store.save("config", "settings", config)
                self.store.save("history", now, result)
                self._status(running=False, duration_seconds=result["duration_seconds"], api=metrics,
                             failure_code=result.get("failure_code", ""))
                self.store.save("health", "last", self.status)
            finally:
                self._status(running=False)
                self.busy.release()
        return result

    def run_background(self):
        if self.busy.locked():
            raise ValueError("同步正在執行。")
        threading.Thread(target=self.run_once, name="calendar-manual", daemon=True).start()
        return {"ok": True}

    def observe_once(self):
        """Read-only five-minute source observation; never calls Calendar or allocates IDs."""
        with self.state_lock:
            epoch = self.pause_epoch
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "busy": True}
        sheets = calendar = None
        self.active_epoch = epoch
        try:
            config = self.store.get("config", "settings")
            if not config.get("enabled") or not config.get("setup_done"):
                return {"ok": False, "paused": True}
            with self.clients() as (sheets, calendar):
                for client in (sheets, calendar):
                    if hasattr(client, "http"):
                        client.http.checkpoint = self._checkpoint
                book = self._read_monitor(sheets)
                self._require_owner(book, config)
                observed = self._observe(book)
                self._status(source_observation_api=self._api_metrics(sheets, calendar))
            with self.state_lock:
                self.store.save("health", "last", dict(self.status))
            return {"ok": True, **observed}
        except Exception as exc:
            # Source observation is distinct from the last Calendar cycle. It
            # must not hide an existing sync failure or kill hourly retries.
            self._status(source_observation_api=self._api_metrics(sheets, calendar))
            if getattr(exc, "code", "") == "paused":
                with self.state_lock:
                    self.store.save("health", "last", dict(self.status))
                return {"ok": False, "paused": True}
            self.next_observation = time.monotonic() + 60
            message = "刀表觀察暫未完成；稍後重試，日曆同步結果另外顯示。"
            if getattr(exc, "code", "") == "usage_budget":
                message = "刀表觀察已達本機 API 用量保護上限；保留觀察紀錄，額度釋出後重試。"
            self._status(source_observation_error=message)
            with self.state_lock:
                self.store.save("health", "last", dict(self.status))
            return {"ok": False}
        finally:
            self.busy.release()

    @staticmethod
    def _api_metrics(sheets, calendar):
        return {label: {name: getattr(client.http, name, 0) for name in (
            "requests", "retries", "quota_units", "read_requests", "write_requests")}
                for label, client in (("sheets", sheets), ("calendar", calendar)) if hasattr(client, "http")}

    def _loop(self):
        while not self.stop.wait(10):
            try:
                config = self.store.get("config", "settings")
                if config["enabled"] and config["next_check"] <= timestamp():
                    self.run_once()
                elif config["enabled"] and time.monotonic() >= self.next_observation:
                    self.observe_once()
            except Exception:
                # A local storage fault must not silently kill the only worker.
                self._status(running=False, failure_code="storage", message="無法保存監控狀態，背景程序仍在；請檢查本機資料目錄。")

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        with self.busy:
            self.store.close()
