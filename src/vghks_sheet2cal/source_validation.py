"""Quarantine bounded source conflicts before calling the strict shared planners.

The projection is never a replacement workbook to upload. Masked rows retain
their physical positions and manual layout footprint, but no clinical values or
identities. Callers must fingerprint the ORIGINAL book and reject any planned
cell writes intersecting ``blocked_rows``.
"""
from __future__ import annotations

import copy
from collections import defaultdict

from vghks_sheet_core.calendar import EVENT_HEADER, SYNC_HEADER, UID, headers, sheet_kind
from vghks_sheet_core.schema import cell_text, monthly_row

_LAYOUT_NOTE = "__VGHKS_SOURCE_QUARANTINE_LAYOUT__"
_MESSAGES = {
    "invalid_sync_id": "永久排程識別格式無效，來源內容保留。",
    "duplicate_sync_id": "永久排程識別重複，所有相關列均暫停；不選取其中一列。",
    "duplicate_event_id": "日曆事件識別重複，所有相關列均暫停；原事件保留。",
    "orphan_sync_id": "此列只剩系統識別，需核對原排程的搬移或刪除。",
    "orphan_event_id": "日曆事件識別缺少有效患者及永久排程識別，原事件保留。",
    "formula_identity": "系統識別欄含公式，暫停此列並保留來源內容。",
    "day_header_event_id": "刀日標題含日曆事件識別，暫停此刀日區塊。",
    "bound_patient_header": "已綁定患者的永久識別變成刀日標題，暫停此刀日區塊。",
    "blocked_day_context": "所在刀日的識別有衝突，暫停此區塊；其他正常刀日仍可同步。",
    "linked_identity_conflict": "此列與其他衝突列共用系統識別，暫停所有相關列。",
    "ambiguous_move_candidate": "刀表仍有孤立系統識別，此未識別新列可能屬於搬移；暫不配置新 ID 或建立事件。",
}


def _value(row, cols, label):
    column = cols.get(label)
    return cell_text(row[column]).strip() if column is not None and column < len(row) else ""


def _identity_formula(row, cols):
    return any("formulaValue" in row[column].get("userEnteredValue", {})
               for label in (SYNC_HEADER, EVENT_HEADER)
               if (column := cols.get(label)) is not None and column < len(row))


def _mask_row(row, technical_columns):
    """Retain layout, including unnamed manual columns occupied only here."""
    masked = [{} for _ in row]
    occupied = [column for column, value in enumerate(row)
                if column not in technical_columns
                and (value.get("userEnteredValue") or value.get("note") or value.get("textFormatRuns"))]
    if occupied:
        masked[max(occupied)] = {"note": _LAYOUT_NOTE}
    return masked


