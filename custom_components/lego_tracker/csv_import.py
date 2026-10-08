"""Collection CSV import (collection sites / Brickset / Rebrickable / own spreadsheet) with validation.

Flow: ``analyze_csv`` parses and checks every line without touching the store (used for the
preview in the panel); ``apply_import`` then merges only the lines without errors.
"""
from __future__ import annotations

import csv
import io
import re
import time
from datetime import date
from typing import Any

from .i18n import T
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
    "set_number": "Set number", "name": "Name", "theme": "Theme", "subtheme": "Subtheme", "year": "Year",
    "pieces": "Pieces", "rrp": "RRP", "qty": "Quantity", "paid": "Paid", "current_value": "Value",
    "added": "Purchase date", "condition": "Condition", "location": "Location", "notes": "Notes",
}
CONDITIONS = {   # any language in -> canonical English value (translated in the panel)
    **{k: "Sealed" for k in ("sealed", "new", "nieuw", "misb", "nisb", "gesealed", "neuf", "neu", "nuevo", "baru", "새제품", "全新")},
    **{k: "Opened" for k in ("opened", "open", "geopend", "ouvert", "geöffnet", "abierto", "dibuka", "개봉", "已开封")},
    **{k: "Built" for k in ("built", "assembled", "gebouwd", "used", "gebruikt", "monté", "gebaut", "montado", "dirakit", "조립", "已拼装")},
    **{k: "Incomplete" for k in ("incomplete", "incompleet", "parts", "incomplet", "unvollständig", "incompleto", "tidak lengkap", "불완전", "不完整")},
    # values stored by versions < 0.9
    "geopend": "Opened", "gebouwd": "Built", "incompleet": "Incomplete",
}
LEGACY_CONDITIONS = {"Geopend": "Opened", "Gebouwd": "Built", "Incompleet": "Incomplete"}


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
        result["fatal"] = T("File too large (max {mb} MB).", mb=MAX_BYTES // 1_000_000)
        return result
    text = text.lstrip("﻿")
    if not text.strip():
        result["fatal"] = T("Empty file.")
        return result
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff(text))
    headers = reader.fieldnames or []
    mapping = _map_headers(headers)
    result["columns"] = {h: FIELD_LABELS[f] for h, f in mapping.items()}
    result["ignored_columns"] = [h for h in headers if h and h not in mapping]
    if "set_number" not in mapping.values():
        result["fatal"] = T("No column with set numbers found. Expected e.g. 'Number' or 'Set Number'. Columns found: {columns}.",
                            columns=", ".join(h for h in headers if h) or "-")
        return result

    seen: dict[str, int] = {}
    for line, raw in enumerate(reader, start=2):
        if line - 1 > MAX_ROWS:
            result["fatal"] = T("Too many lines (max {max}); only the first {max} were checked.", max=MAX_ROWS)
            break
        row = {mapping[k]: (v or "").strip() for k, v in raw.items() if k in mapping and isinstance(v, str)}
        if not any(row.values()):
            continue
        issues: list[tuple[str, str]] = []
        item: dict[str, Any] = {"line": line, "raw_number": row.get("set_number", "")}

        num = normalize_set_number(row.get("set_number", ""))
        if not num:
            issues.append(("error", T("set number missing")))
        elif not re.fullmatch(r"\d{3,7}", num):
            issues.append(("error", T("invalid set number '{value}'", value=row.get("set_number"))))
        item["set_number"] = num

        for f in ("name", "theme", "subtheme", "location", "notes"):
            if row.get(f):
                val = re.sub(r"[\x00-\x1f]", " ", row[f])
                if len(val) > 1 and val[0] == "'" and val[1] in "=+-@'":
                    val = val[1:]          # undo the spreadsheet-safety apostrophe (see models.rows_to_csv)
                if len(val) > MAX_TEXT:
                    issues.append(("warning", T("{field} shortened to {max} characters", field=FIELD_LABELS[f], max=MAX_TEXT)))
                item[f] = val[:MAX_TEXT]
        if cond := normalize_condition(row.get("condition")):
            item["condition"] = cond

        for f, lo, hi in (("year", 1949, today.year + 1), ("pieces", 1, 12000), ("qty", 0, 999)):
            if not row.get(f):
                continue
            # 5.923 / 5,923 / 5 923 is five thousand nine hundred and twenty-three, not 5
            m = re.search(r"-?\d+", re.sub(r"(?<=\d)[.,\s\u00a0\u202f'](?=\d{3}(?!\d))", "", row[f].strip()))
            if not m:
                issues.append(("warning", T("{field} '{value}' is not a number, ignored", field=FIELD_LABELS[f], value=row[f])))
                continue
            n = int(m.group(0))
            if f == "qty" and n <= 0:
                issues.append(("error", T("quantity is {n}", n=n)))
            elif not lo <= n <= hi:
                issues.append(("warning", T("{field} {value} looks unlikely, ignored", field=FIELD_LABELS[f], value=n)))
                continue
            item[f] = n
        if item.get("qty", 1) > 50:
            issues.append(("warning", T("quantity {n} is very high", n=item["qty"])))

        for f in ("rrp", "paid", "current_value"):
            if not row.get(f):
                continue
            if re.match(r"\s*-", row[f]):
                issues.append(("error", T("{field} is negative", field=FIELD_LABELS[f])))
                continue
            p = parse_price(row[f])
            if p is None:
                if re.search(r"[1-9]", row[f]):
                    issues.append(("warning", T("{field} '{value}' not recognised, ignored", field=FIELD_LABELS[f], value=row[f])))
                continue
            if p > 10000:
                issues.append(("warning", T("{field} €{value} looks too high, ignored", field=FIELD_LABELS[f], value=f"{p:.2f}")))
                continue
            item[f] = p
        ref = item.get("rrp") or store["sets"].get(num, {}).get("rrp")
        if ref and item.get("paid") and item["paid"] > ref * 3:
            issues.append(("warning", T("paid €{value} is more than 3× the RRP", value=f"{item['paid']:.2f}")))

        if row.get("added"):
            d = _date(row["added"])
            if d is None:
                issues.append(("warning", T("date '{value}' not recognised, ignored", value=row["added"])))
            elif d > today.isoformat():
                issues.append(("warning", T("purchase date {date} is in the future, ignored", date=d)))
            elif item.get("year") and int(d[:4]) < item["year"] - 1:
                issues.append(("warning", T("purchase date {date} is before the release year {year}", date=d, year=item["year"])))
                item["added"] = d
            else:
                item["added"] = d

        item.setdefault("qty", 1)
        if not item.get("name") and not store["sets"].get(num, {}).get("name"):
            issues.append(("info", T("no name; will be filled in from LEGO.com or Brickset")))
        if num in seen and not any(lvl == "error" for lvl, _ in issues):
            issues.append(("info", T("same set as line {line}: counts as an extra copy", line=seen[num])))
            result["summary"]["merged"] += 1
        elif num and not any(lvl == "error" for lvl, _ in issues):
            seen[num] = line
            if not replace and num in store["collection"]:
                issues.append(("info", T("already in your collection: will be updated")))
                result["summary"]["update"] += 1
            else:
                result["summary"]["new"] += 1

        levels = {lvl for lvl, _ in issues}
        item["status"] = "error" if "error" in levels else "warning" if "warning" in levels else "ok"
        item["issues"] = [{"level": lvl, "text": t} for lvl, t in issues]
        result["summary"][item["status"]] += 1
        result["rows"].append(item)
    if not result["rows"] and not result["fatal"]:
        result["fatal"] = T("No lines with data found.")
    return result


