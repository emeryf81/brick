// ==UserScript==
// @name         LEGO Price Tracker -> Home Assistant
// @namespace    https://github.com/emeryf81/lot
// @version      0.1.0
// @description  Stuurt de prijs van LEGO-productpagina's die je zelf bezoekt naar je Home Assistant (lego_tracker.report_price).
// @match        https://www.amazon.nl/*
// @match        https://www.amazon.de/*
// @match        https://www.amazon.com.be/*
// @match        https://www.bol.com/*
// @match        https://www.kruidvat.be/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// ==/UserScript==
// Werkt in Tampermonkey/Violentmonkey. Jouw eigen browser wordt niet als bot geblokkeerd.
// Stel via het Tampermonkey-menu "HA instellen" je HA-URL en een long-lived access token in
// (Home Assistant -> profiel -> Beveiliging). Alleen prijzen van sets die al gevolgd worden
// (zelfde product-URL) worden bijgewerkt; andere pagina's geven een stille 400 en doen niets.
(function () {
  "use strict";
  GM_registerMenuCommand("HA instellen", () => {
    GM_setValue("ha_url", prompt("Home Assistant URL (bv. http://homeassistant.local:8123)", GM_getValue("ha_url", "")) || "");
    GM_setValue("ha_token", prompt("Long-lived access token", GM_getValue("ha_token", "")) || "");
  });
  const parse = (t) => {
    if (t == null) return null;
    let s = String(t).replace(/[^\d.,]/g, "");
    if (!s) return null;
    if (s.includes(",") && s.includes(".")) s = s.lastIndexOf(",") > s.lastIndexOf(".") ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
    else if (s.includes(",")) { const tail = s.split(",").pop(); s = tail.length <= 2 ? s.replace(/,(?=[^,]*$)/, ".").replace(/,/g, "") : s.replace(/,/g, ""); }
    else if ((s.match(/\./g) || []).length > 1 || /\.\d{3}$/.test(s)) s = s.replace(/\./g, "");
    const v = parseFloat(s); return v > 0 ? v : null;
  };
  function fromJsonLd() {
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const stack = [JSON.parse(el.textContent)];
        while (stack.length) {
          const n = stack.pop();
          if (Array.isArray(n)) { stack.push(...n); continue; }
          if (!n || typeof n !== "object") continue;
          if ([].concat(n["@type"] || []).includes("Product") && n.offers) {
            for (const o of [].concat(n.offers)) { const p = parse(o.price ?? o.lowPrice); if (p && !/OutOfStock|SoldOut/.test(o.availability || "")) return p; }
          }
          stack.push(...Object.values(n));
        }
      } catch (e) { /* ignore */ }
    }
    return null;
  }
  function fromDom() {
    const host = location.hostname;
    if (host.includes("amazon")) {
      const el = document.querySelector("#corePrice_feature_div .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen, #apex_desktop .a-offscreen, .a-price .a-offscreen");
      return el && parse(el.textContent);
    }
    if (host.includes("bol.com")) {
      const el = document.querySelector('[data-test="price"]');
      if (el) { const f = el.querySelector("sup"); const whole = (el.childNodes[0]?.textContent || "").trim(); return parse(`${whole},${f && /\d/.test(f.textContent) ? f.textContent.trim() : "00"}`); }
    }
    const m = document.querySelector('meta[property="product:price:amount"], meta[itemprop="price"]');
    return m && parse(m.content);
  }
  function report() {
    const ha = GM_getValue("ha_url", ""), token = GM_getValue("ha_token", "");
    if (!ha || !token) return;
    const price = fromDom() ?? fromJsonLd();
    if (!price) return;
    const key = "sent:" + location.pathname, last = GM_getValue(key, 0);
    if (Date.now() - last < 3600 * 1000) return;   // max once per hour per page
    GM_xmlhttpRequest({
      method: "POST", url: ha.replace(/\/$/, "") + "/api/services/lego_tracker/report_price",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      data: JSON.stringify({ url: location.href, price }),
      onload: (r) => { if (r.status === 200) GM_setValue(key, Date.now()); },
    });
  }
  setTimeout(report, 2500);   // wait for late-rendered prices
})();
