"""Every user-facing English string must be translated in every language, with the same placeholders."""
import json
import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "custom_components/lego_tracker/panel/i18n"
PH = re.compile(r"\{\w+\}")
STRINGS = runpy.run_path(str(ROOT / "scripts/i18n_extract.py"))["out"]
LANGS = sorted(p.stem for p in I18N.glob("*.json"))


def test_all_languages_present():
    from custom_components.lego_tracker.i18n import LANGUAGES
    assert set(LANGS) == set(LANGUAGES) - {"en"}
    assert len(STRINGS) > 500


@pytest.mark.parametrize("lang", LANGS)
def test_language_complete_and_placeholders_match(lang):
    table = json.loads((I18N / f"{lang}.json").read_text("utf-8"))
    missing = [s for s in STRINGS if s not in table]
    assert not missing, f"{lang}: {len(missing)} untranslated, e.g. {missing[:5]}"
    bad = [(k, v) for k, v in table.items() if sorted(PH.findall(k)) != sorted(PH.findall(v))]
    assert not bad, f"{lang}: placeholder mismatch {bad[:3]}"
    assert all(v.strip() for v in table.values())


def test_tr_and_localized_error():
    from custom_components.lego_tracker import i18n
    try:
        i18n.set_language("de")
        assert i18n.tr("{shop}: {ok} succeeded, {failed} failed", shop="bol.com", ok=2, failed=1) == "bol.com: 2 erfolgreich, 1 fehlgeschlagen"
        assert str(i18n.LocalizedError("Unknown shop {shop}.", shop="x")) == "Unbekannter Shop x."
        assert i18n.T("first price €{price}", price="1.00") == "first price €1.00"     # storage stays English
        assert i18n.tr("not in any table") == "not in any table"
    finally:
        i18n.set_language("en")
    assert i18n.resolve("auto", "fr-BE") == "fr" and i18n.resolve("auto", "pt") == "en" and i18n.resolve("xx") == "en"
