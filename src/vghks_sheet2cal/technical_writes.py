"""Conditional, narrow Sheets writes for monitor-owned technical cells.

Sheets has no cross-cell compare-and-swap.  These requests prevent a queued ID
write from replacing a different ID that reached the target cell meanwhile.
The caller must still verify the UID and patient anchor after writing: moving
another *blank* technical cell into the same address is not detectable by this
single-cell condition.
"""
from __future__ import annotations

import copy
import re

from vghks_sheet_core.identity import (
    EVENT_ID_ALIASES,
    EVENT_ID_HEADER,
    SYNC_ID_HEADER,
    UID_PATTERN,
    header_columns,
    schema_kind,
)


class TechnicalWriteError(ValueError):
    """The request cannot be proven to affect only a supported technical cell."""


def _reject():
    # Do not echo untrusted source/header contents into public diagnostics.
    raise TechnicalWriteError("系統欄更新無法安全核對，未送出；請重新讀取刀表後重試。")


def _literal_replacement(value):
    """Java Matcher.quoteReplacement, as required by FindReplaceRequest."""
    return value.replace("\\", "\\\\").replace("$", "\\$")


def _stored_text(cell):
    entered = cell.get("userEnteredValue", {})
    if not entered:
        return ""
    if set(entered) != {"stringValue"} or not isinstance(entered["stringValue"], str):
        _reject()
    return entered["stringValue"]


def _cell(sheet, row, col):
    rows = sheet.get("rows", [])
    return rows[row][col] if row < len(rows) and col < len(rows[row]) else {}


def _set_cell(sheet, row, col, value):
    rows = sheet.setdefault("rows", [])
    while len(rows) <= row:
        rows.append([])
    while len(rows[row]) <= col:
        rows[row].append({})
    rows[row][col]["userEnteredValue"] = {"stringValue": value}


def _one_cell(bounds, grid):
    if set(bounds) != {"sheetId", "startRowIndex", "endRowIndex", "startColumnIndex", "endColumnIndex"}:
        _reject()
    row, col = bounds["startRowIndex"], bounds["startColumnIndex"]
    if (type(row) is not int or type(col) is not int or row < 0 or col < 0
            or bounds["endRowIndex"] != row + 1 or bounds["endColumnIndex"] != col + 1
            or row >= grid.get("rowCount", 0) or col >= grid.get("columnCount", 0)):
        _reject()
    return row, col


def _guarded_cell(bounds, old, desired):
    # Google does not search a never-written empty cell.  Formatting this one
    # technical cell materializes it without changing userEnteredValue.  A real
    # Google synthetic-sheet check verifies format+findReplace in one batch.
    pattern = r"^\s*$" if not old.strip() else "^" + re.escape(old) + "$"
    return [
        {"repeatCell": {"range": copy.deepcopy(bounds),
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"findReplace": {"range": copy.deepcopy(bounds), "find": pattern,
                         "replacement": _literal_replacement(desired), "searchByRegex": True,
                         "matchEntireCell": True, "matchCase": True, "includeFormulas": False}},
    ]


def guard_technical_writes(book, requests):
    """Convert a monitor technical batch using its original source snapshot.

    Supports the current shared-core identity planner's column append, technical
    header setup, hiding, missing UID allocation, and CalendarEventId binding.
    All value writes become conditional on the original cell value.  Existing
    nonblank UIDs are never replaced.  Input objects remain unchanged.

    Column moves, row moves, shared-state writes, and unknown request types are
    deliberately rejected before *any* request is returned.  The current core
    does not emit these operations, and assuming their coordinate mapping would
    reintroduce the positional overwrite this function is intended to prevent.
    """
    working = copy.deepcopy(book)
    sheets = {}
    for sheet in working.get("sheets", []):
        sid = sheet.get("properties", {}).get("sheetId")
        if sid in sheets:
            _reject()
        sheets[sid] = sheet
    result = []
    for request in requests:
        if not isinstance(request, dict) or len(request) != 1:
            _reject()
        kind, body = next(iter(request.items()))
        if not isinstance(body, dict):
            _reject()
        bounds = body.get("range", body)
        if not isinstance(bounds, dict):
            _reject()
        sheet = sheets.get(bounds.get("sheetId"))
        if sheet is None:
            _reject()
        schema, header_row = schema_kind(sheet)
        grid = sheet["properties"].get("gridProperties", {})
        if schema is None or not grid:
            _reject()
        try:
            cols = header_columns(sheet, header_row)
        except (ValueError, IndexError, KeyError):
            _reject()

        if kind == "appendDimension":
            if (set(body) != {"sheetId", "dimension", "length"} or body["dimension"] != "COLUMNS"
                    or type(body["length"]) is not int or body["length"] <= 0):
                _reject()
            grid["columnCount"] += body["length"]
            result.append(copy.deepcopy(request))
            continue

        if kind == "updateDimensionProperties":
            if (set(body) != {"range", "properties", "fields"}
                    or body["fields"] != "hiddenByUser" or body["properties"] != {"hiddenByUser": True}
                    or set(bounds) != {"sheetId", "dimension", "startIndex", "endIndex"}
                    or bounds["dimension"] != "COLUMNS"
                    or type(bounds["startIndex"]) is not int or type(bounds["endIndex"]) is not int
                    or bounds["endIndex"] != bounds["startIndex"] + 1
                    or bounds["startIndex"] not in {cols.get(EVENT_ID_HEADER), cols.get(SYNC_ID_HEADER)}):
                _reject()
            result.append(copy.deepcopy(request))
            continue

        if kind != "updateCells" or set(body) != {"range", "rows", "fields"} or body["fields"] != "userEnteredValue":
            _reject()
        row, col = _one_cell(bounds, grid)
        rows = body["rows"]
        if (not isinstance(rows, list) or len(rows) != 1 or set(rows[0]) != {"values"}
                or not isinstance(rows[0]["values"], list) or len(rows[0]["values"]) != 1
                or set(rows[0]["values"][0]) != {"userEnteredValue"}):
            _reject()
        desired = _stored_text(rows[0]["values"][0])
        old = _stored_text(_cell(sheet, row, col))
        if not desired.strip() or any(ch in desired or ch in old for ch in ("\r", "\n", "\x00")):
            _reject()

        if row == header_row:
            if desired not in {EVENT_ID_HEADER, SYNC_ID_HEADER}:
                _reject()
            if old.strip():
                if desired != EVENT_ID_HEADER or old.strip() not in EVENT_ID_ALIASES:
                    if old != desired:
                        _reject()
            elif (col in cols.values() or any(
                    _cell(sheet, index, col).get(field)
                    for index in range(len(sheet.get("rows", [])))
                    for field in ("userEnteredValue", "note", "textFormatRuns"))):
                _reject()
        elif col == cols.get(SYNC_ID_HEADER):
            if not UID_PATTERN.fullmatch(desired) or old.strip() and old != desired:
                _reject()
        elif col != cols.get(EVENT_ID_HEADER):
            _reject()

        if old != desired:
            result.extend(_guarded_cell(bounds, old, desired))
        _set_cell(sheet, row, col, desired)
    return result
