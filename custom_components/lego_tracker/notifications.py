"""Notification rules: which sets, which events, to whom and how.

A rule = scope (all / watchlist / collection / themes / sets) + triggers (with parameters) +
targets (mobile app, any notify service, notify entity, e-mail, HA notification, TTS, event).
Rules live in the store (no reload needed) and are edited in the panel.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, RETAILERS
from .i18n import LocalizedError, T, tr
from .models import normalize_set_number

if TYPE_CHECKING:
    from .coordinator import LegoCoordinator

_LOGGER = logging.getLogger(__name__)
EVENT_NOTIFICATION = f"{DOMAIN}_notification"

# trigger id -> (label, per-set?, parameter)
TRIGGERS: dict[str, tuple[str, bool, str | None]] = {
    "all_time_low": ("All-time low", True, None),
    "discount": ("Discount vs RRP", True, "discount_pct"),
    "target_hit": ("Target price reached", True, None),
    "price_below": ("Price below an amount", True, "price_below"),
    "price_drop": ("Price drop", True, "drop_pct"),
    "deal_score": ("Deal score reached", True, "min_score"),
    "retiring_soon": ("Retiring soon", True, None),
    "back_in_stock": ("Back in stock / first price", True, None),
    "any_change": ("Any price change", True, None),
    "digest": ("Daily digest", False, None),
    "new_set": ("New LEGO set announced", False, None),
    "catalog_deal": ("Deal on a set you don't follow", False, None),
    "job_done": ("Job finished (refresh, link search…)", False, None),
    "problems": ("Problems (shop paused, errors)", False, None),
}
SCOPES = ("all", "watchlist", "collection", "themes", "sets")
TARGET_TYPES = ("mobile", "notify", "entity", "email", "persistent", "tts", "event")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def default_rules(threshold: float, notify_service: str = "") -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = [{"type": "persistent"}]
    if notify_service:
        svc = notify_service if notify_service.startswith("notify.") else f"notify.{notify_service}"
        targets.append({"type": "mobile" if "mobile_app_" in svc else "notify", "service": svc})
    return [
        {"id": "deals", "name": "All deals", "enabled": True, "scope": {"type": "all"},
         "triggers": ["all_time_low", "discount", "target_hit"], "params": {"discount_pct": int(threshold)},
         "shops": [], "targets": targets, "cooldown_hours": 24, "quiet": None, "image": True, "link": True},
        {"id": "digest", "name": "Daily digest", "enabled": True, "scope": {"type": "all"},
         "triggers": ["digest"], "params": {}, "shops": [], "targets": list(targets), "cooldown_hours": 0,
         "quiet": None, "image": False, "link": False},
    ]


def validate_rules(rules: Any) -> list[dict[str, Any]]:
    """Clean user input. Raises LocalizedError (English template, translated)."""
    if not isinstance(rules, list) or len(rules) > 50:
        raise LocalizedError("Invalid list of rules (max 50).")
    out = []
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise LocalizedError("Rule {n} is invalid.", n=i)
        name = str(r.get("name") or T("Rule {n}", n=i)).strip()[:60]
        scope = r.get("scope") or {"type": "all"}
        stype = scope.get("type", "all")
        if stype not in SCOPES:
            raise LocalizedError("{rule}: unknown choice of sets.", rule=name)
        themes = [str(t)[:60] for t in scope.get("themes", []) if t][:50]
        sets = list(dict.fromkeys(s for s in (normalize_set_number(x) for x in scope.get("sets", []))
                                  if re.fullmatch(r"\d{3,7}", s)))[:200]
        if stype == "themes" and not themes:
            raise LocalizedError("{rule}: choose at least one theme.", rule=name)
        if stype == "sets" and not sets:
            raise LocalizedError("{rule}: choose at least one set.", rule=name)
        triggers = [t for t in r.get("triggers", []) if t in TRIGGERS]
        if not triggers:
            raise LocalizedError("{rule}: choose at least one event.", rule=name)
        p = r.get("params") or {}
        params: dict[str, float] = {}
        for key, lo, hi in (("discount_pct", 1, 95), ("price_below", 0.01, 10000), ("drop_pct", 1, 95), ("min_score", 1, 100)):
            if key in p and p[key] not in (None, ""):
                try:
                    v = float(p[key])
                except (TypeError, ValueError) as err:
                    raise LocalizedError("{rule}: {field} is not a number.", rule=name, field=key) from err
                if not lo <= v <= hi:
                    raise LocalizedError("{rule}: {field} must be between {lo} and {hi}.", rule=name, field=key, lo=lo, hi=hi)
                params[key] = v
        for t in triggers:
            need = TRIGGERS[t][2]
            if need and need not in params:
                raise LocalizedError("{rule}: enter a value for '{event}'.", rule=name, event=TRIGGERS[t][0])
        targets = []
        for t in r.get("targets", [])[:10]:
            tt = t.get("type")
            if tt not in TARGET_TYPES:
                raise LocalizedError("{rule}: unknown way of sending.", rule=name)
            clean: dict[str, Any] = {"type": tt}
            if tt in ("mobile", "notify", "email"):
                svc = str(t.get("service") or "").strip()
                if not re.fullmatch(r"notify\.[a-z0-9_]+", svc):
                    raise LocalizedError("{rule}: choose a notify service.", rule=name)
                clean["service"] = svc
            if tt == "email":
                addrs = [a.strip() for a in re.split(r"[,;\s]+", str(t.get("to") or "")) if a.strip()]
                if not addrs or not all(EMAIL_RE.match(a) for a in addrs):
                    raise LocalizedError("{rule}: invalid e-mail address.", rule=name)
                clean["to"] = addrs[:10]
            if tt == "entity":
                ent = str(t.get("entity_id") or "")
                if not re.fullmatch(r"notify\.[a-z0-9_]+", ent):
                    raise LocalizedError("{rule}: choose a notify entity.", rule=name)
                clean["entity_id"] = ent
            if tt == "tts":
                tts, mp = str(t.get("tts") or ""), str(t.get("media_player") or "")
                if not re.fullmatch(r"tts\.[a-z0-9_]+", tts) or not re.fullmatch(r"media_player\.[a-z0-9_]+", mp):
                    raise LocalizedError("{rule}: choose a speech service and a media player.", rule=name)
                clean.update(tts=tts, media_player=mp)
            targets.append(clean)
        if not targets:
            raise LocalizedError("{rule}: choose at least one recipient.", rule=name)
        quiet = r.get("quiet")
        if quiet:
            if not (TIME_RE.match(str(quiet.get("from", ""))) and TIME_RE.match(str(quiet.get("to", "")))):
                raise LocalizedError("{rule}: quiet hours as HH:MM.", rule=name)
            quiet = {"from": quiet["from"], "to": quiet["to"]}
        try:
            cooldown = max(0, min(24 * 30, float(r.get("cooldown_hours", 24))))
        except (TypeError, ValueError):
            cooldown = 24
        out.append({
            "id": str(r.get("id") or uuid.uuid4().hex[:8])[:16], "name": name, "enabled": bool(r.get("enabled", True)),
            "scope": {"type": stype, "themes": themes, "sets": sets}, "triggers": triggers, "params": params,
            "shops": [s for s in r.get("shops", []) if s in RETAILERS], "targets": targets,
            "cooldown_hours": cooldown, "quiet": quiet, "image": bool(r.get("image", True)), "link": bool(r.get("link", True)),
        })
    ids = [r["id"] for r in out]
    if len(set(ids)) != len(ids):
        raise LocalizedError("Two rules have the same id.")
    return out


def in_quiet(quiet: dict[str, str] | None, now: datetime) -> bool:
    if not quiet:
        return False
    cur = now.strftime("%H:%M")
    a, b = quiet["from"], quiet["to"]
    return a <= cur < b if a < b else (cur >= a or cur < b)


def set_triggers(rule: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str]]:
    """Which of the rule's per-set triggers fire for this status change: [(trigger, reason)]."""
    p, out = rule["params"], []
    price, old = after.get("best_price"), before.get("best_price")
    if price is None:
        return out
    if rule["shops"] and after.get("best_retailer") not in rule["shops"]:
        return out
    for t in rule["triggers"]:
        if t == "all_time_low" and after.get("is_all_time_low") and not before.get("is_all_time_low"):
            out.append((t, tr("all-time low")))
        elif t == "discount" and (d := after.get("discount_rrp")) is not None and d >= p["discount_pct"] \
                and (before.get("discount_rrp") is None or before["discount_rrp"] < p["discount_pct"] or old is None):
            out.append((t, tr("−{pct}% vs RRP", pct=f"{d:.0f}")))
        elif t == "target_hit" and after.get("target_hit") and not before.get("target_hit"):
            out.append((t, tr("below your target price")))
        elif t == "price_below" and price <= p["price_below"] and (old is None or old > p["price_below"]):
            out.append((t, tr("below €{price}", price=f"{p['price_below']:.2f}")))
        elif t == "price_drop" and old and price < old and (old - price) / old * 100 >= p["drop_pct"]:
            out.append((t, tr("dropped {pct}% (was €{old})", pct=f"{(old - price) / old * 100:.0f}", old=f"{old:.2f}")))
        elif t == "deal_score" and after.get("deal_score", 0) >= p["min_score"] > before.get("deal_score", 0):
            out.append((t, tr("deal score {score}", score=after["deal_score"])))
        elif t == "retiring_soon" and after.get("retiring_soon") and not before.get("retiring_soon"):
            out.append((t, tr("retiring soon")))
        elif t == "back_in_stock" and old is None:
            out.append((t, tr("available again")))
        elif t == "any_change" and old is not None and abs(price - old) >= 0.01:
            out.append((t, tr("price changed (was €{old})", old=f"{old:.2f}")))
    return out


