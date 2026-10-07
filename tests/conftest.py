"""Load the HA-independent modules without importing Home Assistant."""
import importlib.util
import json
import pytest
import sys
import types
from pathlib import Path

PKG = Path(__file__).parent.parent / "custom_components" / "lego_tracker"
pkg = types.ModuleType("lego_pkg")
pkg.__path__ = [str(PKG)]
sys.modules["lego_pkg"] = pkg
for name in ("const", "models", "shops", "parsers", "csv_import"):
    spec = importlib.util.spec_from_file_location(f"lego_pkg.{name}", PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"lego_pkg.{name}"] = mod
    spec.loader.exec_module(mod)

# The integration has no shops of its own: the tests use the example shop settings, imported and accepted.
EXAMPLE = json.loads((Path(__file__).parent.parent / "examples" / "lot-shops.example.json").read_text("utf-8"))
# plus one shop of the tests' own, read with the generic reader, whose search results are built with JavaScript
# (the older tests were written against it; it is not in the example file)
EXAMPLE["shops"].append({"id": "smyths_be", "name": "Smyths Toys", "domain": "smythstoys.com", "reader": "generic",
                         "search": "https://www.smythstoys.com/be/nl-be/search?text={number}",
                         "aliases": ["smythstoys.com", "smythstoys", "smyths toys", "smyths"]})


def with_example(options, legal_version):
    options = dict(options)
    options.setdefault("shop_profile", EXAMPLE)
    options.setdefault("legal", {"accepted": 1, "version": legal_version})
    return options


_lp_shops = sys.modules["lego_pkg.shops"]
_lp_apply = _lp_shops.raw_apply_shop_options = _lp_shops.apply_shop_options
_lp_shops.apply_shop_options = lambda options: _lp_apply(with_example(options, _lp_shops.LEGAL_VERSION))
_lp_shops.apply_shop_options({})


try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # logic-only run (tests/test_logic.py)
    collect_ignore = ["test_integration.py", "test_fetcher.py", "test_i18n.py"]
else:
    pytest_plugins = ["pytest_homeassistant_custom_component"]

    @pytest.fixture(autouse=True)
    def _enable_custom_integrations(enable_custom_integrations):
        yield

    @pytest.fixture(autouse=True)
    def _allow_local_sockets(socket_enabled):
        """Fetcher tests talk to a local aiohttp server."""
        yield

    @pytest.fixture(autouse=True)
    def _shop_settings(request, monkeypatch):
        """Every entry starts with the example shop settings imported (unless the test is marked no_shop_settings)."""
        from custom_components.lego_tracker import shops
        import custom_components.lego_tracker as integration

        orig = shops.apply_shop_options
        if "no_shop_settings" in request.keywords:
            orig({})
            yield
            return

        def apply(options):
            orig(with_example(options, shops.LEGAL_VERSION))
        monkeypatch.setattr(shops, "apply_shop_options", apply)
        monkeypatch.setattr(integration, "apply_shop_options", apply)
        apply({})
        yield

    @pytest.fixture(autouse=True)
    def _english_again():
        """The active language is module state: every test starts (and ends) in English."""
        from custom_components.lego_tracker import i18n
        i18n.set_language("en")
        yield
        i18n.set_language("en")