def importable_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in analysis["rows"] if r["status"] != "error"]


def parse_collection_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Backwards compatible helper: (importable rows, messages for skipped rows)."""
    a = analyze_csv(text)
    if a["fatal"] and not a["rows"]:
        return [], [a["fatal"]]
    msgs = [T("line {line}: {issues}", line=r["line"], issues="; ".join(i["text"] for i in r["issues"] if i["level"] == "error"))
            for r in a["rows"] if r["status"] == "error"]
    return importable_rows(a), msgs


COLLECTION_FIELDS = ("qty", "paid", "current_value", "added", "condition", "location", "notes")


def apply_import(store: dict[str, Any], rows: list[dict[str, Any]], replace: bool = False,
                 now: float | None = None) -> dict[str, int]:
    """Merge validated rows into the store. Existing set metadata is kept when already filled."""
    old_hist = {n: e.get("value_history") for n, e in store["collection"].items() if e.get("value_history")}
    if replace:
        store["collection"] = {}
    from .models import COPY_FIELDS, sync_copies

    added = updated = 0
    merged: dict[str, dict[str, Any]] = {}
    lines: dict[str, list[dict[str, Any]]] = {}       # every copy as it was on its own line
    for r in rows:  # same set on several lines = several copies, each with its own price, date, condition, ...
        one = {k: r[k] for k in COPY_FIELDS if r.get(k) not in (None, "")}
        lines.setdefault(r["set_number"], []).extend(dict(one) for _ in range(max(1, int(r.get("qty", 1) or 1))))
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
                if f in ("name", "pieces"):
                    s[f"{f}_source"] = "import"
        entry = store["collection"].get(num)
        new = {k: r[k] for k in COLLECTION_FIELDS if k in r}
        if "current_value" in new:   # keep a value history so the growth chart follows your re-imports
            hist = list((entry or {}).get("value_history") or old_hist.get(num) or [])
            if not hist or abs(hist[-1][1] - new["current_value"]) > 0.005:
                hist.append([now or time.time(), new["current_value"]])
            new["value_history"] = hist[-500:]
        copies = lines.get(num, [])
        if len({tuple(sorted(c.items())) for c in copies}) > 1:
            new["items"] = copies                      # copies that differ: each its own
        elif entry is not None and entry.get("items"):
            if len(entry["items"]) != len(copies) or len(copies) == 1:
                entry.pop("items")                     # another quantity or one copy: the line's fields count again
            else:
                new = {k: v for k, v in new.items() if k not in COPY_FIELDS and k != "qty"}   # keep your own copies
        if entry is None:
            store["collection"][num] = new
            sync_copies(new)
            added += 1
        else:
            entry.update(new)
            sync_copies(entry)
            updated += 1
    return {"added": added, "updated": updated}
