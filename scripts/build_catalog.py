"""Build custom_components/lego_tracker/data/sets.json (the built-in set catalogue) from the sets a
Home Assistant instance already looked up on LEGO.com.

Input: the rows of the integration's websocket command `lego_tracker/overview` (field `sets`), or the
compact export used for the first catalogue (a list of dicts with num, name, rrp, rrp_src, ...).

    python scripts/build_catalog.py export.json            # merges into the existing catalogue

Only sets whose LEGO.com page was really read (checked on LEGO.com, or an RRP from LEGO.com) are taken.
"In / out of stock" is only marked with evidence: a past exit date or LEGO.com saying unavailable
means retired; LEGO.com selling it (and a recent set, or an exit date in the future) means retail.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "custom_components/lego_tracker/data/sets.json"


def _row(x: dict) -> dict:
    """Accept both an overview card and the compact export."""
    if "num" in x:
        return x
    lego = (x.get("offers") or {}).get("lego_com") or {}
    return {"num": x.get("set_number"), "name": x.get("name"), "rrp": x.get("rrp"), "rrp_src": x.get("rrp_source"),
            "theme": x.get("theme"), "subtheme": x.get("subtheme"), "year": x.get("year"), "pieces": x.get("pieces"),
            "image": x.get("image"), "image_src": x.get("image_source"), "exit_date": x.get("exit_date"),
            "lego_url": lego.get("url"), "lego_available": lego.get("available"), "ean": x.get("ean"),
            "lego_checked": x.get("lego_checked")}


def status(x: dict, today: date) -> str | None:
    exit_date = x.get("exit_date")
    if (exit_date and exit_date < today.isoformat()) or x.get("lego_available") is False:
        return "retired"
    if x.get("lego_available") and ((exit_date and exit_date >= today.isoformat()) or (x.get("year") or 0) >= today.year - 3):
        return "retail"
    return None                        # LEGO.com shows old sets too: don't guess


def entry(x: dict, today: date) -> dict | None:
    lego_rrp = x.get("rrp_src") == "LEGO.com"
    if not (x.get("lego_checked") or lego_rrp) or not x.get("num") or not x.get("name"):   # really read on LEGO.com
        return None
    e = {"name": x.get("name"), "rrp": x.get("rrp") if lego_rrp else None, "theme": x.get("theme"),
         "subtheme": x.get("subtheme"), "year": x.get("year"), "pieces": x.get("pieces"),
         "image": x.get("image") if str(x.get("image") or "").startswith("https://www.lego.com/") else None,   # LEGO images only
         "ean": x.get("ean"), "lego_url": x.get("lego_url"), "exit_date": x.get("exit_date"), "status": status(x, today)}
    return {k: v for k, v in e.items() if v not in (None, "")}


def main(path: str) -> None:
    raw = json.loads(Path(path).read_text("utf-8"))
    rows = raw.get("sets", raw) if isinstance(raw, dict) else raw
    today = date.today()
    cat = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {"locale": "nl-be", "sets": {}}
    added = 0
    for x in map(_row, rows):
        if (e := entry(x, today)):
            added += x["num"] not in cat["sets"]
            cat["sets"][x["num"]] = {**cat["sets"].get(x["num"], {}), **e}
    cat["generated"] = time.strftime("%Y-%m-%d")
    cat["sets"] = dict(sorted(cat["sets"].items(), key=lambda kv: (len(kv[0]), kv[0])))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cat, ensure_ascii=False, indent=0, sort_keys=False) + "\n", "utf-8")
    print(f"{len(cat['sets'])} sets in the catalogue ({added} new) -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
