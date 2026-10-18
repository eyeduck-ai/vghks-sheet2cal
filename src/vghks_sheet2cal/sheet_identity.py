"""Shared contract implementation."""
import sys

from vghks_sheet_core import identity as _implementation

sys.modules[__name__] = _implementation
