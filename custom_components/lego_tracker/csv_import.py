"""Collection CSV import (BrickEconomy / Brickset / Rebrickable / own spreadsheet)."""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from .models import normalize_set_number, parse_price

# canonical field -> accepted (lower-cased, stripped) header names
ALIASES: dict[str, tuple[str, ...]] = {
    "set_number": ("set number", "set_number", "number", "set", "set no", "set num", "setnumber", "item number", "no"),
    "name": ("name", "set name", "title", "description"),
    "theme": ("theme", "category"),
    "subtheme": ("subtheme", "sub theme", "subcategory", "sub-theme"),
    "year": ("year", "release year", "year released"),
    "pieces": ("pieces", "piece count", "parts", "num parts", "pcs"),
    "rrp": ("retail price", "rrp", "msrp", "retail", "retail price eur"),
    "qty": ("qty", "quantity", "count", "owned", "amount"),
    "paid": ("paid", "purchase price", "price paid", "cost", "bought for", "paid price"),
    "current_value": ("value", "current value", "current_value", "market value", "new value", "est. value", "estimated value"),
    "added": ("purchase date", "date", "acquired", "acquired date", "bought", "date added", "added"),
    "condition": ("condition", "status"),
}


def _map_headers(headers: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers:
        key = re.sub(r"\s+", " ", (h or "").strip().lower().lstrip("﻿"))
        for field, names in ALIASES.items():
            if key in names and field not in out.values():
                out[h] = field
                break
    return out


def _date(value: str) -> str | None:
    value = (value or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", value)  # European d/m/Y
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def parse_collection_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (rows, warnings). Delimiter (, ; tab) is auto-detected."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    mapping = _map_headers(reader.fieldnames or [])
    warnings: list[str] = []
    if "set_number" not in mapping.values():
        return [], ["No set-number column found (expected e.g. 'Number' or 'Set Number')."]
    rows: list[dict[str, Any]] = []
    for i, raw in enumerate(reader, start=2):
        row = {mapping[k]: (v or "").strip() for k, v in raw.items() if k in mapping}
        if not row.get("set_number"):
            continue
        num = normalize_set_number(row["set_number"])
        if not num.isdigit():
            warnings.append(f"row {i}: skipped, invalid set number {row['set_number']!r}")
            continue
        item: dict[str, Any] = {"set_number": num}
        for f in ("name", "theme", "subtheme", "condition"):
            if row.get(f):
                item[f] = row[f]
        for f in ("year", "pieces", "qty"):
            if row.get(f) and re.search(r"\d+", row[f]):
                item[f] = int(re.search(r"\d+", row[f]).group(0))
        for f in ("rrp", "paid", "current_value"):
            if (p := parse_price(row.get(f))) is not None:
                item[f] = p
        if row.get("added") and (d := _date(row["added"])):
            item["added"] = d
        item.setdefault("qty", 1)
        rows.append(item)
    return rows, warnings


def apply_import(store: dict[str, Any], rows: list[dict[str, Any]], replace: bool = False) -> dict[str, int]:
    """Merge parsed rows into the store. Existing set metadata is kept when already filled."""
    if replace:
        store["collection"] = {}
    added = updated = 0
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:  # same set on several lines = several copies
        m = merged.get(r["set_number"])
        if m is None:
            merged[r["set_number"]] = dict(r)
            continue
        qty_a, qty_b = m.get("qty", 1), r.get("qty", 1)
        if "paid" in m or "paid" in r:
            m["paid"] = round((m.get("paid", 0) * qty_a + r.get("paid", 0) * qty_b) / (qty_a + qty_b), 2)
        m["qty"] = qty_a + qty_b
        if r.get("added") and (not m.get("added") or r["added"] < m["added"]):
            m["added"] = r["added"]
    for r in merged.values():
        num = r["set_number"]
        s = store["sets"].setdefault(num, {"set_number": num})
        for f in ("name", "theme", "subtheme", "year", "pieces", "rrp"):
            if r.get(f) and not s.get(f):
                s[f] = r[f]
        entry = store["collection"].get(num)
        new = {k: r[k] for k in ("qty", "paid", "current_value", "added", "condition") if k in r}
        if entry is None:
            store["collection"][num] = new
            added += 1
        else:
            entry.update(new)
            updated += 1
    return {"added": added, "updated": updated}
