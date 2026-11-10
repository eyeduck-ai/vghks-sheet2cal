"""Synthetic row-object movement tests; provider behavior is probed separately."""
import copy
import unittest

from vghks_sheet2cal.binding_writes import (
    cleanup_binding_anchors,
    marker_key,
    plan_binding_anchors,
    resolve_binding_anchors,
)
from vghks_sheet2cal.calendar_sync import scan
from vghks_sheet2cal.selftest_calendar import cell, monthly, workbook

WORKER = "1" * 32
OTHER_WORKER = "2" * 32
OPERATION = "3" * 32
OTHER_OPERATION = "4" * 32


def create_markers(book, plan, *, first_id=100):
    result = []
    for index, request in enumerate(plan["requests"], first_id):
        metadata = copy.deepcopy(request["createDeveloperMetadata"]["developerMetadata"])
        metadata.update(metadataId=index)
        metadata["location"]["locationType"] = "ROW"
        result.append(metadata)
    return result


def move_row(book, metadata, index, destination):
    """Model native row-object movement, rather than a cell-value cut/paste."""
    rows = book["sheets"][0]["rows"]
    anchors = [(entry, rows[entry["location"]["dimensionRange"]["startIndex"]]) for entry in metadata]
    moved = rows.pop(index)
    rows.insert(destination, moved)
    for entry, original in anchors:
        index = next(index for index, candidate in enumerate(rows) if candidate is original)
        entry["location"]["dimensionRange"].update(startIndex=index, endIndex=index + 1)


def delete_row(book, metadata, index):
    rows = book["sheets"][0]["rows"]
    rows.pop(index)
    kept = []
    for entry in metadata:
        dimensions = entry["location"]["dimensionRange"]
        old_index = dimensions["startIndex"]
        if old_index == index:
            continue
        if old_index > index:
            dimensions.update(startIndex=old_index - 1, endIndex=old_index)
        kept.append(entry)
    metadata[:] = kept


def apply_filtered_values(book, metadata, body):
    """Apply the proposed wire payload to currently attached synthetic rows."""
    assert body["valueInputOption"] == "RAW"
    changed = 0
    for data in body["data"]:
        lookup = data["dataFilter"]["developerMetadataLookup"]
        matches = [entry for entry in metadata if all(
            entry["location"].get(key) == value if key == "locationType" else entry.get(key) == value
            for key, value in lookup.items())]
        for entry in matches:
            dimensions = entry["location"]["dimensionRange"]
            sheet = next(s for s in book["sheets"] if s["properties"]["sheetId"] == dimensions["sheetId"])
            target = sheet["rows"][dimensions["startIndex"]]
            for col, value in enumerate(data["values"][0]):
                if value is None:
                    continue
                while len(target) <= col:
                    target.append({})
                target[col]["userEnteredValue"] = {"stringValue": value}
                changed += 1
    return changed


