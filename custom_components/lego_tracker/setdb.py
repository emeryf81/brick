"""The LEGO set database: every set there is, with name, year, theme, subtheme, pieces and picture.

Rebrickable publishes the complete list of LEGO sets every day as a free download (no key needed):
sets.csv.gz and themes.csv.gz, about 1 MB together. The integration fetches it once a day, keeps it in
its own storage file and uses it to
- fill in set data right away when you add a set (before the slower online lookups),
- search sets by name or number while adding,
- notice new sets: a set that was not in yesterday's list is new (Deals → New sets).
"""
from __future__ import annotations

import csv
import gzip
import io
import re
import time
from datetime import date
from typing import Any

SETS_URL = "https://cdn.rebrickable.com/media/downloads/sets.csv.gz"
THEMES_URL = "https://cdn.rebrickable.com/media/downloads/themes.csv.gz"
REFRESH_HOURS = 24
MAX_BYTES = 40_000_000             # unpacked size limit per file
NEW_KEEP_DAYS = 365                # how long a set stays in "new sets"
NEW_MAX = 1000
SOURCE = "Rebrickable"
NUM_RE = re.compile(r"^\d{3,7}$")
# a set row: [name, year, theme, subtheme, pieces, image]
NAME, YEAR, THEME, SUB, PIECES, IMAGE = range(6)


def _text(gz: bytes) -> str:
    """Decode gzip data as UTF-8, rejecting content larger than MAX_BYTES."""
    with gzip.GzipFile(fileobj=io.BytesIO(gz)) as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("set list too large")
    return data.decode("utf-8", errors="replace")


def _int(v: str) -> int:
    """Convert a value to an integer, returning zero for invalid or missing values."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse(sets_gz: bytes, themes_gz: bytes) -> dict[str, list[Any]]:
    """{set number: [name, year, theme, subtheme, pieces, image]} for the regular LEGO sets.
    Only the first version of a numbered set ("10281-1") with bricks in it: books, gear, key chains
    and other products without pieces are left out. Runs in an executor."""
    themes: dict[str, tuple[str, str]] = {}
    for row in csv.DictReader(io.StringIO(_text(themes_gz))):
        themes[row.get("id") or ""] = (row.get("name") or "", row.get("parent_id") or "")

    def chain(tid: str) -> list[str]:
        """Return theme names from root to leaf, stopping at missing parents or cycles."""
        names, seen = [], set()
        while tid and tid in themes and tid not in seen:
            seen.add(tid)
            name, tid = themes[tid]
            names.append(name)
        return list(reversed(names))           # root first

    out: dict[str, list[Any]] = {}
    for row in csv.DictReader(io.StringIO(_text(sets_gz))):
        base, _, ver = (row.get("set_num") or "").rpartition("-")
        if ver != "1" or not NUM_RE.match(base):
            continue
        pieces = _int(row.get("num_parts"))
        if pieces <= 0:
            continue
        names = chain(row.get("theme_id") or "")
        img = row.get("img_url") or ""
        out[base] = [(row.get("name") or "").strip()[:120], _int(row.get("year")), names[0] if names else "",
                     names[1] if len(names) > 1 else "", pieces, img if img.startswith("https://") else ""]
    return out


def as_set(num: str, row: list[Any]) -> dict[str, Any]:
    """Expand a compact database row into set fields, using None for empty metadata."""
    return {"set_number": num, "name": row[NAME], "year": row[YEAR] or None, "theme": row[THEME] or None,
            "subtheme": row[SUB] or None, "pieces": row[PIECES] or None, "image": row[IMAGE] or None}


def search(sets: dict[str, list[Any]], q: str, limit: int = 20) -> list[str]:
    """Set numbers matching a number (prefix) or all words of the name; newest first."""
    q = q.strip().lower()
    if not q:
        return []
    if q.isdigit():
        hits = [n for n in sets if n.startswith(q)]
        hits.sort(key=lambda n: (n != q, len(n), -(sets[n][YEAR] or 0)))
        return hits[:limit]
    words = q.split()
    hits = [n for n, r in sets.items() if all(w in f"{r[NAME]} {r[THEME]} {r[SUB]}".lower() for w in words)]
    hits.sort(key=lambda n: -(sets[n][YEAR] or 0))
    return hits[:limit]


def find_new(old: dict[str, list[Any]], new: dict[str, list[Any]], first: bool) -> list[str]:
    """Sets that appeared since the last download. The very first download has nothing to compare
    with: then the sets of this year and later count as new."""
    if first or not old:
        year = date.today().year
        return [n for n, r in new.items() if (r[YEAR] or 0) >= year]
    return [n for n in new if n not in old]


def prune_new(seen: dict[str, float], now: float | None = None) -> dict[str, float]:
    """Keep at most NEW_MAX newest sightings younger than NEW_KEEP_DAYS."""
    now = now or time.time()
    keep = {n: ts for n, ts in seen.items() if now - ts < NEW_KEEP_DAYS * 86400}
    return dict(sorted(keep.items(), key=lambda x: -x[1])[:NEW_MAX])
