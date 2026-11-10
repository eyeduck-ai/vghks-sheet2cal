"""Private source observations, separate from Calendar confirmation and identity repair.

Only complete workbook reads belong here, including reads with schema or row
diagnostics. The journal records observed net changes, never guesses intermediate
edits or matches patients by clinical values. Public summaries contain no values.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter

from vghks_sheet_core.common import digest
from vghks_sheet_core.schema import (
    DATE_HEADER,
    HOSPITALS,
    cancelled_cell,
    canonical_header,
    cell_text,
    full_date,
    monthly_row,
)

from .calendar_sync import sheet_kind
from .monitor_policy import CHANGE_RETENTION, OBSERVATION_RETENTION

UID = re.compile(r"[a-f0-9]{32}\Z")
FIELDS = {
    DATE_HEADER: "time", "醫院": "hospital", "病歷號": "mrn", "姓名": "name", "TEL": "tel",
    "GA": "ga", "側別": "side", "診斷": "diagnosis", "Grade": "grade", "術式": "procedure",
    "IOL": "iol", "IOL Target": "iol_target", "IOL Final": "iol_final", "Axis": "axis", "Plan": "plan",
    "心得": "memo", "SN": "sn", "CDE": "cde", "Energy Time": "energy_time", "Energy %": "energy_percent",
    "屈光數據": "refraction", "Tag": "tag", "Condition": "condition", "日期": "date",
    "CalendarEventId": "event_id", "ScheduleSyncId": "uid", "ArchiveRowType": "archive_row_type",
}
CELL_FIELDS = frozenset({
    "userEnteredValue", "effectiveValue", "formattedValue", "note", "textFormatRuns",
    "userEnteredFormat", "effectiveFormat", "dataValidation",
})
TECHNICAL = frozenset({"uid", "event_id"})


def _pack(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cell(cells, cols, label):
    index = cols.get(label)
    return cells[index] if index is not None and index < len(cells) else {}


def _text(cells, cols, label):
    return cell_text(_cell(cells, cols, label)).strip()


def _content(cell):
    value = cell.get("userEnteredValue", {})
    effective = cell.get("effectiveValue", {})
    return bool(value and value != {"stringValue": ""} or effective and effective != {"stringValue": ""}
                or cell.get("formattedValue") or cell.get("note") or cell.get("textFormatRuns"))


def _field_values(item):
    return dict(zip(item["columns"], item["cells"], strict=True))


def _clinical(item):
    return {"fields": {key: value for key, value in _field_values(item).items()
                       if key.split(":", 1)[0] not in TECHNICAL},
            "kind": item["kind"], "date": item["date"], "hospital": item["hospital"]}


def _snapshot(book):
    if not isinstance(book, dict) or not isinstance(book.get("sheets"), list):
        raise ValueError("A complete workbook read is required for a source observation")
    rows, sheets, diagnostics = [], [], Counter()
    for sheet in book["sheets"]:
        kind = sheet_kind(sheet)
        if not kind:
            continue
        properties = sheet["properties"]
        sid, title = properties["sheetId"], properties["title"]
        source = sheet.get("rows", [])
        header = 5 if kind == "ARCHIVE" else 0
        labels = [canonical_header(cell) for cell in source[header]] if len(source) > header else []
        counts = Counter(label for label in labels if label)
        cols = {label: index for index, label in enumerate(labels) if label and counts[label] == 1}
        required = {"病歷號", "姓名", DATE_HEADER, "醫院"} if kind == "MONTHLY" else {
            "病歷號", "姓名", "日期" if kind == "FU" else "ArchiveRowType"}
        valid = not any(count > 1 for count in counts.values()) and not required - cols.keys()
        if any(count > 1 for count in counts.values()):
            diagnostics["duplicate_header"] += 1
        if required - cols.keys():
            diagnostics["missing_header"] += 1
        keys = [FIELDS.get(label, "custom") + (f":{index}" if label not in FIELDS or counts[label] > 1 else "")
                for index, label in enumerate(labels)]
        sheets.append({"sheet_id": sid, "sheet": title, "kind": kind, "header_row": header + 1})
        block = None
        for index, cells in enumerate(source):
            if index != header and not any(_content(cell) for cell in cells):
                continue
            columns = keys + [f"custom:{column}" for column in range(len(keys), len(cells))]
            raw = [{key: copy.deepcopy(value) for key, value in cell.items() if key in CELL_FIELDS} for cell in cells]
            raw.extend({} for _ in range(len(columns) - len(raw)))
            uid = _text(cells, cols, "ScheduleSyncId") if index > header else ""
            eid = _text(cells, cols, "CalendarEventId") if index > header else ""
            row_kind, day, hospital = "metadata" if index < header else "headers", "", ""
            if index > header:
                mrn, name = _text(cells, cols, "病歷號"), _text(cells, cols, "姓名")
                row_kind = "patient" if mrn or name else "orphan" if uid or eid else "other"
                if not valid:
                    row_kind = "unknown"
                elif kind == "MONTHLY":
                    parsed = monthly_row(cells, cols)
                    if parsed["kind"] == "day":
                        block = parsed
                        row_kind = "day"
                    elif parsed["kind"] == "invalid_header":
                        block = None
                        row_kind = "invalid_header"
                        diagnostics["invalid_day_header"] += 1
                    elif row_kind == "patient" and block is None:
                        diagnostics["missing_day_context"] += 1
                    if block:
                        day, hospital = block["date"], block["hospital"]
                elif kind == "FU":
                    day = full_date(_cell(cells, cols, "日期"))
                    hospital = _text(cells, cols, "醫院")
                    if _text(cells, cols, "日期") and not day:
                        diagnostics["invalid_fu_date"] += 1
                if row_kind == "orphan":
                    diagnostics["orphan_sync_id" if uid else "orphan_event_id"] += 1
                if uid and not UID.fullmatch(uid):
                    diagnostics["invalid_sync_id"] += 1
                if row_kind in {"patient", "day"} and not uid:
                    diagnostics["missing_sync_id"] += 1
            rows.append({"sheet_id": sid, "sheet": title, "row": index + 1, "kind": row_kind,
                         "uid": uid, "event_id": eid, "date": day,
                         "hospital": hospital if hospital in HOSPITALS else "",
                         "cancelled": cancelled_cell(_cell(cells, cols, "病歷號")) if index > header else False,
                         "columns": columns, "cells": raw})
    uid_counts = Counter(item["uid"] for item in rows if item["uid"])
    eid_counts = Counter(item["event_id"] for item in rows if item["event_id"])
    diagnostics["duplicate_sync_id"] = sum(count for count in uid_counts.values() if count > 1)
    diagnostics["duplicate_event_id"] = sum(count for count in eid_counts.values() if count > 1)
    for item in rows:
        uid = item["uid"]
        item["key"] = ("uid:" + uid if UID.fullmatch(uid) and uid_counts[uid] == 1
                       else f"position:{item['sheet_id']}:{item['row']}")
    return {"sheets": sheets, "rows": rows,
            "diagnostic_counts": {key: value for key, value in sorted(diagnostics.items()) if value}}


def _changed_fields(before, after):
    previous, current = _field_values(before), _field_values(after)
    fields = {key.split(":", 1)[0] for key in previous.keys() | current.keys()
              if previous.get(key, {}) != current.get(key, {})}
    for field in ("date", "hospital", "cancelled"):
        if before[field] != after[field]:
            fields.add(field)
    if before["kind"] != after["kind"]:
        fields.add("row_kind")
    if before["sheet_id"] != after["sheet_id"]:
        fields.add("sheet")
    if before["row"] != after["row"]:
        fields.add("row")
    if (before["kind"] == "headers" or after["kind"] == "headers") and fields:
        fields.add("schema")
    return sorted(fields)


def _delta(before, after):
    old = {row["key"]: row for row in before["rows"]}
    new = {row["key"]: row for row in after["rows"]}
    pairs = [(old.pop(key), new.pop(key)) for key in sorted(old.keys() & new.keys())]
    # Recognize only the worker's technical assignment in an unchanged physical
    # row. This never pairs a deleted identified schedule with a new patient.
    old_uids = {row["uid"] for row in before["rows"] if row["uid"]}
    for key, row in list(new.items()):
        if not key.startswith("uid:") or row["uid"] in old_uids:
            continue
        previous_key = f"position:{row['sheet_id']}:{row['row']}"
        previous = old.get(previous_key)
        if previous and not previous["uid"] and _clinical(previous) == _clinical(row):
            pairs.append((old.pop(previous_key), new.pop(key)))
    changes = []
    for previous, current in pairs:
        fields = _changed_fields(previous, current)
        if not fields:
            continue
        moved = any(previous[key] != current[key] for key in ("sheet_id", "row", "date", "hospital"))
        changes.append(("moved" if moved else "modified", previous, current, fields))
    changes.extend(("removed", previous, None, []) for previous in old.values())
    changes.extend(("added", None, current, []) for current in new.values())
    return changes


def _public_change(kind, before, after, fields, at, revision, phase):
    location = after or before
    return {"at": at, "revision": revision, "phase": phase, "kind": kind,
            "sheet": location["sheet"], "row": location["row"],
            "previous_sheet": before["sheet"] if before else "", "previous_row": before["row"] if before else None,
            "date": after["date"] if after else "", "previous_date": before["date"] if before else "",
            "hospital": after["hospital"] if after else "", "previous_hospital": before["hospital"] if before else "",
            "fields": fields}


class SourceJournal:
    def __init__(self, store, *, max_observations=OBSERVATION_RETENTION, max_changes=CHANGE_RETENTION):
        if type(max_observations) is not int or max_observations < 1 or type(max_changes) is not int or max_changes < 1:
            raise ValueError("Source journal retention must be a positive integer")
        self.store, self.max_observations, self.max_changes = store, max_observations, max_changes
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS source_observations (revision INTEGER PRIMARY KEY AUTOINCREMENT,"
                       "source_id TEXT NOT NULL,at TEXT NOT NULL,phase TEXT NOT NULL,summary TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS source_snapshots (source_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,"
                       "at TEXT NOT NULL,phase TEXT NOT NULL,fingerprint TEXT NOT NULL,payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS source_changes (sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                       "source_id TEXT NOT NULL,revision INTEGER NOT NULL,before_json TEXT,after_json TEXT,public_json TEXT NOT NULL)")

    def _source_id(self):
        return (self.store.get("config", "settings") or {}).get("spreadsheet_id", "")

    def latest(self):
        """Private complete observation; never return this through a public API."""
        with self.store.connect() as db:
            found = db.execute("SELECT revision,at,phase,payload FROM source_snapshots WHERE source_id=?",
                               (self._source_id(),)).fetchone()
        return {"revision": found[0], "at": found[1], "phase": found[2], **json.loads(found[3])} if found else None

    def observe(self, book, at, *, phase="source"):
        if not isinstance(at, str) or not at or not isinstance(phase, str) or not re.fullmatch(r"[a-z_]{1,40}", phase):
            raise ValueError("Source observation needs a timestamp and a bounded phase name")
        current = _snapshot(book)
        fingerprint, source_id = digest(current), self._source_id()
        with self.store.connect() as db:
            found = db.execute("SELECT revision,fingerprint,payload FROM source_snapshots WHERE source_id=?",
                               (source_id,)).fetchone()
            unchanged = bool(found and found[1] == fingerprint)
            changes = [] if not found or unchanged else _delta(json.loads(found[2]), current)
            counts = Counter(kind for kind, *_ in changes)
            summary = {"revision": found[0] if found else None, "at": at, "phase": phase,
                       "baseline": found is None, "unchanged": unchanged, "changed": len(changes),
                       **{kind: counts[kind] for kind in ("added", "moved", "modified", "removed")},
                       "row_count": len(current["rows"]), "diagnostic_counts": current["diagnostic_counts"],
                       "source_read_complete": True, "calendar_confirmed": False}
            if unchanged:
                db.execute("UPDATE source_snapshots SET at=?,phase=? WHERE source_id=?", (at, phase, source_id))
                return summary
            cursor = db.execute("INSERT INTO source_observations(source_id,at,phase,summary) VALUES(?,?,?,?)",
                                (source_id, at, phase, "{}"))
            revision = cursor.lastrowid
            summary["revision"] = revision
            db.execute("UPDATE source_observations SET summary=? WHERE revision=?", (_pack(summary), revision))
            for kind, previous, after, fields in changes:
                public = _public_change(kind, previous, after, fields, at, revision, phase)
                db.execute("INSERT INTO source_changes(source_id,revision,before_json,after_json,public_json) VALUES(?,?,?,?,?)",
                           (source_id, revision, _pack(previous) if previous is not None else None,
                            _pack(after) if after is not None else None, _pack(public)))
            db.execute("INSERT OR REPLACE INTO source_snapshots VALUES(?,?,?,?,?,?)",
                       (source_id, revision, at, phase, fingerprint, _pack(current)))
            db.execute("DELETE FROM source_observations WHERE revision NOT IN "
                       "(SELECT revision FROM source_observations ORDER BY revision DESC LIMIT ?)", (self.max_observations,))
            db.execute("DELETE FROM source_changes WHERE revision NOT IN (SELECT revision FROM source_observations)")
            db.execute("DELETE FROM source_changes WHERE sequence NOT IN "
                       "(SELECT sequence FROM source_changes ORDER BY sequence DESC LIMIT ?)", (self.max_changes,))
            db.execute("DELETE FROM source_snapshots WHERE source_id NOT IN (SELECT source_id FROM source_observations)")
            return summary

    def recent(self, limit=20):
        """Bounded, value-free row facts suitable for authenticated status output."""
        if type(limit) is not int or limit < 1:
            raise ValueError("Source journal limit must be a positive integer")
        with self.store.connect() as db:
            found = db.execute("SELECT public_json FROM source_changes WHERE source_id=? ORDER BY sequence DESC LIMIT ?",
                               (self._source_id(), min(limit, 200))).fetchall()
        return [json.loads(row[0]) for row in found]
