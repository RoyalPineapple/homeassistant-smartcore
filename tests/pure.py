"""Load pure integration modules without executing HA's integration setup."""

import sys
import types
from pathlib import Path

if "_atios_pure" not in sys.modules:
    package = types.ModuleType("_atios_pure")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "atios")]
    sys.modules["_atios_pure"] = package
