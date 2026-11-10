"""Real monitor regressions for synthetic concurrent native Sheets operations."""
import copy
import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from vghks_sheet2cal.binding_writes import marker_key
from vghks_sheet2cal.calendar_monitor import CalendarMonitor, timestamp
from vghks_sheet2cal.calendar_sync import scan
from vghks_sheet2cal.google_sheets import SheetError, SheetUncertain
from vghks_sheet2cal.selftest_calendar import cell, prepared

OTHER_WORKER = "9" * 32


@contextmanager
def synthetic_monitor():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
        "VGHKS_SHEET_LOCK_DIR": os.path.join(directory, "locks")
    }):
        monitor, sheets, calendar = prepared(directory)
        for index, item in enumerate(scan(sheets.book)["patients"]):
            raw = sheets.book["sheets"][0]["rows"][item["row"]]
            raw[14] = {"userEnteredValue": {"formulaValue": f'="synthetic-plan-{index}"'},
                       "effectiveValue": {"stringValue": f"synthetic-plan-{index}"},
                       "note": "synthetic manual formula", "userEnteredFormat": {"textFormat": {"bold": True}}}
        try:
            yield monitor, sheets, calendar
        finally:
            monitor.close()


def binding_pairs(sheets):
    return [(item, "synthetic-binding-event-" + str(index)) for index, item in enumerate(scan(sheets.book)["patients"])]


def clinical_by_uid(sheets):
    return {item["uid"]: copy.deepcopy(sheets.book["sheets"][0]["rows"][item["row"]][:16])
            for item in scan(sheets.book)["patients"]}


def native_swap_rows(sheets, first, second):
    """Move row objects and associated native metadata together, as Google does."""
    rows = sheets.book["sheets"][0]["rows"]
    rows[first], rows[second] = rows[second], rows[first]
    for metadata in sheets.book["developerMetadata"]:
        bounds = metadata.get("location", {}).get("dimensionRange", {})
        if bounds.get("sheetId") != 1 or bounds.get("dimension") != "ROWS":
            continue
        index = bounds["startIndex"]
        if index in {first, second}:
            moved = second if index == first else first
            bounds.update(startIndex=moved, endIndex=moved + 1)


def native_delete_row(sheets, index):
    sheets.book["sheets"][0]["rows"].pop(index)
    kept = []
    for metadata in sheets.book["developerMetadata"]:
        bounds = metadata.get("location", {}).get("dimensionRange", {})
        if bounds.get("sheetId") == 1 and bounds.get("dimension") == "ROWS":
            old = bounds["startIndex"]
            if old == index:
                continue
            if old > index:
                bounds.update(startIndex=old - 1, endIndex=old)
        kept.append(metadata)
    sheets.book["developerMetadata"] = kept


def create_foreign_marker(sheets, item):
    metadata = {"metadataId": 123456, "metadataKey": marker_key(OTHER_WORKER),
                "metadataValue": "8" * 32 + ":" + item["uid"], "visibility": "DOCUMENT",
                "location": {"locationType": "ROW", "dimensionRange": {
                    "sheetId": item["sheet_id"], "dimension": "ROWS", "startIndex": item["row"],
                    "endIndex": item["row"] + 1}}}
    sheets.book["developerMetadata"].append(copy.deepcopy(metadata))
    return metadata


