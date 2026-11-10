"""Pure plans for short-lived row anchors during CalendarEventId binding.

An anchor identifies the native row object, so a whole-row move after validation
does not redirect the value write to another row at the old address.  This is
not a transaction across arbitrary cell pastes, technical-cell edits, or column
reordering.  The caller must re-read and verify UID, patient anchor, and event ID
after the filtered write, and must clean up its operation-owned metadata.
"""
from __future__ import annotations

import copy
import re
import uuid

from vghks_sheet_core.identity import EVENT_ID_HEADER, SYNC_ID_HEADER, header_columns, schema_kind

TOKEN = re.compile(r"[a-f0-9]{32}\Z")
MARKER = re.compile(r"(?P<operation>[a-f0-9]{32}):(?P<uid>[a-f0-9]{32})\Z")
KEY_PREFIX = "vghks.calendar.bind."


def marker_key(worker_id):
    if not isinstance(worker_id, str) or not TOKEN.fullmatch(worker_id):
        raise ValueError("監控實例識別格式不正確，未建立事件綁定錨點。")
    return KEY_PREFIX + worker_id


def _positive_id(value):
    return type(value) is int and value > 0


def _lookup(metadata):
    return {"metadataId": metadata["metadataId"], "metadataKey": metadata["metadataKey"],
            "metadataValue": metadata["metadataValue"], "visibility": "DOCUMENT", "locationType": "ROW"}


def cleanup_binding_anchors(worker_id, metadata_items, *, operation_id=None):
    """Delete only this worker's valid row markers, optionally one operation.

    Exact ID/key/value/visibility/type conditions also protect metadata that a
    different actor has changed after the read.  No key-prefix deletion occurs.
    """
    key = marker_key(worker_id)
    if operation_id is not None and (not isinstance(operation_id, str) or not TOKEN.fullmatch(operation_id)):
        raise ValueError("事件綁定操作識別格式不正確。")
    requests, seen = [], set()
    for metadata in metadata_items:
        value = metadata.get("metadataValue", "")
        match = MARKER.fullmatch(value) if isinstance(value, str) else None
        location = metadata.get("location", {})
        if (metadata.get("metadataKey") != key or metadata.get("visibility") != "DOCUMENT" or not match
                or operation_id is not None and match["operation"] != operation_id
                or not _positive_id(metadata.get("metadataId")) or location.get("locationType") != "ROW"):
            continue
        signature = (metadata["metadataId"], value)
        if signature in seen:
            continue
        seen.add(signature)
        requests.append({"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": _lookup(metadata)}}})
    return requests


def plan_binding_anchors(bindings, worker_id, *, operation_id=None):
    """Build metadata creation requests for already validated ``(item, eid)`` pairs."""
    key = marker_key(worker_id)
    operation_id = uuid.uuid4().hex if operation_id is None else operation_id
    if not isinstance(operation_id, str) or not TOKEN.fullmatch(operation_id):
        raise ValueError("事件綁定操作識別格式不正確。")
    intents, requests, uids, events = [], [], set(), set()
    for item, eid in bindings:
        uid, sid, row = item.get("uid"), item.get("sheet_id"), item.get("row")
        if (not isinstance(uid, str) or not TOKEN.fullmatch(uid) or uid in uids
                or type(sid) is not int or sid < 0 or type(row) is not int or row < 1
                or not isinstance(item.get("identity"), str) or not item["identity"]
                or not isinstance(eid, str) or not eid.strip() or eid in events):
            raise ValueError("事件綁定來源識別不完整或重複，未建立錨點。")
        uids.add(uid)
        events.add(eid)
        value = operation_id + ":" + uid
        intents.append({"item": copy.deepcopy(item), "event_id": eid, "marker_value": value})
        requests.append({"createDeveloperMetadata": {"developerMetadata": {
            "metadataKey": key, "metadataValue": value, "visibility": "DOCUMENT",
            "location": {"dimensionRange": {"sheetId": sid, "dimension": "ROWS",
                                           "startIndex": row, "endIndex": row + 1}}}}})
    return {"worker_id": worker_id, "key": key, "operation_id": operation_id,
            "bindings": intents, "requests": requests}


def _cell(sheet, row, col):
    rows = sheet.get("rows", [])
    return rows[row][col] if row < len(rows) and col < len(rows[row]) else {}


