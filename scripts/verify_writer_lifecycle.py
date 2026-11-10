"""Offline compatibility: execute the pinned main writer, then the real monitor.

Only synthetic cells and in-memory Google doubles are used. The small request
adapter rejects unsupported operations rather than substituting planner output
for the requests that Google would receive.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path
from unittest.mock import patch

from vghks_sheet_core.contract import CONTRACT_VERSION

from vghks_sheet2cal.calendar_monitor import CalendarMonitor
from vghks_sheet2cal.calendar_sync import matches, resource, scan
from vghks_sheet2cal.selftest_calendar import MONTH_HEADERS, MemoryCalendar, MemorySheets, cell, row, workbook

SPREADSHEET = "synthetic-writer-lifecycle-00000000001"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class WriterSheets(MemorySheets):
    """Apply the bounded Sheets requests emitted by these real writer cases."""

    def sheet(self, sid):
        return next(sheet for sheet in self.book["sheets"] if sheet["properties"]["sheetId"] == sid)

    @staticmethod
    def extend(sheet, index):
        while len(sheet["rows"]) <= index:
            sheet["rows"].append([])

    def _write(self, requests):
        original, writes = copy.deepcopy(self.book), len(self.writes)
        try:
            for request in requests:
                self._apply(request)
        except Exception:
            self.book = original
            del self.writes[writes:]
            raise

    def _apply(self, request):
        require(len(request) == 1, "Unexpected multi-operation synthetic request")
        kind, body = next(iter(request.items()))
        if kind == "addSheet":
            properties = copy.deepcopy(body["properties"])
            require(not any(sheet["properties"]["sheetId"] == properties["sheetId"]
                            or sheet["properties"]["title"] == properties["title"]
                            for sheet in self.book["sheets"]), "Writer produced a duplicate sheet")
            self.book["sheets"].append({"properties": properties, "rows": [], "merges": [],
                                         "column_widths": [], "protectedRanges": []})
        elif kind in {"insertDimension", "deleteDimension"}:
            bounds = body["range"]
            require(bounds["dimension"] == "ROWS", "Only full-row dimension operations are supported")
            sheet = self.sheet(bounds["sheetId"])
            start, end = bounds["startIndex"], bounds["endIndex"]
            require(0 <= start < end, "Invalid synthetic row bounds")
            self.extend(sheet, start)
            if kind == "insertDimension":
                sheet["rows"][start:start] = [[] for _ in range(end - start)]
            else:
                del sheet["rows"][start:end]
            sheet["properties"]["gridProperties"]["rowCount"] += (end - start) * (
                1 if kind == "insertDimension" else -1)
        elif kind == "cutPaste":
            source, destination = body["source"], body["destination"]
            before, after = self.sheet(source["sheetId"]), self.sheet(destination["sheetId"])
            require(body["pasteType"] == "PASTE_NORMAL"
                    and source.get("startColumnIndex", 0) == destination.get("columnIndex", 0) == 0
                    and source["endRowIndex"] == source["startRowIndex"] + 1
                    and source["endColumnIndex"] == before["properties"]["gridProperties"]["columnCount"],
                    "Only the writer's full-row normal paste is supported")
            self.extend(after, destination["rowIndex"])
            moved = copy.deepcopy(before["rows"][source["startRowIndex"]])
            before["rows"][source["startRowIndex"]] = []
            after["rows"][destination["rowIndex"]] = moved
        elif kind == "updateCells":
            bounds, fields = body["range"], body["fields"].split(",")
            require(set(fields) <= {"userEnteredValue", "userEnteredFormat", "dataValidation", "note", "textFormatRuns"},
                    "Unsupported synthetic cell field mask")
            sheet = self.sheet(bounds["sheetId"])
            for index, data in enumerate(body.get("rows", []), bounds["startRowIndex"]):
                self.extend(sheet, index)
                for column, value in enumerate(data["values"], bounds.get("startColumnIndex", 0)):
                    target = sheet["rows"][index]
                    while len(target) <= column:
                        target.append({})
                    for field in fields:
                        target[column].pop(field, None)
                        if field in value:
                            target[column][field] = copy.deepcopy(value[field])
                    if "userEnteredValue" in fields:
                        target[column].pop("formattedValue", None)
                        target[column].pop("effectiveValue", None)
        else:
            # Metadata ownership, hidden identity columns and protections are
            # already implemented by the monitor's shipped synthetic client.
            super()._write([request])
            return
        self.writes.append(copy.deepcopy(request))


def seed_book():
    return workbook({"properties": {"sheetId": 1, "title": "203011", "gridProperties": {
                         "rowCount": 100, "columnCount": len(MONTH_HEADERS)}},
                     "rows": [list(map(cell, MONTH_HEADERS)), row(MONTH_HEADERS, **{
                         "日期／報到時間": "2030-11-10", "醫院": "高榮", "病歷號": "◆ 刀日",
                         "ScheduleSyncId": "a" * 32})],
                     "column_widths": [], "protectedRanges": [], "merges": []})


def proposal(mrn, name, **overrides):
    values = {"date": "2030-11-10", "hospital": "高榮", "mrn": mrn, "name": name,
              "side": "OD", "procedure": "Phaco-IOL", "diagnosis": "CATA", "iol": "SYNTH-IOL",
              "target": "-0.50", "plan": "合成計畫"}
    values.update(overrides)
    return {"id": "synthetic-" + mrn, "fields": values, "source_records": [], "selected_fields": list(values)}


def patient(sheets, uid):
    snapshot = scan(sheets.read_all())
    require(not snapshot["issues"], "Writer output violates the monitor's structural contract")
    return next(item for item in snapshot["patients"] if item["uid"] == uid)


def edit(sheets, item, label, value):
    column = item["cols"][label]
    sheets.write([{"updateCells": {"range": {"sheetId": item["sheet_id"],
        "startRowIndex": item["row"], "endRowIndex": item["row"] + 1,
        "startColumnIndex": column, "endColumnIndex": column + 1},
        "rows": [{"values": [cell(value)]}], "fields": "userEnteredValue"}}])


def apply_writer(planner, sheets, verify):
    preview = planner.build()
    require(preview["can_apply"], "The real main writer rejected the synthetic producer case")
    sheets.write(preview["requests"])
    require(verify(sheets.read_all(), preview) == "verified", "Main writer request readback failed")
    return preview


def sync(monitor):
    result = monitor.run_once()
    require(result["ok"], "Monitor rejected the real writer lifecycle: " + result["message"])
    return result


def check_event(sheets, calendar, uid, eid):
    item = patient(sheets, uid)
    require(item["event_id"] == eid and matches(calendar.events.get(eid), resource(item, SPREADSHEET)),
            "Calendar event identity or rendered fields differ from the writer's output")
    return item


def exercise(bot_repo):
    project = tomllib.loads((bot_repo / "pyproject.toml").read_text())
    compatible = json.loads((Path(__file__).resolve().parents[1] / "compatibility.json").read_text())
    require(CONTRACT_VERSION == compatible["contract_version"], "Installed shared core version is incompatible")
    pins = [re.search(r"@([a-f0-9]{40})#", dependency) for dependency in project["project"]["dependencies"]
            if dependency.startswith("vghks-sheet-core ")]
    require(len(pins) == 1 and pins[0] and pins[0][1] == compatible["gas_revision"],
            "Main writer and monitor must pin the same shared core revision")
    sys.path.insert(0, str(bot_repo))
    import vghks_bot
    from vghks_bot.sheet_plan import FUPlanner, Planner, verify

    require(Path(vghks_bot.__file__).resolve().parent == bot_repo / "vghks_bot",
            "The requested main checkout was shadowed by another installation")
    checks = []
    with tempfile.TemporaryDirectory(prefix="writer-calendar-lifecycle-") as directory:
        sheets, calendar = WriterSheets(seed_book()), MemoryCalendar()
        monitor = CalendarMonitor(Path(directory) / "state", clients=lambda _: (sheets, calendar),
                                  background=False, today=lambda: date(2030, 11, 10), host_id="synthetic-host")
        try:
            monitor.configure({"spreadsheet_id": SPREADSHEET, "calendar_id": "synthetic-calendar"})
            monitor.prepare()
            first, healthy = proposal("00001", "合成甲"), proposal("00002", "合成乙")
            preview = apply_writer(Planner(sheets.read_all(), SPREADSHEET, [first, healthy]), sheets, verify)
            uid, healthy_uid = [item["sync_id"] for item in preview["items"]]
            for expected, produced_uid in ((first, uid), (healthy, healthy_uid)):
                fields = patient(sheets, produced_uid)["fields"]
                require(all(fields[label] == expected["fields"][key] for key, label in (
                    ("mrn", "病歷號"), ("name", "姓名"), ("side", "側別"), ("procedure", "術式"),
                    ("diagnosis", "診斷"), ("iol", "IOL"), ("target", "IOL Target"), ("plan", "Plan"))),
                    "Main writer values landed in the wrong clinical columns")
            edit(sheets, patient(sheets, uid), "日期／報到時間", "08:30")
            require(sync(monitor)["updated"] == 2 and calendar.inserts == 2, "Writer inserts must create exactly two events")
            eid, healthy_eid = patient(sheets, uid)["event_id"], patient(sheets, healthy_uid)["event_id"]
            update = proposal("00001", "合成甲", iol="SYNTH-IOL-UPDATED", plan="合成更新")
            update.update(target_sync_id=uid, selected_fields=["iol", "plan"])
            apply_writer(Planner(sheets.read_all(), SPREADSHEET, [update]), sheets, verify)
            require(sync(monitor)["updated"] == 1, "Writer field update must update the original Calendar event")
            item = check_event(sheets, calendar, uid, eid)
            require(item["fields"]["IOL"] == "SYNTH-IOL-UPDATED" and item["fields"]["Plan"] == "合成更新",
                    "Main writer updates must reach the agreed IOL and Plan columns")
            require(calendar.inserts == 2 and sync(monitor)["updated"] == 0, "Writer updates must be idempotent")
            checks.append("main-planner-insert-and-field-update-to-calendar")

            move = copy.deepcopy(update)
            move["destination"] = {"date": "2030-12-02", "hospital": "聯醫"}
            moved = apply_writer(Planner(sheets.read_all(), SPREADSHEET, [move]), sheets, verify)
            require(moved["items"][0]["action"] == "move", "Case must execute the real main row-move planner")
            require(sync(monitor)["updated"] == 1, "A stable-ID move must update one original event")
            item = check_event(sheets, calendar, uid, eid)
            require(item["sheet"] == "203012" and item["date"] == "2030-12-02" and item["hospital"] == "聯醫",
                    "Main move must preserve the requested month, date and campus")
            require(item["time"] == "08:30" and not item["fields"]["醫院"] and calendar.inserts == 2,
                    "Whole-row move must preserve arrival time, blank patient campus and original event")
            checks.append("main-planner-cross-month-campus-move-preserves-ids")

            sheets.write([{"deleteDimension": {"range": {"sheetId": item["sheet_id"], "dimension": "ROWS",
                "startIndex": item["row"], "endIndex": item["row"] + 1}}}])
            # Simulate an ordinary user-entered new row while the actual main
            # writer updates an unrelated, already-bound patient in the sheet.
            november = next(sheet for sheet in sheets.book["sheets"] if sheet["properties"]["title"] == "203011")
            index = len(november["rows"])
            manual = row(MONTH_HEADERS, **{"病歷號": "00003", "姓名": "合成丙", "術式": "Phaco-IOL", "側別": "OS"})
            sheets.write([{"updateCells": {"range": {"sheetId": november["properties"]["sheetId"],
                "startRowIndex": index, "endRowIndex": index + 1, "startColumnIndex": 0,
                "endColumnIndex": len(MONTH_HEADERS)}, "rows": [{"values": manual}], "fields": "userEnteredValue"}}])
            healthy.update(target_sync_id=healthy_uid, selected_fields=["plan"])
            healthy["fields"]["plan"] = "另一位合成更新"
            apply_writer(Planner(sheets.read_all(), SPREADSHEET, [healthy]), sheets, verify)
            require(monitor.inspect()["ok"], "Preflight must accept an independent deletion and new row")
            require(sync(monitor)["updated"] == 2, "New unidentified row must not block the main writer's healthy update")
            require(eid in calendar.events and calendar.inserts == 3 and calendar.deletes == 0,
                    "First successful round must retain the removed event while creating the new event once")
            require(check_event(sheets, calendar, healthy_uid, healthy_eid)["fields"]["Plan"] == "另一位合成更新",
                    "Independent healthy writer update was not applied to Plan")
            require(sync(monitor)["deleted"] == 1 and eid not in calendar.events,
                    "Second full successful scan must delete only the removed future event")
            require(sync(monitor)["updated"] == 0 and calendar.inserts == 3 and calendar.deletes == 1,
                    "Delete plus new-row lifecycle must remain idempotent")
            checks.append("main-update-with-independent-deletion-and-unidentified-new-row")

            draft = {"id": "synthetic-fu", "fields": {"mrn": "00004", "name": "合成追蹤",
                                                       "hospital": "高榮", "tag": "FU"}}
            fu = apply_writer(FUPlanner(sheets.read_all(), SPREADSHEET, [draft]), sheets, verify)
            fu_uid = fu["items"][0]["sync_id"]
            fu_item = patient(sheets, fu_uid)
            require([fu_item["fields"][label] for label in ("病歷號", "姓名", "醫院", "Tag")]
                    == ["00004", "合成追蹤", "高榮", "FU"], "Main FU writer values landed in the wrong columns")
            require(sync(monitor)["updated"] == 0 and calendar.inserts == 3, "Undated main FU drafts must not create Calendar events")
            edit(sheets, patient(sheets, fu_uid), "日期", "2030-11-26")
            edit(sheets, patient(sheets, fu_uid), "Condition", "合成追蹤條件")
            before = copy.deepcopy(patient(sheets, fu_uid))
            fu = apply_writer(FUPlanner(sheets.read_all(), SPREADSHEET, [draft]), sheets, verify)
            require(fu["items"][0]["action"] == "keep" and patient(sheets, fu_uid) == before,
                    "Repeated main FU writer must preserve manual dates, details and stable UID")
            require(sync(monitor)["updated"] == 1 and calendar.inserts == 4, "Dated FU must create exactly one event")
            fu_eid = patient(sheets, fu_uid)["event_id"]
            apply_writer(FUPlanner(sheets.read_all(), SPREADSHEET, [draft]), sheets, verify)
            require(sync(monitor)["updated"] == 0 and calendar.inserts == 4, "Repeated FU producer must preserve Calendar binding")
            check_event(sheets, calendar, fu_uid, fu_eid)
            checks.append("main-fu-planner-draft-manual-date-and-idempotent-calendar-binding")
        finally:
            monitor.close()
    return {"ok": True, "mode": "synthetic-only", "bot_version": project["project"]["version"],
            "contract_version": CONTRACT_VERSION, "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-repo", type=Path, required=True)
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="writer-calendar-locks-") as locks, \
                patch.dict(os.environ, {"VGHKS_SHEET_LOCK_DIR": locks}), \
                patch("socket.socket.connect", side_effect=AssertionError("Network access is forbidden in this synthetic check")), \
                patch("socket.socket.connect_ex", side_effect=AssertionError("Network access is forbidden in this synthetic check")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS access is forbidden in this synthetic check")), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP access is forbidden in this synthetic check")):
            result = exercise(args.bot_repo.resolve())
    except Exception as exc:
        print(json.dumps({"ok": False, "mode": "synthetic-only", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
