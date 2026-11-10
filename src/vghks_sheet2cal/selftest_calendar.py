"""Synthetic-only verification shipped in the independent monitor package."""
from __future__ import annotations

import copy
import json
import re
import tempfile
import threading
from datetime import date
from pathlib import Path

from .calendar_client import CalendarError, CalendarUncertain
from .calendar_monitor import CalendarMonitor
from .calendar_sync import scan
from .google_sheets import SheetError

MONTH_HEADERS = ["日期／報到時間", "醫院", "病歷號", "姓名", "TEL", "GA", "側別", "診斷",
                 "Grade", "術式", "IOL", "IOL Target", "IOL Final", "Axis", "Plan", "心得",
                 "CalendarEventId", "ScheduleSyncId"]
FU_HEADERS = ["病歷號", "姓名", "TEL", "醫院", "Tag", "Condition", "日期", "Plan", "心得",
              "CalendarEventId", "ScheduleSyncId"]


def cell(value):
    return {} if value is None or value == "" else {"userEnteredValue": {
        "numberValue" if isinstance(value, (int, float)) else "stringValue": value}}


def row(labels, **values):
    return [cell(values.get(label, "")) for label in labels]


def monthly(title="202610", sid=1, *, ids=True):
    labels = MONTH_HEADERS if ids else MONTH_HEADERS[:-2]
    return {"properties": {"title": title, "sheetId": sid, "gridProperties": {"rowCount": 100, "columnCount": len(labels)}},
            "rows": [list(map(cell, labels)),
                     row(labels, **{"日期／報到時間": "2026-10-12", "醫院": "高榮", "病歷號": "◆ 刀日", "ScheduleSyncId": "a" * 32}),
                     row(labels, **{"日期／報到時間": "08:30", "醫院": "聯醫", "病歷號": "00001", "姓名": "合成甲", "側別": "OD", "術式": "CATA", "ScheduleSyncId": "b" * 32}),
                     row(labels, **{"日期／報到時間": "2026-10-12", "醫院": "聯醫", "病歷號": "◆ 刀日", "ScheduleSyncId": "c" * 32}),
                     row(labels, **{"病歷號": "00002", "姓名": "合成乙", "ScheduleSyncId": "d" * 32})],
            "column_widths": [], "protectedRanges": []}


def workbook(*sheets):
    return {"properties": {"title": "Synthetic only"}, "sheets": list(sheets or [monthly()]), "developerMetadata": []}


