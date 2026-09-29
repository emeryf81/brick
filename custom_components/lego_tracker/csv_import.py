"""Collection CSV import (BrickEconomy / Brickset / Rebrickable / own spreadsheet) with validation.

Flow: ``analyze_csv`` parses and checks every line without touching the store (used for the
preview in the panel); ``apply_import`` then merges only the lines without errors.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any

from .models import normalize_set_number, parse_price

MAX_BYTES = 2_000_000
MAX_ROWS = 5000
MAX_TEXT = 200

# canonical field -> accepted (lower-cased, stripped) header names
ALIASES: dict[str, tuple[str, ...]] = {
    "set_number": ("set number", "set_number", "number", "set", "set no", "set num", "setnumber", "item number", "no",
                   "setnummer", "nummer"),
    "name": ("name", "set name", "title", "description", "naam"),
    "theme": ("theme", "category", "thema"),
    "subtheme": ("subtheme", "sub theme", "subcategory", "sub-theme", "subthema"),
    "year": ("year", "release year", "year released", "jaar"),
    "pieces": ("pieces", "piece count", "parts", "num parts", "pcs", "stenen", "onderdelen"),
    "rrp": ("retail price", "rrp", "msrp", "retail", "retail price eur", "adviesprijs"),
    "qty": ("qty", "quantity", "count", "owned", "amount", "aantal"),
    "paid": ("paid", "purchase price", "price paid", "cost", "bought for", "paid price", "betaald", "aankoopprijs"),
    "current_value": ("value", "current value", "current_value", "market value", "new value", "est. value",
                      "estimated value", "waarde"),
    "added": ("purchase date", "date", "acquired", "acquired date", "bought", "date added", "added", "aankoopdatum",
              "datum"),
    "condition": ("condition", "status", "staat"),
    "location": ("location", "storage", "locatie", "opslag"),
    "notes": ("notes", "note", "comment", "comments", "notitie", "opmerking"),
}
FIELD_LABELS = {
    "set_number": "Setnummer", "name": "Naam", "theme": "Thema", "subtheme": "Subthema", "year": "Jaar",
    "pieces": "Stenen", "rrp": "Adviesprijs", "qty": "Aantal", "paid": "Betaald", "current_value": "Waarde",
    "added": "Aankoopdatum", "condition": "Staat", "location": "Locatie", "notes": "Notitie",
}
CONDITIONS = {
    "sealed": "Sealed", "new": "Sealed", "nieuw": "Sealed", "misb": "Sealed", "nisb": "Sealed", "gesealed": "Sealed",
    "opened": "Geopend", "open": "Geopend", "geopend": "Geopend",
    "built": "Gebouwd", "assembled": "Gebouwd", "gebouwd": "Gebouwd", "used": "Gebouwd", "gebruikt": "Gebouwd",
    "incomplete": "Incompleet", "incompleet": "Incompleet", "parts": "Incompleet",
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
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", value)  # European d/m/Y
        if not m:
            return None
        d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def normalize_condition(value: str | None) -> str | None:
    if not value:
        return None
    return CONDITIONS.get(value.strip().lower(), value.strip()[:40])


def _sniff(text: str) -> csv.Dialect | type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        return csv.excel


def analyze_csv(text: str, store: dict[str, Any] | None = None, *, replace: bool = False,
                today: date | None = None) -> dict[str, Any]:
    """Parse and validate. Nothing is written. Each row gets status ok / warning / error."""
    today = today or date.today()
    store = store or {"sets": {}, "collection": {}}
    result: dict[str, Any] = {"columns": {}, "ignored_columns": [], "rows": [], "fatal": None,
                              "summary": {"ok": 0, "warning": 0, "error": 0, "new": 0, "update": 0, "merged": 0}}
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        result["fatal"] = f"Bestand te groot (max {MAX_BYTES // 1_000_000} MB)."
        return result
    text = text.lstrip("﻿")
    if not text.strip():
        result["fatal"] = "Leeg bestand."
        return result
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff(text))
    headers = reader.fieldnames or []
    mapping = _map_headers(headers)
    result["columns"] = {h: FIELD_LABELS[f] for h, f in mapping.items()}
    result["ignored_columns"] = [h for h in headers if h and h not in mapping]
    if "set_number" not in mapping.values():
        result["fatal"] = ("Geen kolom met setnummers gevonden. Verwacht bv. 'Number', 'Set Number' of 'Setnummer'. "
                           f"Gevonden kolommen: {', '.join(h for h in headers if h) or 'geen'}.")
        return result

    seen: dict[str, int] = {}
    for line, raw in enumerate(reader, start=2):
        if line - 1 > MAX_ROWS:
            result["fatal"] = f"Te veel regels (max {MAX_ROWS}); alleen de eerste {MAX_ROWS} zijn bekeken."
            break
        row = {mapping[k]: (v or "").strip() for k, v in raw.items() if k in mapping and isinstance(v, str)}
        if not any(row.values()):
            continue
        issues: list[tuple[str, str]] = []
        item: dict[str, Any] = {"line": line, "raw_number": row.get("set_number", "")}

        num = normalize_set_number(row.get("set_number", ""))
        if not num:
            issues.append(("error", "setnummer ontbreekt"))
        elif not re.fullmatch(r"\d{3,7}", num):
            issues.append(("error", f"ongeldig setnummer '{row.get('set_number')}'"))
        item["set_number"] = num

        for f in ("name", "theme", "subtheme", "location", "notes"):
            if row.get(f):
                val = re.sub(r"[\x00-\x1f]", " ", row[f])
                if len(val) > MAX_TEXT:
                    issues.append(("warning", f"{FIELD_LABELS[f].lower()} ingekort tot {MAX_TEXT} tekens"))
                item[f] = val[:MAX_TEXT]
        if cond := normalize_condition(row.get("condition")):
            item["condition"] = cond

        for f, lo, hi in (("year", 1949, today.year + 1), ("pieces", 1, 12000), ("qty", 0, 999)):
            if not row.get(f):
                continue
            m = re.search(r"-?\d+", row[f])
            if not m:
                issues.append(("warning", f"{FIELD_LABELS[f].lower()} '{row[f]}' is geen getal, genegeerd"))
                continue
            n = int(m.group(0))
            if f == "qty" and n <= 0:
                issues.append(("error", f"aantal is {n}"))
            elif not lo <= n <= hi:
                issues.append(("warning", f"{FIELD_LABELS[f].lower()} {n} lijkt onwaarschijnlijk, genegeerd"))
                continue
            item[f] = n
        if item.get("qty", 1) > 50:
            issues.append(("warning", f"aantal {item['qty']} is erg hoog"))

        for f in ("rrp", "paid", "current_value"):
            if not row.get(f):
                continue
            if re.match(r"\s*-", row[f]):
                issues.append(("error", f"{FIELD_LABELS[f].lower()} is negatief"))
                continue
            p = parse_price(row[f])
            if p is None:
                if re.search(r"[1-9]", row[f]):
                    issues.append(("warning", f"{FIELD_LABELS[f].lower()} '{row[f]}' niet herkend, genegeerd"))
                continue
            if p > 10000:
                issues.append(("warning", f"{FIELD_LABELS[f].lower()} €{p:.2f} lijkt te hoog, genegeerd"))
                continue
            item[f] = p
        ref = item.get("rrp") or store["sets"].get(num, {}).get("rrp")
        if ref and item.get("paid") and item["paid"] > ref * 3:
            issues.append(("warning", f"betaald €{item['paid']:.2f} is meer dan 3× de adviesprijs"))

        if row.get("added"):
            d = _date(row["added"])
            if d is None:
                issues.append(("warning", f"datum '{row['added']}' niet herkend, genegeerd"))
            elif d > today.isoformat():
                issues.append(("warning", f"aankoopdatum {d} ligt in de toekomst, genegeerd"))
            elif item.get("year") and int(d[:4]) < item["year"] - 1:
                issues.append(("warning", f"aankoopdatum {d} ligt vóór het uitgavejaar {item['year']}"))
                item["added"] = d
            else:
                item["added"] = d

        item.setdefault("qty", 1)
        if not item.get("name") and not store["sets"].get(num, {}).get("name"):
            issues.append(("info", "geen naam; wordt aangevuld als Brickset of een winkel die kent"))
        if num in seen and not any(lvl == "error" for lvl, _ in issues):
            issues.append(("info", f"zelfde set als regel {seen[num]}: telt als extra exemplaar"))
            result["summary"]["merged"] += 1
        elif num and not any(lvl == "error" for lvl, _ in issues):
            seen[num] = line
            if not replace and num in store["collection"]:
                issues.append(("info", "staat al in je collectie: wordt bijgewerkt"))
                result["summary"]["update"] += 1
            else:
                result["summary"]["new"] += 1

        levels = {lvl for lvl, _ in issues}
        item["status"] = "error" if "error" in levels else "warning" if "warning" in levels else "ok"
        item["issues"] = [{"level": lvl, "text": t} for lvl, t in issues]
        result["summary"][item["status"]] += 1
        result["rows"].append(item)
    if not result["rows"] and not result["fatal"]:
        result["fatal"] = "Geen regels met gegevens gevonden."
    return result


def importable_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in analysis["rows"] if r["status"] != "error"]


def parse_collection_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Backwards compatible helper: (importable rows, messages for skipped rows)."""
    a = analyze_csv(text)
    if a["fatal"] and not a["rows"]:
        return [], [a["fatal"]]
    msgs = [f"regel {r['line']}: {'; '.join(i['text'] for i in r['issues'] if i['level'] == 'error')}"
            for r in a["rows"] if r["status"] == "error"]
    return importable_rows(a), msgs


COLLECTION_FIELDS = ("qty", "paid", "current_value", "added", "condition", "location", "notes")


def apply_import(store: dict[str, Any], rows: list[dict[str, Any]], replace: bool = False) -> dict[str, int]:
    """Merge validated rows into the store. Existing set metadata is kept when already filled."""
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
        paid = [(p, q) for p, q in ((m.get("paid"), qty_a), (r.get("paid"), qty_b)) if p is not None]
        if paid:
            m["paid"] = round(sum(p * q for p, q in paid) / sum(q for _, q in paid), 2)
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
        new = {k: r[k] for k in COLLECTION_FIELDS if k in r}
        if entry is None:
            store["collection"][num] = new
            added += 1
        else:
            entry.update(new)
            updated += 1
    return {"added": added, "updated": updated}