class BindingWriteTests(unittest.TestCase):
    def setup_source(self):
        book = workbook(monthly())
        patients = scan(book)["patients"]
        bindings = [(item, "synthetic-event-" + str(index)) for index, item in enumerate(patients)]
        plan = plan_binding_anchors(bindings, WORKER, operation_id=OPERATION)
        return book, patients, plan

    def resolve(self, plan, book, metadata, patients=None):
        return resolve_binding_anchors(plan, book, scan(book)["patients"] if patients is None else patients, metadata)

    def assert_correct_bindings(self, book, plan):
        actual = {item["uid"]: item["event_id"] for item in scan(book)["patients"]}
        self.assertEqual(actual, {intent["item"]["uid"]: intent["event_id"] for intent in plan["bindings"]})

    def test_plan_uses_only_opaque_worker_operation_uid_and_does_not_mutate_input(self):
        book, patients, plan = self.setup_source()
        original = copy.deepcopy(patients)
        self.assertEqual(plan["key"], marker_key(WORKER))
        self.assertEqual(patients, original)
        for request, item in zip(plan["requests"], patients, strict=True):
            metadata = request["createDeveloperMetadata"]["developerMetadata"]
            self.assertEqual(metadata["metadataValue"], OPERATION + ":" + item["uid"])
            self.assertNotIn("metadataId", metadata)
            self.assertEqual(metadata["location"]["dimensionRange"], {
                "sheetId": item["sheet_id"], "dimension": "ROWS", "startIndex": item["row"], "endIndex": item["row"] + 1})
            self.assertNotIn(item["mrn"], metadata["metadataValue"])
        self.assertFalse(any(p["event_id"] for p in scan(book)["patients"]))

    def test_whole_row_move_after_validation_writes_to_original_uid_with_blank_event_cells(self):
        book, patients, plan = self.setup_source()
        metadata = create_markers(book, plan)
        resolved = self.resolve(plan, book, metadata)
        original_cells = {item["uid"]: copy.deepcopy(book["sheets"][0]["rows"][item["row"]][:16]) for item in patients}
        move_row(book, metadata, 2, 4)
        self.assertEqual(apply_filtered_values(book, metadata, resolved["body"]), 2)
        self.assert_correct_bindings(book, plan)
        for item in scan(book)["patients"]:
            self.assertEqual(book["sheets"][0]["rows"][item["row"]][:16], original_cells[item["uid"]])

    def test_move_between_metadata_creation_and_fresh_validation_is_supported(self):
        book, _, plan = self.setup_source()
        metadata = create_markers(book, plan)
        move_row(book, metadata, 2, 4)
        resolved = self.resolve(plan, book, metadata)
        self.assertFalse(resolved["row_issues"])
        self.assertEqual(len(resolved["accepted"]), 2)
        self.assertEqual(apply_filtered_values(book, metadata, resolved["body"]), 2)
        self.assert_correct_bindings(book, plan)

    def test_move_before_marker_attaches_is_deferred_without_any_value_write(self):
        book, _, plan = self.setup_source()
        rows = book["sheets"][0]["rows"]
        rows[2], rows[4] = rows[4], rows[2]
        metadata = create_markers(book, plan)
        resolved = self.resolve(plan, book, metadata)
        self.assertFalse(resolved["body"]["data"])
        self.assertFalse(resolved["accepted"])
        self.assertEqual(len(resolved["row_issues"]), 2)
        self.assertEqual(len(resolved["cleanup"]), 2)
        self.assertFalse(any(item["event_id"] for item in scan(book)["patients"]))

    def test_deleted_row_disappears_with_its_marker_without_redirecting_healthy_write(self):
        book, _, plan = self.setup_source()
        metadata = create_markers(book, plan)
        resolved = self.resolve(plan, book, metadata)
        delete_row(book, metadata, 2)
        self.assertEqual(apply_filtered_values(book, metadata, resolved["body"]), 1)
        remaining = scan(book)["patients"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["uid"], "d" * 32)
        self.assertEqual(remaining[0]["event_id"], "synthetic-event-1")

    def test_tolerant_scan_quarantine_excludes_only_affected_binding(self):
        book, patients, plan = self.setup_source()
        metadata = create_markers(book, plan)
        resolved = self.resolve(plan, book, metadata, patients=patients[1:])
        self.assertEqual(len(resolved["row_issues"]), 1)
        self.assertEqual(resolved["row_issues"][0]["uid"], patients[0]["uid"])
        self.assertEqual(len(resolved["body"]["data"]), 1)
        self.assertEqual(apply_filtered_values(book, metadata, resolved["body"]), 1)
        self.assertFalse(scan(book)["patients"][0]["event_id"])

    def test_formulas_changed_identity_and_foreign_event_ids_are_deferred_per_row(self):
        for value in ("formula", "identity", "foreign_event"):
            with self.subTest(value=value):
                book, patients, plan = self.setup_source()
                metadata = create_markers(book, plan)
                raw = book["sheets"][0]["rows"][2]
                if value == "formula":
                    raw[16] = {"userEnteredValue": {"formulaValue": '=""'}, "effectiveValue": {"stringValue": ""}}
                elif value == "identity":
                    raw[2] = cell("synthetic-new-mrn")
                else:
                    raw[16] = cell("another-existing-event")
                resolved = self.resolve(plan, book, metadata)
                self.assertEqual(len(resolved["row_issues"]), 1)
                self.assertEqual(resolved["row_issues"][0]["uid"], patients[0]["uid"])
                self.assertEqual(len(resolved["body"]["data"]), 1)

    def test_duplicate_markers_and_duplicate_source_uids_have_no_favored_first_row(self):
        for duplicate in ("marker", "source"):
            with self.subTest(duplicate=duplicate):
                book, patients, plan = self.setup_source()
                metadata = create_markers(book, plan)
                if duplicate == "marker":
                    metadata.append({**copy.deepcopy(metadata[0]), "metadataId": 500})
                else:
                    patients = [*patients, copy.deepcopy(patients[0])]
                resolved = self.resolve(plan, book, metadata, patients=patients)
                self.assertEqual(len(resolved["row_issues"]), 1)
                self.assertEqual(len(resolved["body"]["data"]), 1)

    def test_already_written_event_is_accepted_without_rewriting_and_owned_markers_are_cleaned(self):
        book, _, plan = self.setup_source()
        metadata = create_markers(book, plan)
        for intent in plan["bindings"]:
            book["sheets"][0]["rows"][intent["item"]["row"]][16] = cell(intent["event_id"])
        resolved = self.resolve(plan, book, metadata)
        self.assertFalse(resolved["body"]["data"])
        self.assertFalse(resolved["row_issues"])
        self.assertEqual(len(resolved["accepted"]), 2)
        self.assertEqual(len(resolved["cleanup"]), 2)

    def test_cleanup_is_exact_worker_and_operation_with_full_intersection_filter(self):
        book, patients, plan = self.setup_source()
        current = create_markers(book, plan)
        old_plan = plan_binding_anchors([(patients[0], "synthetic-stale-event")], WORKER, operation_id=OTHER_OPERATION)
        old = create_markers(book, old_plan, first_id=200)
        other_plan = plan_binding_anchors([(patients[0], "synthetic-foreign-event")], OTHER_WORKER, operation_id=OPERATION)
        foreign = create_markers(book, other_plan, first_id=300)
        invalid = {**copy.deepcopy(current[0]), "metadataId": 400, "metadataValue": "not-an-owned-operation"}
        all_markers = [*current, *old, *foreign, invalid]
        cleanup = cleanup_binding_anchors(WORKER, all_markers, operation_id=OPERATION)
        self.assertEqual(len(cleanup), 2)
        for request in cleanup:
            lookup = request["deleteDeveloperMetadata"]["dataFilter"]["developerMetadataLookup"]
            self.assertEqual(set(lookup), {"metadataId", "metadataKey", "metadataValue", "visibility", "locationType"})
            self.assertEqual(lookup["metadataKey"], marker_key(WORKER))
            self.assertTrue(lookup["metadataValue"].startswith(OPERATION + ":"))
            self.assertEqual(lookup["visibility"], "DOCUMENT")
            self.assertEqual(lookup["locationType"], "ROW")
        self.assertEqual(len(cleanup_binding_anchors(WORKER, all_markers)), 3)

    def test_lookup_does_not_reuse_retitled_or_reassigned_metadata_id(self):
        book, _, plan = self.setup_source()
        metadata = create_markers(book, plan)
        resolved = self.resolve(plan, book, metadata)
        metadata[0]["metadataValue"] = OTHER_OPERATION + ":" + "f" * 32
        self.assertEqual(apply_filtered_values(book, metadata, resolved["body"]), 1)
        self.assertFalse(scan(book)["patients"][0]["event_id"])

    def test_duplicate_plan_uids_or_event_ids_are_rejected_before_creation(self):
        _, patients, _ = self.setup_source()
        for bindings in (
            [(patients[0], "event-a"), (patients[0], "event-b")],
            [(patients[0], "event-a"), (patients[1], "event-a")],
        ):
            with self.subTest(bindings=len(bindings)), self.assertRaises(ValueError):
                plan_binding_anchors(bindings, WORKER, operation_id=OPERATION)


if __name__ == "__main__":
    unittest.main()
