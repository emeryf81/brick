/* LEGO Price Tracker panel - vanilla web component, no build step. */
const EUR = (v) => (v == null ? "–" : new Intl.NumberFormat("nl-BE", { style: "currency", currency: "EUR" }).format(v));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COLORS = ["#e3000b", "#0055bf", "#237841", "#f2cd37", "#a95500", "#7b1fa2"];

const CHARTS = {}; let CHART_N = 0;
const lastAt = (pts, t) => { let r = null; for (const p of pts) { if (p[0] <= t) r = p; else break; } return r; };
function lineChart(series, { height = 220, money = true } = {}) {
  // series: [{name, color, points:[[ts,val],...], dashed}]
  const W = 720, H = height, P = { l: 52, r: 12, t: 12, b: 24 };
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) return `<div class="empty">Nog te weinig meetpunten voor een grafiek.</div>`;
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.1 || 1; y0 = Math.max(0, y0 - pad); y1 += pad;
  const sx = (x) => P.l + ((x - x0) / (x1 - x0 || 1)) * (W - P.l - P.r);
  const sy = (y) => H - P.b - ((y - y0) / (y1 - y0 || 1)) * (H - P.t - P.b);
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4, y = sy(v);
    g += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="gl"/><text x="${P.l - 6}" y="${y + 4}" text-anchor="end" class="axis">${money ? Math.round(v) : v.toFixed(0)}</text>`;
  }
  for (let i = 0; i <= 3; i++) {
    const t = x0 + ((x1 - x0) * i) / 3;
    g += `<text x="${sx(t)}" y="${H - 6}" text-anchor="middle" class="axis">${new Date(t * 1000).toLocaleDateString("nl-BE", { day: "2-digit", month: "short" })}</text>`;
  }
  const lines = series.map((s) => {
    const pts = s.points.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(" ");
    const last = s.points[s.points.length - 1];
    return `<polyline fill="none" stroke="${s.color}" stroke-width="2" ${s.dashed ? 'stroke-dasharray="5 4"' : ""} points="${pts}"/>` +
      `<circle cx="${sx(last[0])}" cy="${sy(last[1])}" r="3" fill="${s.color}"><title>${esc(s.name)}: ${money ? EUR(last[1]) : last[1]}</title></circle>`;
  }).join("");
  const legend = series.map((s) => `<span class="lg"><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
  const id = ++CHART_N; CHARTS[id] = { W, H, P, x0, x1, series, money, sx };
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" class="chart" data-id="${id}">${g}${lines}<line class="hv" y1="${P.t}" y2="${H - P.b}" style="display:none"/></svg><div class="tip" style="display:none"></div></div><div class="legend">${legend}</div>`;
}

function spark(vals) {
  if (!vals || vals.length < 2) return "";
  const min = Math.min(...vals), max = Math.max(...vals), w = 100, h = 28;
  const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / (max - min || 1)) * (h - 4)).toFixed(1)}`).join(" ");
  const down = vals[vals.length - 1] <= vals[0];
  return `<svg viewBox="0 0 ${w} ${h}" class="spark"><polyline fill="none" stroke="${down ? "#237841" : "#e3000b"}" stroke-width="1.6" points="${pts}"/></svg>`;
}

