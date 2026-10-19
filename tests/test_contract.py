import importlib.metadata
import json
import unittest
from pathlib import Path

from vghks_sheet_core import calendar, identity, schema, state
from vghks_sheet_core.contract import CONTRACT_VERSION
from vghks_sheet_core.verify import verify

from vghks_sheet2cal import calendar_sync, sheet_identity, sheet_schema, sheet_sync_state


class ContractTests(unittest.TestCase):
    def test_worker_executes_the_shared_implementations(self):
        self.assertIs(sheet_schema, schema)
        self.assertIs(sheet_identity, identity)
        self.assertIs(sheet_sync_state, state)
        self.assertIs(calendar_sync, calendar)
        self.assertEqual(CONTRACT_VERSION, json.loads((Path(__file__).resolve().parents[1] / "compatibility.json").read_text())["contract_version"])

    def test_installed_core_matches_the_pinned_source_and_contract(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / "compatibility.json").read_text())
        provenance = json.loads(importlib.metadata.distribution("vghks-sheet-core").read_text("direct_url.json"))
        self.assertEqual(provenance["url"], "https://github.com/eyeduck-ai/op_spreadsheet.git")
        self.assertEqual(provenance["vcs_info"]["commit_id"], manifest["gas_revision"])
        self.assertEqual(provenance["subdirectory"], "packages/python")
        self.assertEqual(CONTRACT_VERSION, manifest["contract_version"])

    def test_real_gas_parsing_workflows_and_archives(self):
        self.assertGreaterEqual(verify(), 70)
