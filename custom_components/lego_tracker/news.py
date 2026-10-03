"""News for the ticker at the bottom of the panel, read from a plain text file on the web.

Format (blocks separated by a line with only '---'; lines before the first header field are ignored):

    id: 2026-10-03-update
    date: 2026-10-03
    lang: nl                 (optional: only shown in this language; without it, in every language)
    title: UPDATE 3 oktober!
    link: /hacs/dashboard    (optional: https://… or a path inside Home Assistant)
    <empty line>
    The text shown when the news is opened (plain text, empty lines separate paragraphs).
"""
from __future__ import annotations

import re
import time
from typing import Any

NEWS_URL = "https://www.emery.be/lot/nieuws.txt"
CACHE_SECONDS = 3 * 3600
MAX_BYTES = 200_000
MAX_ITEMS = 30
FIELDS = ("id", "date", "lang", "title", "link")


def _safe_link(link: str) -> str:
    """Only https links and paths inside Home Assistant (no javascript: or data: links)."""
    link = link.strip()
    if link.startswith("https://") or (link.startswith("/") and not link.startswith("//")):
        return link[:500]
    return ""


BODY_FIELDS = ("body", "text", "tekst", "bericht", "message", "inhoud")


def parse(text: str) -> list[dict[str, Any]]:
    """Parse titled news blocks, sanitize links, and return up to MAX_ITEMS newest entries.
    The text may follow the header fields directly or after an empty line, or start with 'tekst:' / 'body:';
    Windows and old Mac line ends and a byte-order mark are accepted."""
    out = []
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"^\s*---+\s*$", text, flags=re.M):
        item: dict[str, Any] = {}
        body: list[str] = []
        in_body = False
        for line in block.strip("\n").split("\n"):
            m = None if in_body else re.match(r"^\s*(\w+)\s*:\s*(.*)$", line)
            key = m.group(1).lower() if m else ""
            if key in FIELDS:
                item[key] = m.group(2).strip()
            elif key in BODY_FIELDS and item:
                in_body = True
                if m.group(2).strip():
                    body.append(m.group(2).rstrip())
            elif not item:
                continue                                  # comment lines and anything before the first field
            elif in_body or line.strip():
                in_body = True
                body.append(line.rstrip())
        if not item.get("title"):
            continue
        item["body"] = "\n".join(body).strip()[:5000]
        item["title"] = item["title"][:200]
        item["link"] = _safe_link(item.get("link", ""))
        item.setdefault("id", f"{item.get('date', '')}-{item['title'][:40]}")
        out.append(item)
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return out[:MAX_ITEMS]


def for_language(items: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    """News in the panel's language; news without a language is shown everywhere. When nothing is
    written in that language, the English (else Dutch) news is shown."""
    base = (lang or "en").split("-")[0].lower()
    langs = {(i.get("lang") or "").lower() for i in items}
    pick = base if base in langs else "en" if "en" in langs else "nl"
    return [i for i in items if (i.get("lang") or "").lower() in ("", pick)]


class NewsFeed:
    def __init__(self, session_getter: Any) -> None:
        """Initialize an empty news cache with a callable that supplies the HTTP session."""
        self._session = session_getter
        self.items: list[dict[str, Any]] = []
        self.ts = 0.0
        self.error: str | None = None

    async def get(self) -> list[dict[str, Any]]:
        """Refresh expired news; retain cached items on failure and retry after 15 minutes."""
        if time.time() - self.ts < CACHE_SECONDS:
            return self.items
        self.ts = time.time()
        try:
            async with self._session().get(NEWS_URL, timeout=15) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                raw = await resp.content.read(MAX_BYTES)
            self.items, self.error = parse(raw.decode("utf-8", errors="replace")), None
        except Exception as err:  # noqa: BLE001 - news is optional
            self.error = str(err)[:120]
            self.ts = time.time() - CACHE_SECONDS + 900          # try again in 15 minutes
        return self.items