class Notifier:
    def __init__(self, coord: LegoCoordinator) -> None:
        self.coord = coord
        self.hass: HomeAssistant = coord.hass

    # ------------------------------------------------------------------ data
    @property
    def store(self) -> dict[str, Any]:
        return self.coord.store

    @property
    def rules(self) -> list[dict[str, Any]]:
        return self.store.setdefault("notify_rules", [])

    def in_scope(self, rule: dict[str, Any], num: str) -> bool:
        sc = rule["scope"]
        owned = num in self.store["collection"]
        s = self.store["sets"].get(num, {})
        return {"all": True, "watchlist": self.coord.is_watched(num), "collection": owned,
                "themes": s.get("theme") in sc.get("themes", []), "sets": num in sc.get("sets", [])}[sc["type"]]

    def _cooled(self, key: str, hours: float) -> bool:
        sent = self.store.setdefault("notify_sent", {})
        now = time.time()
        if hours and now - sent.get(key, 0) < hours * 3600:
            return False
        sent[key] = now
        if len(sent) > 5000:   # prune
            for k in sorted(sent, key=sent.get)[:1000]:
                del sent[k]
        return True

    def shop_link(self, num: str, status: dict[str, Any]) -> str | None:
        """Return the selected notification offer's product URL, or None if unavailable."""
        return self.shop_offer(num, status)[2]

    def shop_offer(self, num: str, status: dict[str, Any],
                   shops: list[str] | None = None) -> tuple[float | None, str | None, str | None]:
        """The product page at the cheapest shop (never a comparison page or a search page when a real
        product link exists), so one tap opens the item where it is cheapest."""
        from .compare import is_compare_url
        from .parsers import is_search_url

        best = status.get("best_url")
        selected = (status.get("best_price"), status.get("best_retailer"), best)
        if best and not is_compare_url(best) and not is_search_url(best):
            return selected
        offers = self.store["offers"].get(num, {})
        priced = sorted((o["last_price"], rid, o["url"]) for rid, o in offers.items()
                        if (not shops or rid in shops) and o.get("available") and o.get("last_price") and o.get("url")
                        and not is_compare_url(o["url"]) and not is_search_url(o["url"]))
        return priced[0] if priced else selected

    # ---------------------------------------------------------------- events
    async def on_set_change(self, num: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        """Send enabled, in-scope rule notifications for triggers outside their cooldowns."""
        s = self.store["sets"].get(num, {})
        blocked = self.coord.deal_blocked(num, after)        # left out under Deals → Settings
        for rule in self.rules:
            if not rule.get("enabled") or not self.in_scope(rule, num):
                continue
            if blocked and rule["scope"]["type"] != "sets":  # sets you picked by hand in a rule still notify
                continue
            hits = [h for h in set_triggers(rule, before, after)
                    if self._cooled(f"{rule['id']}|{num}|{h[0]}", rule.get("cooldown_hours", 24))]
            if not hits:
                continue
            price, retailer, url = self.shop_offer(num, after, rule["shops"])
            shop = RETAILERS.get(retailer, ("",))[0]
            title = f"🧱 {num} {s.get('name') or ''}".strip()
            message = tr("€{price} at {shop}", price=f"{price:.2f}", shop=shop) + ": " + ", ".join(h[1] for h in hits)
            await self.send(rule, title, message, url=url,
                            image=s.get("image") if rule.get("image") else None,
                            data={"set_number": num, "triggers": [h[0] for h in hits], "price": price})

    async def on_digest(self, digest: dict[str, Any]) -> None:
        """Send each enabled digest rule the deals allowed by its scope and shop filters."""
        for rule in self.rules:
            if not rule.get("enabled") or "digest" not in rule["triggers"]:
                continue
            deals = [d for d in digest["deals"] if self.in_scope(rule, d["set_number"])
                     and (not rule["shops"] or d["retailer"] in rule["shops"])
                     and (rule["scope"]["type"] == "sets" or not self.coord.deal_blocked(d["set_number"]))]
            if not deals:
                continue
            lines = [f"• {d['set_number']} {d['name'] or ''}: " + tr("€{price} at {shop}", price=f"{d['price']:.2f}", shop=RETAILERS.get(d["retailer"], ("",))[0])
                     + (f" (−{d['discount']:.0f}%)" if d.get("discount") else "") + (" 🔻" if d.get("all_time_low") else "")
                     + (f"\n  {u}" if (u := d.get("url")) else "")
                     for d in deals[:15]]
            more = "\n" + tr("… and {n} more", n=len(deals) - 15) if len(deals) > 15 else ""
            await self.send(rule, "🧱 " + tr("LEGO deals today ({n})", n=len(deals)), "\n".join(lines) + more,
                            data={"deals": [d["set_number"] for d in deals]}, notification_id=f"{DOMAIN}_digest_{rule['id']}")

    async def on_new_sets(self, nums: list[str]) -> None:
        """New sets in the LEGO set database (themes switched off under Deals → Settings are already left out).
        A rule for certain themes only hears about new sets in those themes."""
        from .parsers import lego_product_url
        from .setdb import THEME, as_set

        db = self.coord.setdb
        for rule in self.rules:
            if not rule.get("enabled") or "new_set" not in rule["triggers"]:
                continue
            sc = rule["scope"]
            mine = [n for n in nums if n in db and (sc["type"] != "themes" or db[n][THEME] in sc.get("themes", []))]
            if not mine:
                continue
            sets = [as_set(n, db[n]) for n in mine]
            lines = [f"• {x['set_number']} {x['name']}" + (f" ({x['theme']}, {x['year']}, " + tr("{n} pieces", n=x["pieces"]) + ")"
                                                          if x["theme"] and x["pieces"] else "") for x in sets[:15]]
            more = "\n" + tr("… and {n} more", n=len(sets) - 15) if len(sets) > 15 else ""
            title = "🆕 " + (tr("New LEGO set: {name}", name=f"{sets[0]['set_number']} {sets[0]['name']}") if len(sets) == 1
                             else tr("{n} new LEGO sets", n=len(sets)))
            await self.send(rule, title, "\n".join(lines) + more, url=lego_product_url(sets[0]["set_number"]) if len(sets) == 1 else None,
                            image=sets[0]["image"] if len(sets) == 1 and rule.get("image") else None,
                            data={"new_sets": mine}, notification_id=f"{DOMAIN}_new_sets_{rule['id']}")

    async def on_catalog_deal(self, num: str, e: dict[str, Any]) -> None:
        """A deal found on a set of the LEGO set database that you don't follow (Deals → All LEGO sets).
        A rule for certain themes only hears about those themes; switched-off themes never get here."""
        from .setdb import as_set

        db = self.coord.setdb
        if num not in db or not e.get("price"):
            return
        x = as_set(num, db[num])
        shop = RETAILERS.get(e.get("shop"), (e.get("shop") or "",))[0]
        text = tr("€{price} at {shop}", price=f"{e['price']:.2f}", shop=shop)
        if e.get("rrp") and e.get("deal") is not None:
            text += " · " + tr("{pct}% below RRP (€{rrp})", pct=f"{e['deal']:.0f}", rrp=f"{e['rrp']:.2f}")
        if x["theme"]:
            text += f"\n{x['theme']}" + (f", {x['year']}" if x["year"] else "")
        for rule in self.rules:
            if not rule.get("enabled") or "catalog_deal" not in rule["triggers"]:
                continue
            sc = rule["scope"]
            if sc["type"] == "themes" and x["theme"] not in sc.get("themes", []):
                continue
            await self.send(rule, "🏷️ " + tr("Deal: {name}", name=f"{num} {x['name']}"), text, url=e.get("url"),
                            image=x["image"] if rule.get("image") else None,
                            data={"set_number": num, "price": e["price"], "retailer": e.get("shop"), "discount": e.get("deal")},
                            notification_id=f"{DOMAIN}_catalog_{num}_{rule['id']}")

    async def on_job_done(self, job: dict[str, Any]) -> None:
        label = tr(job.get("label") or "Job")
        text = (tr("{label} stopped: {done}/{total} sets", label=label, done=job["done"], total=job["total"]) if job.get("cancelled")
                else tr("{label} finished: {done}/{total} sets", label=label, done=job["done"], total=job["total"]))
        text += "".join(", " + tr(t, n=job[k]) for k, t in (("updated", "{n} updated"), ("found", "{n} links found"),
                                                          ("errors", "{n} errors")) if job.get(k))
        for rule in self.rules:
            if not rule.get("enabled"):
                continue
            if "job_done" in rule["triggers"]:
                await self.send(rule, "🧱 LEGO Organizing Tool", text)
            elif "problems" in rule["triggers"] and job.get("errors") and self._cooled(f"{rule['id']}|job_errors", rule.get("cooldown_hours", 24)):
                await self.send(rule, "⚠️ LEGO Organizing Tool", text)

    async def on_shop_paused(self, retailer: str, hours: float) -> None:
        for rule in self.rules:
            if rule.get("enabled") and "problems" in rule["triggers"] \
                    and self._cooled(f"{rule['id']}|pause|{retailer}", rule.get("cooldown_hours", 24)):
                await self.send(rule, "⚠️ " + tr("Shop paused"),
                                tr("{shop} blocked the price check and is paused for {hours} h.",
                                   shop=RETAILERS.get(retailer, (retailer,))[0], hours=f"{hours:.0f}"))

    # -------------------------------------------------------------- sending
    async def send(self, rule: dict[str, Any], title: str, message: str, *, url: str | None = None,
                   image: str | None = None, data: dict[str, Any] | None = None, notification_id: str | None = None,
                   force: bool = False) -> list[dict[str, Any]]:
        """Send to every target of the rule; queued during quiet hours (except HA notification/event)."""
        results = []
        quiet = not force and in_quiet(rule.get("quiet"), dt_util.now())
        self.hass.bus.async_fire(EVENT_NOTIFICATION, {"rule": rule["id"], "rule_name": rule["name"], "title": title,
                                                      "message": message, "url": url, "image": image, **(data or {})})
        for target in rule["targets"]:
            if quiet and target["type"] not in ("persistent", "event"):
                q = self.store.setdefault("notify_queue", {}).setdefault(rule["id"], [])
                q.append({"title": title, "message": message, "url": url})
                del q[:-50]
                results.append({"target": target, "ok": True, "queued": True})
                continue
            try:
                await self._send_one(target, title, message, url, image, notification_id)
                results.append({"target": target, "ok": True})
            except Exception as err:  # noqa: BLE001 - a broken target must not break the rest
                _LOGGER.warning("Notification via %s failed: %s", target, err)
                results.append({"target": target, "ok": False, "error": str(err)})
        self.coord.log("ok" if all(r["ok"] for r in results) else "error", "notify",
                       f"{rule['name']}: {title} — " + (T("queued (quiet hours)") if quiet else
                       ", ".join(("✓ " if r["ok"] else "✕ ") + r["target"]["type"] + ("" if r["ok"] else f" ({r.get('error')})") for r in results)),
                       set_number=(data or {}).get("set_number"))
        log = self.store.setdefault("notify_log", [])
        log.append({"ts": time.time(), "rule": rule["name"], "title": title, "message": message[:300],
                    "ok": all(r["ok"] for r in results), "queued": quiet})
        del log[:-100]
        return results

    async def _send_one(self, t: dict[str, Any], title: str, message: str, url: str | None, image: str | None,
                        notification_id: str | None) -> None:
        call = self.hass.services.async_call
        full = message + (f"\n{url}" if url and t["type"] in ("email", "notify", "entity") else "")
        if t["type"] == "persistent":
            await call("persistent_notification", "create",
                       {"title": title, "message": message + (f"\n\n[{tr('Open')}]({url})" if url else ""),
                        **({"notification_id": notification_id} if notification_id else {})}, blocking=True)
        elif t["type"] == "mobile":
            extra: dict[str, Any] = {}
            if url:
                extra.update(url=url, clickAction=url)
            if image:
                extra["image"] = image
            await call("notify", t["service"].split(".", 1)[1],
                       {"title": title, "message": message, **({"data": extra} if extra else {})}, blocking=True)
        elif t["type"] == "notify":
            await call("notify", t["service"].split(".", 1)[1], {"title": title, "message": full}, blocking=True)
        elif t["type"] == "email":
            html = (f"<h3>{title}</h3><p>{message.replace(chr(10), '<br>')}</p>"
                    + (f'<p><a href="{url}">{tr("View in the shop")}</a></p>' if url else "")
                    + (f'<img src="{image}" width="240">' if image else ""))
            await call("notify", t["service"].split(".", 1)[1],
                       {"title": title, "message": full, "target": t["to"], "data": {"html": html}}, blocking=True)
        elif t["type"] == "entity":
            await call("notify", "send_message", {"entity_id": t["entity_id"], "title": title, "message": full}, blocking=True)
        elif t["type"] == "tts":
            await call("tts", "speak", {"entity_id": t["tts"], "media_player_entity_id": t["media_player"],
                                        "message": f"{title}. {message}".replace("€", "EUR ").replace("🧱", "")}, blocking=True)
        # "event": the lego_tracker_notification event was already fired

    async def flush_queues(self, _now: Any = None) -> None:
        """After quiet hours: one bundled message per rule."""
        queue = self.store.get("notify_queue") or {}
        now = dt_util.now()
        for rule in self.rules:
            items = queue.get(rule["id"])
            if not items or in_quiet(rule.get("quiet"), now):
                continue
            queue[rule["id"]] = []
            body = "\n".join(f"• {i['title']}: {i['message']}" for i in items[:15])
            await self.send(rule, "🧱 " + tr("{n} LEGO notifications (quiet hours)", n=len(items)), body, force=True)

    # --------------------------------------------------------------- options
    def ha_options(self) -> dict[str, Any]:
        """Everything the panel dropdowns need: services, entities, themes, sets."""
        from homeassistant.helpers import device_registry as dr

        services = sorted(self.hass.services.async_services_for_domain("notify"))
        devs = {d.name_by_user or d.name for d in dr.async_get(self.hass).devices.values()}
        notify = []
        for svc in services:
            if svc in ("send_message", "persistent_notification"):
                continue
            kind = ("mobile" if svc.startswith("mobile_app_") else
                    "email" if re.search(r"smtp|mail|gmail|outlook", svc) else "notify")
            label = svc.replace("mobile_app_", "").replace("_", " ")
            match = next((d for d in devs if d and re.sub(r"[^a-z0-9]+", "_", d.lower()).strip("_") == svc.replace("mobile_app_", "")), None)
            notify.append({"service": f"notify.{svc}", "label": match or label.title(), "kind": kind})
        states = self.hass.states
        ents = lambda domain: [{"entity_id": s.entity_id, "label": s.name} for s in states.async_all(domain)]  # noqa: E731
        themes: dict[str, list[str]] = {}
        for s in self.store["sets"].values():
            if s.get("theme"):
                themes.setdefault(s["theme"], [])
        return {
            "notify": notify, "notify_entities": ents("notify"), "tts": ents("tts"), "media_players": ents("media_player"),
            "persons": [{"entity_id": s.entity_id, "label": s.name} for s in states.async_all("person")],
            "themes": sorted(themes), "triggers": {k: {"label": v[0], "per_set": v[1], "param": v[2]} for k, v in TRIGGERS.items()},
            "retailers": {k: v[0] for k, v in RETAILERS.items()},
        }
