"""Load the HA-independent modules without importing Home Assistant."""
import importlib.util
import pytest
import sys
import types
from pathlib import Path

PKG = Path(__file__).parent.parent / "custom_components" / "lego_tracker"
pkg = types.ModuleType("lego_pkg")
pkg.__path__ = [str(PKG)]
sys.modules["lego_pkg"] = pkg
for name in ("const", "models", "parsers", "csv_import"):
    spec = importlib.util.spec_from_file_location(f"lego_pkg.{name}", PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"lego_pkg.{name}"] = mod
    spec.loader.exec_module(mod)


pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield
