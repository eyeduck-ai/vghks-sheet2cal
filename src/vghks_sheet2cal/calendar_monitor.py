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

from .calendar_client import CalendarClient, CalendarError
from .calendar_sync import (
    OWNER_KEY,
    OWNER_METADATA_ID,
    digest,
    event_id,
    in_calendar_range,
    matches,
    resource,
    scan,
    schedule_in_calendar_range,
    schedule_requires_reconciliation,
)
from .google_sheets import GoogleSettings, SheetError, SheetsClient, spreadsheet_id
from .host_registration import current_host_id
from .locking import lock_file
from .portable_credentials import seal, unseal
from .settings import TAIPEI, timestamp

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/calendar.events"]


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
                    "worker_alive": self.thread.is_alive(), "platform": sys.platform, "history": self.store.recent("history")}

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
                changed["email"] = values["key"]["client_email"]
            changed.pop("autorun", None)
            if changed["enabled"] and not config["enabled"]:
                changed["next_check"] = timestamp()
            self.store.save("config", "settings", changed)
            return self.public()
        finally:
            self.busy.release()

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
        sheets = SheetsClient(config["spreadsheet_id"], AuthorizedSession(credentials))
        calendar = CalendarClient(config["calendar_id"], AuthorizedSession(credentials))
        try:
            yield sheets, calendar
        finally:
            sheets.close()
            calendar.close()

    def _migrate(self, sheets, book):
        from .sheet_archive_lock import assert_archive_available
        from .sheet_identity import plan_identity_setup

        assert_archive_available(book)
        before = scan(book)
        missing_old = [e for e in self.store.all("event") if e["uid"] not in before["identities"]
                       and e.get("status") != "deleted" and not e.get("frozen_past")
                       and in_calendar_range(e.get("resource"), self.today())]
        new_without_id = any(not p["uid"] for p in before["patients"]
                             if not p["date"] or schedule_in_calendar_range(p, self.today()))
        if missing_old and new_without_id:
            raise ValueError("同時發現原排程消失與未識別病人列，疑似只搬移可見欄位；請保留原事件並核對 ID。")
        plan = plan_identity_setup(book, include_archives=True)
        if plan["issues"]:
            raise ValueError("；".join(i.get("reason") or f"{i.get('sheet', '')} {i['code']}" for i in plan["issues"][:8]))
        expected = scan(plan["book"])
        if expected["issues"]:
            raise ValueError("；".join(expected["issues"][:8]))
        if missing_old and any(p["uid"] not in before["identities"] for p in expected["patients"]
                               if not p["date"] or schedule_in_calendar_range(p, self.today())):
            raise ValueError("舊排程識別消失且有新識別待配置，請核對是否僅搬移可見欄位；未變更刀表或日曆。")
        self._validate_bindings(plan["book"], expected)
        requests_ = plan["requests"]
        if not requests_:
            return book
        fresh = self._read_monitor(sheets)
        if fingerprint(fresh) != fingerprint(book):
            raise ValueError("刀表在準備期間有變動，請重新執行；未寫入。")
        self._assert_owner(sheets)
        sheets.write(requests_)
        result = self._read_monitor(sheets)
        actual = scan(result)
        if actual["issues"]:
            raise ValueError("系統欄回讀需核對：" + "；".join(actual["issues"][:6]))
        pairs = {p["uid"]: p["identity"] for p in actual["patients"]}
        if any(pairs.get(p["uid"]) != p["identity"] for p in expected["patients"]):
            raise ValueError("系統欄寫入時病人列已移動，請人工核對；未操作日曆。")
        self._validate_bindings(result, actual)
        return result

    def _validate_bindings(self, book, snapshot):
        from .sheet_sync_state import field_hash, read_sync_state
        shared = read_sync_state(book)
        if shared["issues"]:
            raise ValueError("主程式同步基準格式需核對，未變更刀表或日曆。")
        config = self.store.get("config", "settings")
        patient_ids = {item["uid"] for item in snapshot["patients"]}
        header_ids = snapshot["identities"] - patient_ids - snapshot["archive_ids"]
        historical_ids = {old["uid"] for old in self.store.all("event")
                          if old.get("status") != "pending"
                          and (old.get("frozen_past") or not in_calendar_range(old.get("resource"), self.today()))}
        bound_ids = {old["uid"] for old in self.store.all("event") if old.get("status") != "deleted"}
        bound_ids.update(uid for uid, state in shared["states"].items() if state.get("mrn_hash"))
        bound_ids -= historical_ids
        if header_ids & bound_ids:
            raise ValueError("已綁定患者的排程 ID 變成刀日標題，請核對列類型與識別；未變更刀表或日曆。")
        for item in snapshot["patients"]:
            old = self.store.get("event", item["uid"])
            if item["date"] and not schedule_requires_reconciliation(item, self.today(), old):
                continue
            self._binding(item, old, config)
            state = shared["states"].get(item["uid"], {})
            bound_mrn = state.get("mrn_hash") or state.get("fields", {}).get("mrn")
            if bound_mrn and bound_mrn != field_hash("mrn", item["mrn"]):
                raise ValueError(item["sheet"] + " 排程 ID 與主程式原病歷號綁定不符，未變更刀表或日曆。")

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

                preview = plan_identity_setup(book, include_archives=True)
                if preview["issues"]:
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
                book = self._migrate(sheets, book)
                if scan(book)["issues"]:
                    raise ValueError("；".join(scan(book)["issues"][:8]))
                self._assert_owner(sheets)
            config["setup_done"] = True
            self.store.save("config", "settings", config)
            self._status(message="準備完成。啟用後每小時同步；目前仍停用。", issues=[])
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
                plan = plan_identity_setup(book, include_archives=True)
                snapshot = scan(plan["book"], config["include_fu"])
                cutoff = self.today()
                today = cutoff.isoformat()
                return {"ok": not plan["issues"] and not snapshot["issues"] and (not owner or matched),
                        "issues": plan["issues"] + snapshot["issues"],
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
        if old and old["identity"] != item["identity"]:
            raise ValueError(item["sheet"] + " 排程 ID 對應病人已變更，疑似只搬移可見欄位；未更新日曆。")
        sheet_event = item["event_id"]
        if old and sheet_event and sheet_event not in {old["event_id"], old.get("legacy_event_id")}:
            raise ValueError(item["sheet"] + " 事件綁定與監控紀錄不一致，請核對。")
        return (old or {}).get("event_id") or sheet_event or event_id(config["spreadsheet_id"], item["uid"])

    @staticmethod
    def _read_monitor(sheets):
        reader = getattr(sheets, "read_for_monitor", None) or sheets.read_all
        return reader()

    def _write_bindings(self, sheets, bindings):
        # Re-locate permanent IDs immediately before each bounded batch. Never
        # reuse initial row numbers after Calendar requests or a concurrent move.
        for start in range(0, len(bindings), 100):
            self._checkpoint()
            batch = bindings[start:start + 100]
            current = scan(self._read_monitor(sheets))
            if current["issues"]:
                raise ValueError("回寫事件 ID 前刀表有衝突，事件已保存在監控紀錄，稍後重試。")
            indexed = {p["uid"]: p for p in current["patients"]}
            requests_ = []
            for item, eid in batch:
                target = indexed.get(item["uid"])
                if not target or target["identity"] != item["identity"]:
                    raise ValueError("回寫事件 ID 前病人列已搬移或變更，請重新核對。")
                if target["event_id"] == eid:
                    continue
                if target["event_id"] and target["event_id"] != item["event_id"]:
                    raise ValueError("既有事件 ID 已由其他操作變更，未覆寫。")
                col = target["cols"]["CalendarEventId"]
                requests_.append({"updateCells": {"range": {"sheetId": target["sheet_id"], "startRowIndex": target["row"],
                    "endRowIndex": target["row"] + 1, "startColumnIndex": col, "endColumnIndex": col + 1},
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": eid}}]}], "fields": "userEnteredValue"}})
            if requests_:
                self._assert_owner(sheets)
                sheets.write(requests_)
            verified = scan(self._read_monitor(sheets))
            indexed = {p["uid"]: p for p in verified["patients"]}
            if verified["issues"] or any(not indexed.get(item["uid"]) or indexed[item["uid"]]["event_id"] != eid
                    or indexed[item["uid"]]["identity"] != item["identity"] for item, eid in batch):
                raise ValueError("事件 ID 回讀不一致，請先核對刀表；監控已保留事件綁定。")

    def run_once(self):
        with self.state_lock:
            epoch = self.pause_epoch
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "busy": True}
        updated = deleted = past_skipped = protected_past = 0
        now = timestamp()
        started = time.monotonic()
        self.active_epoch = epoch
        config, result, metrics, sheets, calendar = {}, {"ok": False, "at": now}, {}, None, None
        self._status(running=True, message="正在讀取並核對刀表…", issues=[])
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
                book = self._migrate(sheets, book)
                snapshot = scan(book, config["include_fu"])
                if snapshot["issues"]:
                    raise ValueError("；".join(snapshot["issues"][:8]))
                previous = self.store.get("scan", "last")
                deletion_safe = bool(previous)
                # Any absent sheet disables deletion; archived IDs remain protective.
                # Presence anywhere protects an existing event, even if the
                # row's type changed and it is no longer an eligible patient.
                active = snapshot["identities"]
                bindings, completed = [], []
                for item in snapshot["patients"]:
                    self._checkpoint()
                    old = self.store.get("event", item["uid"])
                    if item["date"] and not schedule_requires_reconciliation(item, self.today(), old):
                        past_skipped += 1
                        continue
                    eid = self._binding(item, old, config)
                    expected = resource(item, config["spreadsheet_id"])
                    if expected is None:
                        if old or item["event_id"]:
                            raise ValueError(item["sheet"] + " 已綁定追蹤列清空日期，請人工確認；事件保留。")
                        continue
                    pending = {"uid": item["uid"], "identity": item["identity"], "event_id": eid,
                        "sheet_id": item["sheet_id"], "hash": digest(expected), "resource": expected,
                        "status": "pending", "missing": 0, "updated_at": now}
                    if old and old.get("legacy_event_id"):
                        pending["legacy_event_id"] = old["legacy_event_id"]
                    self.store.save("event", item["uid"], pending)
                    actual = calendar.get(eid)
                    # Finish an in-flight move into the past, but never create
                    # a past event if the original no longer exists.
                    if not schedule_in_calendar_range(item, self.today()) and actual is None:
                        self.store.save("event", item["uid"], {**pending, "status": "historical"})
                        continue
                    if "@" in eid and not actual:
                        raise ValueError("刀表的舊 iCalUID 查無事件，請人工核對；未以舊格式建立另一事件。")
                    if actual and "@" in eid:
                        pending["legacy_event_id"] = eid
                        eid = actual["id"]
                        pending["event_id"] = eid
                        self.store.save("event", item["uid"], pending)
                    private = (actual or {}).get("extendedProperties", {}).get("private", {})
                    if actual and private.get("vghksSyncId") and private["vghksSyncId"] != item["uid"]:
                        raise ValueError("日曆事件已綁定另一筆排程，未覆寫。")
                    if actual and private.get("vghksSource") and private["vghksSource"] != config["spreadsheet_id"]:
                        raise ValueError("日曆事件屬於另一份刀表，未覆寫。")
                    if actual and not item["event_id"] and not private.get("vghksSyncId"):
                        raise ValueError("固定日曆 ID 已被未識別事件使用，未覆寫。")
                    if actual and not private.get("vghksSyncId") and norm(actual.get("summary", "").split(" | ")[0]) != norm(item["mrn"]):
                        raise ValueError("既有日曆事件的病歷號與刀表不同，請先核對綁定。")
                    if not matches(actual, expected):
                        self._assert_owner(sheets)
                        result = calendar.update(eid, expected) if actual else calendar.insert(eid, expected)
                        if result is None:
                            raise CalendarError("日曆事件未能寫入，將於下一輪核對。")
                        if not matches(calendar.get(eid), expected):
                            raise CalendarError("日曆事件回讀與刀表預期不符，已保留待核對狀態。")
                        updated += 1
                    if item["event_id"] != eid:
                        bindings.append((item, eid))
                    completed.append((item["uid"], {**pending, "event_id": eid, "status": "ready"}))
                self._write_bindings(sheets, bindings)
                # Keep pending state until both Calendar and sheet bindings are
                # verified, so a failed binding write can resume even in the past.
                for uid, ready in completed:
                    self.store.save("event", uid, ready)
                self._checkpoint()
                # Re-read the complete workbook before considering any disappearance.
                final = scan(self._read_monitor(sheets), config["include_fu"])
                if final["issues"] or final["identities"] != snapshot["identities"]:
                    raise ValueError("同步期間刀表結構或識別有變動，本輪不刪除事件。")
                for old in self.store.all("event"):
                    self._checkpoint()
                    if old.get("frozen_past") or not in_calendar_range(old.get("resource"), self.today()):
                        continue
                    if old["uid"] in active:
                        if old.get("missing"):
                            self.store.save("event", old["uid"], {**old, "missing": 0})
                        continue
                    if old.get("status") == "deleted":
                        continue
                    prior = old.get("missing", 0) if self.last_scan_ok else 0
                    count = prior + 1 if deletion_safe and old["sheet_id"] in final["sheet_ids"] else 0
                    self.store.save("event", old["uid"], {**old, "missing": count})
                    if count < 2:
                        continue
                    actual = calendar.get(old["event_id"])
                    if actual and not in_calendar_range(actual, self.today()):
                        self.store.save("event", old["uid"], {**old, "resource": actual,
                            "status": "historical", "frozen_past": True, "missing": 0})
                        protected_past += 1
                        continue
                    private = (actual or {}).get("extendedProperties", {}).get("private", {})
                    if actual and (private.get("vghksSyncId") != old["uid"] or private.get("vghksSource") != config["spreadsheet_id"]):
                        raise ValueError("待刪事件無法確認擁有權，保留並要求人工核對。")
                    self.store.save("event", old["uid"], {**old, "missing": count, "status": "deleting"})
                    if (not in_calendar_range(old["resource"], self.today())
                            or actual and not in_calendar_range(actual, self.today())):
                        continue
                    self._assert_owner(sheets)
                    calendar.delete(old["event_id"])
                    if calendar.get(old["event_id"]) is not None:
                        raise CalendarError("刪除事件尚未確認，下輪會先核對。")
                    self.store.save("event", old["uid"], {**old, "missing": count, "status": "deleted"})
                    deleted += 1
                self.store.save("scan", "last", {"sheet_ids": sorted(snapshot["sheet_ids"]), "at": now})
            result = {"ok": True, "updated": updated, "deleted": deleted, "at": now,
                      "scope_from": self.today().isoformat(), "past_skipped": past_skipped,
                      "protected_past": protected_past,
                      "message": f"同步完成：更新 {updated} 筆，刪除 {deleted} 筆。"}
            if past_skipped:
                result["message"] += f"略過過去排程 {past_skipped} 筆。"
            if protected_past:
                result["message"] += f"保留已無排程且已過去或日期不明的事件 {protected_past} 筆。"
            self._status(**{k: result[k] for k in ("updated", "deleted", "message")}, issues=[])
            self.last_scan_ok = True
        except Exception as exc:
            self.last_scan_ok = False
            message = str(exc) if isinstance(exc, (ValueError, CalendarError, SheetError)) else "同步未完成，已保存待核對狀態；請檢查網路與設定。"
            result = {"ok": False, "at": now, "message": message, "updated": updated, "deleted": deleted,
                      "failure_code": getattr(exc, "code", "") or "conflict", "retryable": bool(getattr(exc, "retryable", False))}
            self._status(message=message, issues=[message], updated=updated, deleted=deleted)
            if result["failure_code"] == "host_conflict":
                config = self.store.get("config", "settings")
                config["enabled"] = False
                self.store.save("config", "settings", config)
                self._status(host_state="conflict")
            # Errors break consecutive-success deletion confirmation.
            for old in self.store.all("event"):
                if old.get("missing"):
                    self.store.save("event", old["uid"], {**old, "missing": 0})
        finally:
            try:
                metrics = {label: {"requests": client.http.requests, "retries": client.http.retries}
                           for label, client in (("sheets", sheets), ("calendar", calendar)) if hasattr(client, "http")}
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

    def _loop(self):
        while not self.stop.wait(10):
            try:
                config = self.store.get("config", "settings")
                if config["enabled"] and config["next_check"] <= timestamp():
                    self.run_once()
            except Exception:
                # A local storage fault must not silently kill the only worker.
                self._status(running=False, failure_code="storage", message="無法保存監控狀態，背景程序仍在；請檢查本機資料目錄。")

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        with self.busy:
            self.store.close()
