"""Cross-project archive ownership. No clinical data or expiring leases."""
from __future__ import annotations

from .google_sheets import SheetError

LOCK_KEY = "vghks.sheet.archive.lock"
LOCK_ID = 2147483646


def archive_lock(book):
    matches = [item for item in book.get("developerMetadata", [])
               if item.get("metadataKey") == LOCK_KEY or item.get("metadataId") == LOCK_ID]
    if len(matches) > 1 or matches and (matches[0].get("metadataKey") != LOCK_KEY
                                      or matches[0].get("metadataId") != LOCK_ID):
        raise SheetError("封存互斥標記衝突，請人工核對；未解除任何標記。", code="archive_lock_conflict")
    return matches[0].get("metadataValue", "") if matches else ""


def assert_archive_available(book, owner=""):
    active = archive_lock(book)
    if active and active != owner:
        raise SheetError("此刀表有未完成封存，請回到原封存入口核對並完成；目前暫停自動寫入。", code="archive_in_progress")
    return active


def acquire_request(owner):
    return {"createDeveloperMetadata": {"developerMetadata": {
        "metadataId": LOCK_ID, "metadataKey": LOCK_KEY, "metadataValue": owner,
        "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}}


def release_request(owner):
    return {"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {
        "metadataId": LOCK_ID, "metadataKey": LOCK_KEY, "metadataValue": owner,
        "visibility": "DOCUMENT"}}}}
