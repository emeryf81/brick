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
from .models import normalize_set_number

if TYPE_CHECKING:
    from .coordinator import LegoCoordinator

_LOGGER = logging.getLogger(__name__)
EVENT_NOTIFICATION = f"{DOMAIN}_notification"

# trigger id -> (label, per-set?, parameter)
TRIGGERS: dict[str, tuple[str, bool, str | None]] = {
    "all_time_low": ("Laagste prijs ooit", True, None),
    "discount": ("Korting t.o.v. adviesprijs", True, "discount_pct"),
    "target_hit": ("Streefprijs bereikt", True, None),
    "price_below": ("Prijs onder een bedrag", True, "price_below"),
    "price_drop": ("Prijsdaling", True, "drop_pct"),
    "deal_score": ("Dealscore bereikt", True, "min_score"),
    "retiring_soon": ("Verdwijnt binnenkort", True, None),
    "back_in_stock": ("Weer leverbaar / eerste prijs", True, None),
    "any_change": ("Elke prijswijziging", True, None),
    "digest": ("Dagelijkse samenvatting", False, None),
    "job_done": ("Taak klaar (verversen, links zoeken…)", False, None),
    "problems": ("Problemen (winkel gepauzeerd, fouten)", False, None),
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
        {"id": "deals", "name": "Alle deals", "enabled": True, "scope": {"type": "all"},
         "triggers": ["all_time_low", "discount", "target_hit"], "params": {"discount_pct": int(threshold)},
         "shops": [], "targets": targets, "cooldown_hours": 24, "quiet": None, "image": True, "link": True},
        {"id": "digest", "name": "Dagelijkse samenvatting", "enabled": True, "scope": {"type": "all"},
         "triggers": ["digest"], "params": {}, "shops": [], "targets": list(targets), "cooldown_hours": 0,
         "quiet": None, "image": False, "link": False},
    ]