const STYLE = `
:host{display:block;background:var(--primary-background-color,#f4f4f6);color:var(--primary-text-color,#1b1b1f);min-height:100vh;font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
h1{margin:0;font-size:22px;flex:1}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--card-background-color,#fff);border-radius:12px;padding:12px;box-shadow:var(--ha-card-box-shadow,0 1px 3px #0003)}
.tile b{display:block;font-size:22px;margin-top:2px}.tile small{color:var(--secondary-text-color,#666)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--divider-color,#d0d0d6);margin-bottom:12px;overflow-x:auto}
.tab{padding:10px 16px;cursor:pointer;border:0;background:none;color:var(--secondary-text-color,#666);font-size:15px;border-bottom:3px solid transparent;white-space:nowrap}
.tab.on{color:var(--primary-color,#0055bf);border-color:var(--primary-color,#0055bf);font-weight:600}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
input,select,textarea,button.b{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--divider-color,#d0d0d6);background:var(--card-background-color,#fff);color:inherit}
button.b{cursor:pointer;background:var(--primary-color,#0055bf);color:var(--text-primary-color,#fff);border:0}button.b.alt{background:var(--card-background-color,#fff);color:inherit;border:1px solid var(--divider-color,#d0d0d6)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.chip{padding:5px 12px;border-radius:99px;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#d0d0d6);cursor:pointer;font-size:13px}
.chip.on{background:var(--primary-color,#0055bf);color:var(--text-primary-color,#fff);border-color:transparent}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.card{background:var(--card-background-color,#fff);border-radius:12px;padding:10px;cursor:pointer;position:relative;box-shadow:var(--ha-card-box-shadow,0 1px 3px #0003);display:flex;flex-direction:column;gap:4px}
.card img{width:100%;height:120px;object-fit:contain;border-radius:8px;background:#fff}
.card .ph{height:120px;display:flex;align-items:center;justify-content:center;font-size:34px;background:var(--secondary-background-color,#e8e8ec);border-radius:8px}
.card .n{font-weight:600;font-size:14px;min-height:36px}.card .m{color:var(--secondary-text-color,#666);font-size:12px}
.card .p{font-size:20px;font-weight:700}.spark{width:100%;height:28px}
.badges{position:absolute;top:14px;left:14px;display:flex;flex-direction:column;gap:4px}
.badge{font-size:11px;font-weight:700;padding:3px 7px;border-radius:6px;color:#fff;background:#237841;width:fit-content}
.badge.disc{background:#e3000b}.badge.own{background:#0055bf}
.chartbox{position:relative}.chart{width:100%;height:auto}.hv{stroke:var(--secondary-text-color,#666);stroke-dasharray:3 3}
.tip{position:absolute;top:6px;left:60px;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#d0d0d6);border-radius:8px;padding:6px 8px;font-size:12px;pointer-events:none;box-shadow:0 2px 6px #0003;white-space:nowrap}
.warn{background:#fff4d6;color:#5a4300;border-radius:10px;padding:8px 12px;margin-bottom:10px;font-size:13px}.warn a{cursor:pointer}
.tr{font-size:11px;font-weight:600}.tr.dn{color:#237841}.tr.up{color:#e3000b}.rng{display:flex;gap:6px;margin:6px 0}.gl{stroke:var(--divider-color,#d0d0d6)}.axis{font-size:10px;fill:var(--secondary-text-color,#666)}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;margin-top:4px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
.empty{padding:24px;text-align:center;color:var(--secondary-text-color,#666)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--divider-color,#d0d0d6)}
a{color:var(--primary-color,#0055bf)}.err{color:#e3000b;font-size:12px}
dialog{border:0;border-radius:14px;padding:16px;max-width:820px;width:96vw;background:var(--card-background-color,#fff);color:var(--primary-text-color,#1b1b1f)}
dialog::backdrop{background:#0008}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-bottom:10px}
.form label{display:flex;flex-direction:column;font-size:12px;color:var(--secondary-text-color,#666);gap:3px}
.section{background:var(--card-background-color,#fff);border-radius:12px;padding:14px;margin-bottom:14px}
.section h3{margin:0 0 8px}.msg{margin-top:8px;font-size:13px}
.hbar{display:flex;align-items:center;gap:8px;font-size:13px;margin:3px 0}.hbar span.f{height:10px;background:var(--primary-color,#0055bf);border-radius:4px;display:inline-block}
`;

class LegoTrackerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = { tab: "today", theme: null, q: "", sort: "discount", owned: "all", subtheme: null, range: 0, threshold: null, data: null, coll: null, msg: "" };
  }
  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first) this.load();
  }
  set narrow(_) {}
  set panel(_) {}
  async load() {
    try {
      this.state.data = await this._hass.callWS({ type: "lego_tracker/overview" });
      if (this.state.threshold == null) this.state.threshold = this.state.data.threshold;
      this.state.coll = await this._hass.callWS({ type: "lego_tracker/collection" });
      this.state.err = null;
    } catch (e) { this.state.err = e.message || String(e); }
    this.render();
  }
  async svc(service, data, returnResponse = false) {
    return this._hass.callWS({ type: "call_service", domain: "lego_tracker", service, service_data: data, return_response: returnResponse });
  }
  isDeal(s) { return s.is_all_time_low || (s.best_price != null && s.discount_rrp != null && s.discount_rrp >= this.state.threshold); }
  filtered() {
    const { data, theme, q, owned, sort } = this.state;
    let sets = data.sets.filter((s) => (!theme || s.theme === theme) && (!this.state.subtheme || s.subtheme === this.state.subtheme) && (owned === "all" || (owned === "yes") === !!s.owned));
    if (q) { const n = q.toLowerCase(); sets = sets.filter((s) => `${s.set_number} ${s.name} ${s.theme} ${s.subtheme}`.toLowerCase().includes(n)); }
    const key = { discount: (s) => -(s.discount_rrp ?? -1e9), price: (s) => s.best_price ?? 1e9, ppp: (s) => s.price_per_piece ?? 1e9, drop: (s) => s.change_30d ?? 1e9, name: (s) => s.name || "", number: (s) => s.set_number }[sort];
    return sets.sort((a, b) => { const x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
  }
  card(s) {
    const badges = [s.is_all_time_low ? `<span class="badge">🔻 Laagste ooit</span>` : "",
      s.discount_rrp != null && s.discount_rrp >= this.state.threshold ? `<span class="badge disc">-${Math.round(s.discount_rrp)}%</span>` : "",
      s.target_hit ? `<span class="badge" style="background:#7b1fa2">🎯 Streefprijs</span>` : "",
      s.owned ? `<span class="badge own">In bezit</span>` : ""].join("");
    const tr = s.change_30d == null ? "" : ` · <span class="tr ${s.change_30d <= 0 ? "dn" : "up"}">${s.change_30d <= 0 ? "▼" : "▲"} ${Math.abs(s.change_30d)}% (30 d)</span>`;
    const ppp = s.price_per_piece ? ` · ${(s.price_per_piece * 100).toFixed(1)} ct/steen` : "";
    const store = s.best_retailer ? this.state.data.retailers[s.best_retailer] : "";
    return `<div class="card" data-set="${esc(s.set_number)}"><div class="badges">${badges}</div>
      ${s.image ? `<img loading="lazy" src="${esc(s.image)}" alt="">` : `<div class="ph">🧱</div>`}
      <div class="n">${esc(s.name || "Set " + s.set_number)}</div>
      <div class="m">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.subtheme ? " / " + esc(s.subtheme) : ""}</div>
      <div class="p">${EUR(s.best_price)}</div>
      <div class="m">${store ? esc(store) : "geen prijs"}${s.rrp ? ` · adviesprijs ${EUR(s.rrp)}` : ""}${s.all_time_low != null ? ` · laagste ${EUR(s.all_time_low)}` : ""}${s.target_price ? ` · 🎯 ${EUR(s.target_price)}` : ""}${ppp}${tr}</div>
      ${spark(s.spark)}</div>`;
  }
  tiles() {
    const d = this.state.data, c = d.summary, sets = d.sets;
    const lows = sets.filter((s) => s.is_all_time_low).length, disc = sets.filter((s) => s.best_price != null && s.discount_rrp != null && s.discount_rrp >= this.state.threshold).length;
    const t = (l, v) => `<div class="tile"><small>${l}</small><b>${v}</b></div>`;
    return `<div class="tiles">${t("Gevolgde sets", sets.length)}${t("Laagste prijs ooit", lows)}${t(`Korting ≥ ${this.state.threshold}%`, disc)}
      ${t("Collectiewaarde", EUR(c.value))}${t("Aankoopkost", EUR(c.cost))}${t("Groei", c.cost ? `${c.growth >= 0 ? "+" : ""}${EUR(c.growth)} (${c.growth_pct ?? 0}%)` : "–")}</div>`;
  }
  controls(showSort = true) {
    const { data, theme, q, sort, owned, threshold } = this.state;
    const counts = {}; data.sets.forEach((s) => { if (s.theme) counts[s.theme] = (counts[s.theme] || 0) + 1; });
    const chips = [`<span class="chip ${!theme ? "on" : ""}" data-theme="">Alle thema's</span>`].concat(
      Object.keys(counts).sort().map((t) => `<span class="chip ${theme === t ? "on" : ""}" data-theme="${esc(t)}">${esc(t)} (${counts[t]})</span>`)).join("");
    return `<div class="bar"><input id="q" placeholder="Zoek set, naam, thema…" value="${esc(q)}">
      ${showSort ? `<select id="sort"><option value="discount" ${sort === "discount" ? "selected" : ""}>Hoogste korting</option><option value="price" ${sort === "price" ? "selected" : ""}>Laagste prijs</option><option value="ppp" ${sort === "ppp" ? "selected" : ""}>Prijs per steen</option><option value="drop" ${sort === "drop" ? "selected" : ""}>Grootste daling (30 d)</option><option value="name" ${sort === "name" ? "selected" : ""}>Naam</option><option value="number" ${sort === "number" ? "selected" : ""}>Setnummer</option></select>` : ""}
      <select id="owned"><option value="all">Alles</option><option value="yes" ${owned === "yes" ? "selected" : ""}>In bezit</option><option value="no" ${owned === "no" ? "selected" : ""}>Wishlist / niet in bezit</option></select>
      <label>Kortingsdrempel: <b>${threshold}%</b> <input id="thr" type="range" min="5" max="80" value="${threshold}"></label>
      <button class="b alt" id="refresh">↻ Nu verversen</button></div><div class="chips">${chips}</div>${this.subChips()}`;
  }
  subChips() {
    const { data, theme, subtheme } = this.state; if (!theme) return "";
    const counts = {}; data.sets.filter((s) => s.theme === theme && s.subtheme).forEach((s) => { counts[s.subtheme] = (counts[s.subtheme] || 0) + 1; });
    const keys = Object.keys(counts).sort(); if (!keys.length) return "";
    return `<div class="chips"><span class="chip ${!subtheme ? "on" : ""}" data-sub="">Alle subthema's</span>${keys.map((k) => `<span class="chip ${subtheme === k ? "on" : ""}" data-sub="${esc(k)}">${esc(k)} (${counts[k]})</span>`).join("")}</div>`;
  }
  viewToday() {
    const sets = this.filtered().filter((s) => this.isDeal(s));
    const lows = sets.filter((s) => s.is_all_time_low), rest = sets.filter((s) => !s.is_all_time_low);
    const sec = (title, list) => list.length ? `<h3>${title} (${list.length})</h3><div class="grid">${list.map((s) => this.card(s)).join("")}</div>` : "";
    const shown = new Set(sets.map((s) => s.set_number));
    const drops = this.filtered().filter((s) => !shown.has(s.set_number) && s.change_30d != null && s.change_30d <= -10).sort((a, b) => a.change_30d - b.change_30d).slice(0, 8);
    const targets = this.filtered().filter((s) => s.target_hit && !lows.includes(s));
    return this.health() + this.controls() + (sets.length || drops.length ? sec("🔻 Laagste prijs ooit", lows) + sec("🎯 Streefprijs bereikt", targets.filter((s) => !rest.includes(s))) + sec(`🏷️ Korting ≥ ${this.state.threshold}%`, rest) + sec("📉 Sterk gedaald (30 dagen)", drops)
      : `<div class="empty">Vandaag geen deals binnen deze filters. Verlaag de drempel of kies een ander thema.</div>`);
  }
  health() {
    const h = this.state.data.health; if (!h || !h.errors) return "";
    const paused = Object.entries(h.paused_hours || {}).map(([k, v]) => `${esc(k)} (${v} u)`).join(", ");
    return `<div class="warn">⚠ ${h.errors} aanbieding${h.errors === 1 ? "" : "en"} zonder prijs${paused ? ` · gepauzeerd na blokkade: ${paused}` : ""}. Open een set voor de foutmelding; je kunt de prijs ook handmatig invullen of via het userscript laten binnenkomen.</div>`;
  }
  viewWishlist() {
    const w = this.state.data.wishlist, sets = this.filtered().filter((s) => !s.owned);
    const tiles = `<div class="tiles"><div class="tile"><small>Sets op wishlist</small><b>${w.sets}</b></div><div class="tile"><small>Totaal nu (beste prijzen)</small><b>${EUR(w.cost)}</b></div><div class="tile"><small>Adviesprijs totaal</small><b>${EUR(w.rrp)}</b></div><div class="tile"><small>Besparing t.o.v. advies</small><b>${EUR(w.saving)}</b></div></div>`;
    sets.sort((a, b) => (b.target_hit - a.target_hit) || ((b.discount_rrp ?? -1e9) - (a.discount_rrp ?? -1e9)));
    return tiles + this.controls() + (sets.length ? `<div class="grid">${sets.map((s) => this.card(s)).join("")}</div>` : `<div class="empty">Geen wishlist-sets. Voeg sets toe zonder ze als "in bezit" te markeren.</div>`);
  }
  viewAll() {
    const sets = this.filtered();
    return this.health() + this.controls() + (sets.length ? `<div class="grid">${sets.map((s) => this.card(s)).join("")}</div>` : `<div class="empty">Geen sets. Voeg sets toe via het tabblad "Toevoegen".</div>`);
  }
  viewCollection() {
    const c = this.state.coll, sum = c.summary;
    if (!sum.sets) return `<div class="empty">Nog geen collectie. Importeer een CSV of markeer sets als "in bezit".</div>`;
    const series = c.series.map((p) => [p.ts, p.value]), cost = c.series.map((p) => [p.ts, p.cost]);
    const max = Math.max(...Object.values(sum.by_theme));
    const bars = Object.entries(sum.by_theme).map(([t, v]) => `<div class="hbar"><span style="width:130px">${esc(t)}</span><span class="f" style="width:${(v / max) * 260}px"></span>${EUR(v)}</div>`).join("");
    const rows = this.state.data.sets.filter((s) => s.owned).sort((a, b) => (b.best_price || 0) - (a.best_price || 0)).map((s) => {
      const q = s.collection.qty || 1, paid = s.collection.paid, now = s.best_price ?? s.collection.current_value ?? s.rrp;
      const gain = paid && now ? ((now - paid) / paid) * 100 : null;
      return `<tr data-set="${esc(s.set_number)}" style="cursor:pointer"><td>${esc(s.set_number)}</td><td>${esc(s.name)}</td><td>${esc(s.theme)}</td><td>${q}</td><td>${EUR(paid)}</td><td>${EUR(now)}</td><td>${gain == null ? "–" : (gain >= 0 ? "+" : "") + gain.toFixed(0) + "%"}</td></tr>`;
    }).join("");
    return `<div class="section"><h3>Collectiegroei</h3>${lineChart([{ name: "Waarde (laagste prijs nieuw)", color: COLORS[1], points: series }, { name: "Aankoopkost", color: COLORS[0], points: cost, dashed: true }])}
      <small>Waarde wordt herrekend uit de prijshistoriek per set; sets zonder prijzen gebruiken de geïmporteerde waarde of de adviesprijs.</small></div>
      <div class="section"><h3>Waarde per thema</h3>${bars}</div>
      <div class="section"><h3>Sets in bezit (${sum.sets} stuks, ${sum.pieces.toLocaleString("nl-BE")} stenen) <button class="b alt" id="expcsv" style="float:right">⬇ CSV</button></h3><table><tr><th>#</th><th>Naam</th><th>Thema</th><th>Aantal</th><th>Betaald</th><th>Nu</th><th>+/-</th></tr>${rows}</table></div>`;
  }
  viewAdd() {
    const retailers = Object.entries(this.state.data.retailers).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    return `<div class="section"><h3>Set toevoegen</h3>
      <div class="form"><label>Setnummer*<input id="a_num" placeholder="10281"></label><label>Naam<input id="a_name"></label>
      <label>Thema<input id="a_theme" list="themes" placeholder="Botanicals, Technic, Creator…"></label><label>Subthema<input id="a_sub"></label>
      <label>Adviesprijs (€)<input id="a_rrp" type="number" step="0.01"></label><label>Stenen<input id="a_pcs" type="number"></label>
      <label>In bezit?<select id="a_owned"><option value="0">Nee (wishlist)</option><option value="1">Ja</option></select></label>
      <label>Aantal<input id="a_qty" type="number" value="1" min="1"></label><label>Betaald (€ per stuk)<input id="a_paid" type="number" step="0.01"></label>
      <label>Aankoopdatum<input id="a_date" type="date"></label><label>Streefprijs (€, melding)<input id="a_target" type="number" step="0.01"></label></div>
      <datalist id="themes">${this.state.data.themes.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
      <button class="b" id="add">Toevoegen &amp; winkels zoeken</button>
      <div class="msg">Winkel-links worden automatisch gezocht (best effort). Klopt een link niet, stel hem in per set of hieronder.</div></div>
    <div class="section"><h3>Meerdere sets tegelijk</h3><textarea id="bulk" rows="3" style="width:100%" placeholder="Plak setnummers, gescheiden door spaties, komma's of nieuwe regels: 10281 10311 42143"></textarea><br>
      <label><input type="checkbox" id="bulk_owned"> alle als "in bezit" markeren</label> <button class="b" id="bulkadd">Toevoegen</button>
      <button class="b alt" id="discover">🔎 Winkels zoeken voor sets zonder link</button></div>
    <div class="section"><h3>Winkel-link koppelen</h3><div class="form"><label>Setnummer<input id="o_num"></label>
      <label>Winkel<select id="o_ret">${retailers}</select></label><label>Product-URL of ASIN<input id="o_url"></label></div><button class="b" id="offer">Koppelen</button></div>
    <div class="section"><h3>Collectie importeren (CSV)</h3>
      <p>Werkt met exports van BrickEconomy, Brickset, Rebrickable of een eigen spreadsheet. Herkende kolommen: nummer, naam, thema, subthema, aantal, betaald, waarde, aankoopdatum, stenen, adviesprijs.</p>
      <input type="file" id="csvfile" accept=".csv,text/csv,text/plain"> <label><input type="checkbox" id="replace"> bestaande collectie vervangen</label><br><br>
      <textarea id="csvtext" rows="5" style="width:100%" placeholder="…of plak hier de CSV-inhoud"></textarea><br>
      <button class="b" id="import">Importeren</button></div><div class="section"><h3>Back-up</h3><p>Volledige back-up (sets, links, prijshistoriek, collectie) als JSON, of collectie als CSV.</p>
      <button class="b alt" id="expjson">⬇ Back-up (JSON)</button> <button class="b alt" id="expcsv2">⬇ Collectie (CSV)</button><br><br>
      <input type="file" id="jsonfile" accept=".json,application/json"> <label><input type="checkbox" id="merge"> samenvoegen i.p.v. vervangen</label> <button class="b alt" id="impjson">Back-up terugzetten</button></div>
      <div class="msg" id="msg">${esc(this.state.msg)}</div>`;
  }
  render() {
    const s = this.state, root = this.shadowRoot;
    if (s.err) { root.innerHTML = `<style>${STYLE}</style><div class="wrap"><div class="empty">Kon gegevens niet laden: ${esc(s.err)}</div></div>`; return; }
    if (!s.data) { root.innerHTML = `<style>${STYLE}</style><div class="wrap"><div class="empty">Laden…</div></div>`; return; }
    const tabs = [["today", "Vandaag"], ["all", "Alle sets"], ["wish", "Wishlist"], ["coll", "Collectie"], ["add", "Toevoegen / import"]];
    const body = { today: () => this.viewToday(), all: () => this.viewAll(), wish: () => this.viewWishlist(), coll: () => this.viewCollection(), add: () => this.viewAdd() }[s.tab]();
    root.innerHTML = `<style>${STYLE}</style><div class="wrap"><header><ha-menu-button></ha-menu-button><h1>🧱 LEGO Price Tracker <small style="font-weight:400;font-size:12px;color:var(--secondary-text-color,#666)">v${esc(s.data.version)} · ${esc(s.data.transport)}</small></h1></header>${this.tiles()}
      <div class="tabs">${tabs.map(([k, l]) => `<button class="tab ${s.tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>${body}<dialog id="dlg"></dialog></div>`;
    const menu = root.querySelector("ha-menu-button"); if (menu) { menu.hass = this._hass; menu.narrow = this.narrow; }
    this.bind();
    this.hookCharts(root);
  }
  hookCharts(root) {
    root.querySelectorAll("svg.chart").forEach((svg) => {
      const m = CHARTS[svg.dataset.id]; if (!m) return;
      const tip = svg.parentElement.querySelector(".tip"), hv = svg.querySelector(".hv");
      svg.addEventListener("mousemove", (e) => {
        const box = svg.getBoundingClientRect(), x = ((e.clientX - box.left) / box.width) * m.W;
        if (x < m.P.l || x > m.W - m.P.r) return;
        const t = m.x0 + ((x - m.P.l) / (m.W - m.P.l - m.P.r)) * (m.x1 - m.x0);
        const rows = m.series.map((s) => { const p = lastAt(s.points, t); return p ? `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:5px"></i>${esc(s.name)}: <b>${m.money ? EUR(p[1]) : p[1]}</b></div>` : ""; }).join("");
        hv.setAttribute("x1", m.sx(t)); hv.setAttribute("x2", m.sx(t)); hv.style.display = "";
        tip.innerHTML = `<div>${new Date(t * 1000).toLocaleDateString("nl-BE", { day: "2-digit", month: "short", year: "numeric" })}</div>${rows}`;
        tip.style.display = ""; tip.style.left = Math.min(box.width - 190, Math.max(50, (e.clientX - box.left) + 12)) + "px";
      });
      svg.addEventListener("mouseleave", () => { tip.style.display = "none"; hv.style.display = "none"; });
    });
  }
  bind() {
    const r = this.shadowRoot, on = (sel, ev, fn) => r.querySelectorAll(sel).forEach((e) => e.addEventListener(ev, fn));
    on(".tab", "click", (e) => { this.state.tab = e.currentTarget.dataset.tab; this.render(); });
    on(".chip[data-theme]", "click", (e) => { this.state.theme = e.currentTarget.dataset.theme || null; this.state.subtheme = null; this.render(); });
    on(".chip[data-sub]", "click", (e) => { this.state.subtheme = e.currentTarget.dataset.sub || null; this.render(); });
    on("[data-set]", "click", (e) => this.openSet(e.currentTarget.dataset.set));
    const q = r.getElementById("q"); if (q) q.addEventListener("input", (e) => { this.state.q = e.target.value; const pos = e.target.selectionStart; this.render(); const n = this.shadowRoot.getElementById("q"); n.focus(); n.setSelectionRange(pos, pos); });
    const sel = (id, key) => { const el = r.getElementById(id); if (el) el.addEventListener("change", (e) => { this.state[key] = e.target.value; this.render(); }); };
    sel("sort", "sort"); sel("owned", "owned");
    const thr = r.getElementById("thr"); if (thr) thr.addEventListener("change", (e) => { this.state.threshold = +e.target.value; this.render(); });
    const rf = r.getElementById("refresh"); if (rf) rf.addEventListener("click", async () => { rf.textContent = "Bezig… (kan even duren)"; try { await this.svc("refresh", {}); } catch (e) { alert(e.message); } await this.load(); });
    const v = (id) => r.getElementById(id)?.value?.trim();
    const add = r.getElementById("add"); if (add) add.addEventListener("click", async () => {
      if (!v("a_num")) return alert("Setnummer is verplicht");
      const d = { set_number: v("a_num"), owned: v("a_owned") === "1", quantity: +v("a_qty") || 1 };
      for (const [k, id] of [["name", "a_name"], ["theme", "a_theme"], ["subtheme", "a_sub"], ["purchase_date", "a_date"]]) if (v(id)) d[k] = v(id);
      for (const [k, id] of [["rrp", "a_rrp"], ["pieces", "a_pcs"], ["paid", "a_paid"], ["target_price", "a_target"]]) if (v(id)) d[k] = +v(id);
      add.disabled = true; add.textContent = "Bezig…";
      try { await this.svc("add_set", d); this.state.msg = `Set ${d.set_number} toegevoegd.`; } catch (e) { this.state.msg = "Fout: " + e.message; }
      await this.load();
    });
    const off = r.getElementById("offer"); if (off) off.addEventListener("click", async () => {
      try { await this.svc("set_offer", { set_number: v("o_num"), retailer: v("o_ret"), url: v("o_url") }); this.state.msg = "Link gekoppeld. Klik op 'Nu verversen' om de prijs op te halen."; } catch (e) { this.state.msg = "Fout: " + e.message; }
      await this.load();
    });
    const dl = (name, text, mime) => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: mime })); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); };
    const expCsv = async () => { try { const x = await this.svc("export_collection", {}, true); dl("lego_collectie.csv", x.response.csv, "text/csv"); } catch (e) { alert(e.message); } };
    ["expcsv", "expcsv2"].forEach((id) => { const b = r.getElementById(id); if (b) b.addEventListener("click", expCsv); });
    const ej = r.getElementById("expjson"); if (ej) ej.addEventListener("click", async () => { try { const x = await this.svc("export_data", {}, true); dl(`lego_backup_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(x.response), "application/json"); } catch (e) { alert(e.message); } });
    const ij = r.getElementById("impjson"); if (ij) ij.addEventListener("click", async () => {
      const f = r.getElementById("jsonfile").files[0]; if (!f) return alert("Kies eerst een back-upbestand");
      const merge = r.getElementById("merge").checked; if (!merge && !confirm("Alle huidige gegevens worden vervangen. Doorgaan?")) return;
      try { const res = await this.svc("import_data", { data: JSON.parse(await f.text()), merge }, true); this.state.msg = `Back-up teruggezet: ${res.response.sets} sets, ${res.response.collection} in collectie.`; } catch (e) { this.state.msg = "Fout: " + e.message; }
      await this.load();
    });
    const ba = r.getElementById("bulkadd"); if (ba) ba.addEventListener("click", async () => {
      const t = r.getElementById("bulk").value; if (!t.trim()) return; ba.disabled = true; ba.textContent = "Bezig… (winkels zoeken duurt even)";
      try { const res = await this.svc("add_sets", { set_numbers: t, owned: r.getElementById("bulk_owned").checked }, true); this.state.msg = `${res.response.added} sets toegevoegd.`; } catch (e) { this.state.msg = "Fout: " + e.message; }
      await this.load();
    });
    const dc = r.getElementById("discover"); if (dc) dc.addEventListener("click", async () => {
      dc.disabled = true; dc.textContent = "Zoeken…";
      try { const res = await this.svc("discover_offers", {}, true); this.state.msg = `${res.response.found} nieuwe winkel-links gevonden.`; } catch (e) { this.state.msg = "Fout: " + e.message; }
      await this.load();
    });
    const file = r.getElementById("csvfile"); if (file) file.addEventListener("change", async (e) => { const f = e.target.files[0]; if (f) r.getElementById("csvtext").value = await f.text(); });
    const imp = r.getElementById("import"); if (imp) imp.addEventListener("click", async () => {
      const text = r.getElementById("csvtext").value; if (!text.trim()) return alert("Kies een bestand of plak CSV-tekst");
      imp.disabled = true; imp.textContent = "Importeren…";
      try {
        const res = await this.svc("import_collection", { csv_text: text, replace: r.getElementById("replace").checked }, true);
        const x = res.response; this.state.msg = `Import klaar: ${x.added} nieuw, ${x.updated} bijgewerkt.` + (x.warnings.length ? " Waarschuwingen: " + x.warnings.join("; ") : "") + " Winkel-links worden op de achtergrond gezocht.";
      } catch (e) { this.state.msg = "Fout: " + e.message; }
      await this.load(); this.state.tab = "coll"; this.render();
    });
  }
  async openSet(num) {
    const dlg = this.shadowRoot.getElementById("dlg");
    dlg.innerHTML = `<div class="empty">Laden…</div>`; dlg.showModal();
    let s; try { s = await this._hass.callWS({ type: "lego_tracker/set", set_number: num }); } catch (e) { dlg.innerHTML = `<div class="empty">${esc(e.message)}</div><button class="b" id="x">Sluiten</button>`; dlg.querySelector("#x").onclick = () => dlg.close(); return; }
    const days = this.state.range, cut = days ? Date.now() / 1000 - days * 86400 : 0;
    const win = (h) => { if (!cut) return h; const keep = h.filter((p) => p[0] >= cut), prev = lastAt(h, cut); return prev && (!keep.length || keep[0][0] > cut) ? [[cut, prev[1]], ...keep] : keep; };
    const series = Object.entries(s.history).filter(([, h]) => h.length).map(([rid, h], i) => ({ name: s.offers[rid]?.label || rid, color: COLORS[i % COLORS.length], points: win(h) })).filter((x) => x.points.length);
    if (s.rrp && series.length) { const all = series.flatMap((x) => x.points.map((p) => p[0])); series.push({ name: "Adviesprijs", color: "#888", dashed: true, points: [[Math.min(...all), s.rrp], [Math.max(...all), s.rrp]] }); }
    const rows = Object.entries(s.offers).map(([rid, o]) => `<tr><td>${esc(o.label)}</td><td>${EUR(o.price)}${o.error ? `<div class="err">${esc(o.error)}</div>` : ""}</td><td>${EUR(o.low)}</td><td>${o.checked ? new Date(o.checked * 1000).toLocaleString("nl-BE") : "–"}</td><td><a href="${esc(o.url)}" target="_blank" rel="noreferrer noopener">open ↗</a></td><td style="white-space:nowrap"><input class="mp" data-rid="${rid}" type="number" step="0.01" placeholder="prijs" style="width:80px"> <button class="b alt mpb" data-rid="${rid}">✓</button></td></tr>`).join("");
    const c = s.collection || {};
    dlg.innerHTML = `<h2 style="margin-top:0">${esc(s.set_number)} · ${esc(s.name || "")}</h2><div class="m">${esc(s.theme || "")}${s.subtheme ? " / " + esc(s.subtheme) : ""}${s.pieces ? " · " + s.pieces + " stenen" : ""}${s.year ? " · " + s.year : ""}</div>
      <div class="rng">${[[30, "30 d"], [90, "90 d"], [365, "1 jaar"], [0, "Alles"]].map(([d, l]) => `<span class="chip ${this.state.range === d ? "on" : ""}" data-range="${d}">${l}</span>`).join("")}</div>
      ${lineChart(series)}
      <div class="m">${s.price_per_piece ? `${(s.price_per_piece * 100).toFixed(1)} ct per steen · ` : ""}${s.change_7d != null ? `7 d: ${s.change_7d > 0 ? "+" : ""}${s.change_7d}% · ` : ""}${s.change_30d != null ? `30 d: ${s.change_30d > 0 ? "+" : ""}${s.change_30d}%` : ""}</div>
      <h3>Winkels</h3>${rows ? `<table><tr><th>Winkel</th><th>Nu</th><th>Laagste</th><th>Laatst gecontroleerd</th><th></th><th>Handmatig</th></tr>${rows}</table>` : `<div class="empty">Nog geen winkel-links. Koppel er een via "Toevoegen".</div>`}
      <h3>Bewerken</h3><div class="form"><label>Thema<input id="e_theme" value="${esc(s.theme || "")}"></label><label>Subthema<input id="e_sub" value="${esc(s.subtheme || "")}"></label>
      <label>Adviesprijs<input id="e_rrp" type="number" step="0.01" value="${s.rrp ?? ""}"></label><label>In bezit<select id="e_owned"><option value="0">Nee</option><option value="1" ${s.owned ? "selected" : ""}>Ja</option></select></label>
      <label>Aantal<input id="e_qty" type="number" min="1" value="${c.qty ?? 1}"></label><label>Betaald<input id="e_paid" type="number" step="0.01" value="${c.paid ?? ""}"></label>
      <label>Streefprijs (melding)<input id="e_target" type="number" step="0.01" value="${s.target_price ?? ""}"></label><label>Notitie<input id="e_notes" value="${esc(s.notes || "")}"></label></div>
      <button class="b" id="save">Opslaan</button> <button class="b alt" id="find">🔎 Winkels zoeken</button> <button class="b alt" id="rm">Set verwijderen</button> <button class="b alt" id="x">Sluiten</button>`;
    const q = (id) => dlg.querySelector("#" + id);
    q("x").onclick = () => dlg.close();
    dlg.querySelectorAll("[data-range]").forEach((c) => c.onclick = () => { this.state.range = +c.dataset.range; this.openSet(num); });
    q("find").onclick = async () => { q("find").textContent = "Zoeken…"; try { await this.svc("discover_offers", { set_number: num }, true); } catch (e) { alert(e.message); } await this.load(); this.openSet(num); };
    dlg.querySelectorAll(".mpb").forEach((btn) => btn.onclick = async () => {
      const rid = btn.dataset.rid, val = parseFloat(dlg.querySelector(`.mp[data-rid="${rid}"]`).value);
      if (!(val > 0)) return alert("Geef een prijs in");
      try { await this.svc("report_price", { set_number: num, retailer: rid, price: val }); } catch (e) { return alert(e.message); }
      await this.load(); this.openSet(num);
    });
    q("save").onclick = async () => {
      const f = { theme: q("e_theme").value.trim(), subtheme: q("e_sub").value.trim(), owned: q("e_owned").value === "1", qty: +q("e_qty").value || 1 };
      if (q("e_rrp").value) f.rrp = +q("e_rrp").value; if (q("e_paid").value) f.paid = +q("e_paid").value;
      f.target_price = q("e_target").value ? +q("e_target").value : ""; f.notes = q("e_notes").value.trim();
      try { await this._hass.callWS({ type: "lego_tracker/update_set", set_number: num, fields: f }); } catch (e) { return alert(e.message); }
      dlg.close(); await this.load();
    };
    this.hookCharts(dlg);
    q("rm").onclick = async () => { if (!confirm(`Set ${num} en zijn prijshistoriek verwijderen?`)) return; await this.svc("remove_set", { set_number: num }); dlg.close(); await this.load(); };
  }
}
customElements.define("lego-tracker-panel", LegoTrackerPanel);
