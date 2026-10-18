import unittest

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
        self.assertEqual(CONTRACT_VERSION, "1.0.0")

    def test_real_gas_parsing_workflows_and_archives(self):
        self.assertGreaterEqual(verify(), 70)
