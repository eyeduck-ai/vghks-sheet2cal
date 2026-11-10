"""Verify bundled monitor policy against its separately pinned canonical source."""
import argparse
import json
from pathlib import Path

from vghks_sheet_core.contract import CONTRACT_VERSION

from vghks_sheet2cal.monitor_policy import POLICY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-repo", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((Path(__file__).resolve().parents[1] / "compatibility.json").read_text())
    canonical = json.loads((args.policy_repo / "contracts/calendar-monitor-policy.json").read_text())
    assert POLICY == canonical, "Bundled monitor policy differs from pinned canonical source"
    assert POLICY["policy_version"] == manifest["policy_version"]
    assert POLICY["schema_contract_version"] == manifest["contract_version"] == CONTRACT_VERSION
    assert POLICY["applies_to"] == "vghks-sheet2cal"
    print(f"Monitor policy {POLICY['policy_version']} matches canonical source; shared core {CONTRACT_VERSION} unchanged")


if __name__ == "__main__":
    main()