class MemorySheets:
    def __init__(self, book):
        self.book = copy.deepcopy(book)
        self.writes = []
        self.lock = threading.RLock()

    def read_all(self):
        with self.lock:
            return copy.deepcopy(self.book)

    def read_metadata(self):
        with self.lock:
            return {"developerMetadata": copy.deepcopy(self.book.get("developerMetadata", []))}

    def search_developer_metadata(self, key):
        with self.lock:
            return [copy.deepcopy(item) for item in self.book.get("developerMetadata", [])
                    if item.get("metadataKey") == key and item.get("visibility") == "DOCUMENT"]

    @staticmethod
    def _metadata_matches(item, lookup):
        return all((item.get("location", {}).get("locationType") if key == "locationType" else item.get(key)) == value
                   for key, value in lookup.items())

    def write_filtered_values(self, body):
        with self.lock:
            count = 0
            for value_range in body["data"]:
                lookup = value_range["dataFilter"]["developerMetadataLookup"]
                for metadata in self.book.get("developerMetadata", []):
                    if not self._metadata_matches(metadata, lookup):
                        continue
                    bounds = metadata["location"]["dimensionRange"]
                    row = bounds["startIndex"]
                    for col, value in enumerate(value_range["values"][0]):
                        if value is None:
                            continue
                        self._write([{"updateCells": {"range": {"sheetId": bounds["sheetId"],
                            "startRowIndex": row, "endRowIndex": row + 1, "startColumnIndex": col,
                            "endColumnIndex": col + 1}, "rows": [{"values": [cell(value)]}], "fields": "userEnteredValue"}}])
                        count += 1
            return {"totalUpdatedCells": count}

    def write(self, requests):
        with self.lock:
            return self._write(requests)

    def _write(self, requests):
        ids = {item["metadataId"] for item in self.book.get("developerMetadata", [])}
        for request in requests:
            metadata = request.get("createDeveloperMetadata", {}).get("developerMetadata", {})
            if metadata.get("metadataId") in ids:
                raise SheetError("Synthetic duplicate metadata ID")
            if metadata.get("metadataId"):
                ids.add(metadata["metadataId"])
        self.writes.extend(copy.deepcopy(requests))
        for request in requests:
            kind, body = next(iter(request.items()))
            if kind == "createDeveloperMetadata":
                new = copy.deepcopy(body["developerMetadata"])
                available = 1
                while available in ids:
                    available += 1
                new.setdefault("metadataId", available)
                ids.add(new["metadataId"])
                if new.get("location", {}).get("dimensionRange", {}).get("dimension") == "ROWS":
                    new["location"]["locationType"] = "ROW"
                self.book["developerMetadata"].append(new)
                continue
            if kind == "updateDeveloperMetadata":
                self.book["developerMetadata"][0].update(body["developerMetadata"])
                continue
            if kind == "deleteDeveloperMetadata":
                lookup = body["dataFilter"]["developerMetadataLookup"]
                self.book["developerMetadata"] = [item for item in self.book["developerMetadata"]
                                                  if not self._metadata_matches(item, lookup)]
                continue
            target = body.get("range", body)
            if kind == "addProtectedRange":
                target = body["protectedRange"]["range"]
            sheet = next(s for s in self.book["sheets"] if s["properties"]["sheetId"] == target["sheetId"])
            if kind == "updateCells":
                for offset, data in enumerate(body["rows"]):
                    index = target["startRowIndex"] + offset
                    while len(sheet["rows"]) <= index:
                        sheet["rows"].append([])
                    for delta, entered in enumerate(data["values"]):
                        col = target["startColumnIndex"] + delta
                        while len(sheet["rows"][index]) <= col:
                            sheet["rows"][index].append({})
                        sheet["rows"][index][col].update(copy.deepcopy(entered))
            elif kind in {"repeatCell", "findReplace"}:
                for index in range(target["startRowIndex"], target["endRowIndex"]):
                    while len(sheet["rows"]) <= index:
                        sheet["rows"].append([])
                    for col in range(target["startColumnIndex"], target["endColumnIndex"]):
                        while len(sheet["rows"][index]) <= col:
                            sheet["rows"][index].append({})
                        current = sheet["rows"][index][col]
                        if kind == "repeatCell":
                            assert body["fields"] == "userEnteredFormat.numberFormat"
                            current.setdefault("userEnteredFormat", {})["numberFormat"] = copy.deepcopy(
                                body["cell"]["userEnteredFormat"]["numberFormat"])
                        elif "formulaValue" not in current.get("userEnteredValue", {}):
                            value = current.get("userEnteredValue", {}).get("stringValue", "")
                            if re.fullmatch(body["find"], value):
                                replacement = re.sub(r"\\(.)", r"\1", body["replacement"])
                                current["userEnteredValue"] = {"stringValue": replacement}
                                current.pop("effectiveValue", None)
                                current.pop("formattedValue", None)
            elif kind == "appendDimension":
                sheet["properties"]["gridProperties"]["columnCount"] += body["length"]
            elif kind == "updateDimensionProperties":
                for col in range(target["startIndex"], target["endIndex"]):
                    while len(sheet["column_widths"]) <= col:
                        sheet["column_widths"].append({})
                    sheet["column_widths"][col].update(body["properties"])
            elif kind == "addProtectedRange":
                sheet.setdefault("protectedRanges", []).append(copy.deepcopy(body["protectedRange"]))
            else:
                raise AssertionError("Unsupported synthetic request: " + kind)


