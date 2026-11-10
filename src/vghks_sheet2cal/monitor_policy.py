"""Bundled, versioned execution policy; no network or main-program dependency."""
import json
from importlib.resources import files

POLICY = json.loads(files("vghks_sheet2cal").joinpath("data/calendar-monitor-policy.json").read_text(encoding="utf-8"))
POLICY_VERSION = POLICY["policy_version"]
OBSERVATION_SECONDS = POLICY["observation"]["interval_seconds"]
OBSERVATION_RETENTION = POLICY["observation"]["retention"]["observations"]
CHANGE_RETENTION = POLICY["observation"]["retention"]["changes"]
DELETION_CONFIRMATION_CYCLES = POLICY["calendar"]["deletion_confirmation_cycles"]