def _plain_value(cell):
    value = cell.get("userEnteredValue", {})
    if not value:
        return ""
    if set(value) != {"stringValue"} or not isinstance(value["stringValue"], str):
        return None
    return value["stringValue"].strip()


def _issue(item):
    return {"uid": item["uid"], "sheet": item.get("sheet", ""), "row": item["row"] + 1,
            "code": "binding_anchor", "message": "事件綁定錨點或來源列已變動，保留事件並待下輪重新核對。"}


def resolve_binding_anchors(plan, book, patients, metadata_items):
    """Validate fresh source rows and produce a RAW batchUpdateByDataFilter body.

    ``patients`` must come from the monitor's fresh tolerant scan, so quarantined
    rows cannot become write targets.  ``metadata_items`` is a flat list returned
    by an exact-key developerMetadata search or the equivalent full metadata
    read.  Merely receiving a create reply is insufficient proof of attachment.
    """
    if marker_key(plan["worker_id"]) != plan["key"]:
        raise ValueError("事件綁定錨點擁有者不一致。")
    sheets = {sheet["properties"]["sheetId"]: sheet for sheet in book.get("sheets", [])}
    by_uid, by_location, markers = {}, {}, {}
    for item in patients:
        by_uid.setdefault(item["uid"], []).append(item)
        by_location.setdefault((item["sheet_id"], item["row"]), []).append(item)
    for metadata in metadata_items:
        if metadata.get("metadataKey") == plan["key"]:
            markers.setdefault(metadata.get("metadataValue"), []).append(metadata)

    data, accepted, issues = [], [], []
    for intent in plan["bindings"]:
        original, eid = intent["item"], intent["event_id"]
        matches = markers.get(intent["marker_value"], [])
        current = by_uid.get(original["uid"], [])
        diagnostic = current[0] if len(current) == 1 else original
        if len(matches) != 1 or len(current) != 1:
            issues.append(_issue(diagnostic))
            continue
        metadata = matches[0]
        location = metadata.get("location", {})
        dimensions = location.get("dimensionRange", {})
        sid, row = dimensions.get("sheetId"), dimensions.get("startIndex")
        if (not _positive_id(metadata.get("metadataId")) or metadata.get("visibility") != "DOCUMENT"
                or location.get("locationType") != "ROW" or dimensions.get("dimension") != "ROWS"
                or type(row) is not int or row < 1 or dimensions.get("endIndex") != row + 1):
            issues.append(_issue(diagnostic))
            continue
        attached = by_location.get((sid, row), [])
        if (len(attached) != 1 or attached[0]["uid"] != original["uid"]
                or attached[0]["identity"] != original["identity"]):
            issues.append(_issue(diagnostic))
            continue
        target = attached[0]
        sheet = sheets.get(sid)
        if sheet is None:
            issues.append(_issue(target))
            continue
        try:
            schema, header = schema_kind(sheet)
            cols = header_columns(sheet, header)
            event_col, uid_col = cols[EVENT_ID_HEADER], cols[SYNC_ID_HEADER]
            grid = sheet["properties"]["gridProperties"]
            raw_uid, raw_eid = _plain_value(_cell(sheet, row, uid_col)), _plain_value(_cell(sheet, row, event_col))
            safe = (schema in {"month", "fu"} and row > header and row < grid["rowCount"]
                    and 0 <= event_col < grid["columnCount"] and 0 <= uid_col < grid["columnCount"]
                    and raw_uid == original["uid"] and raw_eid is not None
                    and raw_eid == target["event_id"]
                    and raw_eid in {original["event_id"], eid})
        except (ValueError, KeyError, TypeError, IndexError):
            safe = False
        if not safe:
            issues.append(_issue(target))
            continue
        accepted.append((copy.deepcopy(original), eid))
        if raw_eid != eid:
            # ValueRange input nulls are skipped, rather than clearing clinical
            # cells preceding CalendarEventId. RAW prevents formula parsing.
            data.append({"dataFilter": {"developerMetadataLookup": _lookup(metadata)},
                         "majorDimension": "ROWS", "values": [[None] * event_col + [eid]]})
    return {"body": {"valueInputOption": "RAW", "data": data, "includeValuesInResponse": False},
            "accepted": accepted, "row_issues": issues,
            "cleanup": cleanup_binding_anchors(plan["worker_id"], metadata_items,
                                                operation_id=plan["operation_id"])}
