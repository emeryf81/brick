// ==UserScript==
// @name         LEGO Price Tracker -> Home Assistant
// @namespace    https://github.com/emeryf81/lot
// @version      {{VERSION}}
// @description  Stuurt prijs en producttitel van LEGO-pagina's die je zelf bezoekt naar je Home Assistant (lego_tracker.report_price).
{{MATCHES}}
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// @downloadURL  {{SELF_URL}}
// @updateURL    {{SELF_URL}}
// ==/UserScript==
// Door Home Assistant gegenereerd voor {{HA_URL}}. Jouw eigen browser wordt niet als bot geblokkeerd.
// Eenmalig: Tampermonkey-menu → "HA instellen" → plak een long-lived access token
// (Home Assistant → profiel → Beveiliging → Long-lived access tokens).
// Alleen producten die de integratie al volgt (zelfde URL/ASIN) worden bijgewerkt; andere pagina's doen niets.
(function () {
  "use strict";
  const DEFAULT_HA = "{{HA_URL}}";
  if (!GM_getValue("ha_url", "")) GM_setValue("ha_url", DEFAULT_HA);
  GM_registerMenuCommand("HA instellen", () => {
    const url = prompt("Home Assistant URL", GM_getValue("ha_url", DEFAULT_HA));
    if (url !== null) GM_setValue("ha_url", url.trim());
    const token = prompt("Long-lived access token (profiel → Beveiliging)", GM_getValue("ha_token", ""));
    if (token !== null) GM_setValue("ha_token", token.trim());
  });
  const note = (text, ok) => {
    const d = document.createElement("div");
    d.textContent = "🧱 " + text;
    d.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:10px 14px;border-radius:10px;font:13px system-ui;color:#fff;box-shadow:0 6px 20px rgba(0,0,0,.3);background:" + (ok ? "#0b5d22" : "#8e0b0c");
    document.body.appendChild(d); setTimeout(() => d.remove(), 3500);
  };
  const pageTitle = () => (document.querySelector("#productTitle, h1[data-test='title'], h1")?.textContent
    || document.querySelector('meta[property="og:title"]')?.content || document.title || "").replace(/\s+/g, " ").trim().slice(0, 300);
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
  // Amazon: only the buy box. The first ".a-price" on the page is often an accessory, a unit
  // price, a coupon or a struck-through list price (e.g. €13,69 on a €99,95 set).
  function aPrice(el) {
    if (!el) return null;
    const off = el.querySelector(".a-offscreen");
    const v = parse(off && off.textContent.trim());
    if (v) return v;
    const whole = el.querySelector(".a-price-whole"), frac = el.querySelector(".a-price-fraction");
    return whole ? parse(`${whole.textContent.replace(/[^\d]/g, "")},${frac ? frac.textContent.replace(/[^\d]/g, "") : "00"}`) : null;
  }
  function amazonPrice() {
    const hidden = document.querySelector('input[name="items[0.base][customerVisiblePrice][amount]"], #twister-plus-price-data-price');
    if (hidden && parse(hidden.value)) return parse(hidden.value);
    for (const id of ["corePriceDisplay_desktop_feature_div", "corePrice_feature_div", "apex_desktop", "corePrice_desktop", "desktop_buybox", "buybox"]) {
      const box = document.getElementById(id);
      if (!box) continue;
      const pay = aPrice(box.querySelector(".priceToPay, .apexPriceToPay, .reinventPricePriceToPayMargin"));
      if (pay) return pay;
      for (const el of box.querySelectorAll(".a-price")) {
        if (el.matches(".a-text-price, [data-a-strike]") || el.closest(".a-text-price, [data-a-strike], .basisPrice, .a-size-small")) continue;
        const v = aPrice(el);
        if (v) return v;
      }
    }
    for (const id of ["priceblock_dealprice", "priceblock_ourprice", "priceblock_saleprice", "price_inside_buybox", "newBuyBoxPrice"]) {
      const el = document.getElementById(id);
      if (el && parse(el.textContent)) return parse(el.textContent);
    }
    return null;   // better no price than a wrong one
  }
  function fromDom() {
    const host = location.hostname;
    if (host.includes("amazon")) return amazonPrice();
    if (host.includes("bol.com")) {
      const el = document.querySelector('[data-test="price"]');
      if (el) { const f = el.querySelector("sup"); const whole = (el.childNodes[0]?.textContent || "").trim(); return parse(`${whole},${f && /\d/.test(f.textContent) ? f.textContent.trim() : "00"}`); }
    }
    const m = document.querySelector('meta[property="product:price:amount"], meta[itemprop="price"]');
    return m && parse(m.content);
  }
  function report() {
    const ha = GM_getValue("ha_url", ""), token = GM_getValue("ha_token", "");
    if (!ha || !token) { if (!GM_getValue("hinted", false)) { GM_setValue("hinted", true); note("LEGO Price Tracker: stel je token in via het Tampermonkey-menu → HA instellen", false); } return; }
    const price = location.hostname.includes("amazon") ? fromDom() : (fromDom() ?? fromJsonLd());
    if (!price) return;
    const key = "sent:" + location.pathname, last = GM_getValue(key, 0);
    if (Date.now() - last < 3600 * 1000) return;   // max once per hour per page
    GM_xmlhttpRequest({
      method: "POST", url: ha.replace(/\/$/, "") + "/api/services/lego_tracker/report_price",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      data: JSON.stringify({ url: location.href, price, title: pageTitle() }),
      onload: (r) => { if (r.status === 200) { GM_setValue(key, Date.now()); note(`Prijs €${price.toFixed(2)} naar Home Assistant gestuurd`, true); } else if (r.status === 401) note("Home Assistant weigert het token (401)", false); },
    });
  }
  setTimeout(report, 2500);   // wait for late-rendered prices
})();