def project_source(book, *, bound_patient_uids=()):
    """Return a write-safe diagnostic projection with connected rows quarantined.

    Row references in diagnostics are one-based. ``blocked_rows`` uses zero-based
    physical ``(sheetId, row)`` coordinates for request filtering. Raw malformed
    identifiers remain in internal protective sets, never user-facing messages.
    A conflicting day header quarantines its complete block so the strict core
    neither inherits the wrong day nor invents a replacement header identity.
    """
    projected = copy.deepcopy(book)
    records, by_uid, by_event, blocks = {}, defaultdict(set), defaultdict(set), defaultdict(set)
    reasons, global_issues = defaultdict(set), []
    active_uids, active_events, bound = set(), set(), set(bound_patient_uids)
    for sheet in book.get("sheets", []):
        kind = sheet_kind(sheet)
        if not kind:
            continue
        sid, name = sheet["properties"]["sheetId"], sheet["properties"]["title"]
        try:
            header, cols = headers(sheet)
        except ValueError:
            global_issues.append(f"{name} 欄位標題重複或無法辨識，未操作日曆。")
            continue
        required = {"病歷號", "姓名"}
        required.update({"日期／報到時間", "醫院"} if kind == "MONTHLY" else {"日期"} if kind == "FU" else {"ArchiveRowType"})
        if required - cols.keys():
            global_issues.append(f"{name} 缺少必要欄位：{'、'.join(sorted(required - cols.keys()))}")
            continue
        block = None
        for index, row in enumerate(sheet.get("rows", [])[header + 1:], header + 1):
            key = sid, index
            uid, eid = _value(row, cols, SYNC_HEADER), _value(row, cols, EVENT_HEADER)
            person = bool(_value(row, cols, "病歷號") or _value(row, cols, "姓名"))
            is_header = False
            if kind == "MONTHLY":
                parsed = monthly_row(row, cols)
                if parsed["kind"] == "invalid_header":
                    # An otherwise valid day plus an event ID is a bounded ID
                    # conflict. Malformed dates/campus/marker remain global.
                    without_event = copy.deepcopy(row)
                    column = cols.get(EVENT_HEADER)
                    if column is not None and column < len(without_event):
                        without_event[column] = {}
                    if eid and monthly_row(without_event, cols)["kind"] == "day":
                        is_header = True
                        reasons[key].add("day_header_event_id")
                    else:
                        global_issues.append(f"{name}!{index + 1} 刀日結構不明，未操作日曆。")
                        block = None
                elif parsed["kind"] == "day":
                    is_header = True
                elif parsed["kind"] == "patient" and block is None:
                    global_issues.append(f"{name}!{index + 1} 病人列上方沒有有效刀日，未操作日曆。")
                if is_header:
                    block = key
                if block is not None:
                    blocks[block].add(key)
            records[key] = {"uid": uid, "event_id": eid, "sheet": name, "cols": cols,
                            "is_header": is_header, "is_patient": person and not is_header,
                            "kind": kind, "block": block}
            if uid:
                active_uids.add(uid)
                by_uid[uid].add(key)
                if not UID.fullmatch(uid):
                    reasons[key].add("invalid_sync_id")
                if not person and not is_header:
                    reasons[key].add("orphan_sync_id")
                if is_header and uid in bound:
                    reasons[key].add("bound_patient_header")
            if eid:
                active_events.add(eid)
                by_event[eid].add(key)
                if not uid or not person and not is_header:
                    reasons[key].add("orphan_event_id")
            if _identity_formula(row, cols):
                reasons[key].add("formula_identity")
    if any(codes & {"orphan_sync_id", "orphan_event_id"} for codes in reasons.values()):
        # An orphan left at a cleared source may belong to any unidentified
        # destination. Do not guess using patient fields or create a duplicate.
        # A genuine full-row deletion leaves no orphan and does not trigger this.
        for key, record in records.items():
            if record["kind"] != "ARCHIVE" and record["is_patient"] and not record["uid"]:
                reasons[key].add("ambiguous_move_candidate")
    for grouped, reason in ((by_uid, "duplicate_sync_id"), (by_event, "duplicate_event_id")):
        for locations in grouped.values():
            if len(locations) > 1:
                for key in locations:
                    reasons[key].add(reason)

    # Reach a fixed point: blocking a header blocks its entire context; identities
    # in those patient rows can in turn connect to another row or day block.
    blocked = set(reasons)
    pending = list(blocked)
    while pending:
        key = pending.pop()
        record = records[key]
        linked = set()
        if record["uid"]:
            linked.update(by_uid[record["uid"]])
        if record["event_id"]:
            linked.update(by_event[record["event_id"]])
        if record["is_header"]:
            for other in blocks[key]:
                if other != key:
                    reasons[other].add("blocked_day_context")
            linked.update(blocks[key])
        for other in linked - blocked:
            if not reasons[other]:
                reasons[other].add("linked_identity_conflict")
            blocked.add(other)
            pending.append(other)

    row_issues = []
    for key in sorted(blocked):
        record = records[key]
        if not (record["uid"] or record["event_id"] or record["is_header"] or record["is_patient"]
                or "formula_identity" in reasons[key]):
            # Template filler rows still belong to the protected block, but
            # counting them as failed schedules would inflate the diagnostics.
            continue
        codes = sorted(reasons[key])
        uid = record["uid"]
        row_issues.append({"uid": uid if UID.fullmatch(uid) else "", "sheet": record["sheet"],
                           "row": key[1] + 1, "code": codes[0] if len(codes) == 1 else "source_conflict",
                           "reasons": codes, "message": "；".join(_MESSAGES[code] for code in codes)})
    for sheet in projected.get("sheets", []):
        sid = sheet["properties"]["sheetId"]
        for index, row in enumerate(sheet.get("rows", [])):
            if (sid, index) in blocked:
                cols = records[sid, index]["cols"]
                technical = {cols[label] for label in (SYNC_HEADER, EVENT_HEADER) if label in cols}
                sheet["rows"][index] = _mask_row(row, technical)
    return {"book": projected, "row_issues": row_issues, "blocked_rows": blocked,
            "blocked_uids": {records[key]["uid"] for key in blocked if records[key]["uid"]},
            "blocked_event_ids": {records[key]["event_id"] for key in blocked if records[key]["event_id"]},
            "active_uids": active_uids, "active_event_ids": active_events, "global_issues": global_issues}
