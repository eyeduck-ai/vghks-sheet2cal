"""Execution policy is versioned independently from the immutable sheet schema."""
import json
import unittest
from pathlib import Path

from vghks_sheet_core.contract import CONTRACT_VERSION

from vghks_sheet2cal.monitor_policy import (
    CHANGE_RETENTION,
    DELETION_CONFIRMATION_CYCLES,
    OBSERVATION_RETENTION,
    OBSERVATION_SECONDS,
    POLICY,
    POLICY_VERSION,
)


class MonitorPolicyTests(unittest.TestCase):
    def test_bundled_policy_matches_schema_and_manifest_and_bounds_runtime(self):
        manifest = json.loads((Path(__file__).parents[1] / "compatibility.json").read_text())
        self.assertEqual(POLICY_VERSION, manifest["policy_version"])
        self.assertEqual(POLICY["schema_contract_version"], CONTRACT_VERSION)
        self.assertEqual(POLICY["applies_to"], "vghks-sheet2cal")
        self.assertEqual((OBSERVATION_SECONDS, OBSERVATION_RETENTION, CHANGE_RETENTION), (300, 200, 10000))
        self.assertEqual(DELETION_CONFIRMATION_CYCLES, 2)
        self.assertEqual(POLICY["calendar"]["conditional_writes"]["update_and_delete_header"], "If-Match")
