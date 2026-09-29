"""Translations shared by the backend and the panel.

English is the source language: every user-facing string is written in English and looked up in
panel/i18n/<lang>.json ({"English text": "translation"}). Missing entries fall back to English,
so adding a language is just adding one JSON file. Placeholders use {name}.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)
I18N_DIR = Path(__file__).parent / "panel" / "i18n"

# code -> native name (shown in the language dropdown)
LANGUAGES: dict[str, str] = {
    "en": "English", "nl": "Nederlands", "fr": "Français", "de": "Deutsch", "es": "Español",
    "zh": "中文 (简体)", "ko": "한국어", "id": "Bahasa Indonesia",
}
DEFAULT_LANGUAGE = "en"
_CACHE: dict[str, dict[str, str]] = {}
CURRENT = {"lang": DEFAULT_LANGUAGE}


def load(lang: str) -> dict[str, str]:
    if lang == "en" or lang not in LANGUAGES:
        return {}
    if lang not in _CACHE:
        try:
            _CACHE[lang] = json.loads((I18N_DIR / f"{lang}.json").read_text("utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Translation file for %s unavailable: %s", lang, err)
            _CACHE[lang] = {}
    return _CACHE[lang]


def resolve(lang: str | None, ha_language: str | None = None) -> str:
    """'auto' follows Home Assistant's language when we have it, else English."""
    if lang == "auto" or not lang:
        base = (ha_language or "").split("-")[0].lower()
        return base if base in LANGUAGES else DEFAULT_LANGUAGE
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def set_language(lang: str) -> None:
    CURRENT["lang"] = lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def fmt(template: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


def T(template: str, **params: Any) -> str:
    """English text for storage/UI (the panel translates it). Marks the template for extraction."""
    return fmt(template, params)


def tr(text: str, lang: str | None = None, **params: Any) -> str:
    """Translate an English template and fill in the placeholders."""
    table = load(lang or CURRENT["lang"])
    return fmt(table.get(text, text), params)


class LocalizedError(ValueError):
    """ValueError whose message is an English template (+ params), rendered in the active language."""

    def __init__(self, template: str, **params: Any) -> None:
        self.template, self.params = template, params
        super().__init__(tr(template, **params))
