"""Shared contract implementation."""
import sys

from vghks_sheet_core import calendar as _implementation

sys.modules[__name__] = _implementation