class MemoryCalendar:
    def __init__(self):
        self.events = {}
        self.inserts = self.updates = self.deletes = 0
        self.timeout_insert = False

    def probe(self):
        return {"ok": True}

    def get(self, eid):
        if "@" in eid:
            return next((copy.deepcopy(e) for e in self.events.values() if e.get("iCalUID") == eid), None)
        return copy.deepcopy(self.events.get(eid))

    def insert(self, eid, body):
        if eid not in self.events:
            self.events[eid] = {"id": eid, **copy.deepcopy(body)}
            self.inserts += 1
        if self.timeout_insert:
            self.timeout_insert = False
            raise CalendarUncertain("synthetic uncertain insert")
        return self.get(eid)

    def update(self, eid, body, *, etag=None):
        if etag and self.events[eid].get("etag") != etag:
            raise CalendarError("Synthetic event version changed", code="event_changed", retryable=True)
        self.updates += 1
        self.events[eid].update(copy.deepcopy(body))
        if etag:
            self.events[eid]["etag"] = f'"synthetic-{self.updates}"'
        return self.get(eid)

    def delete(self, eid, *, etag=None):
        if etag and self.events.get(eid, {}).get("etag") != etag:
            raise CalendarError("Synthetic event version changed", code="event_changed", retryable=True)
        self.deletes += 1
        self.events.pop(eid, None)


def prepared(directory, book=None):
    sheets, calendar = MemorySheets(book or workbook()), MemoryCalendar()
    monitor = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                              today=lambda: date(2026, 10, 1))
    monitor.configure({"spreadsheet_id": "synthetic-spreadsheet-00000000000", "calendar_id": "synthetic-calendar"})
    try:
        monitor.prepare()
    except Exception:
        monitor.close()
        raise
    return monitor, sheets, calendar


def checks(directory):
    monitor, sheets, calendar = prepared(directory, workbook(monthly(ids=False)))
    try:
        assert monitor.run_once()["ok"]
        assert calendar.inserts == 2  # Date headers never become events.
        patients = scan(sheets.book)["patients"]
        assert patients[0]["hospital"] == "高榮" and patients[1]["hospital"] == "聯醫"
        assert "醫院: 高榮" in calendar.events[patients[0]["event_id"]]["description"]
        assert monitor.run_once()["ok"] and calendar.inserts == 2 and calendar.updates == 0
        # An entire row carries its permanent identity to another month's header.
        old_id = patients[0]["event_id"]
        moved = sheets.book["sheets"][0]["rows"].pop(2)
        target = monthly("202611", 2)
        target["rows"] = [copy.deepcopy(sheets.book["sheets"][0]["rows"][0]),
                          row(MONTH_HEADERS, **{"日期／報到時間": "2026-11-02", "醫院": "聯醫", "病歷號": "◆ 刀日", "ScheduleSyncId": "e" * 32}), moved]
        target["properties"]["gridProperties"]["columnCount"] = len(target["rows"][0])
        sheets.book["sheets"].append(target)
        assert monitor.run_once()["ok"] and calendar.inserts == 2
        assert calendar.events[old_id]["start"]["dateTime"].startswith("2026-11-02")
        assert "醫院: 聯醫" in calendar.events[old_id]["description"]
        # Two complete successful scans confirm one row deletion.
        target["rows"].pop()
        assert monitor.run_once()["ok"] and calendar.deletes == 0
        assert monitor.run_once()["ok"] and calendar.deletes == 1
        # Whole-page disappearance is held on every later scan.
        sheets.book["sheets"].pop(0)
        for _ in range(3):
            assert monitor.run_once()["ok"]
        assert calendar.deletes == 1
        return ["automatic-system-column-setup", "header-date-and-hospital", "headers-do-not-create-events",
                "idempotent-full-scan", "full-row-cross-month-stable-event", "two-scan-row-deletion", "absent-sheet-deletion-guard"]
    finally:
        monitor.close()


def self_test(path):
    result = {"ok": False, "mode": "synthetic-only", "checks": []}
    try:
        with tempfile.TemporaryDirectory() as directory:
            result["checks"] = checks(directory)
        result["ok"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