class BindingConcurrencyTests(unittest.TestCase):
    def assert_bindings(self, sheets, bindings):
        actual = {item["uid"]: item["event_id"] for item in scan(sheets.book)["patients"]}
        self.assertEqual(actual, {item["uid"]: eid for item, eid in bindings})

    def assert_no_own_markers(self, monitor, sheets):
        worker = monitor.store.get("config", "settings")["worker_id"]
        self.assertFalse(sheets.search_developer_metadata(marker_key(worker)))
        self.assertFalse(monitor.store.get("binding", "anchors_pending")["pending"])

    def test_native_move_after_resolve_preserves_each_uid_binding_and_clinical_formulas(self):
        with synthetic_monitor() as (monitor, sheets, calendar):
            bindings = binding_pairs(sheets)
            before = clinical_by_uid(sheets)
            original = sheets.write_filtered_values
            calls = []

            def move_before_write(body):
                calls.append(copy.deepcopy(body))
                native_swap_rows(sheets, bindings[0][0]["row"], bindings[1][0]["row"])
                return original(body)

            with patch.object(sheets, "write_filtered_values", side_effect=move_before_write):
                succeeded, issues = monitor._write_bindings(sheets, bindings)
            self.assertFalse(issues)
            self.assertEqual(succeeded, {item["uid"] for item, _ in bindings})
            self.assertEqual(len(calls), 1)
            self.assert_bindings(sheets, bindings)
            self.assertEqual(clinical_by_uid(sheets), before)
            for value in calls[0]["data"]:
                self.assertTrue(all(item is None for item in value["values"][0][:-1]))
            self.assert_no_own_markers(monitor, sheets)
            self.assertEqual(calendar.inserts + calendar.updates + calendar.deletes, 0)

    def test_move_before_anchor_creation_defers_without_writing_event_id(self):
        with synthetic_monitor() as (monitor, sheets, _):
            bindings = binding_pairs(sheets)
            before = clinical_by_uid(sheets)
            original = sheets.write
            moved = []

            def move_before_create(requests):
                if not moved and any("createDeveloperMetadata" in request for request in requests):
                    moved.append(True)
                    native_swap_rows(sheets, bindings[0][0]["row"], bindings[1][0]["row"])
                return original(requests)

            with patch.object(sheets, "write", side_effect=move_before_create), patch.object(
                    sheets, "write_filtered_values", wraps=sheets.write_filtered_values) as filtered:
                succeeded, issues = monitor._write_bindings(sheets, bindings)
            self.assertFalse(succeeded)
            self.assertEqual({issue["uid"] for issue in issues}, {item["uid"] for item, _ in bindings})
            filtered.assert_not_called()
            self.assertFalse(any(item["event_id"] for item in scan(sheets.book)["patients"]))
            self.assertEqual(clinical_by_uid(sheets), before)
            self.assert_no_own_markers(monitor, sheets)

    def test_deleted_anchor_batch_rejection_reresolves_and_binds_other_healthy_row(self):
        with synthetic_monitor() as (monitor, sheets, _):
            bindings = binding_pairs(sheets)
            before = clinical_by_uid(sheets)
            original = sheets.write_filtered_values
            calls = []

            def delete_before_first_write(body):
                calls.append(copy.deepcopy(body))
                if len(calls) == 1:
                    native_delete_row(sheets, bindings[0][0]["row"])
                    raise SheetError("synthetic deleted metadata filter", code="binding_changed", retryable=True)
                return original(body)

            with patch.object(sheets, "write_filtered_values", side_effect=delete_before_first_write):
                succeeded, issues = monitor._write_bindings(sheets, bindings)
            self.assertEqual(succeeded, {bindings[1][0]["uid"]})
            self.assertEqual({issue["uid"] for issue in issues}, {bindings[0][0]["uid"]})
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(calls[0]["data"]), 2)
            self.assertEqual(len(calls[1]["data"]), 1)
            self.assert_bindings(sheets, bindings[1:])
            self.assertEqual(clinical_by_uid(sheets), {bindings[1][0]["uid"]: before[bindings[1][0]["uid"]]})
            self.assert_no_own_markers(monitor, sheets)

    def test_ambiguous_committed_create_keeps_pending_then_cleans_own_stale_and_finishes(self):
        with synthetic_monitor() as (monitor, sheets, calendar):
            config = monitor.store.get("config", "settings")
            bindings = []
            for item in scan(sheets.book)["patients"]:
                binding, _, _ = monitor._sync_item(sheets, calendar, item, config, timestamp())
                bindings.append(binding)
            foreign = create_foreign_marker(sheets, bindings[1][0])
            original = sheets.write
            creates = []

            def committed_create_then_timeout(requests):
                if any("createDeveloperMetadata" in request for request in requests):
                    creates.append(copy.deepcopy(requests))
                    result = original(requests)
                    if len(creates) == 1:
                        raise SheetUncertain("synthetic committed create timeout")
                    return result
                return original(requests)

            with patch.object(sheets, "write", side_effect=committed_create_then_timeout):
                with self.assertRaises(SheetUncertain):
                    monitor._write_bindings(sheets, bindings)
                self.assertEqual(len(creates), 1)
                self.assertTrue(monitor.store.get("binding", "anchors_pending")["pending"])
                self.assertEqual(len(sheets.search_developer_metadata(marker_key(config["worker_id"]))), 2)
                self.assertTrue(all(monitor.store.get("event", item["uid"])["status"] == "pending"
                                    for item, _ in bindings))
                result = monitor.run_once()
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(creates), 2)
            self.assertEqual(calendar.inserts, 2)
            self.assertEqual(calendar.updates, 0)
            self.assertEqual(calendar.deletes, 0)
            self.assertTrue(all(monitor.store.get("event", item["uid"])["status"] == "ready" for item, _ in bindings))
            self.assert_bindings(sheets, bindings)
            self.assertEqual(sheets.search_developer_metadata(marker_key(OTHER_WORKER)), [foreign])
            self.assert_no_own_markers(monitor, sheets)

    def test_competing_uid_after_last_fingerprint_is_preserved_and_calendar_is_not_touched(self):
        with synthetic_monitor() as (monitor, sheets, calendar):
            sheet = sheets.book["sheets"][0]
            sheet["rows"][2][17] = {}
            before = copy.deepcopy(sheets.book)
            clinical = copy.deepcopy(sheet["rows"][2][:16])
            original = sheets.read_metadata
            triggered = []

            def other_writer_assigns_uid():
                if not triggered:
                    triggered.append(True)
                    sheet["rows"][2][17] = cell("e" * 32)
                return original()

            with patch.object(sheets, "read_metadata", side_effect=other_writer_assigns_uid):
                with self.assertRaisesRegex(ValueError, "未操作日曆"):
                    monitor._migrate(sheets, before)
            self.assertEqual(sheet["rows"][2][17]["userEnteredValue"], {"stringValue": "e" * 32})
            self.assertEqual(sheet["rows"][2][:16], clinical)
            self.assertFalse(scan(sheets.book)["issues"])
            self.assertEqual(calendar.inserts + calendar.updates + calendar.deletes, 0)
            # The external UID is valid durable input on the next cycle; the
            # failed expected-ID comparison does not force a permanent repair.
            self.assertEqual(monitor._migrate(sheets, sheets.read_all()), sheets.book)

    def test_ambiguous_committed_filtered_write_is_not_replayed_and_recovers_by_readback(self):
        with synthetic_monitor() as (monitor, sheets, _):
            bindings = binding_pairs(sheets)
            original = sheets.write_filtered_values

            def committed_write_then_timeout(body):
                original(body)
                raise SheetUncertain("synthetic committed filtered update timeout")

            with patch.object(sheets, "write_filtered_values", side_effect=committed_write_then_timeout) as filtered:
                with self.assertRaises(SheetUncertain):
                    monitor._write_bindings(sheets, bindings)
                filtered.assert_called_once()
            self.assert_bindings(sheets, bindings)
            self.assertTrue(monitor.store.get("binding", "anchors_pending")["pending"])
            with patch.object(sheets, "write_filtered_values", wraps=original) as retried:
                succeeded, issues = monitor._write_bindings(sheets, bindings)
                retried.assert_not_called()
            self.assertFalse(issues)
            self.assertEqual(succeeded, {item["uid"] for item, _ in bindings})
            self.assert_no_own_markers(monitor, sheets)

    def test_cleanup_failure_recovers_even_when_no_bindings_remain(self):
        for committed in (False, True):
            with self.subTest(committed=committed), synthetic_monitor() as (monitor, sheets, _):
                bindings = binding_pairs(sheets)
                original = sheets.write

                def failed_cleanup(requests, *, _committed=committed, _original=original):
                    if any("deleteDeveloperMetadata" in request for request in requests):
                        if _committed:
                            _original(requests)
                        raise SheetUncertain("synthetic cleanup timeout")
                    return _original(requests)

                with patch.object(sheets, "write", side_effect=failed_cleanup):
                    with self.assertRaises(SheetUncertain):
                        monitor._write_bindings(sheets, bindings)
                self.assert_bindings(sheets, bindings)
                self.assertTrue(monitor.store.get("binding", "anchors_pending")["pending"])
                succeeded, issues = monitor._write_bindings(sheets, [])
                self.assertFalse(succeeded)
                self.assertFalse(issues)
                self.assert_no_own_markers(monitor, sheets)

    def test_pending_cleanup_and_worker_identity_survive_actual_monitor_restart(self):
        with synthetic_monitor() as (monitor, sheets, calendar):
            bindings = binding_pairs(sheets)
            foreign = create_foreign_marker(sheets, bindings[1][0])
            original = sheets.write_filtered_values

            def committed_write_then_timeout(body):
                original(body)
                raise SheetUncertain("synthetic process interrupted after committed write")

            with patch.object(sheets, "write_filtered_values", side_effect=committed_write_then_timeout):
                with self.assertRaises(SheetUncertain):
                    monitor._write_bindings(sheets, bindings)
            directory = monitor.store.directory
            worker_id = monitor.store.get("config", "settings")["worker_id"]
            monitor.close()
            restarted = CalendarMonitor(directory, clients=lambda _: (sheets, calendar), background=False,
                                        today=monitor.today, host_id=monitor.host_id)
            try:
                self.assertEqual(restarted.store.get("config", "settings")["worker_id"], worker_id)
                self.assertTrue(restarted.store.get("binding", "anchors_pending")["pending"])
                with patch.object(sheets, "write_filtered_values", wraps=original) as filtered:
                    succeeded, issues = restarted._write_bindings(sheets, [])
                    filtered.assert_not_called()
                self.assertFalse(succeeded)
                self.assertFalse(issues)
                self.assert_bindings(sheets, bindings)
                self.assert_no_own_markers(restarted, sheets)
                self.assertEqual(sheets.search_developer_metadata(marker_key(OTHER_WORKER)), [foreign])
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