def validate_rules(rules: Any) -> list[dict[str, Any]]:
    """Clean user input. Raises ValueError with a Dutch message."""
    if not isinstance(rules, list) or len(rules) > 50:
        raise ValueError("Ongeldige lijst met regels (max 50).")
    out = []
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise ValueError(f"Regel {i} is ongeldig.")
        name = str(r.get("name") or f"Regel {i}").strip()[:60]
        scope = r.get("scope") or {"type": "all"}
        stype = scope.get("type", "all")
        if stype not in SCOPES:
            raise ValueError(f"{name}: onbekende keuze van sets.")
        themes = [str(t)[:60] for t in scope.get("themes", []) if t][:50]
        sets = list(dict.fromkeys(s for s in (normalize_set_number(x) for x in scope.get("sets", []))
                                  if re.fullmatch(r"\d{3,7}", s)))[:200]
        if stype == "themes" and not themes:
            raise ValueError(f"{name}: kies minstens één thema.")
        if stype == "sets" and not sets:
            raise ValueError(f"{name}: kies minstens één set.")
        triggers = [t for t in r.get("triggers", []) if t in TRIGGERS]
        if not triggers:
            raise ValueError(f"{name}: kies minstens één gebeurtenis.")
        p = r.get("params") or {}
        params: dict[str, float] = {}
        for key, lo, hi in (("discount_pct", 1, 95), ("price_below", 0.01, 10000), ("drop_pct", 1, 95), ("min_score", 1, 100)):
            if key in p and p[key] not in (None, ""):
                try:
                    v = float(p[key])
                except (TypeError, ValueError) as err:
                    raise ValueError(f"{name}: {key} is geen getal.") from err
                if not lo <= v <= hi:
                    raise ValueError(f"{name}: {key} moet tussen {lo} en {hi} liggen.")
                params[key] = v
        for t in triggers:
            need = TRIGGERS[t][2]
            if need and need not in params:
                raise ValueError(f"{name}: vul een waarde in voor '{TRIGGERS[t][0]}'.")
        targets = []
        for t in r.get("targets", [])[:10]:
            tt = t.get("type")
            if tt not in TARGET_TYPES:
                raise ValueError(f"{name}: onbekende manier van versturen.")
            clean: dict[str, Any] = {"type": tt}
            if tt in ("mobile", "notify", "email"):
                svc = str(t.get("service") or "").strip()
                if not re.fullmatch(r"notify\.[a-z0-9_]+", svc):
                    raise ValueError(f"{name}: kies een notify-service.")
                clean["service"] = svc
            if tt == "email":
                addrs = [a.strip() for a in re.split(r"[,;\s]+", str(t.get("to") or "")) if a.strip()]
                if not addrs or not all(EMAIL_RE.match(a) for a in addrs):
                    raise ValueError(f"{name}: ongeldig e-mailadres.")
                clean["to"] = addrs[:10]
            if tt == "entity":
                ent = str(t.get("entity_id") or "")
                if not re.fullmatch(r"notify\.[a-z0-9_]+", ent):
                    raise ValueError(f"{name}: kies een notify-entiteit.")
                clean["entity_id"] = ent
            if tt == "tts":
                tts, mp = str(t.get("tts") or ""), str(t.get("media_player") or "")
                if not re.fullmatch(r"tts\.[a-z0-9_]+", tts) or not re.fullmatch(r"media_player\.[a-z0-9_]+", mp):
                    raise ValueError(f"{name}: kies een spraakdienst én een mediaspeler.")
                clean.update(tts=tts, media_player=mp)
            targets.append(clean)
        if not targets:
            raise ValueError(f"{name}: kies minstens één ontvanger.")
        quiet = r.get("quiet")
        if quiet:
            if not (TIME_RE.match(str(quiet.get("from", ""))) and TIME_RE.match(str(quiet.get("to", "")))):
                raise ValueError(f"{name}: stille uren als UU:MM.")
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
        raise ValueError("Twee regels hebben dezelfde id.")
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
            out.append((t, "laagste prijs ooit"))
        elif t == "discount" and (d := after.get("discount_rrp")) is not None and d >= p["discount_pct"] \
                and (before.get("discount_rrp") is None or before["discount_rrp"] < p["discount_pct"] or old is None):
            out.append((t, f"−{d:.0f}% t.o.v. adviesprijs"))
        elif t == "target_hit" and after.get("target_hit") and not before.get("target_hit"):
            out.append((t, "onder je streefprijs"))
        elif t == "price_below" and price <= p["price_below"] and (old is None or old > p["price_below"]):
            out.append((t, f"onder €{p['price_below']:.2f}"))
        elif t == "price_drop" and old and price < old and (old - price) / old * 100 >= p["drop_pct"]:
            out.append((t, f"gedaald met {(old - price) / old * 100:.0f}% (was €{old:.2f})"))
        elif t == "deal_score" and after.get("deal_score", 0) >= p["min_score"] > before.get("deal_score", 0):
            out.append((t, f"dealscore {after['deal_score']}"))
        elif t == "retiring_soon" and after.get("retiring_soon") and not before.get("retiring_soon"):
            out.append((t, "verdwijnt binnenkort"))
        elif t == "back_in_stock" and old is None:
            out.append((t, "weer te koop"))
        elif t == "any_change" and old is not None and abs(price - old) >= 0.01:
            out.append((t, f"prijs gewijzigd (was €{old:.2f})"))
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
        return {"all": True, "watchlist": not owned, "collection": owned,
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

    # ---------------------------------------------------------------- events
    async def on_set_change(self, num: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        s = self.store["sets"].get(num, {})
        for rule in self.rules:
            if not rule.get("enabled") or not self.in_scope(rule, num):
                continue
            hits = [h for h in set_triggers(rule, before, after)
                    if self._cooled(f"{rule['id']}|{num}|{h[0]}", rule.get("cooldown_hours", 24))]
            if not hits:
                continue
            shop = RETAILERS.get(after.get("best_retailer"), ("",))[0]
            title = f"🧱 {num} {s.get('name') or ''}".strip()
            message = f"€{after['best_price']:.2f} bij {shop}: " + ", ".join(h[1] for h in hits)
            await self.send(rule, title, message, url=after.get("best_url") if rule.get("link") else None,
                            image=s.get("image") if rule.get("image") else None,
                            data={"set_number": num, "triggers": [h[0] for h in hits], "price": after["best_price"]})

    async def on_digest(self, digest: dict[str, Any]) -> None:
        for rule in self.rules:
            if not rule.get("enabled") or "digest" not in rule["triggers"]:
                continue
            deals = [d for d in digest["deals"] if self.in_scope(rule, d["set_number"])
                     and (not rule["shops"] or d["retailer"] in rule["shops"])]
            if not deals:
                continue
            lines = [f"• {d['set_number']} {d['name'] or ''}: €{d['price']:.2f} bij {RETAILERS.get(d['retailer'], ('',))[0]}"
                     + (f" (−{d['discount']:.0f}%)" if d.get("discount") else "") + (" 🔻" if d.get("all_time_low") else "")
                     for d in deals[:15]]
            more = f"\n… en nog {len(deals) - 15}" if len(deals) > 15 else ""
            await self.send(rule, f"🧱 LEGO-deals vandaag ({len(deals)})", "\n".join(lines) + more,
                            data={"deals": [d["set_number"] for d in deals]}, notification_id=f"{DOMAIN}_digest_{rule['id']}")

    async def on_job_done(self, job: dict[str, Any]) -> None:
        text = (f"{job.get('label', 'Taak')} {'gestopt' if job.get('cancelled') else 'klaar'}: {job['done']}/{job['total']} sets"
                + (f", {job['updated']} bijgewerkt" if job.get("updated") else "")
                + (f", {job['found']} links gevonden" if job.get("found") else "")
                + (f", {job['errors']} fouten" if job.get("errors") else ""))
        for rule in self.rules:
            if not rule.get("enabled"):
                continue
            if "job_done" in rule["triggers"]:
                await self.send(rule, "🧱 LEGO Price Tracker", text)
            elif "problems" in rule["triggers"] and job.get("errors") and self._cooled(f"{rule['id']}|job_errors", rule.get("cooldown_hours", 24)):
                await self.send(rule, "⚠️ LEGO Price Tracker", text)

    async def on_shop_paused(self, retailer: str, hours: float) -> None:
        for rule in self.rules:
            if rule.get("enabled") and "problems" in rule["triggers"] \
                    and self._cooled(f"{rule['id']}|pause|{retailer}", rule.get("cooldown_hours", 24)):
                await self.send(rule, "⚠️ Winkel gepauzeerd",
                                f"{RETAILERS.get(retailer, (retailer,))[0]} blokkeerde de prijsopvraging en wordt {hours:.0f} u gepauzeerd.")

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
                       f"{rule['name']}: {title} — " + ("in wachtrij (stille uren)" if quiet else
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
                       {"title": title, "message": message + (f"\n\n[Openen]({url})" if url else ""),
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
                    + (f'<p><a href="{url}">Bekijk in de winkel</a></p>' if url else "")
                    + (f'<img src="{image}" width="240">' if image else ""))
            await call("notify", t["service"].split(".", 1)[1],
                       {"title": title, "message": full, "target": t["to"], "data": {"html": html}}, blocking=True)
        elif t["type"] == "entity":
            await call("notify", "send_message", {"entity_id": t["entity_id"], "title": title, "message": full}, blocking=True)
        elif t["type"] == "tts":
            await call("tts", "speak", {"entity_id": t["tts"], "media_player_entity_id": t["media_player"],
                                        "message": f"{title}. {message}".replace("€", "euro ").replace("🧱", "")}, blocking=True)
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
            await self.send(rule, f"🧱 {len(items)} LEGO-meldingen (stille uren)", body, force=True)

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
