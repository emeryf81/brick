/* LEGO Price Tracker panel – vanilla web component, no build step.
 * Sections: "Deals & watchlist" (sets you keep an eye on), "My collection" (what you own),
 * "Logbook" (everything the integration does) and "Manage" (adding, import, shops, settings).
 * English is the source language; translations live in i18n/<lang>.json ({"English": "translation"}). */

// ------------------------------------------------------------------ i18n
const LOCALES = { en: "en-IE", nl: "nl-BE", fr: "fr-BE", de: "de-DE", es: "es-ES", zh: "zh-CN", ko: "ko-KR", id: "id-ID" };
let LANG = "en", DICT = {}, PATTERNS = null, LOC = "en-IE", NF, NF0;
const fill = (s, p) => (p ? String(s).replace(/\{(\w+)\}/g, (m, k) => (p[k] ?? m)) : String(s));
/** Translate a UI string (English source) and fill in {params}. */
const t = (s, p) => fill(DICT[s] || s, p);
/** Translate a text produced by the backend: English with the parameters already filled in.
 *  Exact match first, else the templates are matched in reverse and the parameters translated too. */
function tx(msg, depth = 0) {
  if (msg == null || msg === "") return "";
  msg = String(msg);
  if (LANG === "en") return msg;
  if (DICT[msg]) return DICT[msg];
  if (depth > 2) return msg;
  if (!PATTERNS) {
    PATTERNS = Object.keys(DICT).filter((k) => k.includes("{")).map((k) => {
      const names = [];
      const src = k.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{(\w+)\}/g, (m, n) => { names.push(n); return "([\\s\\S]+?)"; });
      return { k, names, re: new RegExp(`^${src}$`) };
    }).sort((a, b) => b.k.replace(/\{\w+\}/g, "").length - a.k.replace(/\{\w+\}/g, "").length);
  }
  for (const { k, names, re } of PATTERNS) {
    const m = msg.match(re);
    if (m) { const p = {}; names.forEach((n, i) => { p[n] = tx(m[i + 1], depth + 1); }); return fill(DICT[k], p); }
  }
  return msg;
}
function setLocale(lang) {
  LOC = LOCALES[lang] || "en-IE";
  NF = new Intl.NumberFormat(LOC, { style: "currency", currency: "EUR" });
  NF0 = new Intl.NumberFormat(LOC, { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
}
setLocale("en");
async function loadLanguage(lang, version) {
  if (!LOCALES[lang]) lang = "en";
  if (lang === LANG) return false;
  let dict = {};
  if (lang !== "en") {
    try { const r = await fetch(`/lego_tracker_static/i18n/${lang}.json?v=${encodeURIComponent(version || "")}`); if (r.ok) dict = await r.json(); } catch (e) { /* fall back to English */ }
  }
  LANG = lang; DICT = dict; PATTERNS = null; setLocale(lang);
  return true;
}

// ------------------------------------------------------------------ helpers
const EUR = (v) => (v == null || isNaN(v) ? "–" : NF.format(v));
const EUR0 = (v) => (v == null || isNaN(v) ? "–" : NF0.format(v));
const INT = (v) => (v == null ? "–" : Math.round(v).toLocaleString(LOC));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const signPct = (v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${v.toLocaleString(LOC, { maximumFractionDigits: 1 })}%`);
const DATE = (ts, o = { day: "2-digit", month: "short" }) => new Date(ts * 1000).toLocaleDateString(LOC, o);
const TIME = (ts) => new Date(ts * 1000).toLocaleTimeString(LOC, { hour: "2-digit", minute: "2-digit" });
const ago = (ts) => {
  if (!ts) return t("never");
  const s = Date.now() / 1000 - ts;
  if (s < 90) return t("just now");
  if (s < 3600) return t("{n} min ago", { n: Math.round(s / 60) });
  if (s < 86400) return t("{n} h ago", { n: Math.round(s / 3600) });
  return t("{n} d ago", { n: Math.round(s / 86400) });
};
const dur = (sec) => (sec >= 5400 ? t("{n} h", { n: (sec / 3600).toFixed(1) }) : sec >= 90 ? t("{n} min", { n: Math.round(sec / 60) }) : t("{n} s", { n: Math.round(sec) }));
const COLORS = ["#d01012", "#0057a6", "#00852b", "#f5a800", "#7a3c9e", "#00a3da", "#e76318", "#6c6e68"];
const CONDITIONS = ["Sealed", "Opened", "Built", "Incomplete"];   // stored in English, shown with t()
const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const lastAt = (pts, t) => { let r = null; for (const p of pts) { if (p[0] <= t) r = p; else break; } return r; };

// ------------------------------------------------------------------ charts (inline SVG)
const CHARTS = {}; let CHART_N = 0;
function lineChart(series, { height = 240, money = true, area = true } = {}) {
  const W = 760, H = height, P = { l: 56, r: 14, t: 14, b: 26 };
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) return `<div class="empty small">📈 ${t("Not enough data points for a chart yet. The price history appears here after a few refreshes.")}</div>`;
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.12 || Math.max(1, y1 * 0.05); y0 = Math.max(0, y0 - pad); y1 += pad;
  const sx = (x) => P.l + ((x - x0) / (x1 - x0 || 1)) * (W - P.l - P.r);
  const sy = (y) => H - P.b - ((y - y0) / (y1 - y0 || 1)) * (H - P.t - P.b);
  const id = ++CHART_N;
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4, y = sy(v);
    g += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="gl"/><text x="${P.l - 8}" y="${y + 4}" text-anchor="end" class="axis">${money ? EUR0(v) : Math.round(v)}</text>`;
  }
  const long = x1 - x0 > 300 * 86400;   // spans > ~10 months: show the year, not the day
  let prevLbl = "";
  for (let i = 0; i <= 4; i++) {
    const t = x0 + ((x1 - x0) * i) / 4, lbl = DATE(t, long ? { month: "short", year: "numeric" } : undefined);
    if (lbl === prevLbl) continue;
    prevLbl = lbl;
    g += `<text x="${sx(t)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}" class="axis">${DATE(t, long ? { month: "short", year: "numeric" } : undefined)}</text>`;
  }
  const defs = `<defs>${series.map((s, i) => `<linearGradient id="gr${id}_${i}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity=".28"/><stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`).join("")}</defs>`;
  const lines = series.map((s, i) => {
    const pts = s.points.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`);
    const last = s.points[s.points.length - 1];
    const fill = area && i === 0 && !s.dashed && s.points.length > 1
      ? `<polygon class="area" fill="url(#gr${id}_${i})" points="${sx(s.points[0][0]).toFixed(1)},${H - P.b} ${pts.join(" ")} ${sx(last[0]).toFixed(1)},${H - P.b}"/>` : "";
    return `${fill}<polyline class="ln${s.dashed ? " dash" : ""}" pathLength="1" fill="none" stroke="${s.color}" stroke-width="${s.dashed ? 1.5 : 2.5}" stroke-linejoin="round" stroke-linecap="round" points="${pts.join(" ")}"/>` +
      `<circle class="dot" cx="${sx(last[0])}" cy="${sy(last[1])}" r="4" fill="${s.color}"/>`;
  }).join("");
  CHARTS[id] = { W, H, P, x0, x1, series, money, sx };
  const legend = series.map((s) => `<span class="lg"><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" class="chart" data-id="${id}" role="img">${defs}${g}${lines}<line class="hv" y1="${P.t}" y2="${H - P.b}" style="display:none"/></svg><div class="tip" style="display:none"></div></div><div class="legend">${legend}</div>`;
}

function donut(items, { size = 170, label = "" } = {}) {
  const total = items.reduce((a, b) => a + b.value, 0);
  if (!total) return `<div class="empty small">${t("No data.")}</div>`;
  const r = 60, C = 2 * Math.PI * r; let off = 0;
  const arcs = items.map((it, i) => {
    const len = (it.value / total) * C;
    const a = `<circle class="arc" r="${r}" cx="80" cy="80" fill="none" stroke="${it.color || COLORS[i % COLORS.length]}" stroke-width="22" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" style="--d:${i * 60}ms"><title>${esc(it.label)}: ${EUR(it.value)}</title></circle>`;
    off += len; return a;
  }).join("");
  const legend = items.map((it, i) => `<div class="dl"><i style="background:${it.color || COLORS[i % COLORS.length]}"></i><span>${esc(it.label)}</span><b>${Math.round((it.value / total) * 100)}%</b></div>`).join("");
  return `<div class="donut"><svg viewBox="0 0 160 160" width="${size}" height="${size}"><g transform="rotate(-90 80 80)">${arcs}</g><text x="80" y="76" text-anchor="middle" class="dn-v">${EUR0(total)}</text><text x="80" y="96" text-anchor="middle" class="dn-l">${esc(label)}</text></svg><div class="dlegend">${legend}</div></div>`;
}

function bars(items, { fmt = INT } = {}) {
  const max = Math.max(1, ...items.map((i) => i.value));
  return `<div class="bars">${items.map((it, i) => `<div class="bar" style="--i:${i}"><div class="bv">${fmt(it.value)}</div><div class="bf" style="--h:${(it.value / max) * 100}%"></div><div class="bl">${esc(it.label)}</div></div>`).join("")}</div>`;
}

function spark(vals) {
  if (!vals || vals.length < 2) return `<div class="spark ph-spark"></div>`;
  const min = Math.min(...vals), max = Math.max(...vals), w = 100, h = 26;
  const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / (max - min || 1)) * (h - 4)).toFixed(1)}`).join(" ");
  const down = vals[vals.length - 1] <= vals[0];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="spark"><polyline class="ln" pathLength="1" fill="none" stroke="${down ? "#00852b" : "#d01012"}" stroke-width="1.8" points="${pts}"/></svg>`;
}

function ring(score, size = 44) {
  const r = 17, C = 2 * Math.PI * r, v = Math.max(0, Math.min(100, score || 0));
  const col = v >= 70 ? "#00852b" : v >= 45 ? "#f5a800" : "var(--lt-muted)";
  return `<div class="ring" style="width:${size}px;height:${size}px" title="${t("Deal score {n}/100", { n: v })}"><svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="${r}" class="ring-bg"/><circle cx="22" cy="22" r="${r}" class="ring-fg" stroke="${col}" stroke-dasharray="${C}" style="--off:${C * (1 - v / 100)};--c:${C}" transform="rotate(-90 22 22)"/></svg><span>${v}</span></div>`;
}

const BRICK = `<svg viewBox="0 0 64 44" class="logo" aria-hidden="true"><rect x="2" y="12" width="60" height="30" rx="4" fill="#d01012"/><rect x="9" y="4" width="14" height="10" rx="3" fill="#d01012"/><rect x="41" y="4" width="14" height="10" rx="3" fill="#d01012"/><rect x="9" y="4" width="14" height="4" rx="2" fill="#ff5a4d" opacity=".7"/><rect x="41" y="4" width="14" height="4" rx="2" fill="#ff5a4d" opacity=".7"/><rect x="2" y="12" width="60" height="6" rx="3" fill="#ff5a4d" opacity=".45"/></svg>`;

// ------------------------------------------------------------------ styles
const STYLE = `
:host{--lt-bg:var(--primary-background-color,#f3f4f7);--lt-card:var(--card-background-color,#fff);--lt-text:var(--primary-text-color,#1b1c20);--lt-muted:var(--secondary-text-color,#6b6f7a);
--lt-line:var(--divider-color,#e1e3e8);--lt-accent:var(--primary-color,#0057a6);--lt-on-accent:var(--text-primary-color,#fff);--lt-soft:color-mix(in srgb,var(--lt-accent) 10%,var(--lt-card));
--lt-red:#d01012;--lt-green:#00852b;--lt-yellow:#f5a800;--lt-purple:#7a3c9e;--lt-radius:16px;--lt-shadow:var(--ha-card-box-shadow,0 1px 2px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.06));
display:block;background:var(--lt-bg);color:var(--lt-text);min-height:100vh;font-family:var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif);-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}[hidden]{display:none!important}
.wrap{max-width:1280px;margin:0 auto;padding:12px 16px 48px}
header{display:flex;align-items:center;gap:12px;padding:6px 0 14px}
.logo{width:40px;height:28px;flex:none;filter:drop-shadow(0 2px 3px rgba(208,16,18,.3))}img.logo{width:44px;height:44px;filter:drop-shadow(0 2px 4px rgba(0,0,0,.18))}
h1{margin:0;font-size:21px;font-weight:700;letter-spacing:-.01em}
.meta{font-size:12px;color:var(--lt-muted)}
.hsp{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;background:var(--lt-soft);font-size:12px;color:var(--lt-muted)}
.dotst{width:8px;height:8px;border-radius:50%;background:var(--lt-green);display:inline-block}.dotst.warn{background:var(--lt-yellow)}.dotst.bad{background:var(--lt-red)}
button{font:inherit;color:inherit}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:10px;border:1px solid transparent;background:var(--lt-accent);color:var(--lt-on-accent);cursor:pointer;font-weight:500;transition:transform .15s,box-shadow .15s,background .15s,opacity .15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.12)}.btn:active{transform:translateY(0)}
.btn.ghost{background:var(--lt-card);color:var(--lt-text);border-color:var(--lt-line)}.btn.danger{background:var(--lt-card);color:var(--lt-red);border-color:color-mix(in srgb,var(--lt-red) 40%,var(--lt-line))}
a.btn{text-decoration:none}.btn[disabled]{opacity:.5;pointer-events:none}.btn.sm{padding:5px 10px;font-size:13px;border-radius:8px}
.spin{display:inline-block;width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
.erow.on{background:var(--lt-soft)}.setlink{cursor:pointer;color:inherit;text-decoration:none;border-bottom:1px dashed var(--lt-muted)}.setlink:hover{color:var(--lt-accent);border-color:var(--lt-accent)}
.res{display:inline-flex;gap:4px;align-items:center;font-size:12px;padding:2px 8px;border-radius:99px;margin:1px 2px 1px 0;white-space:nowrap}.res.ok{background:color-mix(in srgb,var(--lt-green) 16%,transparent);color:var(--lt-green)}
.res.fail{background:color-mix(in srgb,var(--lt-red) 14%,transparent);color:var(--lt-red)}.res.skip{background:var(--lt-soft);color:var(--lt-muted)}div.res{border-radius:8px;display:flex}
.tbl.log tr.lrow td:first-child{border-left:3px solid transparent}.tbl.log tr.lv-ok td:first-child{border-left-color:var(--lt-green)}.tbl.log tr.lv-error td:first-child{border-left-color:var(--lt-red)}.tbl.log tr.lv-warning td:first-child{border-left-color:var(--lt-yellow)}
.tag{white-space:nowrap}.chip.okc.on{background:var(--lt-green)}
@media(max-width:640px){.tbl.log tr:not(:has(td[colspan])) > :nth-child(3),.tbl.log tr:not(:has(td[colspan])) > :nth-child(5){display:none}.tbl.log td,.tbl.log th{padding:6px 4px}.tbl.log td:first-child{font-size:11px;white-space:normal!important;min-width:54px}.otbl .ou{min-width:150px}}.chip.failc.on{background:var(--lt-red)}
.mbadge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px;background:var(--lt-purple);color:#fff;vertical-align:1px}.abadge{font-size:10px;font-weight:600;padding:1px 6px;border-radius:6px;background:var(--lt-soft);color:var(--lt-muted);vertical-align:1px}
.otbl input{padding:6px 8px;font-size:13px}.otbl .ou{min-width:220px;width:100%}.otbl .op{width:96px}.orow.dirty td{background:color-mix(in srgb,var(--lt-yellow) 10%,transparent)}.fixbox{background:var(--lt-soft);border-radius:12px;padding:12px;margin:2px 0 8px;animation:rise .25s both}
.rule{border:1px solid var(--lt-line);border-radius:14px;padding:12px 14px;margin-bottom:10px;display:flex;gap:12px;align-items:flex-start;transition:border-color .2s}.rule:hover{border-color:var(--lt-accent)}
.rule.off{opacity:.55}.rule .sum{font-size:13px;color:var(--lt-muted);margin-top:4px;line-height:1.6}.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:var(--lt-soft);margin:2px 4px 2px 0}
.switch{position:relative;width:40px;height:22px;flex:none;cursor:pointer}.switch input{display:none}.switch i{position:absolute;inset:0;border-radius:99px;background:var(--lt-line);transition:background .2s}
.switch i::after{content:"";position:absolute;left:3px;top:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .2s cubic-bezier(.3,1.4,.5,1)}.switch input:checked+i{background:var(--lt-accent)}.switch input:checked+i::after{transform:translateX(18px)}
.trig{display:grid;grid-template-columns:24px 1fr auto;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--lt-line)}.trig:last-child{border:0}.trig select,.trig input{padding:6px 8px}
.tgt{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px;border:1px solid var(--lt-line);border-radius:10px;margin-bottom:6px}.tgt select,.tgt input{padding:6px 8px}
.jobbar{display:flex;gap:12px;align-items:center;background:var(--lt-card);border-radius:14px;padding:10px 14px;margin-bottom:12px;box-shadow:var(--lt-shadow);border-left:4px solid var(--lt-accent)}.jobbar.in{animation:rise .35s both}
.jobbar .spin{color:var(--lt-accent);flex:none}.jt{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lk{font-size:12px;font-weight:600;padding:2px 7px;border-radius:6px;white-space:nowrap}.lk.suspect{background:color-mix(in srgb,var(--lt-red) 15%,transparent);color:var(--lt-red)}
.lk.ok,.lk.confirmed{background:color-mix(in srgb,var(--lt-green) 15%,transparent);color:var(--lt-green)}.lk.unknown{background:var(--lt-soft);color:var(--lt-muted)}
.actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.action{border:1px solid var(--lt-line);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:8px}
.action b{font-size:15px}.action p{margin:0;font-size:13px}.action .btn{align-self:flex-start;margin-top:auto}
/* section switch */
.seg{display:flex;gap:6px;background:var(--lt-card);padding:5px;border-radius:14px;box-shadow:var(--lt-shadow);position:relative;margin-bottom:12px}
.seg button{flex:1;border:0;background:none;padding:11px 12px;border-radius:10px;cursor:pointer;font-weight:600;font-size:15px;color:var(--lt-muted);position:relative;z-index:1;transition:color .25s}
.seg button.on{color:var(--lt-on-accent)}
.seg .ind{position:absolute;top:5px;bottom:5px;border-radius:10px;background:var(--lt-accent);transition:left .35s cubic-bezier(.3,1.3,.5,1),width .35s cubic-bezier(.3,1.3,.5,1);box-shadow:0 3px 10px color-mix(in srgb,var(--lt-accent) 40%,transparent)}
.seg small{display:block;font-weight:400;font-size:11px;opacity:.8}
.sub{display:flex;gap:4px;margin:0 0 14px;overflow-x:auto;scrollbar-width:none}
.sub button{border:0;background:none;padding:8px 14px;border-radius:99px;cursor:pointer;color:var(--lt-muted);font-weight:500;white-space:nowrap;transition:background .2s,color .2s}
.sub button:hover{background:var(--lt-soft)}.sub button.on{background:var(--lt-soft);color:var(--lt-accent)}
.sub .count{font-size:11px;background:var(--lt-accent);color:var(--lt-on-accent);border-radius:99px;padding:1px 7px;margin-left:5px}
/* kpis */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--lt-card);border-radius:var(--lt-radius);padding:14px 16px;box-shadow:var(--lt-shadow);position:relative;overflow:hidden}
.kpi small{color:var(--lt-muted);font-size:12px;display:flex;align-items:center;gap:6px}.kpi b{display:block;font-size:24px;font-weight:700;margin-top:4px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kpi .sub2{font-size:12px;color:var(--lt-muted);margin-top:2px}.kpi.hl::before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:var(--c,var(--lt-accent))}
.up{color:var(--lt-green)}.down{color:var(--lt-red)}
/* filter bar */
.fbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.search{flex:1;min-width:200px;position:relative}.search input{width:100%;padding-left:34px}.search::before{content:"🔍";position:absolute;left:11px;top:50%;transform:translateY(-50%);font-size:13px;opacity:.6}
input,select,textarea{font:inherit;padding:9px 11px;border-radius:10px;border:1px solid var(--lt-line);background:var(--lt-card);color:var(--lt-text);transition:border-color .15s,box-shadow .15s;min-width:0}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--lt-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--lt-accent) 20%,transparent)}
input[type=range]{padding:0;accent-color:var(--lt-accent)}input[type=checkbox]{accent-color:var(--lt-accent);width:16px;height:16px}
.thr{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--lt-muted);background:var(--lt-card);border:1px solid var(--lt-line);border-radius:10px;padding:6px 12px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.chip{padding:5px 12px;border-radius:99px;background:var(--lt-card);border:1px solid var(--lt-line);cursor:pointer;font-size:13px;transition:all .2s;user-select:none}
.chip:hover{border-color:var(--lt-accent)}.chip.on{background:var(--lt-accent);color:var(--lt-on-accent);border-color:transparent}.chip.sm{font-size:12px;padding:3px 10px}
.toggle{display:inline-flex;border:1px solid var(--lt-line);border-radius:10px;overflow:hidden}.toggle button{border:0;background:var(--lt-card);padding:8px 11px;cursor:pointer}.toggle button.on{background:var(--lt-soft);color:var(--lt-accent)}
/* layout */
.cols{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px}@media(max-width:980px){.cols{grid-template-columns:1fr}}
.panel{background:var(--lt-card);border-radius:var(--lt-radius);padding:16px;box-shadow:var(--lt-shadow);margin-bottom:16px}
.panel h3{margin:0 0 12px;font-size:16px;display:flex;align-items:center;gap:8px}.panel h3 .hsp{flex:1}
.panel p{margin:0 0 10px;color:var(--lt-muted);font-size:14px;line-height:1.5}
h2.sec{font-size:16px;margin:18px 0 10px;display:flex;align-items:center;gap:8px}h2.sec .n{font-size:12px;color:var(--lt-muted);font-weight:500}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:14px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:800px){.two{grid-template-columns:1fr}}
/* cards */
.card{background:var(--lt-card);border-radius:var(--lt-radius);padding:10px 10px 12px;cursor:pointer;position:relative;box-shadow:var(--lt-shadow);display:flex;flex-direction:column;gap:3px;transition:transform .2s cubic-bezier(.3,1.4,.5,1),box-shadow .2s;outline:none}
.card:hover,.card:focus-visible{transform:translateY(-3px);box-shadow:0 10px 28px rgba(0,0,0,.14)}
.card .img{height:130px;border-radius:12px;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden;margin-bottom:6px;position:relative}
.card .img img{max-width:92%;max-height:118px;object-fit:contain;transition:transform .3s}.card:hover .img img{transform:scale(1.05)}
.card .img .ph{font-size:40px;opacity:.5}
.card .n{font-weight:600;font-size:14px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:36px}
.card .m{color:var(--lt-muted);font-size:12px}.card .row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.card .p{font-size:20px;font-weight:700;letter-spacing:-.02em}.card s{color:var(--lt-muted);font-size:13px}
.badges{position:absolute;top:16px;left:16px;display:flex;flex-direction:column;gap:4px;z-index:1}
.badge{font-size:11px;font-weight:700;padding:3px 8px;border-radius:7px;color:#fff;background:var(--lt-green);width:fit-content;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.badge.red{background:var(--lt-red)}.badge.blue{background:#0057a6}.badge.purple{background:var(--lt-purple)}.badge.yellow{background:var(--lt-yellow);color:#241a00}.badge.grey{background:#6c6e68}
.card .ring{position:absolute;top:14px;right:14px;z-index:1;background:var(--lt-card);border-radius:50%;box-shadow:0 1px 4px rgba(0,0,0,.15)}
.trend{font-size:12px;font-weight:600}.stars{color:var(--lt-yellow);letter-spacing:1px;font-size:13px}
.spark{width:100%;height:26px;margin-top:2px}.ph-spark{border-bottom:1px dashed var(--lt-line);height:14px;margin-bottom:12px}
/* hero */
.hero{display:grid;grid-template-columns:220px 1fr;gap:18px;background:linear-gradient(135deg,color-mix(in srgb,var(--lt-yellow) 18%,var(--lt-card)),var(--lt-card) 60%);border-radius:20px;padding:16px;box-shadow:var(--lt-shadow);margin-bottom:6px;cursor:pointer;position:relative;overflow:hidden;transition:transform .25s}
.hero:hover{transform:translateY(-2px)}.hero .img{height:170px;background:#fff;border-radius:14px;display:flex;align-items:center;justify-content:center}.hero .img img{max-width:92%;max-height:160px}
.hero .k{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--lt-muted);font-weight:700}.hero h2{margin:4px 0 6px;font-size:22px}
.hero .p{font-size:34px;font-weight:800;letter-spacing:-.03em}.hero .ring{position:absolute;right:16px;top:16px}
@media(max-width:600px){.hero{grid-template-columns:1fr}}
/* timeline */
.tl{list-style:none;margin:0;padding:0}.tl li{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--lt-line);font-size:13px;cursor:pointer;align-items:flex-start}.tl li:last-child{border:0}
.tl .ic{width:28px;height:28px;border-radius:9px;display:flex;align-items:center;justify-content:center;flex:none;background:var(--lt-soft)}
.tl .t{color:var(--lt-muted);font-size:11px}
/* tables */
.tbl{color:inherit;width:100%;border-collapse:collapse;font-size:14px}.tbl th{text-align:left;font-size:12px;color:var(--lt-muted);font-weight:600;padding:8px;border-bottom:1px solid var(--lt-line);white-space:nowrap}
.tbl th[data-sort]{cursor:pointer}.tbl th[data-sort]:hover{color:var(--lt-accent)}
.tbl td{padding:8px;border-bottom:1px solid var(--lt-line);vertical-align:top}.tbl tr.click{cursor:pointer;transition:background .15s}.tbl tr.click:hover{background:var(--lt-soft)}
.tbl .num{text-align:right;font-variant-numeric:tabular-nums}.tscroll{overflow-x:auto}
/* charts */
.chartbox{position:relative}.chart{width:100%;height:auto;display:block}.gl{stroke:var(--lt-line)}.axis{font-size:11px;fill:var(--lt-muted)}.hv{stroke:var(--lt-muted);stroke-dasharray:3 3}
.ln{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.1s cubic-bezier(.4,0,.2,1) forwards}.ln.dash{stroke-dasharray:.012 .01;stroke-dashoffset:0;animation:fade .8s both}
.area{animation:fade 1s .4s both}.dot{animation:pop .4s .9s both;transform-box:fill-box;transform-origin:center}
.tip{position:absolute;top:8px;left:60px;background:var(--lt-card);border:1px solid var(--lt-line);border-radius:10px;padding:8px 10px;font-size:12px;pointer-events:none;box-shadow:0 6px 18px rgba(0,0,0,.15);white-space:nowrap;z-index:2}
.tip i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;margin-top:6px;color:var(--lt-muted)}.lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.donut{display:flex;gap:18px;align-items:center;flex-wrap:wrap}.arc{animation:arc .9s cubic-bezier(.4,0,.2,1) both;animation-delay:var(--d)}
.dn-v{font-size:17px;font-weight:700;fill:var(--lt-text)}.dn-l{font-size:10px;fill:var(--lt-muted)}
.dlegend{flex:1;min-width:150px}.dl{display:flex;align-items:center;gap:8px;font-size:13px;padding:3px 0}.dl i{width:10px;height:10px;border-radius:3px;flex:none}.dl span{flex:1}
.bars{display:flex;align-items:flex-end;gap:6px;height:170px;padding-top:18px;overflow-x:auto}
.bar{flex:1;min-width:30px;display:flex;flex-direction:column;align-items:center;height:100%;justify-content:flex-end}
.bf{width:70%;height:var(--h);background:linear-gradient(var(--lt-accent),color-mix(in srgb,var(--lt-accent) 60%,var(--lt-card)));border-radius:6px 6px 2px 2px;animation:grow .8s cubic-bezier(.3,1.2,.5,1) both;animation-delay:calc(var(--i) * 40ms);transform-origin:bottom}
.bv{font-size:11px;color:var(--lt-muted);margin-bottom:3px}.bl{font-size:11px;color:var(--lt-muted);margin-top:4px}
.ring{position:relative;display:inline-block}.ring svg{width:100%;height:100%}.ring-bg{fill:none;stroke:var(--lt-line);stroke-width:4}
.ring-fg{fill:none;stroke-width:4;stroke-linecap:round;stroke-dashoffset:var(--off);animation:ring 1s cubic-bezier(.4,0,.2,1) both}
.ring span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
/* misc */
.empty{padding:36px 20px;text-align:center;color:var(--lt-muted);background:var(--lt-card);border-radius:var(--lt-radius);border:1px dashed var(--lt-line)}.empty.small{padding:18px;font-size:13px;background:none}
.empty .big{font-size:40px;display:block;margin-bottom:8px}
.banner{display:flex;gap:10px;align-items:center;background:color-mix(in srgb,var(--lt-yellow) 16%,var(--lt-card));border:1px solid color-mix(in srgb,var(--lt-yellow) 45%,transparent);border-radius:12px;padding:10px 14px;margin-bottom:14px;font-size:13px}
.banner a{color:var(--lt-accent);cursor:pointer;text-decoration:underline}
a{color:var(--lt-accent)}.err{color:var(--lt-red);font-size:12px}.ok{color:var(--lt-green)}.muted{color:var(--lt-muted)}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:12px}
.form label{display:flex;flex-direction:column;font-size:12px;color:var(--lt-muted);gap:4px;font-weight:500}.form label.chk{flex-direction:row;align-items:center;gap:8px;font-size:14px;color:var(--lt-text)}
.hint{font-size:12px;margin-top:-4px;min-height:16px}
.radio{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}.radio label{flex:1;min-width:200px;border:2px solid var(--lt-line);border-radius:12px;padding:12px;cursor:pointer;transition:border-color .2s,background .2s}
.radio label.on{border-color:var(--lt-accent);background:var(--lt-soft)}.radio input{display:none}.radio b{display:block}.radio span{font-size:12px;color:var(--lt-muted)}
.drop{border:2px dashed var(--lt-line);border-radius:16px;padding:30px;text-align:center;transition:all .2s;cursor:pointer;background:var(--lt-card)}
.drop.over{border-color:var(--lt-accent);background:var(--lt-soft);transform:scale(1.01)}.drop .big{font-size:36px;display:block;margin-bottom:6px}
.steps{display:flex;gap:8px;margin-bottom:14px;font-size:13px}.steps span{padding:4px 12px;border-radius:99px;background:var(--lt-card);border:1px solid var(--lt-line);color:var(--lt-muted)}.steps span.on{background:var(--lt-accent);color:var(--lt-on-accent);border-color:transparent}
.st{display:inline-flex;width:22px;height:22px;border-radius:50%;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff}.st.ok{background:var(--lt-green)}.st.warning{background:var(--lt-yellow);color:#241a00}.st.error{background:var(--lt-red)}
.iss{font-size:12px;line-height:1.4}.iss .error{color:var(--lt-red)}.iss .warning{color:#9a6a00}.iss .info{color:var(--lt-muted)}
.sumpills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.sumpills span{padding:6px 12px;border-radius:10px;font-size:13px;font-weight:600;background:var(--lt-soft)}
.shops{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.shop{background:var(--lt-card);border-radius:var(--lt-radius);padding:14px;box-shadow:var(--lt-shadow)}.shop h4{margin:0 0 8px;display:flex;align-items:center;gap:8px}
.meter{height:8px;border-radius:99px;background:var(--lt-line);overflow:hidden;margin:8px 0}.meter i{display:block;height:100%;background:var(--lt-green);border-radius:99px;animation:grow-x .9s cubic-bezier(.4,0,.2,1) both;transform-origin:left}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:2px 0}.kv span{color:var(--lt-muted)}
.skel{background:linear-gradient(90deg,var(--lt-line) 25%,color-mix(in srgb,var(--lt-line) 40%,var(--lt-card)) 50%,var(--lt-line) 75%);background-size:200% 100%;animation:shimmer 1.3s infinite;border-radius:var(--lt-radius)}
/* dialog */
dialog{border:0;border-radius:22px;padding:0;max-width:920px;width:calc(100vw - 24px);max-height:calc(100vh - 32px);background:var(--lt-card);color:var(--lt-text);box-shadow:0 24px 80px rgba(0,0,0,.35)}
dialog[open]{animation:dlg .32s cubic-bezier(.3,1.3,.5,1)}dialog.closing{animation:dlgout .18s ease-in forwards}
dialog::backdrop{background:rgba(10,12,20,.55);backdrop-filter:blur(3px);animation:fade .25s}
.dhead{display:grid;grid-template-columns:150px 1fr auto;gap:16px;padding:18px 20px;border-bottom:1px solid var(--lt-line);position:sticky;top:0;background:var(--lt-card);z-index:3}
.dhead .img{height:110px;background:#fff;border-radius:12px;display:flex;align-items:center;justify-content:center}.dhead .img img{max-width:92%;max-height:100px}
.dhead h2{margin:0 0 4px;font-size:20px}.dbody{padding:16px 20px 20px}.x{border:0;background:var(--lt-soft);width:34px;height:34px;border-radius:50%;cursor:pointer;font-size:18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:12px 0}.stat{background:var(--lt-soft);border-radius:12px;padding:8px 10px}.stat small{display:block;font-size:11px;color:var(--lt-muted)}.stat b{font-size:15px}
fieldset{border:1px solid var(--lt-line);border-radius:14px;padding:12px 14px 4px;margin:0 0 12px}legend{padding:0 6px;font-weight:600;font-size:14px}
@media(max-width:640px){.dhead{grid-template-columns:72px 1fr;gap:12px;padding:14px}.dhead .img{height:72px}.dhead .img img{max-height:64px}.dhead h2{font-size:17px;padding-right:40px}
.dhead>div:last-child{position:absolute;top:12px;right:12px}.dhead .ring{display:none}.dbody{padding:12px 14px 16px}.axis{font-size:22px}}
/* toast */
.toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:8px;z-index:20}
.toast{background:#23252c;color:#fff;padding:11px 16px;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.3);font-size:14px;max-width:380px;animation:toast .35s cubic-bezier(.3,1.3,.5,1);display:flex;gap:8px;align-items:flex-start}
.toast.err{background:#8e0b0c}.toast.ok{background:#0b5d22}.toast.out{animation:toastout .25s forwards}
/* motion */
.enter>*{animation:rise .45s cubic-bezier(.2,.8,.3,1) both}.enter>*:nth-child(2){animation-delay:40ms}.enter>*:nth-child(3){animation-delay:80ms}.enter>*:nth-child(4){animation-delay:120ms}.enter>*:nth-child(n+5){animation-delay:160ms}
.enter .card,.enter .shop{animation:pop .45s cubic-bezier(.3,1.3,.5,1) both;animation-delay:calc(min(var(--i,0),14) * 30ms + 80ms)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}@keyframes pop{from{opacity:0;transform:scale(.94) translateY(8px)}}@keyframes fade{from{opacity:0}}
@keyframes draw{to{stroke-dashoffset:0}}@keyframes grow{from{transform:scaleY(0)}}@keyframes grow-x{from{transform:scaleX(0)}}@keyframes spin{to{transform:rotate(360deg)}}
@keyframes ring{from{stroke-dashoffset:var(--c)}}@keyframes arc{from{stroke-dasharray:0 999}}@keyframes shimmer{to{background-position:-200% 0}}
@keyframes dlg{from{opacity:0;transform:translateY(16px) scale(.97)}}@keyframes dlgout{to{opacity:0;transform:translateY(10px) scale(.98)}}
@keyframes toast{from{opacity:0;transform:translateY(16px) scale(.95)}}@keyframes toastout{to{opacity:0;transform:translateX(30px)}}
@media(max-width:640px){.wrap{padding:8px 10px 40px}h1{font-size:17px}header{gap:8px}header .lbl{display:none}.logo{width:32px;height:22px}img.logo{width:36px;height:36px}
.seg{gap:2px;padding:4px}.seg button{font-size:13px;padding:9px 4px}.seg small{display:none}
.kpis{grid-template-columns:1fr 1fr;gap:8px}.kpi{padding:10px 12px}.kpi b{font-size:19px}
.grid{grid-template-columns:1fr 1fr;gap:10px}.card .img{height:96px}.card .img img{max-height:88px}.card .p{font-size:17px}.card .ring{width:34px!important;height:34px!important}
.hero .img{height:130px}.hero .p{font-size:28px}.fbar .search{min-width:100%}.panel{padding:12px}}
@media (prefers-reduced-motion: reduce){*,*::before{animation:none!important;transition:none!important}.ln{stroke-dashoffset:0}}
`;

// ------------------------------------------------------------------ component
// labels are English source strings, translated with t() when rendered
const SECTIONS = {
  deals: { label: "🏷️ Deals & watchlist", hint: "sets you keep an eye on", subs: [["today", "Today"], ["watch", "Watchlist"], ["all", "All prices"]] },
  collection: { label: "📦 My collection", hint: "what you own", subs: [["overview", "Overview"], ["sets", "Sets"]] },
  log: { label: "📜 Logbook", hint: "checks, prices, errors", subs: [["all", "Everything"], ["checks", "Shop checks"], ["errors", "Open errors"]] },
  manage: { label: "⚙️ Manage", hint: "add, import, shops, settings", subs: [["add", "Add"], ["import", "Import"], ["links", "Link check"], ["notify", "Notifications"], ["shops", "Shops & jobs"], ["settings", "Settings"], ["userscript", "Userscript"], ["backup", "Backup"]] },
};

class LegoTrackerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      section: "deals", sub: { deals: "today", collection: "overview", log: "all", manage: "add" },
      f: { q: "", theme: null, subtheme: null, sort: "score", cond: null }, threshold: null,
      cview: "grid", csort: { key: "value", dir: -1 }, range: 90, addMode: "watch",
      imp: { text: "", name: "", analysis: null, only: false, replace: false, track: true, update: true },
      links: { status: "suspect", scope: "owned", edit: null },
      errs: { type: "all", scope: "all", shop: "", open: null, showIgnored: false },
      logv: { level: "", kind: "", retailer: "", source: "", status: "", set: "", q: "", data: null, open: null, extra: [] },
      notif: { data: null, edit: null, idx: null, err: null },
      data: null, coll: null, err: null, busy: false,
    };
    try {
      const saved = JSON.parse(localStorage.getItem("lego_tracker_ui") || "{}");
      if (SECTIONS[saved.section]) this.state.section = saved.section;
      if (saved.cview === "grid" || saved.cview === "table") this.state.cview = saved.cview;
      if ([0, 30, 90, 365].includes(saved.range)) this.state.range = saved.range;
      for (const [k, v] of Object.entries(saved.sub || {})) if (SECTIONS[k] && SECTIONS[k].subs.some(([x]) => x === v)) this.state.sub[k] = v;
    } catch (e) { /* storage unavailable */ }
    this.resetFilters();
  }
  set hass(h) { const first = !this._hass; this._hass = h; if (first) { this.render(true); this.load(); } }
  set narrow(v) { this._narrow = v; }
  set panel(_) {}
  persist() { try { localStorage.setItem("lego_tracker_ui", JSON.stringify({ section: this.state.section, sub: this.state.sub, cview: this.state.cview, range: this.state.range })); } catch (e) { /* ignore */ } }

  // ---------------------------------------------------------------- data
  async load(animate = false) {
    try {
      const [d, c] = await Promise.all([this._hass.callWS({ type: "lego_tracker/overview" }), this._hass.callWS({ type: "lego_tracker/collection" })]);
      if (await loadLanguage(d.language, d.version)) { animate = true; this.shadowRoot.host.setAttribute("lang", LANG); }
      this.state.data = d; this.state.coll = c; this.state.err = null;
      if (this.state.threshold == null) this.state.threshold = d.threshold;
      this.state.job = d.job; if (d.job && d.job.running) this.pollJob();
    } catch (e) { this.state.err = e.message || String(e); }
    this.render(animate || !this._rendered);
  }
  svc(service, data = {}, ret = false) {
    return this._hass.callWS({ type: "call_service", domain: "lego_tracker", service, service_data: data, return_response: ret });
  }
  toast(msg, kind = "") {
    const box = this.shadowRoot.querySelector(".toasts"); if (!box) return;
    const t = document.createElement("div"); t.className = `toast ${kind}`;
    t.innerHTML = `<span>${kind === "err" ? "⚠️" : kind === "ok" ? "✅" : "ℹ️"}</span><span>${esc(msg)}</span>`;
    box.appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 260); }, kind === "err" ? 6500 : 3800);
  }
  async busy(btn, label, fn) {
    const old = btn ? btn.innerHTML : ""; if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${esc(label)}`; }
    try { return await fn(); } catch (e) { this.toast(tx(e.message || String(e)), "err"); return undefined; } finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
  }

  // value of one unit, honouring the "value based on" setting
  unitValue(s) {
    const c = s.collection || {};
    return this.state.data && this.state.data.value_source === "import_first" ? (c.current_value ?? s.best_price ?? s.rrp) : (s.best_price ?? c.current_value ?? s.rrp);
  }
  // ---------------------------------------------------------------- derived
  get sets() { return this.state.data ? this.state.data.sets : []; }
  isDeal(s) {
    return s.best_price != null && (s.is_all_time_low || s.target_hit || s.deal_score >= 70 || (s.discount_rrp != null && s.discount_rrp >= this.state.threshold));
  }
  filtered(list) {
    const { q, theme, subtheme, cond } = this.state.f;
    let out = list.filter((s) => (!theme || s.theme === theme) && (!subtheme || s.subtheme === subtheme) && (!cond || (s.collection && (s.collection.condition || "Unknown") === cond)));
    if (q) { const n = q.toLowerCase(); out = out.filter((s) => `${s.set_number} ${s.name} ${s.theme} ${s.subtheme} ${s.notes || ""}`.toLowerCase().includes(n)); }
    return out;
  }
  sorted(list, key) {
    const k = {
      score: (s) => -(s.deal_score || 0) - (s.best_price != null ? 0 : -1000), discount: (s) => -(s.discount_rrp ?? -1e9), price: (s) => s.best_price ?? 1e9,
      ppp: (s) => s.price_per_piece ?? 1e9, drop: (s) => s.change_30d ?? 1e9, name: (s) => (s.name || "").toLowerCase(), number: (s) => +s.set_number,
      priority: (s) => -(s.priority || 0) * 1000 - (s.deal_score || 0),
    }[key] || ((s) => 0);
    return [...list].sort((a, b) => { const x = k(a), y = k(b); return x < y ? -1 : x > y ? 1 : 0; });
  }

  // ---------------------------------------------------------------- shell
  render(animate = false) {
    const s = this.state, root = this.shadowRoot;
    this._rendered = true;
    const sec = SECTIONS[s.section], sub = s.sub[s.section];
    const d = s.data;
    const status = d ? this.healthDot() : "";
    const keepToasts = root.querySelector(".toasts"), keepDlg = root.getElementById("dlg");
    root.innerHTML = `<style>${STYLE}</style><div class="wrap">
      <header>${this._narrow ? `<ha-menu-button></ha-menu-button>` : ""}<img class="logo" src="/lego_tracker_static/icon.png${d ? `?v=${encodeURIComponent(d.version)}` : ""}" alt="" onerror="this.outerHTML=this.dataset.fb" data-fb="${esc(BRICK)}"><div><h1>LEGO Organizing Tool</h1><div class="meta">${d ? `v${esc(d.version)} · ${t("{n} sets tracked", { n: d.sets.length })}` : t("loading…")}</div></div>
        <div class="hsp"></div>${status}<button class="btn ghost sm" data-act="discover" title="${t("Search shop links for sets that have none yet")}">🔎 <span class="lbl">${t("Find links")}</span></button><button class="btn sm" data-act="refresh" title="${t("Fetch all shop prices now")}">↻ <span class="lbl">${t("Refresh prices")}</span></button></header>
      <div id="jobbar"></div>
      <nav class="seg" role="tablist">${Object.entries(SECTIONS).map(([k, v]) => `<button role="tab" data-sec="${k}" class="${k === s.section ? "on" : ""}">${t(v.label)}<small>${t(v.hint)}</small></button>`).join("")}<span class="ind"></span></nav>
      <div class="sub">${sec.subs.map(([k, l]) => `<button data-sub="${k}" class="${k === sub ? "on" : ""}">${t(l)}${this.subCount(s.section, k)}</button>`).join("")}</div>
      <div id="content"></div></div><div class="toasts"></div><dialog id="dlg"></dialog>`;
    // keep notifications and an open set dialog alive across re-renders (e.g. when a job finishes)
    if (keepToasts) root.querySelector(".toasts").replaceWith(keepToasts);
    if (keepDlg) { root.getElementById("dlg").replaceWith(keepDlg); keepDlg._bound = true; }
    const menu = root.querySelector("ha-menu-button"); if (menu) { menu.hass = this._hass; menu.narrow = this._narrow; }
    this.placeIndicator(false);
    root.querySelectorAll("[data-sec]").forEach((b) => b.addEventListener("click", () => { if (s.section === b.dataset.sec) return; s.section = b.dataset.sec; this.resetFilters(); this.persist(); this.render(true); }));
    root.querySelectorAll(".sub [data-sub]").forEach((b) => b.addEventListener("click", () => { s.sub[s.section] = b.dataset.sub; if (s.section === "log") { s.logv.data = null; s.logv.open = null; } this.resetFilters(); this.persist(); this.render(true); }));
    this.renderJob();
    const dlg = root.getElementById("dlg");
    if (!dlg._bound) dlg.addEventListener("cancel", (e) => { e.preventDefault(); this.closeDialog(); });
    if (!dlg._bound) dlg.addEventListener("click", (e) => { if (e.target === dlg) this.closeDialog(); });
    dlg._bound = true;
    if (!this._keys) { this._keys = true; this.addEventListener("keydown", (e) => { if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test((e.composedPath()[0] || {}).tagName || "")) { const q = this.shadowRoot.getElementById("q"); if (q) { e.preventDefault(); q.focus(); } } }); }
    root.querySelectorAll("header [data-act]").forEach((b) => b.addEventListener("click", () => this.startJob(b.dataset.act, b)));
    this.renderContent(animate);
    this.renderJob();
    if (animate) requestAnimationFrame(() => this.placeIndicator(true));
  }
  // ---------------------------------------------------------------- background jobs
  async startJob(kind, btn) {
    const d = this.state.data || {}, paused = Object.entries(d.paused || {});
    const svc = { refresh: "refresh", discover: "discover_offers", enrich: "enrich_sets" }[kind];
    const data = {};
    if (paused.length && kind !== "enrich") {
      const list = paused.map(([k, h]) => `• ${k} (${t("{time} left", { time: dur(h * 3600) })})`).join("\n");
      data.force = confirm(`${t("These shops are paused after being blocked:")}\n${list}\n\n${t("OK = try anyway (risk of a new block)")}\n${t("Cancel = skip the paused shops")}`);
    }
    if (kind === "enrich" && btn && btn.dataset.all) data.all = true;
    await this.busy(btn, t("Starting…"), async () => {
      const r = (await this.svc(svc, data, true)).response;
      if (!r.total) { this.toast(kind === "refresh" ? t("Nothing to refresh: no sets with a link at an active shop") + (r.note ? ` (${tx(r.note)})` : "") : kind === "discover" ? t("All sets already have a link at every active shop") : t("All sets already have complete data"), ""); }
      else this.toast(`${t("{job} started for {n} sets", { job: tx(r.label), n: r.total })}${r.note ? ` · ${tx(r.note)}` : ""}`, "ok");
      this.state.job = r; this.pollJob();
    });
    this.renderJob();
  }
  pollJob() {
    if (this._poll) return;
    const tick = async () => {
      let info; try { info = await this._hass.callWS({ type: "lego_tracker/job" }); } catch (e) { this._poll = null; return; }
      const was = this.state.job && this.state.job.running;
      this.state.job = info.job; this.state.data && Object.assign(this.state.data, { paused: info.paused, schedule: info.schedule, last: info.last });
      this.renderJob();
      if (info.job && info.job.running) { if (info.job.done && info.job.done % 10 === 0 && info.job.done !== this._lastReload) { this._lastReload = info.job.done; this.load(); } this._poll = setTimeout(tick, 2000); return; }
      this._poll = null;
      if (was && info.last) {
        const l = info.last, parts = [l.updated && t("{n} updated", { n: l.updated }), l.found && t("{n} links found", { n: l.found }), l.skipped && t("{n} skipped (paused)", { n: l.skipped }), l.errors && t("{n} errors", { n: l.errors })].filter(Boolean).join(", ");
        this.toast(`${l.cancelled ? t("{job} stopped", { job: tx(l.label || "Job") }) : t("{job} done", { job: tx(l.label || "Job") })}${parts ? ": " + parts : ""}`, l.errors && !l.updated && !l.found ? "err" : "ok");
        this.load();
      }
    };
    this._poll = setTimeout(tick, 1200);
  }
  renderJob() {
    const box = this.shadowRoot.getElementById("jobbar"); if (!box) return;
    const j = this.state.job;
    this.shadowRoot.querySelectorAll("[data-act]").forEach((b) => { b.disabled = !!(j && j.running); });
    if (!j || !j.running) { box.innerHTML = ""; return; }
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    const el = (j.started && j.done) ? (Date.now() / 1000 - j.started) / j.done * (j.total - j.done) : null;
    const fresh = !box.querySelector(".jobbar");
    box.innerHTML = `<div class="jobbar${fresh ? " in" : ""}"><span class="spin"></span><div style="flex:1;min-width:0"><div class="jt"><b>${esc(tx(j.label))}</b> · ${j.done}/${j.total}${el ? ` · ${t("± {time} left", { time: dur(el) })}` : ""}${j.found ? ` · ${t("{n} found", { n: j.found })}` : ""}${j.updated ? ` · ${t("{n} updated", { n: j.updated })}` : ""}${j.errors ? ` · ${t("{n} errors", { n: j.errors })}` : ""}</div>
      <div class="meter" style="margin:6px 0 3px"><i style="width:${pct}%;animation:none;transition:width .6s"></i></div><div class="muted jt">${esc(tx(j.current || ""))}${j.note ? ` · ${esc(tx(j.note))}` : ""}</div></div><button class="btn ghost sm" id="jobstop">${t("Stop")}</button></div>`;
    box.querySelector("#jobstop").onclick = async () => { await this.svc("cancel_job", {}, true); this.toast(t("The job stops after the current set")); };
  }
  placeIndicator(anim) {
    const seg = this.shadowRoot.querySelector(".seg"); if (!seg) return;
    const on = seg.querySelector("button.on"), ind = seg.querySelector(".ind");
    if (!anim) ind.style.transition = "none";
    ind.style.left = on.offsetLeft + "px"; ind.style.width = on.offsetWidth + "px";
    if (!anim) requestAnimationFrame(() => { ind.style.transition = ""; });
  }
  resetFilters() { Object.assign(this.state.f, { theme: null, subtheme: null, cond: null, q: "" }); const s = this.state; s.f.sort = s.section === "collection" ? "value" : s.sub.deals === "watch" && s.section === "deals" ? "priority" : "score"; }
  subCount(sec, sub) {
    const d = this.state.data; if (!d) return "";
    let n = 0;
    if (sec === "deals" && sub === "today") n = d.sets.filter((s) => s.watched && this.isDeal(s)).length;
    if (sec === "deals" && sub === "watch") n = d.sets.filter((s) => s.watched).length;
    if (sec === "manage" && sub === "shops") n = Object.keys(d.paused || {}).length;
    if (sec === "log" && sub === "errors") n = this.errorRows().length;
    if (sec === "manage" && sub === "links") n = d.sets.reduce((a, s) => a + (s.offers_suspect || 0), 0);
    return n ? `<span class="count">${n}</span>` : "";
  }
  healthDot() {
    const h = this.state.data.health || {}; const paused = Object.keys(h.paused_hours || {}).length;
    const cls = paused ? "bad" : h.errors ? "warn" : "";
    const txt = paused ? t(paused > 1 ? "{n} shops paused" : "{n} shop paused", { n: paused }) : h.errors ? t(h.errors > 1 ? "{n} errors" : "{n} error", { n: h.errors }) : t("all shops OK");
    return `<span class="pill" title="${esc(this.state.data.transport)}" style="cursor:pointer" data-goto="manage/shops"><span class="dotst ${cls}"></span>${txt}</span>`;
  }

  renderContent(animate = false) {
    const s = this.state, el = this.shadowRoot.getElementById("content"); if (!el) return;
    if (s.err) { el.innerHTML = `<div class="empty"><span class="big">⚠️</span>${t("Could not load data: {error}", { error: esc(s.err) })}<br><br><button class="btn" id="retry">${t("Try again")}</button></div>`; el.querySelector("#retry").onclick = () => this.load(true); return; }
    if (!s.data) { el.innerHTML = `<div class="kpis">${"<div class='skel' style='height:86px'></div>".repeat(4)}</div><div class="grid">${"<div class='skel' style='height:260px'></div>".repeat(8)}</div>`; return; }
    const view = { deals: { today: this.vToday, watch: this.vWatch, all: this.vAll }, collection: { overview: this.vCollOverview, sets: this.vCollSets }, log: { all: this.vLog, checks: this.vLog, errors: this.vErrors }, manage: { notify: this.vNotify, add: this.vAdd, import: this.vImport, links: this.vLinks, shops: this.vShops, settings: this.vSettings, userscript: this.vUserscript, backup: this.vBackup } }[s.section][s.sub[s.section]];
    el.className = animate && !REDUCED ? "enter" : "";
    el.innerHTML = view.call(this);
    this.bindContent(el);
    this.hookCharts(el);
    if (animate) this.countUp(el);
  }
  // only the results area (keeps focus in the search box while typing)
  renderResults() {
    const el = this.shadowRoot.getElementById("results"); if (!el) return this.renderContent();
    const fn = el.dataset.fn; el.innerHTML = this[fn](); el.className = ""; this.bindCards(el); this.hookCharts(el);
  }

  // ---------------------------------------------------------------- shared pieces
  kpi(label, value, { fmt = "int", sub = "", color = "", icon = "" } = {}) {
    const shown = fmt === "eur" ? EUR0(value) : fmt === "pct" ? signPct(value) : fmt === "dec" ? (value == null ? "–" : value.toLocaleString(LOC, { maximumFractionDigits: 1 })) : INT(value);
    return `<div class="kpi${color ? " hl" : ""}" style="${color ? `--c:${color}` : ""}"><small>${icon} ${label}</small><b data-to="${value ?? ""}" data-fmt="${fmt}">${shown}</b>${sub ? `<div class="sub2">${sub}</div>` : ""}</div>`;
  }
  countUp(root) {
    if (REDUCED) return;
    root.querySelectorAll("[data-to]").forEach((el) => {
      const to = parseFloat(el.dataset.to); if (isNaN(to) || to === 0) return;
      const fmt = el.dataset.fmt, t0 = performance.now(), ms = 700;
      const f = fmt === "eur" ? EUR0 : fmt === "pct" ? signPct : fmt === "dec" ? (v) => v.toLocaleString(LOC, { maximumFractionDigits: 1 }) : INT;
      const step = (now) => { const p = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - p, 3); el.textContent = f(fmt === "pct" || fmt === "dec" ? Math.round(to * e * 10) / 10 : to * e); if (p < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
  }
  filterBar({ sorts, threshold = false, cond = false, viewToggle = false, list }) {
    const f = this.state.f;
    const counts = {}; list.forEach((s) => { const th = s.theme || "Unknown"; counts[th] = (counts[th] || 0) + 1; });
    const themes = Object.keys(counts).sort();
    const subs = {}; if (f.theme) list.filter((s) => s.theme === f.theme && s.subtheme).forEach((s) => { subs[s.subtheme] = (subs[s.subtheme] || 0) + 1; });
    const conds = {}; if (cond) list.forEach((s) => { const c = (s.collection && s.collection.condition) || "Unknown"; conds[c] = (conds[c] || 0) + 1; });
    return `<div class="fbar"><div class="search"><input id="q" placeholder="${t("Search by number, name, theme…")}  ( / )" value="${esc(f.q)}" autocomplete="off"></div>
      <select id="sort" aria-label="${t("Sort")}">${sorts.map(([k, l]) => `<option value="${k}" ${f.sort === k ? "selected" : ""}>${t(l)}</option>`).join("")}</select>
      ${threshold ? `<label class="thr">${t("Discount")} ≥ <b id="thrv">${this.state.threshold}%</b><input id="thr" type="range" min="5" max="70" step="5" value="${this.state.threshold}"></label>` : ""}
      ${viewToggle ? `<div class="toggle"><button data-cview="grid" class="${this.state.cview === "grid" ? "on" : ""}" title="${t("Tiles")}">▦</button><button data-cview="table" class="${this.state.cview === "table" ? "on" : ""}" title="${t("Table")}">☰</button></div>` : ""}</div>
      ${themes.length > 1 ? `<div class="chips"><span class="chip ${!f.theme ? "on" : ""}" data-theme="">${t("All themes")}</span>${themes.map((th) => `<span class="chip ${f.theme === th ? "on" : ""}" data-theme="${esc(th)}">${esc(th === "Unknown" ? t("Unknown") : th)} <span class="muted">${counts[th]}</span></span>`).join("")}</div>` : ""}
      ${Object.keys(subs).length ? `<div class="chips"><span class="chip sm ${!f.subtheme ? "on" : ""}" data-subtheme="">${t("All subthemes")}</span>${Object.keys(subs).sort().map((st) => `<span class="chip sm ${f.subtheme === st ? "on" : ""}" data-subtheme="${esc(st)}">${esc(st)} <span class="muted">${subs[st]}</span></span>`).join("")}</div>` : ""}
      ${Object.keys(conds).length > 1 ? `<div class="chips"><span class="chip sm ${!f.cond ? "on" : ""}" data-cond="">${t("Any condition")}</span>${Object.keys(conds).map((c) => `<span class="chip sm ${f.cond === c ? "on" : ""}" data-cond="${esc(c)}">${esc(t(c))} <span class="muted">${conds[c]}</span></span>`).join("")}</div>` : ""}`;
  }
  img(s, big = false) { return s.image ? `<img loading="lazy" src="${esc(s.image)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ph',textContent:'🧱'}))">` : `<span class="ph"${big ? ' style="font-size:60px"' : ""}>🧱</span>`; }
  badges(s) {
    return [
      s.is_all_time_low ? `<span class="badge">🔻 ${t("Lowest ever")}</span>` : "",
      s.discount_rrp != null && s.discount_rrp >= this.state.threshold && s.best_price != null ? `<span class="badge red">−${Math.round(s.discount_rrp)}%</span>` : "",
      s.target_hit ? `<span class="badge purple">🎯 ${t("Target price")}</span>` : "",
      s.retiring_soon ? `<span class="badge yellow">⏳ ${s.retires_in_days != null ? t("Retiring in {n} d", { n: s.retires_in_days }) : t("Retiring")}</span>` : s.retired ? `<span class="badge grey">${t("Retired")}</span>` : "",
      s.owned ? `<span class="badge blue">${t("Owned")}${s.collection && s.collection.qty > 1 ? ` ×${s.collection.qty}` : ""}</span>` : "",
    ].join("");
  }
  card(s, i = 0, mode = "deal") {
    const store = s.best_retailer ? this.state.data.retailers[s.best_retailer] : "";
    const trd = s.change_30d == null ? "" : `<span class="trend ${s.change_30d <= 0 ? "up" : "down"}">${s.change_30d <= 0 ? "▼" : "▲"} ${Math.abs(s.change_30d)}%</span>`;
    const stars = s.priority ? `<span class="stars">${"★".repeat(s.priority)}</span>` : "";
    if (mode === "coll") {
      const c = s.collection || {}, now = this.unitValue(s), g = c.paid && now ? ((now - c.paid) / c.paid) * 100 : null;
      return `<div class="card" tabindex="0" data-set="${esc(s.set_number)}" style="--i:${i}"><div class="badges">${c.condition ? `<span class="badge grey">${esc(t(c.condition))}</span>` : ""}${s.retiring_soon ? `<span class="badge yellow">⏳</span>` : ""}</div>
        <div class="img">${this.img(s)}</div><div class="n">${esc(s.name || "Set " + s.set_number)}</div>
        <div class="m">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.year ? ` · ${s.year}` : ""}${c.qty > 1 ? ` · ×${c.qty}` : ""}</div>
        <div class="row"><span class="p">${EUR(now)}</span>${g != null ? `<span class="trend ${g >= 0 ? "up" : "down"}">${signPct(Math.round(g))}</span>` : ""}</div>
        <div class="m">${c.paid ? t("paid {price}", { price: EUR(c.paid) }) : t("purchase price unknown")}${c.location ? ` · 📍 ${esc(c.location)}` : ""}</div></div>`;
    }
    return `<div class="card" tabindex="0" data-set="${esc(s.set_number)}" style="--i:${i}"><div class="badges">${this.badges(s)}</div>${s.best_price != null ? ring(s.deal_score, 40) : ""}
      <div class="img">${this.img(s)}</div><div class="n">${esc(s.name || "Set " + s.set_number)}</div>
      <div class="m">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.subtheme ? ` / ${esc(s.subtheme)}` : ""} ${stars}</div>
      <div class="row"><span class="p">${EUR(s.best_price)}</span>${s.rrp && s.best_price != null && s.best_price < s.rrp ? `<s>${EUR(s.rrp)}</s>` : ""}${trd}</div>
      ${s.offers_suspect ? `<div class="m" style="color:var(--lt-red)">⚠ ${t(s.offers_suspect > 1 ? "{n} suspicious links" : "{n} suspicious link", { n: s.offers_suspect })}</div>` : ""}
      <div class="m">${store ? t("at {shop}", { shop: esc(store) }) : s.offers_live === 0 && Object.keys(s.offers || {}).length ? "⚠ " + t("no price found") : Object.keys(s.offers || {}).length ? t("no price") : t("no shop links yet")}${s.price_per_piece ? ` · ${t("{n} ct/piece", { n: (s.price_per_piece * 100).toFixed(1) })}` : ""}${s.target_price ? ` · 🎯 ${EUR0(s.target_price)}` : ""}</div>
      ${spark(s.spark)}</div>`;
  }
  gridOf(list, mode) { return `<div class="grid">${list.map((s, i) => this.card(s, i, mode)).join("")}</div>`; }
  emptyState(icon, text, action = "") { return `<div class="empty"><span class="big">${icon}</span>${text}${action ? `<br><br>${action}` : ""}</div>`; }
  banner() {
    const h = this.state.data.health; if (!h || !h.errors) return "";
    const paused = Object.entries(h.paused_hours || {}).map(([k, v]) => `${esc(k)} (${dur(v * 3600)})`).join(", ");
    return `<div class="banner">⚠️ <span>${t(h.errors === 1 ? "{n} shop price could not be fetched" : "{n} shop prices could not be fetched", { n: h.errors })}${paused ? `; ${t("paused after a block: {shops}", { shops: paused })}` : ""}. <a data-goto="log/errors">${t("Show errors")}</a> · <a data-goto="manage/shops">${t("Shop status")}</a></span></div>`;
  }

  // ---------------------------------------------------------------- DEALS
  vToday() {
    const watched = this.sets.filter((s) => s.watched);
    if (!this.sets.length) return this.emptyState("🧱", t("No sets yet. Add sets to track their prices, or import your collection."), `<button class="btn" data-goto="manage/add">＋ ${t("Add a set")}</button> <button class="btn ghost" data-goto="manage/import">⇪ ${t("Import collection")}</button>`);
    const deals = this.sorted(watched.filter((s) => this.isDeal(s)), "score");
    const top = deals[0];
    const lows = watched.filter((s) => s.is_all_time_low).length, targets = watched.filter((s) => s.target_hit).length, retiring = watched.filter((s) => s.retiring_soon);
    const w = this.state.data.wishlist;
    const kpis = `<div class="kpis">${this.kpi(t("Deals now"), deals.length, { icon: "🏷️", color: "var(--lt-red)", sub: t("of {n} watched sets", { n: watched.length }) })}${this.kpi(t("Lowest price ever"), lows, { icon: "🔻", color: "var(--lt-green)" })}${this.kpi(t("Target price reached"), targets, { icon: "🎯", color: "var(--lt-purple)" })}${this.kpi(t("Retiring soon"), retiring.length, { icon: "⏳", color: "var(--lt-yellow)" })}${this.kpi(t("Watchlist now"), w.cost, { fmt: "eur", icon: "🛒", sub: w.saving > 0 ? `<span class="up">${t("{amount} below RRP", { amount: EUR0(w.saving) })}</span>` : "" })}</div>`;
    const hero = top ? `<div class="hero" data-set="${esc(top.set_number)}"><div class="img">${this.img(top, true)}</div><div><div class="k">⭐ ${t("Deal of the day")}</div><h2>${esc(top.name || top.set_number)}</h2><div class="m muted">${esc(top.set_number)} · ${esc(top.theme || "")}</div>
      <div style="display:flex;align-items:baseline;gap:10px;margin:8px 0"><span class="p">${EUR(top.best_price)}</span>${top.rrp ? `<s class="muted">${EUR(top.rrp)}</s>` : ""}${top.discount_rrp > 0 ? `<span class="badge red">−${Math.round(top.discount_rrp)}%</span>` : ""}</div>
      <div class="muted" style="font-size:13px">${t("at {shop}", { shop: esc(this.state.data.retailers[top.best_retailer] || "") })}${top.is_all_time_low ? " · " + t("lowest price ever") : ""}${top.target_hit ? " · " + t("below your target price") : ""}</div>
      ${top.best_url ? `<a class="btn sm" style="margin-top:12px" href="${esc(top.best_url)}" target="_blank" rel="noopener noreferrer" data-stop>${t("Go to the shop")} ↗</a>` : ""}</div>${ring(top.deal_score, 64)}</div>` : "";
    const drops = this.sorted(watched.filter((s) => !this.isDeal(s) && s.change_30d != null && s.change_30d <= -8), "drop").slice(0, 8);
    const main = `${hero}${deals.length > 1 ? `<h2 class="sec">🏷️ ${t("All deals")} <span class="n">${deals.length}</span></h2>${this.gridOf(deals.slice(1))}` : ""}
      ${!deals.length ? this.emptyState("😴", t("No deals today among your {n} watched sets. Tip: lower the discount threshold or set target prices.", { n: watched.length })) : ""}
      ${retiring.length ? `<h2 class="sec">⏳ ${t("Retiring soon")} <span class="n">${t("buy while you can?")}</span></h2>${this.gridOf(this.sorted(retiring, "score"))}` : ""}
      ${drops.length ? `<h2 class="sec">📉 ${t("Big price drops")} <span class="n">${t("last 30 days")}</span></h2>${this.gridOf(drops)}` : ""}`;
    return `${this.banner()}${kpis}<div class="cols"><div>${main}</div><aside>${this.timeline()}${this.thresholdPanel()}</aside></div>`;
  }
  thresholdPanel() {
    return `<div class="panel"><h3>🎚️ ${t("When is it a deal?")}</h3><p>${t("Discount vs. RRP of at least {pct}, or lowest price ever, target price reached, or deal score ≥ 70.", { pct: `<b id="thrv">${this.state.threshold}%</b>` })}</p><input id="thr" type="range" min="5" max="70" step="5" value="${this.state.threshold}" style="width:100%"><p style="margin-top:8px;font-size:12px">${t("The default threshold is set under Manage → Settings (now {pct}%).", { pct: this.state.data.threshold })}</p></div>`;
  }
  timeline() {
    const ev = this.state.data.events || [];
    const ic = { is_all_time_low: ["🔻", "lowest price ever"], high_discount: ["🏷️", "high discount"], target_hit: ["🎯", "target price reached"] };
    return `<div class="panel"><h3>🕒 ${t("Recent deals")}</h3>${ev.length ? `<ul class="tl">${ev.slice(0, 12).map((e) => { const [i, lbl] = ic[e.kind] || ["•", e.kind]; return `<li data-set="${esc(e.set_number)}"><span class="ic">${i}</span><div><b>${esc(e.name || e.set_number)}</b><div>${t(lbl)} · ${EUR(e.price)}${e.retailer ? ` · ${esc(this.state.data.retailers[e.retailer] || e.retailer)}` : ""}</div><div class="t">${DATE(e.ts, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</div></div></li>`; }).join("")}</ul>` : `<p>${t("Nothing reported yet. New lowest prices, high discounts and reached target prices appear here.")}</p>`}</div>`;
  }
  vWatch() {
    const list = this.sets.filter((s) => s.watched);
    if (!list.length) return this.emptyState("👀", t("Your watchlist is empty. Add sets you don't own yet; you get a heads-up when the price is right."), `<button class="btn" data-goto="manage/add">＋ ${t("Add a set")}</button>`);
    const w = this.state.data.wishlist;
    return `${this.banner()}<div class="kpis">${this.kpi(t("Sets on watchlist"), w.sets, { icon: "👀" })}${this.kpi(t("Total now"), w.cost, { fmt: "eur", icon: "🛒", sub: t("{n} with a price", { n: w.priced }) })}${this.kpi(t("RRP"), w.rrp, { fmt: "eur", icon: "🏷️" })}${this.kpi(t("Saving"), w.saving, { fmt: "eur", icon: "💰", color: "var(--lt-green)" })}</div>
      ${this.filterBar({ list, sorts: [["priority", "Priority"], ["score", "Deal score"], ["discount", "Highest discount"], ["price", "Lowest price"], ["ppp", "Price per piece"], ["drop", "Biggest drop"], ["name", "Name"]] })}
      <div id="results" data-fn="rWatch">${this.rWatch()}</div>`;
  }
  rWatch() { const l = this.sorted(this.filtered(this.sets.filter((s) => s.watched)), this.state.f.sort); return l.length ? this.gridOf(l) : this.emptyState("🔍", t("Nothing found with these filters.")); }
  vAll() {
    return `${this.banner()}${this.filterBar({ list: this.sets, threshold: true, sorts: [["score", "Deal score"], ["discount", "Highest discount"], ["price", "Lowest price"], ["ppp", "Price per piece"], ["drop", "Biggest drop"], ["name", "Name"], ["number", "Set number"]] })}<div id="results" data-fn="rAll">${this.rAll()}</div>`;
  }
  rAll() { const l = this.sorted(this.filtered(this.sets), this.state.f.sort); return l.length ? this.gridOf(l) : this.emptyState("🔍", t("Nothing found with these filters.")); }

  // ---------------------------------------------------------------- COLLECTION
  vCollOverview() {
    const c = this.state.coll, sum = c.summary, a = this.state.data.analytics || {};
    if (!sum.sets) return this.emptyState("📦", t("Your collection is still empty. Import a CSV (BrickEconomy, Brickset, Rebrickable or your own sheet) or mark sets as ‘owned’."), `<button class="btn" data-goto="manage/import">⇪ ${t("Import collection")}</button>`);
    const growth = sum.cost ? sum.growth_pct : null;
    const kpis = `<div class="kpis">${this.kpi(t("Collection value"), sum.value, { fmt: "eur", icon: "💎", color: "var(--lt-accent)" })}${this.kpi(t("Purchase cost"), sum.cost, { fmt: "eur", icon: "🧾" })}${this.kpi(t("Growth"), growth, { fmt: "pct", icon: "📈", color: growth >= 0 ? "var(--lt-green)" : "var(--lt-red)", sub: sum.cost ? `<span class="${sum.growth >= 0 ? "up" : "down"}">${sum.growth >= 0 ? "+" : ""}${EUR0(sum.growth)}</span>` : "" })}${this.kpi(t("Sets"), sum.sets, { icon: "🧱", sub: t("{n} pieces", { n: INT(sum.pieces) }) })}${this.kpi(t("Paid per piece"), a.avg_paid_per_piece != null ? a.avg_paid_per_piece * 100 : null, { icon: "🔢", sub: t("cents, average") })}</div>`;
    const ser = c.series || [];
    const chart = lineChart([{ name: t("Value"), color: COLORS[1], points: ser.map((p) => [p.ts, p.value]) }, { name: t("Purchase cost"), color: COLORS[0], points: ser.map((p) => [p.ts, p.cost]), dashed: true }]);
    const themes = Object.entries(a.by_theme || {}); const topT = themes.slice(0, 7), rest = themes.slice(7).reduce((x, [, v]) => x + v.value, 0);
    const donutItems = topT.map(([k, v]) => ({ label: `${k} (${v.count})`, value: v.value })).concat(rest ? [{ label: t("Other"), value: rest, color: "#9aa0a6" }] : []);
    const years = Object.entries(a.by_year || {}).map(([k, v]) => ({ label: k, value: v.count }));
    const mv = (list, cls) => list.length ? `<table class="tbl">${list.map((m) => `<tr class="click" data-set="${esc(m.set_number)}"><td>${esc(m.name || m.set_number)}<div class="muted" style="font-size:12px">${EUR(m.paid)} → ${EUR(m.value)}</div></td><td class="num ${cls}"><b>${signPct(m.pct)}</b></td></tr>`).join("")}</table>` : `<p>${t("No data yet (purchase price needed).")}</p>`;
    const conds = Object.entries(a.by_condition || {});
    return `${kpis}<div class="panel"><h3>📈 ${t("Collection growth")}<span class="hsp"></span><span class="muted" style="font-size:12px;font-weight:400">${t("value = lowest new price × quantity")}</span></h3>${chart}</div>
      <div class="two"><div class="panel"><h3>🎨 ${t("Value per theme")}</h3>${donut(donutItems, { label: t("total") })}</div><div class="panel"><h3>📅 ${t("Sets per release year")}</h3>${years.length > 1 ? bars(years) : `<p>${t("Year unknown for most sets; add a Brickset or Rebrickable key under Settings to fill this in.")}</p>`}</div></div>
      <div class="two"><div class="panel"><h3>🚀 ${t("Biggest risers")}</h3>${mv(a.top_gainers || [], "up")}</div><div class="panel"><h3>📉 ${t("Below purchase price")}</h3>${mv(a.top_losers || [], "down")}</div></div>
      ${conds.length ? `<div class="panel"><h3>📦 ${t("Condition of your sets")}</h3><div class="chips">${conds.map(([k, v]) => `<span class="chip">${esc(t(k))} <b>${v}</b></span>`).join("")}</div></div>` : ""}`;
  }
  vCollSets() {
    const list = this.sets.filter((s) => s.owned);
    if (!list.length) return this.emptyState("📦", t("No sets in your collection yet."), `<button class="btn" data-goto="manage/import">⇪ ${t("Import")}</button> <button class="btn ghost" data-goto="manage/add">＋ ${t("Add")}</button>`);
    return `${this.filterBar({ list, cond: true, viewToggle: true, sorts: [["value", "Highest value"], ["gain", "Biggest gain"], ["name", "Name"], ["number", "Set number"], ["year", "Year"]] })}
      <div class="fbar" style="justify-content:flex-end"><button class="btn ghost sm" id="expcsv">⬇ ${t("Export CSV")}</button></div><div id="results" data-fn="rColl">${this.rColl()}</div>`;
  }
  collSort(list) {
    const v = (s) => { const c = s.collection || {}; return (this.unitValue(s) ?? 0) * (c.qty || 1); };
    const g = (s) => { const c = s.collection || {}, now = this.unitValue(s); return c.paid && now ? (now - c.paid) / c.paid : -1e9; };
    const key = { value: (s) => -v(s), gain: (s) => -g(s), name: (s) => (s.name || "").toLowerCase(), number: (s) => +s.set_number, year: (s) => -(s.year || 0), score: (s) => -v(s) }[this.state.f.sort] || ((s) => -v(s));
    return [...list].sort((a, b) => { const x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
  }
  rColl() {
    const list = this.collSort(this.filtered(this.sets.filter((s) => s.owned)));
    if (!list.length) return this.emptyState("🔍", t("Nothing found with these filters."));
    if (this.state.cview === "grid") return this.gridOf(list, "coll");
    const rows = list.map((s) => {
      const c = s.collection || {}, now = this.unitValue(s), g = c.paid && now ? ((now - c.paid) / c.paid) * 100 : null;
      return `<tr class="click" data-set="${esc(s.set_number)}"><td>${esc(s.set_number)}</td><td>${esc(s.name || "")}</td><td>${esc(s.theme || "")}</td><td>${s.year || ""}</td><td class="num">${c.qty || 1}</td><td class="num">${EUR(c.paid)}</td><td class="num">${EUR(c.current_value)}</td><td class="num">${s.best_price != null ? EUR(s.best_price) : "–"}</td><td class="num"><b>${EUR(now)}</b></td><td class="num ${g == null ? "" : g >= 0 ? "up" : "down"}">${g == null ? "–" : signPct(Math.round(g))}</td><td>${esc(c.condition ? t(c.condition) : "")}</td><td>${esc(c.location || "")}</td></tr>`;
    }).join("");
    return `<div class="panel tscroll"><table class="tbl"><tr><th>#</th><th>${t("Name")}</th><th>${t("Theme")}</th><th>${t("Year")}</th><th class="num">${t("Qty")}</th><th class="num">${t("Paid")}</th><th class="num" title="${t("Current value from your import (e.g. BrickEconomy)")}">${t("Value (import)")}</th><th class="num" title="${t("Cheapest shop price now")}">${t("Shop now")}</th><th class="num" title="${t("Used for the collection value")}">${t("Value")}</th><th class="num">+/−</th><th>${t("Condition")}</th><th>${t("Location")}</th></tr>${rows}</table>
      <p class="muted" style="font-size:12px;margin-top:8px">${this.state.data.value_source === "import_first" ? t("“Value” uses the imported value first, else the shop price") : t("“Value” uses the lowest shop price first, else the imported value")} (${t("configurable under Manage → Settings")}). ${t("Import your CSV again to refresh the values.")}</p></div>`;
  }

  // ---------------------------------------------------------------- MANAGE
  vAdd() {
    const m = this.state.addMode;
    const themes = this.state.data.themes.map((th) => `<option value="${esc(th)}">`).join("");
    return `<div class="panel"><h3>＋ ${t("Add a set")}</h3>
      <div class="radio"><label class="${m === "watch" ? "on" : ""}"><input type="radio" name="mode" value="watch" ${m === "watch" ? "checked" : ""}><b>👀 ${t("Keep an eye on it")}</b><span>${t("Track prices and get notified of a deal")}</span></label>
      <label class="${m === "own" ? "on" : ""}"><input type="radio" name="mode" value="own" ${m === "own" ? "checked" : ""}><b>📦 ${t("Add to my collection")}</b><span>${t("I already own this set")}</span></label></div>
      <div class="form"><label>${t("Set number")} *<input id="a_num" inputmode="numeric" placeholder="${t("e.g. {example}", { example: "10281" })}" autocomplete="off"><div class="hint" id="a_hint"></div></label><label>${t("Name")}<input id="a_name" placeholder="${t("optional, filled in automatically")}"></label>
      <label>${t("Theme")}<input id="a_theme" list="themes" placeholder="Botanicals, Technic, Icons…"></label><label>${t("Subtheme")}<input id="a_sub"></label><label>${t("RRP")} (€)<input id="a_rrp" type="number" min="0" step="0.01"></label><label>${t("Pieces")}<input id="a_pcs" type="number" min="0"></label></div>
      ${m === "watch" ? `<div class="form"><label>${t("Target price (€) – notify when the price drops below it")}<input id="a_target" type="number" min="0" step="0.01"></label><label>${t("Priority")}<select id="a_prio"><option value="0">–</option><option value="1">★</option><option value="2">★★</option><option value="3">★★★</option></select></label></div>`
        : `<div class="form"><label>${t("Quantity")}<input id="a_qty" type="number" value="1" min="1"></label><label>${t("Paid (€ each)")}<input id="a_paid" type="number" min="0" step="0.01"></label><label>${t("Purchase date")}<input id="a_date" type="date" max="${new Date().toISOString().slice(0, 10)}"></label><label>${t("Condition")}<select id="a_cond"><option value="">–</option>${CONDITIONS.map((c) => `<option value="${c}">${t(c)}</option>`).join("")}</select></label><label>${t("Location")}<input id="a_loc" placeholder="${t("e.g. {example}", { example: t("attic, cupboard 2") })}"></label></div>`}
      <datalist id="themes">${themes}</datalist><button class="btn" id="add">＋ ${t("Add & find shops")}</button> <span class="muted" style="font-size:12px">${t("Shop links are searched automatically; this takes a moment.")}</span></div>
      <div class="panel"><h3>⚡ ${t("Several sets at once")}</h3><p>${t("Paste set numbers, separated by spaces, commas or new lines.")}</p><textarea id="bulk" rows="3" style="width:100%" placeholder="10281 10311 42143"></textarea><div class="hint" id="bulk_hint"></div>
      <label class="chk" style="display:flex;gap:8px;align-items:center;margin:6px 0 10px"><input type="checkbox" id="bulk_owned"> ${t("mark all as owned")}</label><button class="btn" id="bulkadd">${t("Add")}</button> <button class="btn ghost" id="discover">🔎 ${t("Find shops for sets without a link")}</button></div>
      <div class="panel"><h3>🔗 ${t("Link a shop page")}</h3><p>${t("When the automatic search found the wrong page or nothing at all. Tip: you can also click any set and edit its links there.")}</p><div class="form"><label>${t("Set number")}<input id="o_num" list="nums"></label><label>${t("Shop")}<select id="o_ret">${Object.entries(this.state.data.retailers).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label><label>${t("Product URL or ASIN")}<input id="o_url" placeholder="https://… / B0…"></label></div>
      <datalist id="nums">${this.sets.map((s) => `<option value="${esc(s.set_number)}">${esc(s.name || "")}</option>`).join("")}</datalist><button class="btn" id="offer">${t("Link")}</button></div>`;
  }
  vImport() {
    const imp = this.state.imp, a = imp.analysis;
    const steps = `<div class="steps"><span class="${!a ? "on" : ""}">1 · ${t("Choose file")}</span><span class="${a ? "on" : ""}">2 · ${t("Check")}</span><span>3 · ${t("Import")}</span></div>`;
    if (!a) return `${steps}<div class="panel"><label class="drop" id="drop"><span class="big">📄</span><b>${t("Drop your CSV here")}</b> ${t("or click to choose")}<br><span class="muted" style="font-size:13px">${t("BrickEconomy, Brickset, Rebrickable or your own spreadsheet (comma, semicolon or tab)")}</span><input type="file" id="csvfile" accept=".csv,.tsv,.txt,text/csv,text/plain" hidden></label>
      <p style="margin-top:14px">${t("…or paste the contents:")}</p><textarea id="csvtext" rows="6" style="width:100%" placeholder="Number;Name;Theme;Qty;Paid;Purchase Date&#10;10281;Bonsai Tree;Botanicals;1;39,99;24/12/2023">${esc(imp.text)}</textarea><br><br><button class="btn" id="analyze">${t("Check")} →</button>
      <p style="margin-top:14px;font-size:12px">${t("Recognised columns: set number, name, theme, subtheme, year, pieces, RRP, quantity, paid, value, purchase date, condition, location, notes (in several languages). Nothing is saved before step 3.")}<br><b>${t("Updating your collection?")}</b> ${t("Just import your new export again: existing sets are updated (quantity, paid, current value, condition…), new sets are added, and afterwards the data and prices are filled in per set.")}</p></div>`;
    if (a.fatal && !a.rows.length) return `${steps}<div class="panel"><h3 class="err">✕ ${t("This file cannot be imported")}</h3><p>${esc(tx(a.fatal))}</p><button class="btn ghost" id="impback">← ${t("Other file")}</button></div>`;
    const sm = a.summary, n = sm.ok + sm.warning;
    const rows = a.rows.filter((r) => !imp.only || r.status !== "ok").slice(0, 500);
    const tbl = rows.map((r) => `<tr><td class="muted">${r.line}</td><td><span class="st ${r.status}">${r.status === "ok" ? "✓" : r.status === "warning" ? "!" : "✕"}</span></td><td><b>${esc(r.set_number || r.raw_number)}</b></td><td>${esc(r.name || "")}</td><td class="num">${r.qty ?? ""}</td><td class="num">${r.paid != null ? EUR(r.paid) : ""}</td><td>${esc(r.added || "")}</td><td>${esc(r.condition ? t(r.condition) : "")}</td><td class="iss">${r.issues.map((i) => `<div class="${i.level}">${esc(tx(i.text))}</div>`).join("")}</td></tr>`).join("");
    return `${steps}<div class="panel"><h3>🔎 ${t("Check of {name}", { name: esc(imp.name || t("pasted data")) })}<span class="hsp"></span><button class="btn ghost sm" id="impback">← ${t("Other file")}</button></h3>
      ${a.fatal ? `<div class="banner">⚠️ ${esc(tx(a.fatal))}</div>` : ""}
      <div class="sumpills"><span class="ok">✓ ${t("{n} OK", { n: sm.ok })}</span><span style="color:#9a6a00">! ${t("{n} with a warning", { n: sm.warning })}</span><span class="err">✕ ${t("{n} will be skipped", { n: sm.error })}</span><span>＋ ${t("{n} new", { n: sm.new })}</span><span>↻ ${t("{n} updated", { n: sm.update })}</span>${sm.merged ? `<span>⧉ ${t("{n} merged", { n: sm.merged })}</span>` : ""}</div>
      <p><b>${t("Recognised columns:")}</b> ${Object.entries(a.columns).map(([k, v]) => `<span class="chip sm">${esc(k)} → ${esc(t(v))}</span>`).join(" ")}${a.ignored_columns.length ? `<br><span class="muted">${t("Ignored: {columns}", { columns: a.ignored_columns.map(esc).join(", ") })}</span>` : ""}</p>
      <div class="fbar"><label class="chk" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="imp_only" ${imp.only ? "checked" : ""}> ${t("only lines with remarks")}</label>
      <label class="chk" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="imp_replace" ${imp.replace ? "checked" : ""}> ${t("replace current collection")}</label>
      <label class="chk" style="display:flex;gap:6px;align-items:center" title="${t("Per set: fill in data, find missing shop links and fetch prices")}"><input type="checkbox" id="imp_update" ${imp.update ? "checked" : ""}> ${t("update afterwards (data, links, prices)")}</label><span class="hsp"></span>
      <button class="btn" id="impgo" ${n ? "" : "disabled"}>${t(n === 1 ? "Import {n} line" : "Import {n} lines", { n })} →</button></div>
      <div class="tscroll"><table class="tbl"><tr><th>${t("Line")}</th><th></th><th>Set</th><th>${t("Name")}</th><th class="num">${t("Qty")}</th><th class="num">${t("Paid")}</th><th>${t("Date")}</th><th>${t("Condition")}</th><th>${t("Remarks")}</th></tr>${tbl}</table></div>
      ${a.rows.length > 500 ? `<p class="muted">${t("First {n} lines shown.", { n: 500 })}</p>` : ""}</div>`;
  }
  // ---------------------------------------------------------------- errors tab
  errType(o) {
    const e = (o.error || "").toLowerCase();
    if (o.link_status === "suspect") return ["suspect", t("Suspicious link")];
    if (e.startsWith("paused")) return ["paused", t("Shop paused")];
    if (e.includes("blocked")) return ["blocked", t("Blocked by the shop")];
    if (e.includes("suspicious price")) return ["price", t("Suspicious price")];
    if (e.includes("price not found")) return ["noprice", t("Price not found")];
    if (e.includes("404") || e.includes("not found")) return ["gone", t("Page no longer exists")];
    return ["other", t("Other error")];
  }
  errorRows() {
    const out = [], E = this.state.errs;
    for (const s of this.sets) {
      const offers = Object.entries(s.offers || {});
      if (!offers.length) out.push({ s, rid: null, o: null, type: "nolinks", label: t("No shop link at all"), msg: t("No shop is linked to this set yet.") });
      for (const [rid, o] of offers) {
        if (!(o.error || o.link_status === "suspect")) continue;
        if (o.ignored && !E.showIgnored) continue;
        const [type, label] = this.errType(o);
        out.push({ s, rid, o, type, label, msg: tx(o.link_status === "suspect" ? o.link_reason : o.error) });
      }
    }
    return out;
  }
  vErrors() {
    const E = this.state.errs, all = this.errorRows(), retailers = this.state.data.retailers;
    const types = {}; all.forEach((r) => { types[r.type] = types[r.type] || [r.label, 0]; types[r.type][1]++; });
    let rows = all.filter((r) => (E.type === "all" || r.type === E.type) && (E.scope === "all" || (E.scope === "owned") === !!r.s.owned) && (!E.shop || r.rid === E.shop));
    rows.sort((a, b) => (b.s.owned - a.s.owned) || a.s.set_number.localeCompare(b.s.set_number));
    const trs = rows.slice(0, 300).map((r) => {
      const key = `${r.s.set_number}|${r.rid || ""}`, open = E.open === key;
      const head = `<tr class="click erow${open ? " on" : ""}" data-ekey="${esc(key)}"><td style="width:28px">${open ? "▾" : "▸"}</td><td><a class="setlink" data-set="${esc(r.s.set_number)}"><b>${esc(r.s.set_number)}</b> ${esc(r.s.name || "")}</a><div class="muted" style="font-size:12px">${esc(r.s.theme || "")}${r.s.owned ? " · 📦 " + t("owned") : ""}</div></td>
        <td>${r.rid ? esc(retailers[r.rid] || r.rid) : "–"}</td><td><span class="lk ${r.type === "suspect" || r.type === "price" ? "suspect" : "unknown"}">${esc(r.label)}</span>${r.o && r.o.ignored ? ` <span class="muted" style="font-size:11px">(${t("ignored")})</span>` : ""}<div class="err" style="font-size:12px;margin-top:2px">${esc(r.msg || "")}</div></td>
        <td class="num">${r.o && r.o.price != null ? EUR(r.o.price) : "–"}</td></tr>`;
      if (!open) return head;
      return head + `<tr class="efix"><td></td><td colspan="4">${this.fixBoxHtml(r.s, r.rid, r.o)}</td></tr>`;
    }).join("");
    const chip = (k, l, n) => `<span class="chip ${E.type === k ? "on" : ""}" data-etype="${k}">${esc(l)}${n != null ? ` <span class="muted">${n}</span>` : ""}</span>`;
    return `<div class="panel"><h3>⚠️ ${t("Open errors")} <span class="muted" style="font-weight:400">${all.length}</span><span class="hsp"></span><label class="chk" style="font-size:13px;font-weight:400;display:flex;gap:6px;align-items:center"><input type="checkbox" id="e_ign" ${E.showIgnored ? "checked" : ""}> ${t("show ignored")}</label></h3>
      <p>${t("Click an error to fix it right away: enter the correct product page and/or the price. Anything you enter by hand wins over automatic values.")} ${t("Does a shop keep blocking? See the alternatives under {link}.", { link: `<a data-goto="manage/shops" style="cursor:pointer">${t("Shops & jobs")}</a>` })}</p>
      <div class="chips">${chip("all", t("All"), all.length)}${Object.entries(types).map(([k, [l, n]]) => chip(k, l, n)).join("")}</div>
      <div class="fbar"><select id="e_scope"><option value="all">${t("All sets")}</option><option value="owned" ${E.scope === "owned" ? "selected" : ""}>📦 ${t("My collection")}</option><option value="watch" ${E.scope === "watch" ? "selected" : ""}>👀 ${t("Watchlist")}</option></select>
      <select id="e_shop"><option value="">${t("All shops")}</option>${Object.entries(retailers).map(([k, v]) => `<option value="${k}" ${E.shop === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div></div>
      ${rows.length ? `<div class="panel tscroll"><table class="tbl"><tr><th></th><th>Set</th><th>${t("Shop")}</th><th>${t("Error")}</th><th class="num">${t("Last price")}</th></tr>${trs}</table>${rows.length > 300 ? `<p class="muted">${t("First {n} of {total}.", { n: 300, total: rows.length })}</p>` : ""}</div>` : this.emptyState("✅", t("No errors in this selection."))}`;
  }
  // ---------------------------------------------------------------- logbook
  logQuery() {
    const L = this.state.logv, checks = this.state.sub.log === "checks";
    return { type: "lego_tracker/log", level: L.level, kind: checks ? "check" : L.kind, retailer: L.retailer, source: L.source, status: L.status, set_number: L.set, q: L.q, limit: 100 };
  }
  async loadLog(more = false) {
    const L = this.state.logv, q = this.logQuery();
    if (more && L.data && L.data.entries.length) q.before = L.data.entries[L.data.entries.length - 1].ts;
    try { const r = await this._hass.callWS(q); if (more) { r.entries = L.data.entries.concat(r.entries); } L.data = r; L.err = null; } catch (e) { L.err = e.message; }
    if (this.state.section === "log" && this.state.sub.log !== "errors") this.renderLogList();
  }
  renderLogList() {
    const el = this.shadowRoot.getElementById("loglist"); if (!el) return this.renderContent();
    el.innerHTML = this.logListHtml(); this.bindLogList(el);
  }
  scheduleLine() {
    const sc = (this.state.data.schedule || {}), set = sc.next_set && this.sets.find((x) => x.set_number === sc.next_set);
    if (sc.mode === "spread") {
      return `${t("Spread checks: every set once every {h} h, about {n} sets per hour.", { h: (sc.cycle_hours ?? 24).toLocaleString(LOC), n: (sc.per_hour ?? 0).toLocaleString(LOC, { maximumFractionDigits: 1 }) })} ${sc.next ? t("Next check {time}", { time: TIME(sc.next) }) + (set ? `: <a data-set="${esc(set.set_number)}" class="setlink">${esc(set.set_number)} ${esc(set.name || "")}</a>` : "") + "." : ""}`;
    }
    if (sc.mode === "times") return t("Automatic refresh at {times}.", { times: (sc.times || []).join(", ") });
    return t("Automatic checks are off.");
  }
  vLog() {
    const L = this.state.logv, checks = this.state.sub.log === "checks", sc = this.state.data.schedule || {};
    if (!L.data && !L.err) this.loadLog();
    const d = L.data, f = d ? d.facets : { kind: {}, retailer: {}, source: {} }, kinds = d ? d.kinds : {};
    const srcLabel = { server: "Server", schedule: "Schedule (automatic)", panel: "Panel (you)", userscript: "Tampermonkey userscript", import: "Import" };
    const opt = (list, cur, all) => `<option value="">${all}</option>` + list.map(([v, l, n]) => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(l)}${n != null ? ` (${n})` : ""}</option>`).join("");
    const st = (v, l, cls) => `<span class="chip ${L.status === v ? "on" : ""} ${cls}" data-lstat="${v}">${l}</span>`;
    const errs = this.errorRows().length;
    const kpis = `<div class="kpis">${this.kpi(t("Checked in the last 24 h"), sc.checked_24h ?? null, { icon: "🔄", sub: sc.total != null ? t("of {n} sets with shop links", { n: sc.total }) : "" })}${this.kpi(t("Pace"), sc.mode === "spread" ? sc.per_hour ?? null : null, { fmt: "dec", icon: "⏱️", sub: sc.mode === "spread" ? t("sets per hour") : t("spread checks are off") })}${this.kpi(t("Open errors"), errs, { icon: "⚠️", color: errs ? "var(--lt-red)" : "var(--lt-green)", sub: `<a data-goto="log/errors" style="cursor:pointer">${t("Show and fix")} →</a>` })}</div>`;
    const filtered = L.level || (!checks && L.kind) || L.retailer || L.source || L.status || L.set || L.q;
    return `${kpis}<div class="panel"><h3>${checks ? "🔄 " + t("Shop checks") : "📜 " + t("Logbook")}<span class="hsp"></span><button class="btn ghost sm" id="lg_reload">↻ ${t("Refresh")}</button></h3>
      <p>${checks ? t("One line per set check with a result per shop: green = price fetched, red = failed, grey = skipped (shop paused).") : t("Everything the integration does: price changes, shop checks (what works and what doesn't), links found or rejected, prices from Tampermonkey, imports, jobs, notifications and your own actions. Identical repeated messages are merged (×count). Click a line for details and to fix it.")} <span class="muted">${this.scheduleLine()}</span></p>
      <div class="chips">${st("", t("All"), "")}${st("ok", "✓ " + t("Succeeded"), "okc")}${st("fail", "✕ " + t("Failed"), "failc")}</div>
      <div class="fbar"><select id="lg_level" aria-label="${t("Level")}">${opt([["problems", "⚠️ " + t("Errors & warnings")], ["events", "✅ " + t("Events")], ["error", "⛔ " + t("Errors only")]], L.level, t("All levels"))}</select>
      ${checks ? "" : `<select id="lg_kind" aria-label="${t("Kind")}">${opt(Object.entries(kinds).map(([k, l]) => [k, t(l), f.kind[k] || 0]), L.kind, t("All kinds"))}</select>`}
      <select id="lg_shop" aria-label="${t("Shop")}">${opt(Object.entries(this.state.data.retailers).map(([k, l]) => [k, l, f.retailer[k] || 0]), L.retailer, t("All shops"))}</select>
      <select id="lg_src" aria-label="${t("Source")}">${opt(Object.keys(f.source).map((k) => [k, srcLabel[k] ? t(srcLabel[k]) : k, f.source[k]]), L.source, t("All sources"))}</select>
      <input id="lg_set" placeholder="${t("set number")}" value="${esc(L.set)}" style="width:140px" inputmode="numeric" list="lg_nums"><datalist id="lg_nums">${this.sets.map((s) => `<option value="${esc(s.set_number)}">${esc(s.name || "")}</option>`).join("")}</datalist>
      <div class="search" style="min-width:180px"><input id="lg_q" placeholder="${t("Search message or link…")}" value="${esc(L.q)}"></div>
      ${filtered ? `<button class="btn ghost sm" id="lg_clear">✕ ${t("Clear filters")}</button>` : ""}</div></div>
      <div id="loglist">${this.logListHtml()}</div>`;
  }
  shopResults(e, full = false) {
    const retailers = this.state.data.retailers;
    return Object.entries(e.results || {}).map(([rid, r]) => {
      const cls = r.ok ? "ok" : r.ok === false ? "fail" : "skip", name = esc(retailers[rid] || rid);
      const val = r.ok ? `${r.price != null ? EUR(r.price) : "✓"}${r.manual ? " ✎" : ""}` : r.ok === false ? "✕" : "⏸";
      const why = r.error ? tx(r.error) : r.manual ? t("manual price wins; the shop said {price}", { price: EUR(r.price) }) : "";
      return full ? `<div class="res ${cls}" style="display:flex;gap:8px;align-items:center;margin:3px 0"><b style="min-width:110px">${name}</b><span>${val}</span><span class="muted" style="font-size:12px">${esc(why)}</span></div>`
        : `<span class="res ${cls}" title="${esc(why)}">${name} <b>${val}</b></span>`;
    }).join(full ? "" : " ");
  }
  logListHtml() {
    const L = this.state.logv;
    if (L.err) return this.emptyState("⚠️", esc(L.err));
    if (!L.data) return `<div class="skel" style="height:200px"></div>`;
    const d = L.data, retailers = this.state.data.retailers;
    if (!d.entries.length) return this.emptyState("📭", t("No log lines with these filters."));
    const icon = { error: "⛔", warning: "⚠️", ok: "✅", info: "ℹ️" };
    const rows = d.entries.map((e) => {
      const s = e.set_number ? this.sets.find((x) => x.set_number === e.set_number) : null, open = L.open === e.id;
      const setCell = e.set_number ? `<a class="setlink" data-set="${esc(e.set_number)}" title="${t("Open set")}"><b>${esc(e.set_number)}</b> <span class="muted">${esc(s ? (s.name || "") : "")}</span></a>` : "";
      const msg = e.kind === "check" && e.results ? `<div class="resrow">${this.shopResults(e)}</div>` : `${esc(tx(e.message))}`;
      const head = `<tr class="click lrow lv-${esc(e.level)}${open ? " on" : ""}" data-lid="${esc(e.id)}"><td style="white-space:nowrap" class="muted">${DATE(e.ts, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
        <td>${icon[e.level] || ""}</td><td><span class="tag">${esc(t(d.kinds[e.kind] || e.kind))}</span></td>
        <td>${setCell}</td><td>${e.retailer ? esc(retailers[e.retailer] || e.retailer) : ""}</td>
        <td class="${e.level === "error" ? "err" : ""}">${msg}${e.count > 1 ? ` <span class="tag">×${e.count}</span>` : ""}</td></tr>`;
      if (!open) return head;
      const det = `<div class="kv"><span>${t("Time")}</span><b>${DATE(e.ts, { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</b></div>
        <div class="kv"><span>${t("Message")}</span><b>${esc(tx(e.message))}</b></div>
        <div class="kv"><span>${t("Source")}</span><b>${esc(e.source || "")}</b></div>${e.url ? `<div class="kv"><span>${t("Link")}</span><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.url.slice(0, 80))} ↗</a></div>` : ""}
        ${e.price != null ? `<div class="kv"><span>${t("Price")}</span><b>${e.old_price != null ? EUR(e.old_price) + " → " : ""}${EUR(e.price)}</b></div>` : ""}
        ${e.results ? `<h4 style="margin:10px 0 4px">${t("Result per shop")}</h4>${this.shopResults(e, true)}` : ""}`;
      const failed = e.results ? Object.entries(e.results).find(([, r]) => r.ok === false) : null;
      const rid = e.retailer || (failed ? failed[0] : null);
      const fixable = s && rid && (e.level === "error" || e.level === "warning") && ["fetch", "link", "userscript", "discover", "price", "check"].includes(e.kind);
      const fix = fixable ? `<h4 style="margin:10px 0 6px">${t("Fix it")}</h4>${this.fixBoxHtml(s, rid, (s.offers || {})[rid])}` : s ? `<button class="btn ghost sm" data-set="${esc(s.set_number)}" style="margin-top:8px">${t("Open set")}</button>` : "";
      return head + `<tr><td colspan="6"><div class="fixbox" style="background:var(--lt-card);border:1px solid var(--lt-line)">${det}${fix}</div></td></tr>`;
    }).join("");
    return `<div class="panel tscroll"><div class="muted" style="font-size:12px;margin-bottom:6px">${t(d.total === 1 ? "{n} line" : "{n} lines", { n: d.total })}</div><table class="tbl log"><tr><th>${t("Time")}</th><th></th><th>${t("Kind")}</th><th>Set</th><th>${t("Shop")}</th><th>${t("Message")}</th></tr>${rows}</table>
      ${d.more ? `<div style="text-align:center;margin-top:10px"><button class="btn ghost" id="lg_more">${t("Load more")}</button></div>` : ""}</div>`;
  }
  bindLogList(el) {
    const L = this.state.logv;
    el.querySelectorAll("tr.lrow").forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.closest("[data-set],a")) return; L.open = L.open === tr.dataset.lid ? null : tr.dataset.lid; this.renderLogList(); }));
    el.querySelectorAll(".fixbox[data-fnum]").forEach((b) => this.bindFixBox(b, () => this.loadLog()));
    this.bindCards(el);
    const more = el.querySelector("#lg_more"); if (more) more.onclick = () => this.busy(more, "…", () => this.loadLog(true));
  }
  bindLog(root, $) {
    const L = this.state.logv;
    const refilter = () => { L.open = null; this.loadLog(); };
    for (const [id, key] of [["lg_level", "level"], ["lg_kind", "kind"], ["lg_shop", "retailer"], ["lg_src", "source"]]) if ($(id)) $(id).addEventListener("change", (e) => { L[key] = e.target.value; refilter(); });
    root.querySelectorAll("[data-lstat]").forEach((c) => c.addEventListener("click", () => { L.status = c.dataset.lstat; root.querySelectorAll("[data-lstat]").forEach((x) => x.classList.toggle("on", x === c)); refilter(); }));
    let tm; const typed = (key) => (e) => { L[key] = e.target.value; clearTimeout(tm); tm = setTimeout(refilter, 350); };
    $("lg_set").addEventListener("input", typed("set")); $("lg_q").addEventListener("input", typed("q"));
    $("lg_reload").onclick = () => this.busy($("lg_reload"), "…", async () => { await this.load(); await this.loadLog(); });
    if ($("lg_clear")) $("lg_clear").onclick = () => { Object.assign(L, { level: "", kind: "", retailer: "", source: "", status: "", set: "", q: "", data: null }); this.renderContent(); };
    const list = this.shadowRoot.getElementById("loglist"); if (list) this.bindLogList(list);
  }
  /** Filter the logbook on one set and jump there (used from the set dialog). */
  showLogFor(num) {
    Object.assign(this.state.logv, { level: "", kind: "", retailer: "", source: "", status: "", set: num, q: "", data: null, open: null });
    this.state.section = "log"; this.state.sub.log = "all"; this.persist(); this.closeDialog(); this.render(true);
  }
  fixBoxHtml(set, rid, o) {
    const retailers = this.state.data.retailers;
    const google = `https://www.google.com/search?q=${encodeURIComponent(`LEGO ${set.set_number} ${set.name || ""}`)}${rid ? `+site:${{ lego_com: "lego.com", amazon_nl: "amazon.nl", amazon_de: "amazon.de", amazon_be: "amazon.com.be", bol: "bol.com", kruidvat_be: "kruidvat.be", dreamland_be: "dreamland.be" }[rid] || ""}` : ""}`;
    const shopOpts = Object.entries(retailers).map(([k, v]) => `<option value="${k}" ${k === (rid || "lego_com") ? "selected" : ""}>${esc(v)}</option>`).join("");
    const mp = o && o.manual_price != null ? o.manual_price : "";
    return `<div class="fixbox" data-fnum="${esc(set.set_number)}" data-frid="${esc(rid || "")}">
        <div class="form"><label>${t("Shop")}<select class="f_shop">${shopOpts}</select></label>
        <label style="grid-column:span 2">${t("Correct product page (URL or ASIN)")}<input class="f_url" value="${esc(o && o.url || "")}" placeholder="https://…"></label>
        <label>${t("Price now (€, optional)")}<input class="f_price" type="number" min="0" step="0.01" value="${esc(mp)}" placeholder="${set.rrp ? t("RRP {price}", { price: set.rrp }) : t("e.g. {example}", { example: "39.99" })}"></label></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><button class="btn sm f_go">✓ ${t("Fix")}</button>
        ${o && o.url ? `<a class="btn ghost sm" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${t("Current link")} ↗</a>` : ""}
        <a class="btn ghost sm" href="${google}" target="_blank" rel="noopener noreferrer">🔎 ${t("Search the web")} ↗</a>
        ${rid && o ? `<button class="btn ghost sm f_retry">↻ ${t("Try again")}</button><button class="btn ghost sm f_ign">${o.ignored ? t("Stop ignoring") : t("Ignore")}</button><button class="btn danger sm f_rm">🗑 ${t("Remove link")}</button>` : ""}
        <button class="btn ghost sm" data-set="${esc(set.set_number)}">${t("Open set")}</button></div>
        <p class="muted" style="font-size:12px;margin:8px 0 0">${t("The link and price are saved as manual values: they always win over automatic ones and are never removed. A link to another product starts with an empty price history.")}</p></div>`;
  }
  bindFixBox(box, after) {
    const num = box.dataset.fnum, rid0 = box.dataset.frid, q = (c) => box.querySelector("." + c);
    const done = async (msg) => { await this.load(); if (after) await after(); this.toast(msg, "ok"); };
    q("f_go").onclick = () => {
      const retailer = q("f_shop").value, url = q("f_url").value.trim(), price = q("f_price").value ? +q("f_price").value : null;
      if (!url && price == null) return this.toast(t("Enter a link and/or a price"), "err");
      if (price != null && !(price > 0 && price <= 10000)) return this.toast(t("Invalid price"), "err");
      const data = { set_number: num, retailer };
      if (url) data.url = url;           // same product page keeps its history, another page starts fresh
      if (price != null) data.price = price;
      this.busy(q("f_go"), t("Working…"), async () => { await this.svc("fix_offer", data); this.state.errs.open = null; this.state.logv.open = null; await done(t("Set {number} fixed", { number: num })); });
    };
    if (q("f_retry")) q("f_retry").onclick = () => this.busy(q("f_retry"), "…", async () => { const r = (await this.svc("refresh", { set_number: num, force: true }, true)).response; await done(r.updated ? t("Price fetched") : t("Still no price")); });
    if (q("f_ign")) q("f_ign").onclick = () => this.busy(q("f_ign"), "…", async () => { const o = (this.sets.find((x) => x.set_number === num).offers || {})[rid0] || {}; await this._hass.callWS({ type: "lego_tracker/ignore_error", set_number: num, retailer: rid0, ignore: !o.ignored }); await done(o.ignored ? t("The error is shown again") : t("Error ignored until something changes")); });
    if (q("f_rm")) q("f_rm").onclick = () => { if (!confirm(t("Remove this link? It will not be linked automatically again."))) return; this.busy(q("f_rm"), "…", async () => { await this.svc("remove_offer", { set_number: num, retailer: rid0 }); await done(t("Link removed")); }); };
  }
  bindErrors(root, $) {
    const E = this.state.errs;
    root.querySelectorAll("[data-etype]").forEach((c) => c.addEventListener("click", () => { E.type = c.dataset.etype; E.open = null; this.renderContent(); }));
    $("e_scope").addEventListener("change", (e) => { E.scope = e.target.value; this.renderContent(); });
    $("e_shop").addEventListener("change", (e) => { E.shop = e.target.value; this.renderContent(); });
    $("e_ign").addEventListener("change", (e) => { E.showIgnored = e.target.checked; this.renderContent(); });
    root.querySelectorAll("tr.erow").forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.closest("[data-set],a")) return; E.open = E.open === tr.dataset.ekey ? null : tr.dataset.ekey; this.renderContent(); const u = this.shadowRoot.querySelector(".f_url"); if (u) u.focus(); }));
    const box = root.querySelector(".fixbox"); if (box) this.bindFixBox(box);
  }
  // ---------------------------------------------------------------- notifications tab
  async loadNotify() {
    const N = this.state.notif;
    try { N.data = await this._hass.callWS({ type: "lego_tracker/notify/get" }); N.err = null; } catch (e) { N.err = e.message || String(e); }
    if (this.state.section === "manage" && this.state.sub.manage === "notify") this.renderContent();
  }
  ruleTemplate(kind) {
    const o = this.state.notif.data.options, mob = o.notify.find((n) => n.kind === "mobile");
    const targets = mob ? [{ type: "mobile", service: mob.service }] : [{ type: "persistent" }];
    const base = { enabled: true, scope: { type: "all", themes: [], sets: [] }, params: {}, shops: [], targets, cooldown_hours: 24, quiet: null, image: true, link: true };
    return {
      deals: { ...base, name: t("All deals"), triggers: ["all_time_low", "discount", "target_hit"], params: { discount_pct: this.state.data.threshold } },
      watch: { ...base, name: t("Watchlist: lowest price ever"), scope: { type: "watchlist", themes: [], sets: [] }, triggers: ["all_time_low", "target_hit"] },
      theme: { ...base, name: t("Theme below an amount"), scope: { type: "themes", themes: o.themes.slice(0, 1), sets: [] }, triggers: ["price_below"], params: { price_below: 50 } },
      set: { ...base, name: t("Specific sets"), scope: { type: "sets", themes: [], sets: [] }, triggers: ["price_below", "price_drop"], params: { price_below: 50, drop_pct: 10 } },
      retire: { ...base, name: t("Retiring sets"), scope: { type: "watchlist", themes: [], sets: [] }, triggers: ["retiring_soon"] },
      digest: { ...base, name: t("Daily digest"), triggers: ["digest"], cooldown_hours: 0, image: false, link: false },
      problems: { ...base, name: t("Report problems"), triggers: ["problems"], targets: [{ type: "persistent" }], image: false, link: false },
    }[kind];
  }
  ruleSummary(r) {
    const o = this.state.notif.data.options, trg = o.triggers, p = r.params || {};
    const scope = { all: t("all sets"), watchlist: t("watchlist"), collection: t("my collection"), themes: `${t("themes")}: ${(r.scope.themes || []).join(", ")}`, sets: `sets: ${(r.scope.sets || []).slice(0, 8).join(", ")}${(r.scope.sets || []).length > 8 ? "…" : ""}` }[r.scope.type];
    const trig = r.triggers.map((k) => { const x = trg[k] || { label: k }; const v = { discount_pct: `≥ ${p.discount_pct}%`, price_below: `≤ €${p.price_below}`, drop_pct: `≥ ${p.drop_pct}%`, min_score: `≥ ${p.min_score}` }[x.param] || ""; return `${t(x.label)}${v ? " " + v : ""}`; });
    const tgt = r.targets.map((x) => this.targetLabel(x));
    return `<span class="tag">🎯 ${esc(scope)}</span>${trig.map((x) => `<span class="tag">⚡ ${esc(x)}</span>`).join("")}${r.shops && r.shops.length ? `<span class="tag">🏪 ${r.shops.map((x) => esc(o.retailers[x] || x)).join(", ")}</span>` : ""}<br>${tgt.map((x) => `<span class="tag">➜ ${esc(x)}</span>`).join("")}${r.quiet ? `<span class="tag">🌙 ${t("quiet {from}–{to}", { from: r.quiet.from, to: r.quiet.to })}</span>` : ""}`;
  }
  targetLabel(x) {
    const o = this.state.notif.data.options, svc = (s) => (o.notify.find((n) => n.service === s) || {}).label || s;
    return { mobile: `📱 ${svc(x.service)}`, notify: `💬 ${svc(x.service)}`, email: `✉️ ${(x.to || []).join(", ")}`, persistent: "🔔 " + t("Notification in Home Assistant"),
      entity: `📣 ${(o.notify_entities.find((e) => e.entity_id === x.entity_id) || {}).label || x.entity_id}`, tts: `🔊 ${(o.media_players.find((e) => e.entity_id === x.media_player) || {}).label || x.media_player}`, event: "⚡ " + t("Event only (automations)") }[x.type] || x.type;
  }
  vNotify() {
    const N = this.state.notif;
    if (N.err) return this.emptyState("🔒", `${t("Notifications not available: {error}", { error: esc(tx(N.err)) })}<br>${t("Only administrators can set up notifications.")}`);
    if (!N.data) { this.loadNotify(); return `<div class="skel" style="height:300px"></div>`; }
    if (N.edit) return this.vRuleEditor();
    const d = N.data;
    const list = d.rules.map((r, i) => `<div class="rule ${r.enabled ? "" : "off"}" data-ri="${i}"><label class="switch" title="${t("On/off")}"><input type="checkbox" class="r_on" ${r.enabled ? "checked" : ""}><i></i></label>
      <div style="flex:1;min-width:0"><b>${esc(r.name)}</b>${d.queued[r.id] ? ` <span class="tag">🌙 ${t("{n} queued", { n: d.queued[r.id] })}</span>` : ""}<div class="sum">${this.ruleSummary(r)}</div></div>
      <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end"><button class="btn ghost sm r_edit">✎ ${t("Edit")}</button><button class="btn ghost sm r_test" title="${t("Send a test notification")}">🔔</button><button class="btn ghost sm r_dup" title="${t("Duplicate")}">⧉</button><button class="btn danger sm r_del" title="${t("Delete")}">🗑</button></div></div>`).join("");
    const tpl = [["deals", "🏷️ " + t("All deals")], ["watch", "👀 " + t("Watchlist lowest price")], ["theme", "🎨 " + t("Theme below an amount")], ["set", "🧱 " + t("Specific sets")], ["retire", "⏳ " + t("Retiring soon")], ["digest", "🗞️ " + t("Daily digest")], ["problems", "⚠️ " + t("Problems")]];
    const log = d.log.slice(0, 15).map((l) => `<li><span class="ic">${l.queued ? "🌙" : l.ok ? "✅" : "⚠️"}</span><div><b>${esc(tx(l.title))}</b><div>${esc(tx(l.message.split("\n")[0]))}</div><div class="t">${esc(l.rule)} · ${DATE(l.ts, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</div></div></li>`).join("");
    return `<div class="cols" data-nf="1"><div><div class="panel"><h3>🔔 ${t("Notification rules")}<span class="hsp"></span></h3><p>${t("Each rule decides for which sets, when, and to whom and how you get a notification. Every notification also fires the event {event} for your own automations, and ends up in the Logbook.", { event: "<code>lego_tracker_notification</code>" })}</p>
      ${list || this.emptyState("🔕", t("No rules yet. Pick a template below."))}</div>
      <div class="panel"><h3>＋ ${t("New rule")}</h3><div class="chips">${tpl.map(([k, l]) => `<span class="chip" data-tpl="${k}">${l}</span>`).join("")}</div></div></div>
      <aside><div class="panel"><h3>📜 ${t("Recently sent")}</h3>${log ? `<ul class="tl">${log}</ul>` : `<p>${t("Nothing sent yet.")}</p>`}</div></aside></div>`;
  }
  vRuleEditor() {
    const N = this.state.notif, r = N.edit, o = N.data.options, p = r.params || (r.params = {});
    const opt = (list, cur, ph) => `${ph ? `<option value="">${ph}</option>` : ""}${list.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(cur) ? "selected" : ""}>${esc(l)}</option>`).join("")}`;
    const setsBy = {}; this.sets.forEach((x) => { const th = x.theme || "Unknown"; (setsBy[th] = setsBy[th] || []).push(x); });
    const themes = Object.keys(setsBy).sort();
    const pickTheme = N.pickTheme && setsBy[N.pickTheme] ? N.pickTheme : themes[0];
    const scopeBox = r.scope.type === "themes"
      ? `<div class="fbar"><select id="n_theme_add">${opt(o.themes.filter((th) => !r.scope.themes.includes(th)).map((th) => [th, `${th} (${(setsBy[th] || []).length})`]), "", t("Choose a theme…"))}</select><button class="btn ghost sm" id="n_theme_btn">＋ ${t("Add")}</button></div>
         <div class="chips">${r.scope.themes.map((th) => `<span class="chip on" data-rmtheme="${esc(th)}">${esc(th)} ✕</span>`).join("") || `<span class="muted">${t("No theme chosen yet.")}</span>`}</div>`
      : r.scope.type === "sets"
      ? `<div class="fbar"><select id="n_set_theme" title="${t("Choose a theme first")}">${opt(themes.map((th) => [th, `${th === "Unknown" ? t("Unknown") : th} (${setsBy[th].length})`]), pickTheme)}</select>
         <select id="n_set_pick" style="min-width:240px">${opt(setsBy[pickTheme] ? setsBy[pickTheme].slice().sort((a, b) => a.set_number.localeCompare(b.set_number)).filter((x) => !r.scope.sets.includes(x.set_number)).map((x) => [x.set_number, `${x.set_number} ${x.name || ""}${x.owned ? " 📦" : ""}`]) : [], "", t("Choose a set…"))}</select>
         <button class="btn ghost sm" id="n_set_btn">＋</button><span class="muted">${t("or")}</span><input id="n_set_free" placeholder="${t("set number")}" inputmode="numeric" style="width:120px"><button class="btn ghost sm" id="n_set_free_btn">＋</button></div>
         <div class="chips">${r.scope.sets.map((n) => { const x = this.sets.find((y) => y.set_number === n); return `<span class="chip on" data-rmset="${esc(n)}">${esc(n)} ${esc(x ? (x.name || "") : "(" + t("not tracked") + ")")} ✕</span>`; }).join("") || `<span class="muted">${t("No set chosen yet.")}</span>`}</div>`
      : "";
    const pc = (k, list) => `<select data-param="${k}">${opt(list.map((v) => [v, k !== "min_score" ? `${v}%` : v]), p[k] ?? list[Math.floor(list.length / 2)])}</select>`;
    const trigRow = (k) => { const tg = o.triggers[k]; const on = r.triggers.includes(k);
      const par = { discount_pct: pc("discount_pct", [10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70]), drop_pct: pc("drop_pct", [5, 10, 15, 20, 25, 30, 40, 50]), min_score: pc("min_score", [45, 50, 60, 70, 80, 90]),
        price_below: `<span style="display:flex;gap:4px;align-items:center">€ <input data-param="price_below" type="number" min="1" step="1" value="${esc(p.price_below ?? "")}" placeholder="${t("amount")}" style="width:90px"></span>` }[tg.param] || "";
      return `<div class="trig"><input type="checkbox" data-trig="${k}" ${on ? "checked" : ""}><label style="cursor:pointer" data-trigl="${k}">${esc(t(tg.label))}${tg.per_set ? "" : ` <span class="muted" style="font-size:12px">(${t("general")})</span>`}</label><div>${par}</div></div>`; };
    const perSet = Object.keys(o.triggers).filter((k) => o.triggers[k].per_set), general = Object.keys(o.triggers).filter((k) => !o.triggers[k].per_set);
    const mobiles = o.notify.filter((n) => n.kind === "mobile"), mails = o.notify.filter((n) => n.kind === "email");
    const tgtRow = (x, i) => {
      let body = "";
      if (x.type === "mobile") body = mobiles.length ? `<select data-tf="service">${opt(mobiles.map((n) => [n.service, n.label]), x.service, t("Choose a device…"))}</select>` : `<span class="err">${t("No Home Assistant app found. Install the Companion app on your phone.")}</span>`;
      if (x.type === "notify") body = `<select data-tf="service">${opt(o.notify.map((n) => [n.service, `${n.label} (${n.service})`]), x.service, t("Choose a notify service…"))}</select>`;
      if (x.type === "email") body = `<select data-tf="service" title="${t("Service that sends the e-mail")}">${opt((mails.length ? mails : o.notify).map((n) => [n.service, `${n.label} (${n.service})`]), x.service, t("Send via…"))}</select><input data-tf="to" placeholder="name@example.com, …" value="${esc((x.to || []).join(", "))}" style="min-width:220px">${mails.length ? "" : `<span class="muted" style="font-size:12px">${t("Tip: add the SMTP integration to send e-mails.")}</span>`}`;
      if (x.type === "entity") body = o.notify_entities.length ? `<select data-tf="entity_id">${opt(o.notify_entities.map((n) => [n.entity_id, n.label]), x.entity_id, t("Choose a notify entity…"))}</select>` : `<span class="muted">${t("No notify entities found.")}</span>`;
      if (x.type === "tts") body = `<select data-tf="tts">${opt(o.tts.map((n) => [n.entity_id, n.label]), x.tts, t("Speech service…"))}</select><select data-tf="media_player">${opt(o.media_players.map((n) => [n.entity_id, n.label]), x.media_player, t("Speaker…"))}</select>`;
      if (x.type === "persistent") body = `<span class="muted">${t("Appears among the notifications in Home Assistant.")}</span>`;
      if (x.type === "event") body = `<span class="muted">${t("Only the event {event}, for your own automations.", { event: "<code>lego_tracker_notification</code>" })}</span>`;
      return `<div class="tgt" data-ti="${i}"><select data-tf="type">${opt([["mobile", "📱 " + t("Mobile app")], ["email", "✉️ " + t("E-mail")], ["notify", "💬 " + t("Notify service (Telegram, Signal, …)")], ["entity", "📣 " + t("Notify entity")], ["persistent", "🔔 " + t("Notification in Home Assistant")], ["tts", "🔊 " + t("Speech on a speaker")], ["event", "⚡ " + t("Event only")]], x.type)}</select>${body}<span style="flex:1"></span><button class="btn ghost sm" data-rmt="${i}">✕</button></div>`;
    };
    const hours = Array.from({ length: 24 }, (_, h) => [`${String(h).padStart(2, "0")}:00`, `${String(h).padStart(2, "0")}:00`]);
    return `<div data-nf="1"><div class="panel"><h3>${N.idx == null ? "＋ " + t("New rule") : "✎ " + t("Edit rule")}<span class="hsp"></span><button class="btn ghost sm" id="n_cancel">← ${t("Back")}</button></h3>
      <div class="form"><label style="grid-column:span 2">${t("Name")}<input id="n_name" value="${esc(r.name)}" maxlength="60"></label><label class="chk" style="align-self:end"><input type="checkbox" id="n_enabled" ${r.enabled ? "checked" : ""}> ${t("rule is on")}</label></div></div>
      <div class="panel"><h3>1 · ${t("For which sets?")}</h3><div class="fbar"><select id="n_scope">${opt([["all", t("All tracked sets")], ["watchlist", "👀 " + t("My watchlist (not owned)")], ["collection", "📦 " + t("My collection")], ["themes", "🎨 " + t("Certain themes")], ["sets", "🧱 " + t("Certain sets")]], r.scope.type)}</select></div>${scopeBox}</div>
      <div class="panel"><h3>2 · ${t("When?")}</h3><p style="margin-bottom:4px">${t("Per set:")}</p>${perSet.map(trigRow).join("")}<p style="margin:12px 0 4px">${t("General:")}</p>${general.map(trigRow).join("")}
        <div class="fbar" style="margin-top:10px"><span class="muted">${t("In which shops?")}</span><select id="n_shop_add">${opt(Object.entries(o.retailers).filter(([k]) => !r.shops.includes(k)), "", r.shops.length ? "＋ " + t("add a shop") : t("All shops (or choose…)"))}</select>
        ${r.shops.map((k) => `<span class="chip on" data-rmshop="${k}">${esc(o.retailers[k] || k)} ✕</span>`).join("")}</div></div>
      <div class="panel"><h3>3 · ${t("To whom and how?")}</h3>${r.targets.map(tgtRow).join("")}<button class="btn ghost sm" id="n_tadd">＋ ${t("Add a recipient")}</button>
        <div class="form" style="margin-top:12px"><label>${t("Don't notify again within")}<select id="n_cool">${opt([[0, t("always notify")], [1, t("1 hour")], [6, t("{n} hours", { n: 6 })], [12, t("{n} hours", { n: 12 })], [24, t("1 day")], [72, t("{n} days", { n: 3 })], [168, t("1 week")]], r.cooldown_hours)}</select></label>
        <label>${t("Quiet hours")}<select id="n_qon">${opt([["", t("none")], ["1", t("on")]], r.quiet ? "1" : "")}</select></label>
        ${r.quiet ? `<label>${t("from")}<select id="n_qf">${opt(hours, r.quiet.from)}</select></label><label>${t("to")}<select id="n_qt">${opt(hours, r.quiet.to)}</select></label>` : ""}</div>
        <div class="form"><label class="chk"><input type="checkbox" id="n_img" ${r.image ? "checked" : ""}> ${t("include an image")}</label><label class="chk"><input type="checkbox" id="n_link" ${r.link ? "checked" : ""}> ${t("link to the shop")}</label></div>
        <p style="font-size:12px">${t("During quiet hours notifications are collected and sent afterwards in one message (the Home Assistant notification and the event come right away).")}</p></div>
      <div class="panel" style="position:sticky;bottom:12px;z-index:2;display:flex;gap:10px;flex-wrap:wrap"><button class="btn" id="n_save">💾 ${t("Save rule")}</button><button class="btn ghost" id="n_test">🔔 ${t("Test notification")}</button><button class="btn ghost" id="n_cancel2">${t("Cancel")}</button></div></div>`;
  }
  async saveRules(rules, msg) {
    const N = this.state.notif;
    const r = await this._hass.callWS({ type: "lego_tracker/notify/set", rules });
    N.data.rules = r.rules; if (msg) this.toast(msg, "ok");
  }
  bindNotify(root, $) {
    const N = this.state.notif, d = N.data;
    root.querySelectorAll("[data-tpl]").forEach((c) => c.addEventListener("click", () => { N.edit = JSON.parse(JSON.stringify(this.ruleTemplate(c.dataset.tpl))); N.idx = null; this.renderContent(true); }));
    root.querySelectorAll(".rule[data-ri]").forEach((el) => {
      const i = +el.dataset.ri, q = (c) => el.querySelector("." + c);
      q("r_on").addEventListener("change", (e) => { const rules = JSON.parse(JSON.stringify(d.rules)); rules[i].enabled = e.target.checked; this.saveRules(rules, e.target.checked ? t("Rule switched on") : t("Rule switched off")).then(() => this.renderContent()).catch((err) => this.toast(tx(err.message), "err")); });
      q("r_edit").onclick = () => { N.edit = JSON.parse(JSON.stringify(d.rules[i])); N.idx = i; this.renderContent(true); };
      q("r_dup").onclick = () => { const c = JSON.parse(JSON.stringify(d.rules[i])); delete c.id; c.name += " (" + t("copy") + ")"; N.edit = c; N.idx = null; this.renderContent(true); };
      q("r_del").onclick = () => { if (!confirm(t("Delete rule “{name}”?", { name: d.rules[i].name }))) return; const rules = d.rules.filter((_, j) => j !== i); this.busy(q("r_del"), "", async () => { await this.saveRules(rules, t("Rule deleted")); this.renderContent(); }); };
      q("r_test").onclick = () => this.testRule(d.rules[i], q("r_test"));
    });
    if (!N.edit) return;
    const r = N.edit;
    const sync = () => {
      const v = (id) => (root.querySelector("#" + id) || {}).value;
      if ($("n_name")) r.name = $("n_name").value; if ($("n_enabled")) r.enabled = $("n_enabled").checked;
      root.querySelectorAll("[data-trig]").forEach((c) => { const k = c.dataset.trig; r.triggers = r.triggers.filter((x) => x !== k); if (c.checked) r.triggers.push(k); });
      root.querySelectorAll("[data-param]").forEach((c) => { r.params[c.dataset.param] = c.value === "" ? undefined : +c.value; });
      root.querySelectorAll(".tgt").forEach((el) => { const tg = r.targets[+el.dataset.ti]; el.querySelectorAll("[data-tf]").forEach((f) => { if (f.dataset.tf === "type") return; tg[f.dataset.tf] = f.dataset.tf === "to" ? f.value.split(/[,;\s]+/).filter(Boolean) : f.value; }); });
      if ($("n_cool")) r.cooldown_hours = +v("n_cool");
      if ($("n_qon")) r.quiet = v("n_qon") ? { from: v("n_qf") || "22:00", to: v("n_qt") || "07:00" } : null;
      if ($("n_img")) r.image = $("n_img").checked; if ($("n_link")) r.link = $("n_link").checked;
    };
    const rerender = () => { sync(); this.renderContent(); };
    root.querySelectorAll("[data-trigl]").forEach((l) => l.addEventListener("click", () => { const c = root.querySelector(`[data-trig="${l.dataset.trigl}"]`); c.checked = !c.checked; }));
    $("n_scope").addEventListener("change", (e) => { sync(); r.scope.type = e.target.value; this.renderContent(); });
    if ($("n_theme_btn")) $("n_theme_btn").onclick = () => { const th = $("n_theme_add").value; if (!th) return this.toast(t("Choose a theme first"), "err"); sync(); r.scope.themes.push(th); this.renderContent(); };
    if ($("n_theme_add")) $("n_theme_add").addEventListener("change", () => $("n_theme_btn").click());
    root.querySelectorAll("[data-rmtheme]").forEach((c) => c.onclick = () => { sync(); r.scope.themes = r.scope.themes.filter((th) => th !== c.dataset.rmtheme); this.renderContent(); });
    if ($("n_set_theme")) $("n_set_theme").addEventListener("change", (e) => { sync(); N.pickTheme = e.target.value; this.renderContent(); });
    const addSet = (n) => { n = (String(n).match(/\d{3,7}/) || [""])[0]; if (!n) return this.toast(t("Enter a set number of 3–7 digits"), "err"); if (r.scope.sets.includes(n)) return this.toast(t("Already in the list"), "err"); sync(); r.scope.sets.push(n); this.renderContent(); if (!this.sets.some((x) => x.set_number === n)) this.toast(t("Set {number} is not tracked yet: add it to get notifications", { number: n })); };
    if ($("n_set_btn")) $("n_set_btn").onclick = () => addSet($("n_set_pick").value);
    if ($("n_set_pick")) $("n_set_pick").addEventListener("change", (e) => e.target.value && addSet(e.target.value));
    if ($("n_set_free_btn")) { $("n_set_free_btn").onclick = () => addSet($("n_set_free").value); $("n_set_free").addEventListener("keydown", (e) => { if (e.key === "Enter") addSet(e.target.value); }); }
    root.querySelectorAll("[data-rmset]").forEach((c) => c.onclick = () => { sync(); r.scope.sets = r.scope.sets.filter((x) => x !== c.dataset.rmset); this.renderContent(); });
    $("n_shop_add").addEventListener("change", (e) => { if (!e.target.value) return; sync(); r.shops.push(e.target.value); this.renderContent(); });
    root.querySelectorAll("[data-rmshop]").forEach((c) => c.onclick = () => { sync(); r.shops = r.shops.filter((x) => x !== c.dataset.rmshop); this.renderContent(); });
    root.querySelectorAll('.tgt [data-tf="type"]').forEach((sel) => sel.addEventListener("change", (e) => { sync(); r.targets[+e.target.closest(".tgt").dataset.ti] = { type: e.target.value }; this.renderContent(); }));
    root.querySelectorAll("[data-rmt]").forEach((b) => b.onclick = () => { sync(); r.targets.splice(+b.dataset.rmt, 1); this.renderContent(); });
    $("n_tadd").onclick = () => { sync(); const mob = d.options.notify.find((n) => n.kind === "mobile"); r.targets.push(mob ? { type: "mobile", service: mob.service } : { type: "persistent" }); this.renderContent(); };
    $("n_qon").addEventListener("change", rerender);
    const back = () => { N.edit = null; N.idx = null; this.renderContent(true); };
    $("n_cancel").onclick = back; $("n_cancel2").onclick = back;
    $("n_save").onclick = () => {
      sync();
      Object.keys(r.params).forEach((k) => { if (r.params[k] == null || isNaN(r.params[k])) delete r.params[k]; });
      const rules = JSON.parse(JSON.stringify(d.rules)); if (N.idx == null) rules.push(r); else rules[N.idx] = r;
      this.busy($("n_save"), t("Saving…"), async () => { await this.saveRules(rules, t("Rule saved")); back(); });
    };
    $("n_test").onclick = () => { sync(); this.testRule(r, $("n_test")); };
  }
  testRule(rule, btn) {
    const r = JSON.parse(JSON.stringify(rule)); Object.keys(r.params || {}).forEach((k) => { if (r.params[k] == null) delete r.params[k]; });
    return this.busy(btn, "…", async () => {
      const res = (await this._hass.callWS({ type: "lego_tracker/notify/test", rule: r })).results;
      const bad = res.filter((x) => !x.ok);
      if (bad.length) this.toast(t("Failed: {list}", { list: bad.map((x) => `${this.targetLabel(x.target)} (${tx(x.error)})`).join("; ") }), "err");
      else this.toast(t(res.length > 1 ? "Test notification sent to {n} recipients" : "Test notification sent to {n} recipient", { n: res.length }), "ok");
    });
  }
  vLinks() {
    const L = this.state.links, rows = [];
    for (const s of this.sets) {
      if (L.scope === "owned" && !s.owned) continue;
      for (const [rid, o] of Object.entries(s.offers || {})) {
        const st = o.link_status || "unknown";
        if (L.status !== "all" && !(L.status === st || (L.status === "ok" && st === "confirmed"))) continue;
        rows.push({ s, rid, o, st });
      }
    }
    const count = (st) => this.sets.filter((s) => L.scope !== "owned" || s.owned).reduce((a, s) => a + Object.values(s.offers || {}).filter((o) => (o.link_status || "unknown") === st || (st === "ok" && o.link_status === "confirmed")).length, 0);
    const chip = (k, l) => `<span class="chip ${L.status === k ? "on" : ""}" data-lstatus="${k}">${l}${k !== "all" ? ` <span class="muted">${count(k)}</span>` : ""}</span>`;
    const trs = rows.slice(0, 400).map(({ s, rid, o, st }) => {
      const key = `${s.set_number}|${rid}`, editing = L.edit === key;
      return `<tr data-key="${esc(key)}"><td style="min-width:150px"><a class="setlink" data-set="${esc(s.set_number)}"><b>${esc(s.set_number)}</b> ${esc(s.name || "")}</a><div class="muted" style="font-size:12px">${esc(s.theme || "")}${s.rrp ? ` · ${t("RRP {price}", { price: EUR(s.rrp) })}` : ""}</div></td>
        <td>${esc(o.label)}</td><td><span class="lk ${st}">${{ ok: "✓ " + t("correct"), confirmed: "✓ " + t("approved"), suspect: "⚠ " + t("suspicious"), unknown: "? " + t("unknown") }[st]}</span><div class="${st === "suspect" ? "err" : "muted"}" style="font-size:12px;margin-top:3px">${esc(tx(o.link_reason || ""))}</div></td>
        <td style="max-width:280px;font-size:13px">${o.title ? esc(o.title.slice(0, 120)) : `<span class="muted">${o.url ? esc(decodeURIComponent(o.url.replace(/^https?:\/\/(www\.)?/, "")).slice(0, 70)) : ""}</span>`}</td>
        <td class="num">${o.price != null ? EUR(o.price) : o.low != null ? `<span class="muted">${EUR(o.low)}</span>` : "–"}</td>
        <td style="white-space:nowrap">${editing ? `<input class="lnew" placeholder="${t("new URL or ASIN")}" style="width:220px;padding:6px 8px"> <button class="btn sm lsave">${t("Save")}</button> <button class="btn ghost sm lcancel">✕</button>`
          : `${o.url ? `<a class="btn ghost sm" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">↗</a> ` : ""}${st !== "confirmed" ? `<button class="btn ghost sm lok" title="${t("Correct")}">✓</button> ` : ""}<button class="btn ghost sm ledit" title="${t("Other link")}">✎</button> <button class="btn ghost sm lrm" title="${t("Remove")}">🗑</button>`}</td></tr>`;
    }).join("");
    return `<div class="panel"><h3>🔗 ${t("Link check")}<span class="hsp"></span><button class="btn ghost sm" id="lverify">↺ ${t("Judge again")}</button></h3>
      <p>${t("Checks whether every shop link points to the right set: the set number must be in the product title, and it must not be an accessory (lighting, display case…) or a knock-off brand. A far too low price is suspicious too. Suspicious links don't count for prices and collection value until you approve them. For Amazon the title is only known after a price check.")}</p>
      <div class="chips">${chip("suspect", "⚠ " + t("Suspicious"))}${chip("unknown", "? " + t("Not checked"))}${chip("ok", "✓ " + t("OK"))}${chip("all", t("All"))}<span style="flex:1"></span><span class="chip ${L.scope === "owned" ? "on" : ""}" data-lscope="owned">📦 ${t("My collection")}</span><span class="chip ${L.scope === "all" ? "on" : ""}" data-lscope="all">${t("All sets")}</span></div></div>
      ${rows.length ? `<div class="panel tscroll"><table class="tbl"><tr><th>Set</th><th>${t("Shop")}</th><th>${t("Verdict")}</th><th>${t("Product title / URL")}</th><th class="num">${t("Price")}</th><th></th></tr>${trs}</table>${rows.length > 400 ? `<p class="muted">${t("First {n} of {total}.", { n: 400, total: rows.length })}</p>` : ""}</div>` : this.emptyState("✅", L.status === "suspect" ? t("No suspicious links. Nice!") : t("No links in this selection."))}`;
  }
  // ---------------------------------------------------------------- settings
  async loadSettings() {
    try { this.state.settings = await this._hass.callWS({ type: "lego_tracker/settings/get" }); this.state.settingsErr = null; }
    catch (e) { this.state.settingsErr = e.message || String(e); }
    this.state.draft = null;
    if (this.state.section === "manage" && this.state.sub.manage === "settings") this.renderContent();
  }
  vSettings() {
    const st = this.state.settings;
    if (this.state.settingsErr) return this.emptyState("🔒", `${t("Settings not available: {error}", { error: esc(tx(this.state.settingsErr)) })}<br>${t("Only administrators can change settings.")}`);
    if (!st) { this.loadSettings(); return `<div class="skel" style="height:300px"></div>`; }
    const d = this.state.draft || (this.state.draft = { shops: st.shops.map((x) => ({ ...x })), custom: st.shops.filter((x) => !x.builtin).map((x) => ({ id: x.id, name: x.label, domain: x.domain, search: x.search })), mode: st.refresh_mode || "spread" });
    const keyRow = (k, label, help, link) => { const ks = st.keys[k]; return `<div class="form" style="align-items:end"><label style="grid-column:span 2">${label}<input id="k_${k}" autocomplete="off" placeholder="${ks.set ? t("set ({masked}) – leave empty to keep", { masked: esc(ks.masked) }) : t("paste your key here")}"></label>
      <div style="display:flex;gap:6px;flex-wrap:wrap"><button class="btn ghost sm" data-testkey="${k}">${t("Test")}</button>${ks.set ? `<button class="btn ghost sm" data-clearkey="${k}">${t("Clear")}</button>` : ""}</div></div><p style="font-size:12px;margin-top:-6px">${help} <a href="${link}" target="_blank" rel="noopener noreferrer">${t("Request a key")} ↗</a> <span id="kres_${k}"></span></p>`; };
    const shopRows = d.shops.map((x) => `<tr data-shop="${esc(x.id)}"><td><label class="chk" style="display:flex;gap:8px;align-items:center"><input type="checkbox" class="s_on" ${x.enabled ? "checked" : ""}> <b>${esc(x.label)}</b></label>${x.builtin ? "" : `<div class="muted" style="font-size:12px">${t("own shop")} · ${esc(x.domain)}</div>`}</td>
      <td>${x.paused_hours > 0 ? `<span class="lk suspect">${t("paused {time}", { time: dur(x.paused_hours * 3600) })}</span> <button class="btn ghost sm" data-resume="${esc(x.id)}">▶ ${t("Resume")}</button>` : `<span class="lk ok">${t("active")}</span>`}${x.blocks ? `<div class="muted" style="font-size:12px">${t("blocked {n}×", { n: x.blocks })}</div>` : ""}</td>
      <td><label class="chk" style="display:flex;gap:8px;align-items:center" title="${t("After a block (403/captcha) leave this shop alone for a while")}"><input type="checkbox" class="s_ap" ${x.autopause ? "checked" : ""}> ${t("pause automatically")}</label></td>
      <td style="min-width:300px"><div style="display:flex;gap:4px"><input class="s_search" value="${esc(x.search || "")}" placeholder="${esc(x.default_search || "https://…{query}")}" style="flex:1;min-width:220px;font-size:12px">${x.default_search ? `<button class="btn ghost sm s_reset" title="${t("Back to the default: {url}", { url: esc(x.default_search) })}" ${x.search === x.default_search ? "hidden" : ""}>↺</button>` : ""}</div><div class="s_prev muted" style="font-size:11px;margin-top:3px">${this.searchPreview(x.search || x.default_search, st.lego_locale)}</div></td>
      <td>${x.builtin ? "" : `<button class="btn danger sm s_del" title="${t("Remove shop")}">🗑</button>`}</td></tr>`).join("");
    const langs = st.languages || {}, mode = d.mode;
    const perHour = (h) => { const n = (this.state.data.schedule || {}).total || this.sets.length; return n ? Math.round((n / h) * 10) / 10 : 0; };
    return `<div class="panel"><h3>🌐 ${t("Language")}</h3><div class="form" style="max-width:520px"><label>${t("Language of the panel, notifications and userscript")}<select id="o_lang"><option value="auto" ${st.language === "auto" ? "selected" : ""}>${t("Automatic (Home Assistant language)")}</option>${Object.entries(langs).map(([k, l]) => `<option value="${k}" ${st.language === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label></div></div>
      <div class="panel"><h3>🏷️ ${t("Deals")}</h3><div class="form"><label>${t("Discount threshold (%)")}<input id="o_thr" type="number" min="1" max="90" value="${st.discount_threshold}"></label>
        <label>${t("Min. days of history for “lowest ever”")}<input id="o_hist" type="number" min="0" max="90" value="${st.min_history_days}"></label>
        <label>${t("Notifications")}<a class="btn ghost sm" data-goto="manage/notify" style="cursor:pointer;margin-top:4px;align-self:flex-start">🔔 ${t("Go to Notifications")}</a><input id="o_notify" type="hidden" value="${esc(st.notify_service)}"></label>
        <label>${t("Daily digest at")}<input id="o_digest" type="time" value="${esc(st.digest_time)}"></label></div></div>
      <div class="panel"><h3>⏰ ${t("Automatic price checks")}</h3><div class="radio">
        <label class="${mode === "spread" ? "on" : ""}"><input type="radio" name="rmode" value="spread" ${mode === "spread" ? "checked" : ""}><b>🔄 ${t("Spread over the day")}</b><span>${t("Every set is checked once per cycle at every shop, evenly spread (e.g. 240 sets in 24 h = 10 sets per hour). Gentle on the shops, fewer blocks.")}</span></label>
        <label class="${mode === "times" ? "on" : ""}"><input type="radio" name="rmode" value="times" ${mode === "times" ? "checked" : ""}><b>🕖 ${t("At fixed times")}</b><span>${t("A full round of all sets at the times you choose.")}</span></label>
        <label class="${mode === "off" ? "on" : ""}"><input type="radio" name="rmode" value="off" ${mode === "off" ? "checked" : ""}><b>⏸ ${t("Off")}</b><span>${t("Only when you press “Refresh prices”.")}</span></label></div>
        <div class="form" id="rm_spread" ${mode === "spread" ? "" : "hidden"}><label>${t("Cycle: every set once every … hours")}<input id="o_spread" type="number" min="1" max="168" value="${st.spread_hours}"></label><div class="hint" id="o_spread_hint" style="align-self:end">${t("≈ {n} sets per hour", { n: perHour(st.spread_hours).toLocaleString(LOC) })}</div></div>
        <div class="form" id="rm_times" ${mode === "times" ? "" : "hidden"}><label style="grid-column:span 2">${t("Times (1 to 6, separated by commas)")}<input id="o_times" value="${esc(st.refresh_times)}" placeholder="07:30, 19:30"></label></div>
        <p class="muted" style="font-size:12px">${this.scheduleLine()} <a data-goto="log/checks" style="cursor:pointer">${t("See the shop checks")} →</a></p></div>
      <div class="panel"><h3>💎 ${t("Collection value")}</h3><div class="radio">
        <label class="${st.value_source === "shop_first" ? "on" : ""}"><input type="radio" name="vsrc" value="shop_first" ${st.value_source === "shop_first" ? "checked" : ""}><b>${t("Shop price first")}</b><span>${t("Lowest new price now; without a shop price the imported value")}</span></label>
        <label class="${st.value_source === "import_first" ? "on" : ""}"><input type="radio" name="vsrc" value="import_first" ${st.value_source === "import_first" ? "checked" : ""}><b>${t("Imported value first")}</b><span>${t("E.g. BrickEconomy from your CSV; better for sets that are no longer in the shops")}</span></label></div></div>
      <div class="panel"><h3>🔑 ${t("Set data: API keys")}</h3><p>${t("The first source is always LEGO.com (RRP, image, name, “retiring soon”); the LEGO.com page is also tracked as a shop. Then: Brickset → Rebrickable → the public Brickset page (no key). If a source fails or misses something, the next one fills it in. Both keys are free and never sent back to your browser.")}</p>
        ${keyRow("brickset_api_key", t("Brickset API key"), t("Gives name, theme, year, pieces, image, RRP and retirement date."), "https://brickset.com/tools/webservices/requestkey")}
        ${keyRow("rebrickable_api_key", t("Rebrickable API key"), t("Gives name, theme, year, pieces and image (no RRP). After signing in: Account → Settings → API."), "https://rebrickable.com/api/")}</div>
      <div class="panel"><h3>🏪 ${t("Shops")}</h3><p>${t("Tick the shops to check. “Pause automatically” pauses a shop after a block (1 → 3 → 6 → 12 → 24 h); switch it off if you don't want that (more risk of stricter blocks).")}</p>
        <p>${t("The search URL decides how “Find links” finds a product; every shop has a default that you can change. Easiest: search the shop for e.g. “lego 10311”, copy the address bar and paste it here — the set number is replaced by {query} automatically. {query} = “LEGO + set number”, {number} = the set number, {locale} = the LEGO.com country. ↺ restores the default.", { query: "<code>{query}</code>", number: "<code>{number}</code>", locale: "<code>{locale}</code>" })}</p>
        <div class="form" style="max-width:420px"><label>${t("LEGO.com country (language-country)")}<input id="o_locale" value="${esc(st.lego_locale || "nl-be")}" placeholder="nl-be"></label></div>
        <div class="tscroll"><table class="tbl"><tr><th>${t("Shop")}</th><th>${t("Status")}</th><th>${t("Pause")}</th><th>${t("Search URL")}</th><th></th></tr>${shopRows}</table></div>
        <h3 style="margin-top:16px">＋ ${t("Add your own shop")}</h3><div class="form"><label>${t("Name")}<input id="n_name" placeholder="${t("e.g. {example}", { example: "Intertoys" })}"></label><label>${t("Domain")}<input id="n_domain" placeholder="intertoys.be"></label>
        <label style="grid-column:span 2">${t("Search URL")}<input id="n_search" placeholder="https://www.intertoys.be/zoeken?q=lego+10311"><div class="hint muted" id="n_search_prev"></div></label></div>
        <p style="font-size:12px">${t("Prices are read from the standard product data (JSON-LD/meta) that most web shops have.")}</p><button class="btn ghost" id="n_add">＋ ${t("Add to the list")}</button></div>
      <div class="panel"><h3>🛠️ ${t("Technical")}</h3><div class="form"><label class="chk"><input type="checkbox" id="o_imp" ${st.use_impersonation ? "checked" : ""}> ${t("Imitate the Chrome browser (curl_cffi) – now: {transport}", { transport: esc(st.transport) })}</label></div></div>
      <div class="panel" style="position:sticky;bottom:12px;z-index:2;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><button class="btn" id="o_save">💾 ${t("Save settings")}</button><span class="muted" style="font-size:13px">${t("After saving the integration restarts briefly (a running job stops).")}</span></div>`;
  }
  /** Turn a pasted search-result URL into a template: the set number becomes {query} / {number}. */
  toTemplate(url) {
    url = (url || "").trim();
    if (!url || /\{(query|number|locale)\}/.test(url)) return url;
    const m = url.match(/lego(?:\+|%20|%2B| |-|_)(\d{3,7})(?:-1)?/i);
    if (m) return url.replace(m[0], "{query}");
    const n = url.match(/([=/])(\d{4,7})(?=[&#/]|$)/);
    if (n) return url.replace(n[0], `${n[1]}{number}`);
    return url;
  }
  searchPreview(tpl, locale) {
    if (!tpl) return "";
    if (!/\{(query|number)\}/.test(tpl)) return `<span class="err">${t("Must contain {query} or {number}", { query: "{query}", number: "{number}" })}</span>`;
    const ex = tpl.replace("{query}", "LEGO+10281").replace("{number}", "10281").replace("{locale}", locale || "nl-be");
    return `${t("Example")}: <a href="${esc(ex)}" target="_blank" rel="noopener noreferrer">${esc(ex.length > 70 ? ex.slice(0, 70) + "…" : ex)} ↗</a>`;
  }
  vUserscript() {
    const origin = location.origin, url = `${origin}/api/lego_tracker/lego-tracker.user.js`, last = this.state.data.userscript_last;
    const ua = navigator.userAgent, browser = /Edg\//.test(ua) ? "edge" : /Firefox\//.test(ua) ? "firefox" : /Safari\//.test(ua) && !/Chrome\//.test(ua) ? "safari" : "chrome";
    const stores = { chrome: ["Chrome", "https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo"], edge: ["Edge", "https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd"], firefox: ["Firefox", "https://addons.mozilla.org/firefox/addon/tampermonkey/"], safari: ["Safari", "https://www.tampermonkey.net/?browser=safari"] };
    const step = (n, title, body) => `<div class="action" style="--i:${n}"><b><span class="st ok" style="margin-right:6px">${n}</span>${title}</b>${body}</div>`;
    return `<div class="panel"><h3>🧩 ${t("Send prices from your own browser")}</h3><p>${t("Shops block servers, but not your browser. The userscript reads the price and product title on every product page you visit and sends them to Home Assistant. That also works for Amazon and bol.com, and the title helps the link check. Only products that are already tracked (same link or ASIN) are updated.")}</p>
      ${last ? `<div class="banner" style="background:color-mix(in srgb,var(--lt-green) 12%,var(--lt-card));border-color:color-mix(in srgb,var(--lt-green) 40%,transparent)">✅ ${t("Works: last price received {when} (set {number}, {price} at {shop}).", { when: ago(last.ts), number: esc(last.set_number), price: EUR(last.price), shop: esc(this.state.data.retailers[last.retailer] || last.retailer) })} <a data-goto="log/all" style="cursor:pointer">${t("Logbook")} →</a></div>` : `<div class="banner">${t("No price received through the userscript yet.")}</div>`}</div>
      <div class="actions">
      ${step(1, t("Install Tampermonkey"), `<p>${t("Free browser extension. For your browser ({browser}):", { browser: stores[browser][0] })}</p><a class="btn" href="${stores[browser][1]}" target="_blank" rel="noopener noreferrer">${t("Tampermonkey for {browser}", { browser: stores[browser][0] })} ↗</a><p style="font-size:12px">${t("Other browsers:")} ${Object.entries(stores).filter(([k]) => k !== browser).map(([, [n, u]]) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${n}</a>`).join(" · ")}. ${t("In Chrome/Edge also switch on “Allow user scripts” for the extension, or developer mode.")}</p>`)}
      ${step(2, t("Install the userscript"), `<p>${t("Tampermonkey opens an install screen: click Install there. The script is made for your Home Assistant ({origin}) and your shops.", { origin: esc(origin) })}</p><a class="btn" href="${esc(url)}" target="_blank" rel="noopener">⬇ ${t("Install userscript")}</a><p style="font-size:12px">${t("Adding shops or changing the language later? Install it again (Tampermonkey also updates it by itself).")}</p>`)}
      ${step(3, t("Create a token"), `<p>${t("In Home Assistant: your profile → Security → Long-lived access tokens → Create token (name e.g. “LEGO userscript”). Copy the token, you only see it once.")}</p><a class="btn ghost" href="/profile/security" target="_blank" rel="noopener">${t("Go to profile → Security")} ↗</a>`)}
      ${step(4, t("Set the token"), `<p>${t("In your browser click the Tampermonkey icon → LEGO Price Tracker settings. The address is already filled in ({origin}); then paste the token.", { origin: esc(origin) })}</p>`)}
      ${step(5, t("Test"), `<p>${t("Open a product page of a set you track (click a set → “open ↗”). After a few seconds a green message appears at the bottom right, and “✅ Works” appears above.")}</p>`)}
      </div>
      <div class="panel" style="margin-top:16px"><h3>🔒 ${t("Security")}</h3><p>${t("The script contains no token: that is only stored in Tampermonkey on your device. It only runs on the shop domains in your list and only sends the set number, URL, title and price to your own Home Assistant. You can revoke the token in your profile at any time.")}</p></div>`;
  }
  vShops() {
    const st = this.state.data.retailer_stats || {};
    const cards = Object.entries(st).map(([rid, r], i) => {
      const ratio = r.offers ? r.ok / r.offers : 0;
      const state = !r.enabled ? ["", t("disabled")] : r.paused_hours > 0 ? ["bad", t("paused ({time})", { time: dur(r.paused_hours * 3600) })] : r.errors ? ["warn", t(r.errors > 1 ? "{n} errors" : "{n} error", { n: r.errors })] : ["", r.offers ? t("works") : t("no links")];
      return `<div class="shop" style="--i:${i}"><h4><span class="dotst ${state[0]}"></span>${esc(r.label)}</h4><div class="muted" style="font-size:13px">${state[1]}${r.paused_hours > 0 ? ` <button class="btn ghost sm" data-resume="${esc(rid)}">▶ ${t("Resume")}</button>` : ""}</div>
        <div class="meter"><i style="width:${Math.round(ratio * 100)}%"></i></div>
        <div class="kv"><span>${t("Links")}</span><b>${r.offers}</b></div><div class="kv"><span>${t("With a price")}</span><b>${r.ok}</b></div><div class="kv"><span>${t("Cheapest for")}</span><b>${t("{n} sets", { n: r.cheapest })}</b></div><div class="kv"><span>${t("Last success")}</span><b>${ago(r.last_ok)}</b></div>
        ${r.errors ? `<a class="muted" style="font-size:12px;cursor:pointer" data-shoplog="${esc(rid)}">📜 ${t("Show failed checks")} →</a>` : ""}</div>`;
    }).join("");
    const failing = Object.values(st).flatMap((r) => r.failing.map((f) => ({ ...f, shop: r.label })));
    const d = this.state.data, last = d.last;
    const lastTxt = last ? `${esc(tx(last.label))}: ${last.cancelled ? t("stopped") : t("done")} ${ago(last.finished)} · ${last.done}/${last.total}${last.updated ? ` · ${t("{n} updated", { n: last.updated })}` : ""}${last.found ? ` · ${t("{n} found", { n: last.found })}` : ""}${last.errors ? ` · ${t("{n} errors", { n: last.errors })}` : ""}` : t("no job run since the last restart");
    const pausedTxt = Object.entries(d.paused || {}).map(([k, h]) => `${esc(k)} (${dur(h * 3600)})`).join(", ");
    const actions = `<div class="panel"><h3>⚡ ${t("Actions")}</h3><div class="actions">
      <div class="action"><b>🔎 ${t("Find missing shop links")}</b><p>${t("Searches every shop for a product page of each set without a link. Every title found is checked for the set number, accessories and knock-off brands.")}</p><button class="btn" data-act="discover">${t("Find links")}</button></div>
      <div class="action"><b>↻ ${t("Refresh shop prices")}</b><p>${t("Fetches the price for every linked page. Shops are queried in parallel, each shop calmly one after another.")}</p><button class="btn" data-act="refresh">${t("Refresh prices")}</button></div>
      <div class="action"><b>ℹ️ ${t("Fill in set data")}</b><p>${t("First gets the RRP, image and name from LEGO.com, then fills in theme, year and pieces via Brickset/Rebrickable (keys under Settings) or the public Brickset page. Replaces names that came from a wrong product; values you entered yourself are kept.")}</p><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:auto"><button class="btn" data-act="enrich">${t("Fill in")}</button><button class="btn ghost" data-act="enrich" data-all="1" title="${t("Also sets that already look complete")}">${t("Everything again")}</button></div></div>
      <div class="action"><b>🔗 ${t("Check links")}</b><p>${t("Judges all links again and shows suspicious or unchecked links, so you can approve or replace them.")}</p><button class="btn" data-goto="manage/links">${t("Go to link check")}</button></div></div></div>
      <div class="panel"><h3>⏰ ${t("Automatic checks")}</h3><p>${this.scheduleLine()} <a data-goto="manage/settings" style="cursor:pointer">${t("Change under Settings")} →</a></p>
      <p><b>${t("Last job:")}</b> ${lastTxt}</p>${pausedTxt ? `<p><b>${t("Paused after a block:")}</b> ${pausedTxt}. ${t("With the buttons you can choose to try anyway.")} <button class="btn ghost sm" data-resume="">▶ ${t("Lift all pauses")}</button></p>` : ""}</div>`;
    return `${actions}<div class="panel"><h3>🏪 ${t("Shop status")}<span class="hsp"></span><span class="pill">${esc(this.state.data.transport)}</span></h3><p>${t("After a block a shop is paused automatically for a while (1 → 3 → 6 → 12 → 24 h) so the protection doesn't get stricter.")}</p></div>
      <div class="shops">${cards}</div>
      <div class="panel" style="margin-top:16px"><h3>⚠️ ${t("Offers without a price")} <span class="muted" style="font-weight:400">${failing.length}</span><span class="hsp"></span>${failing.length ? `<a class="btn ghost sm" data-goto="log/errors">${t("Fix in the logbook")} →</a>` : ""}</h3>${failing.length ? `<div class="tscroll"><table class="tbl"><tr><th>Set</th><th>${t("Shop")}</th><th>${t("Message")}</th></tr>${failing.map((f) => `<tr class="click" data-set="${esc(f.set_number)}"><td><b>${esc(f.set_number)}</b> ${esc(f.name || "")}</td><td>${esc(f.shop)}</td><td class="err">${esc(tx(f.error))}</td></tr>`).join("")}</table></div>` : `<p class="ok">${t("All OK.")}</p>`}</div>
      <div class="panel"><h3>🧩 ${t("Does a shop keep blocking?")}</h3><p>1. ${t("Enter the price by hand: click a set → Shops → price field. A manual price always wins.")}<br>2. ${t("Install the {link} in Tampermonkey: your own browser sends the price when you visit a product page.", { link: `<a data-goto="manage/userscript" style="cursor:pointer">${t("userscript")}</a>` })}<br>3. ${t("Or switch off “pause automatically” per shop under {link}.", { link: `<a data-goto="manage/settings" style="cursor:pointer">${t("Settings")}</a>` })}<br>4. ${t("Use the action {action} from an automation or n8n.", { action: "<code>lego_tracker.report_price</code>" })}</p></div>`;
  }
  vBackup() {
    return `<div class="two"><div class="panel"><h3>⬇ ${t("Export")}</h3><p>${t("Full backup (sets, shop links, price history, collection, timeline) as JSON, or only your collection as CSV (can be imported again).")}</p><button class="btn" id="expjson">⬇ ${t("Backup (JSON)")}</button> <button class="btn ghost" id="expcsv">⬇ ${t("Collection (CSV)")}</button></div>
      <div class="panel"><h3>⬆ ${t("Restore")}</h3><p>${t("The backup is fully checked first (structure, set numbers, URLs, price history) before anything changes.")}</p><label class="drop" id="jdrop" style="padding:18px"><span class="big" style="font-size:26px">🗂️</span><span id="jname">${t("Choose or drop a backup file (.json)")}</span><input type="file" id="jsonfile" accept=".json,application/json" hidden></label>
      <label class="chk" style="display:flex;gap:8px;align-items:center;margin:10px 0"><input type="checkbox" id="merge" checked> ${t("merge with the current data (otherwise: replace everything)")}</label><button class="btn" id="impjson" disabled>${t("Restore")}</button></div></div>`;
  }

  // ---------------------------------------------------------------- events
  bindCards(root) {
    root.querySelectorAll("[data-set]").forEach((el) => {
      el.addEventListener("click", (e) => { if (e.target.closest("[data-stop]")) return; this.openSet(el.dataset.set); });
      el.addEventListener("keydown", (e) => { if (e.key === "Enter") this.openSet(el.dataset.set); });
    });
  }
  bindContent(root) {
    const s = this.state, $ = (id) => root.querySelector("#" + id), on = (sel, ev, fn) => root.querySelectorAll(sel).forEach((e) => e.addEventListener(ev, fn));
    this.bindCards(root);
    this.shadowRoot.querySelectorAll("[data-goto]").forEach((el) => { el.onclick = () => { const [a, b] = el.dataset.goto.split("/"); s.section = a; s.sub[a] = b; if (a === "log") s.logv.data = null; this.closeDialog(true); this.resetFilters(); this.persist(); this.render(true); }; });
    // filters
    const q = $("q"); if (q) q.addEventListener("input", (e) => { s.f.q = e.target.value; this.renderResults(); });
    const so = $("sort"); if (so) so.addEventListener("change", (e) => { s.f.sort = e.target.value; this.renderResults(); });
    on("[data-theme]", "click", (e) => { s.f.theme = e.currentTarget.dataset.theme || null; s.f.subtheme = null; this.renderContent(); });
    on("[data-subtheme]", "click", (e) => { s.f.subtheme = e.currentTarget.dataset.subtheme || null; this.renderContent(); });
    on("[data-cond]", "click", (e) => { s.f.cond = e.currentTarget.dataset.cond || null; this.renderContent(); });
    on("[data-cview]", "click", (e) => { s.cview = e.currentTarget.dataset.cview; this.persist(); this.renderContent(); });
    const thr = $("thr"); if (thr) { thr.addEventListener("input", (e) => { const v = $("thrv"); if (v) v.textContent = e.target.value + "%"; }); thr.addEventListener("change", (e) => { s.threshold = +e.target.value; this.render(); }); }
    // add
    on("input[name=mode]", "change", (e) => { s.addMode = e.target.value; this.renderContent(); });
    const num = $("a_num"); if (num) num.addEventListener("input", () => {
      const n = (num.value.match(/\d{3,7}/) || [""])[0], hint = $("a_hint"), ex = this.sets.find((x) => x.set_number === n);
      hint.innerHTML = !num.value ? "" : !n ? `<span class="err">${t("A set number has 3–7 digits")}</span>` : ex ? `<span style="color:#9a6a00">${t(ex.owned ? "Already tracked and in your collection" : "Already tracked")}: ${esc(ex.name || "")} – ${t("its data will be updated")}</span>` : `<span class="ok">✓ ${t("new")}</span>`;
    });
    const add = $("add"); if (add) add.addEventListener("click", () => {
      const v = (id) => (root.querySelector("#" + id) || {}).value?.trim?.() || "";
      const n = (v("a_num").match(/\d{3,7}/) || [""])[0]; if (!n) return this.toast(t("Enter a valid set number (3–7 digits)"), "err");
      const d = { set_number: n, owned: s.addMode === "own" };
      for (const [k, id] of [["name", "a_name"], ["theme", "a_theme"], ["subtheme", "a_sub"], ["purchase_date", "a_date"]]) if (v(id)) d[k] = v(id);
      for (const [k, id] of [["rrp", "a_rrp"], ["pieces", "a_pcs"], ["paid", "a_paid"], ["target_price", "a_target"]]) if (v(id)) { const x = +v(id); if (!(x >= 0)) return this.toast(t("{field}: invalid number", { field: k }), "err"); d[k] = x; }
      if (v("a_qty")) d.quantity = Math.max(1, +v("a_qty") || 1);
      this.busy(add, t("Adding and finding shops…"), async () => {
        await this.svc("add_set", d);
        const extra = {}; if (v("a_prio") && +v("a_prio")) extra.priority = +v("a_prio"); if (v("a_cond")) extra.condition = v("a_cond"); if (v("a_loc")) extra.location = v("a_loc");
        if (Object.keys(extra).length) await this._hass.callWS({ type: "lego_tracker/update_set", set_number: n, fields: extra });
        await this.load(); this.toast(t("Set {number} added", { number: n }), "ok"); this.openSet(n);
      });
    });
    const bulk = $("bulk"); if (bulk) bulk.addEventListener("input", () => { const ns = [...new Set(bulk.value.match(/\d{3,7}/g) || [])], known = ns.filter((n) => this.sets.some((x) => x.set_number === n)).length; $("bulk_hint").textContent = ns.length ? t(ns.length > 1 ? "{n} set numbers recognised" : "{n} set number recognised", { n: ns.length }) + (known ? ", " + t("{n} already tracked", { n: known }) : "") : ""; });
    const ba = $("bulkadd"); if (ba) ba.addEventListener("click", () => { if (!bulk.value.trim()) return; this.busy(ba, t("Working… (finding shops takes a moment)"), async () => { const r = await this.svc("add_sets", { set_numbers: bulk.value, owned: $("bulk_owned").checked }, true); await this.load(); this.toast(t("{n} sets added", { n: r.response.added }), "ok"); }); });
    const dc = $("discover"); if (dc) dc.addEventListener("click", () => this.startJob("discover", dc));
    const of = $("offer"); if (of) of.addEventListener("click", () => this.busy(of, t("Linking…"), async () => { await this._hass.callWS({ type: "lego_tracker/offer/update", set_number: $("o_num").value.trim(), retailer: $("o_ret").value, url: $("o_url").value.trim() }); await this.load(); this.toast(t("Link saved. Refresh to fetch the price."), "ok"); }));
    // header-style action buttons inside content (Shops & jobs)
    root.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => this.startJob(b.dataset.act, b)));
    // shop pause: resume one ("") = all
    root.querySelectorAll("[data-resume]").forEach((b) => b.addEventListener("click", () => this.busy(b, "…", async () => {
      const rid = b.dataset.resume;
      const info = await this._hass.callWS({ type: "lego_tracker/shop_action", action: rid ? "resume" : "resume_all", ...(rid ? { retailer: rid } : {}) });
      Object.assign(this.state.data, { paused: info.paused }); this.state.settings = null; await this.load(); this.toast(rid ? t("Shop resumed") : t("All pauses lifted"), "ok");
    })));
    root.querySelectorAll("[data-shoplog]").forEach((a) => a.addEventListener("click", () => { Object.assign(s.logv, { level: "", kind: "", retailer: a.dataset.shoplog, source: "", status: "fail", set: "", q: "", data: null, open: null }); s.section = "log"; s.sub.log = "checks"; this.persist(); this.render(true); }));
    // settings, errors, notifications
    if ($("o_save")) this.bindSettings(root, $);
    if ($("e_scope")) this.bindErrors(root, $);
    if ($("lg_reload")) this.bindLog(root, $);
    if (root.querySelector("[data-nf]")) this.bindNotify(root, $);
    // link check
    const L = s.links;
    on("[data-lstatus]", "click", (e) => { L.status = e.currentTarget.dataset.lstatus; L.edit = null; this.renderContent(); });
    on("[data-lscope]", "click", (e) => { L.scope = e.currentTarget.dataset.lscope; L.edit = null; this.renderContent(); });
    const lv = $("lverify"); if (lv) lv.addEventListener("click", () => this.busy(lv, "…", async () => { const r = (await this.svc("verify_links", {}, true)).response; await this.load(); this.toast(t("{suspect} suspicious, {ok} OK, {unknown} cannot be judged yet", { suspect: r.suspect, ok: r.ok + r.confirmed, unknown: r.unknown }), "ok"); }));
    root.querySelectorAll("tr[data-key]").forEach((tr) => {
      const [num, rid] = tr.dataset.key.split("|"), b = (c) => tr.querySelector("." + c);
      if (b("lok")) b("lok").onclick = () => this.busy(b("lok"), "", async () => { await this.svc("confirm_offer", { set_number: num, retailer: rid }); await this.load(); this.toast(t("Link of {number} approved", { number: num }), "ok"); });
      if (b("lrm")) b("lrm").onclick = () => { if (!confirm(t("Remove the link of set {number}? It will not be linked automatically again.", { number: num }))) return; this.busy(b("lrm"), "", async () => { await this.svc("remove_offer", { set_number: num, retailer: rid }); await this.load(); this.toast(t("Link removed"), "ok"); }); };
      if (b("ledit")) b("ledit").onclick = () => { L.edit = tr.dataset.key; this.renderContent(); const i = this.shadowRoot.querySelector(".lnew"); if (i) i.focus(); };
      if (b("lcancel")) b("lcancel").onclick = () => { L.edit = null; this.renderContent(); };
      if (b("lsave")) b("lsave").onclick = () => { const url = b("lnew").value.trim(); if (!url) return this.toast(t("Enter a URL or ASIN"), "err"); this.busy(b("lsave"), "", async () => { await this._hass.callWS({ type: "lego_tracker/offer/update", set_number: num, retailer: rid, url }); L.edit = null; await this.load(); this.toast(t("Link replaced and approved"), "ok"); }); };
      const inp = b("lnew"); if (inp) inp.addEventListener("keydown", (e) => { if (e.key === "Enter") b("lsave").click(); if (e.key === "Escape") b("lcancel").click(); });
    });
    // import wizard
    const imp = s.imp;
    const readFile = async (f) => { if (!f) return; if (f.size > 2_000_000) return this.toast(t("File too large (max {mb} MB).", { mb: 2 }), "err"); imp.text = await f.text(); imp.name = f.name; await analyze(); };
    const analyze = async () => { if (!imp.text.trim()) return this.toast(t("Choose a file or paste CSV text"), "err"); try { imp.analysis = await this._hass.callWS({ type: "lego_tracker/import_preview", csv_text: imp.text, replace: imp.replace }); this.renderContent(true); } catch (e) { this.toast(tx(e.message), "err"); } };
    const drop = $("drop"); if (drop) {
      ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
      drop.addEventListener("drop", (e) => readFile(e.dataTransfer.files[0]));
      $("csvfile").addEventListener("change", (e) => readFile(e.target.files[0]));
    }
    const an = $("analyze"); if (an) an.addEventListener("click", () => { imp.text = $("csvtext").value; imp.name = ""; this.busy(an, t("Checking…"), analyze); });
    const back = $("impback"); if (back) back.addEventListener("click", () => { imp.analysis = null; this.renderContent(true); });
    const only = $("imp_only"); if (only) only.addEventListener("change", (e) => { imp.only = e.target.checked; this.renderContent(); });
    const rep = $("imp_replace"); if (rep) rep.addEventListener("change", async (e) => { imp.replace = e.target.checked; await analyze(); });
    const upd = $("imp_update"); if (upd) upd.addEventListener("change", (e) => { imp.update = e.target.checked; });
    const go = $("impgo"); if (go) go.addEventListener("click", () => {
      if (imp.replace && !confirm(t("Your current collection will be completely replaced by this file. Continue?"))) return;
      this.busy(go, t("Importing…"), async () => {
        const r = (await this.svc("import_collection", { csv_text: imp.text, replace: imp.replace, track_prices: imp.update, update_after: imp.update }, true)).response;
        s.imp = { text: "", name: "", analysis: null, only: false, replace: false, track: true, update: true };
        await this.load(); this.toast(t("Import done: {added} new, {updated} updated", { added: r.added, updated: r.updated }) + (r.skipped ? ", " + t("{n} skipped", { n: r.skipped }) : ""), "ok");
        s.section = "collection"; s.sub.collection = "sets"; this.persist(); this.render(true);
      });
    });
    // backup
    const dl = (name, text, mime) => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: mime })); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000); };
    const ec = $("expcsv"); if (ec) ec.addEventListener("click", () => this.busy(ec, "…", async () => { const x = await this.svc("export_collection", {}, true); dl("lego_collection.csv", x.response.csv, "text/csv;charset=utf-8"); }));
    const ej = $("expjson"); if (ej) ej.addEventListener("click", () => this.busy(ej, "…", async () => { const x = await this.svc("export_data", {}, true); dl(`lego_backup_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(x.response, null, 1), "application/json"); this.toast(t("Backup downloaded"), "ok"); }));
    const jd = $("jdrop"); if (jd) {
      let payload = null;
      const pick = async (f) => { if (!f) return; try { payload = JSON.parse(await f.text()); $("jname").textContent = `${f.name} · ${Object.keys(payload.sets || {}).length} sets`; $("impjson").disabled = false; } catch (e) { payload = null; $("impjson").disabled = true; this.toast(t("Not a valid JSON file"), "err"); } };
      ["dragenter", "dragover"].forEach((ev) => jd.addEventListener(ev, (e) => { e.preventDefault(); jd.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => jd.addEventListener(ev, (e) => { e.preventDefault(); jd.classList.remove("over"); }));
      jd.addEventListener("drop", (e) => pick(e.dataTransfer.files[0])); $("jsonfile").addEventListener("change", (e) => pick(e.target.files[0]));
      const ij = $("impjson"); ij.addEventListener("click", () => {
        const merge = $("merge").checked; if (!merge && !confirm(t("All current data will be replaced. Continue?"))) return;
        this.busy(ij, t("Checking and restoring…"), async () => { const r = await this.svc("import_data", { data: payload, merge }, true); await this.load(); this.toast(t("Backup restored: {sets} sets, {coll} in collection", { sets: r.response.sets, coll: r.response.collection }), "ok"); });
      });
    }
  }
  bindSettings(root, $) {
    const d = this.state.draft, st = this.state.settings;
    const syncShops = () => root.querySelectorAll("tr[data-shop]").forEach((tr) => {
      const x = d.shops.find((y) => y.id === tr.dataset.shop); if (!x) return;
      x.enabled = tr.querySelector(".s_on").checked; x.autopause = tr.querySelector(".s_ap").checked;
      const se = tr.querySelector(".s_search"); if (se) { x.search = se.value.trim() || x.default_search || ""; const c = d.custom.find((y) => y.id === x.id); if (c) c.search = x.search; }
    });
    const radios = (name) => root.querySelectorAll(`input[name=${name}]`).forEach((r) => r.addEventListener("change", () => r.closest(".radio").querySelectorAll("label").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked))));
    radios("vsrc"); radios("rmode");
    root.querySelectorAll("input[name=rmode]").forEach((r) => r.addEventListener("change", () => { d.mode = r.value; $("rm_spread").hidden = r.value !== "spread"; $("rm_times").hidden = r.value !== "times"; }));
    $("o_spread").addEventListener("input", (e) => { const h = +e.target.value, n = (this.state.data.schedule || {}).total || this.sets.length; $("o_spread_hint").textContent = h >= 1 ? t("≈ {n} sets per hour", { n: (Math.round((n / h) * 10) / 10).toLocaleString(LOC) }) : ""; });
    // search URLs: paste a result page, the set number becomes {query}; live example link
    root.querySelectorAll(".s_search").forEach((i) => {
      const tr = i.closest("tr"), x = d.shops.find((y) => y.id === tr.dataset.shop), prev = tr.querySelector(".s_prev"), reset = tr.querySelector(".s_reset");
      const upd = () => { prev.innerHTML = this.searchPreview(i.value.trim() || x.default_search, $("o_locale").value.trim()); if (reset) reset.hidden = !x.default_search || (i.value.trim() || x.default_search) === x.default_search; };
      i.addEventListener("input", upd);
      i.addEventListener("change", () => { const v = this.toTemplate(i.value); if (v !== i.value.trim()) { i.value = v; this.toast(t("Set number replaced by {query}", { query: "{query}" }), "ok"); } upd(); });
    });
    root.querySelectorAll(".s_reset").forEach((b) => b.addEventListener("click", () => { const tr = b.closest("tr"), i = tr.querySelector(".s_search"), x = d.shops.find((y) => y.id === tr.dataset.shop); i.value = x.default_search; x.search = x.default_search; i.dispatchEvent(new Event("input")); }));
    root.querySelectorAll(".s_del").forEach((b) => b.addEventListener("click", () => { syncShops(); const id = b.closest("tr").dataset.shop; d.shops = d.shops.filter((x) => x.id !== id); d.custom = d.custom.filter((x) => x.id !== id); this.renderContent(); this.toast(t("Shop removed from the list; press Save to confirm")); }));
    const ns = $("n_search"); ns.addEventListener("change", () => { ns.value = this.toTemplate(ns.value); $("n_search_prev").innerHTML = this.searchPreview(ns.value.trim(), $("o_locale").value.trim()); });
    ns.addEventListener("input", () => { $("n_search_prev").innerHTML = this.searchPreview(this.toTemplate(ns.value), $("o_locale").value.trim()); });
    $("n_add").addEventListener("click", () => {
      syncShops();
      const name = $("n_name").value.trim(), domain = $("n_domain").value.trim().replace(/^https?:\/\/(www\.)?/, "").split("/")[0].toLowerCase(), search = this.toTemplate($("n_search").value);
      if (!name || !/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(domain)) return this.toast(t("Enter a name and a valid domain (e.g. intertoys.be)"), "err");
      if (search && (!search.startsWith("https://") || !/\{(query|number)\}/.test(search) || !search.includes(domain))) return this.toast(t("Search URL: https://, on the shop's domain and with {query}", { query: "{query}" }), "err");
      const id = "c_" + (name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || "shop").slice(0, 30);
      if (d.shops.some((x) => x.id === id)) return this.toast(t("This shop is already in the list"), "err");
      d.custom.push({ id, name, domain, search }); d.shops.push({ id, label: name, builtin: false, generic: true, domain, search, enabled: true, autopause: true, paused_hours: 0, blocks: 0 });
      this.renderContent(); this.toast(t("{name} added; press Save", { name }), "ok");
    });
    root.querySelectorAll("[data-testkey]").forEach((b) => b.addEventListener("click", () => this.busy(b, t("Testing…"), async () => {
      const k = b.dataset.testkey, key = $("k_" + k).value.trim(), out = $("kres_" + k);
      const r = await this._hass.callWS({ type: "lego_tracker/settings/test_key", source: k.split("_")[0], ...(key ? { key } : {}) });
      out.innerHTML = r.ok ? `<b class="ok">✓ ${esc(tx(r.message))}</b>` : `<b class="err">✕ ${esc(tx(r.message))}</b>`;
    })));
    root.querySelectorAll("[data-clearkey]").forEach((b) => b.addEventListener("click", () => { $("k_" + b.dataset.clearkey).value = ""; $("k_" + b.dataset.clearkey).dataset.clear = "1"; this.toast(t("The key is cleared when you save")); }));
    $("o_save").addEventListener("click", () => {
      syncShops();
      const f = {
        discount_threshold: +$("o_thr").value, min_history_days: +$("o_hist").value, notify_service: $("o_notify").value.trim(),
        digest_time: $("o_digest").value, refresh_mode: d.mode, spread_hours: +$("o_spread").value, refresh_times: $("o_times").value,
        value_source: (root.querySelector("input[name=vsrc]:checked") || {}).value || "shop_first", use_impersonation: $("o_imp").checked,
        retailers: d.shops.filter((x) => x.enabled).map((x) => x.id), no_autopause: d.shops.filter((x) => !x.autopause).map((x) => x.id),
        custom_shops: d.custom, shop_search: Object.fromEntries(d.shops.filter((x) => x.builtin).map((x) => [x.id, x.search === x.default_search ? "" : this.toTemplate(x.search || "")])),
        lego_locale: ($("o_locale").value || "nl-be").trim(), language: $("o_lang").value,
      };
      for (const k of ["brickset_api_key", "rebrickable_api_key"]) { const el = $("k_" + k), v = el.value.trim(); if (v) f[k] = v; else if (el.dataset.clear) f[k] = ""; }
      if (!f.retailers.length) return this.toast(t("Switch on at least one shop"), "err");
      if (d.mode === "spread" && !(f.spread_hours >= 1 && f.spread_hours <= 168)) return this.toast(t("Cycle: between 1 and 168 hours"), "err");
      const langChanged = f.language !== st.language;
      this.busy($("o_save"), t("Saving…"), async () => {
        await this._hass.callWS({ type: "lego_tracker/settings/set", fields: f });
        await new Promise((r) => setTimeout(r, 1500));   // entry reloads
        this.state.settings = null; this.state.logv.data = null; await this.load(langChanged);
        this.toast(t("Settings saved"), "ok");
      });
    });
  }
  hookCharts(root) {
    root.querySelectorAll("svg.chart").forEach((svg) => {
      const m = CHARTS[svg.dataset.id]; if (!m) return;
      const tip = svg.parentElement.querySelector(".tip"), hv = svg.querySelector(".hv");
      const move = (clientX) => {
        const box = svg.getBoundingClientRect(), x = ((clientX - box.left) / box.width) * m.W;
        if (x < m.P.l || x > m.W - m.P.r) return;
        const t = m.x0 + ((x - m.P.l) / (m.W - m.P.l - m.P.r)) * (m.x1 - m.x0);
        const rows = m.series.map((s) => { const p = lastAt(s.points, t); return p ? `<div><i style="background:${s.color}"></i>${esc(s.name)}: <b>${m.money ? EUR(p[1]) : p[1]}</b></div>` : ""; }).join("");
        hv.setAttribute("x1", m.sx(t)); hv.setAttribute("x2", m.sx(t)); hv.style.display = "";
        tip.innerHTML = `<div class="muted" style="margin-bottom:3px">${DATE(t, { day: "numeric", month: "short", year: "numeric" })}</div>${rows}`;
        tip.style.display = ""; tip.style.left = Math.min(box.width - 200, Math.max(40, clientX - box.left + 14)) + "px";
      };
      svg.addEventListener("mousemove", (e) => move(e.clientX));
      svg.addEventListener("touchmove", (e) => move(e.touches[0].clientX), { passive: true });
      svg.addEventListener("mouseleave", () => { tip.style.display = "none"; hv.style.display = "none"; });
    });
  }

  // ---------------------------------------------------------------- set dialog
  closeDialog(instant = false) { const d = this.shadowRoot.getElementById("dlg"); if (!d || !d.open) return; if (instant || REDUCED) { d.close(); return; } d.classList.add("closing"); setTimeout(() => { d.classList.remove("closing"); d.close(); }, 170); }
  async openSet(num, focusShop = null) {
    const dlg = this.shadowRoot.getElementById("dlg");
    if (!dlg.open) { dlg.innerHTML = `<div class="dbody"><div class="skel" style="height:110px;margin-bottom:12px"></div><div class="skel" style="height:240px"></div></div>`; dlg.showModal(); }
    let s; try { s = await this._hass.callWS({ type: "lego_tracker/set", set_number: num }); } catch (e) { dlg.innerHTML = `<div class="dbody"><div class="empty">${esc(tx(e.message))}</div><br><button class="btn" id="x2">${t("Close")}</button></div>`; dlg.querySelector("#x2").onclick = () => this.closeDialog(); return; }
    const scroll = dlg.scrollTop;
    const days = this.state.range, cut = days ? Date.now() / 1000 - days * 86400 : 0;
    const win = (h) => { if (!cut) return h; const keep = h.filter((p) => p[0] >= cut), prev = lastAt(h, cut); return prev && (!keep.length || keep[0][0] > cut) ? [[cut, prev[1]], ...keep] : keep; };
    const series = Object.entries(s.history).filter(([, h]) => h.length).map(([rid, h], i) => ({ name: s.offers[rid]?.label || rid, color: COLORS[i % COLORS.length], points: win(h) })).filter((x) => x.points.length);
    const allT = series.flatMap((x) => x.points.map((p) => p[0]));
    if (s.rrp && allT.length) series.push({ name: t("RRP"), color: "#9aa0a6", dashed: true, points: [[Math.min(...allT), s.rrp], [Math.max(...allT), s.rrp]] });
    if (s.target_price && allT.length) series.push({ name: t("Target price"), color: "#7a3c9e", dashed: true, points: [[Math.min(...allT), s.target_price], [Math.max(...allT), s.target_price]] });
    // every shop with a link, plus the enabled shops without one (so a link can be added by hand)
    const stats = this.state.data.retailer_stats || {}, retailers = this.state.data.retailers;
    const rids = Object.keys(retailers).filter((rid) => s.offers[rid] || (stats[rid] && stats[rid].enabled));
    const cheapest = Math.min(...Object.values(s.offers).map((o) => o.price ?? Infinity));
    const lk = (o) => { const st = o.link_status || "unknown"; return `<span class="lk ${st}" title="${esc(tx(o.link_reason || ""))}">${{ ok: "✓ " + t("correct"), confirmed: "✓ " + t("approved"), suspect: "⚠ " + t("suspicious"), unknown: "? " + t("not checked") }[st] || st}</span>`; };
    const man = `<span class="mbadge" title="${t("Entered by hand: always wins over automatic values")}">✎ ${t("manual")}</span>`, auto = `<span class="abadge">${t("auto")}</span>`;
    const orows = rids.map((rid) => {
      const o = s.offers[rid];
      if (!o) return `<tr class="orow" data-rid="${rid}"><td><b>${esc(retailers[rid])}</b><div class="muted" style="font-size:12px">${t("no link yet")}</div></td>
        <td><input class="ou" data-orig="" placeholder="${t("paste the product URL or ASIN")}"></td><td class="num"><input class="op" type="number" min="0" step="0.01" data-orig="" placeholder="€" disabled title="${t("Add a link first")}"></td><td></td></tr>`;
      const autoP = o.manual_price != null ? o.auto_price : o.price;
      return `<tr class="orow" data-rid="${rid}"><td><b>${esc(o.label)}</b> ${lk(o)}${o.title ? `<div class="muted" style="font-size:12px;max-width:240px">${esc(o.title.slice(0, 90))}</div>` : ""}${o.link_status === "suspect" ? `<div class="err">${esc(tx(o.link_reason || ""))}</div>` : ""}${o.error ? `<div class="err">${esc(tx(o.error))}</div>` : ""}</td>
        <td><div style="display:flex;gap:6px;align-items:center">${o.manual_url ? man : auto}${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer" data-stop style="font-size:12px;white-space:nowrap">${t("open")} ↗</a>` : ""}</div><input class="ou" value="${esc(o.url || "")}" data-orig="${esc(o.url || "")}" placeholder="${t("empty = search automatically")}" style="margin-top:4px"></td>
        <td class="num"><div>${o.manual_price != null ? man : auto} ${o.price != null ? `<b class="${o.price === cheapest ? "ok" : ""}">${EUR(o.price)}</b>` : "–"}</div>
          <input class="op" type="number" min="0" step="0.01" value="${o.manual_price ?? ""}" data-orig="${o.manual_price ?? ""}" placeholder="${autoP != null ? EUR(autoP) : t("auto")}" title="${t("empty = automatic price")}" style="margin-top:4px">
          <div class="muted" style="font-size:11px;margin-top:2px">${o.manual_price != null ? t("shop: {price}", { price: EUR(autoP) }) + " · " : ""}${t("low {price}", { price: EUR(o.low) })} · ${ago(o.checked)}</div></td>
        <td style="white-space:nowrap">${o.link_status !== "confirmed" ? `<button class="btn ghost sm okb" data-rid="${rid}" title="${t("This link is the right set")}">✓</button> ` : ""}<button class="btn ghost sm rmb" data-rid="${rid}" title="${t("Remove wrong link and never link it again")}">🗑</button></td></tr>`;
    }).join("");
    const c = s.collection || {};
    const stat = (l, v) => `<div class="stat"><small>${l}</small><b>${v}</b></div>`;
    const src = (k) => (s[`${k}_source`] === "user" ? ` ${man}` : "");
    dlg.innerHTML = `<div class="dhead"><div class="img">${this.img(s, true)}</div><div><h2>${esc(s.name || "Set " + s.set_number)}</h2><div class="muted">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.subtheme ? " / " + esc(s.subtheme) : ""}${s.year ? " · " + s.year : ""}${s.pieces ? " · " + t("{n} pieces", { n: INT(s.pieces) }) : ""}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${this.badges(s)}</div>
        <div style="margin-top:8px;display:flex;align-items:baseline;gap:6px 10px;flex-wrap:wrap"><span style="font-size:26px;font-weight:800">${EUR(s.best_price)}</span>${s.best_retailer ? `<span class="muted">${t("at {shop}", { shop: esc(retailers[s.best_retailer]) })}</span>` : ""}${s.best_url ? `<a class="btn sm" href="${esc(s.best_url)}" target="_blank" rel="noopener noreferrer">${t("Buy")} ↗</a>` : ""}</div></div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:10px"><button class="x" id="x" aria-label="${t("Close")}">✕</button>${s.best_price != null ? ring(s.deal_score, 56) : ""}</div></div>
      <div class="dbody"><div class="chips">${[[30, t("{n} d", { n: 30 })], [90, t("{n} d", { n: 90 })], [365, t("1 year")], [0, t("All")]].map(([d, l]) => `<span class="chip sm ${this.state.range === d ? "on" : ""}" data-range="${d}">${l}</span>`).join("")}</div>
        ${lineChart(series, { area: true })}
        <div class="stats">${stat(t("Lowest ever"), EUR(s.all_time_low))}${stat(t("RRP"), EUR(s.rrp))}${s.owned && c.current_value != null ? stat(t("Value (import)"), EUR(c.current_value)) : ""}${stat(t("Discount"), s.discount_rrp != null ? `${s.discount_rrp > 0 ? "−" : "+"}${Math.abs(s.discount_rrp)}%` : "–")}${stat(t("Per piece"), s.price_per_piece ? t("{n} ct", { n: (s.price_per_piece * 100).toFixed(1) }) : "–")}${stat(t("7 days"), signPct(s.change_7d))}${stat(t("30 days"), signPct(s.change_30d))}${s.retires_in_days != null ? stat(s.retired ? t("Retired since") : t("Retires in"), s.retired ? esc(s.exit_date) : t("{n} d", { n: s.retires_in_days })) : ""}${stat(t("Last check"), ago(s.checked))}</div>
        <h3 style="margin:16px 0 4px;display:flex;align-items:center;gap:8px">🏪 ${t("Shops: links & prices")}<span class="hsp" style="flex:1"></span><button class="btn ghost sm" id="lg" type="button">📜 ${t("Logbook")}</button></h3>
        <p class="muted" style="font-size:12px;margin:0 0 6px">${t("Edit a link or price and press Save. What you enter by hand always wins and is never overwritten; empty a field to hand it back to the automatic search / price.")}</p>
        ${rids.length ? `<div class="tscroll"><table class="tbl otbl"><tr><th>${t("Shop")}</th><th>${t("Link")}</th><th class="num">${t("Price")}</th><th></th></tr>${orows}</table></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap"><button class="btn" id="osave" type="button" disabled>💾 ${t("Save shop changes")}</button><span class="muted" id="odirty" style="font-size:12px"></span></div>` : `<div class="empty small">${t("No shops enabled.")} <a data-goto="manage/settings" style="cursor:pointer">${t("Settings")}</a></div>`}
        <form id="ef" style="margin-top:16px" autocomplete="off">
        <fieldset><legend>👀 ${t("Tracking")}</legend><div class="form"><label>${t("Target price")} (€)<input name="target_price" type="number" min="0" step="0.01" value="${s.target_price ?? ""}"></label>
          <label>${t("Priority")}<select name="priority">${[0, 1, 2, 3].map((p) => `<option value="${p}" ${(s.priority || 0) === p ? "selected" : ""}>${p ? "★".repeat(p) : "–"}</option>`).join("")}</select></label>
          <label>${t("Retires on")}<input name="exit_date" type="date" value="${esc((s.exit_date || "").slice(0, 10))}"></label><label class="chk"><input name="retiring" type="checkbox" ${s.retiring ? "checked" : ""}> ${t("retiring soon")}</label></div>
          <div class="form"><label style="grid-column:1/-1">${t("Notes")}<input name="notes" value="${esc(s.notes || "")}" maxlength="200"></label></div></fieldset>
        <fieldset><legend>🧱 ${t("Set details")}</legend><p class="muted" style="font-size:12px;margin:0 0 8px">${t("Filled in automatically from LEGO.com, Brickset or Rebrickable. Change a value to fix it for good; empty it to fill it in automatically again.")}</p>
          <div class="form"><label style="grid-column:span 2">${t("Name")}${src("name")}<input name="name" value="${esc(s.name || "")}"></label><label>${t("Theme")}${src("theme")}<input name="theme" value="${esc(s.theme || "")}" list="dthemes"></label><label>${t("Subtheme")}<input name="subtheme" value="${esc(s.subtheme || "")}"></label>
          <label>${t("RRP")} (€)${src("rrp")}<input name="rrp" type="number" min="0" step="0.01" value="${s.rrp ?? ""}"></label><label>${t("Pieces")}<input name="pieces" type="number" min="0" value="${s.pieces ?? ""}"></label><label>${t("Year")}<input name="year" type="number" min="1949" max="2100" value="${s.year ?? ""}"></label>
          <label style="grid-column:span 2">${t("Image (https URL)")}${src("image")}<input name="image" value="${esc(s.image || "")}" placeholder="https://…"></label></div></fieldset>
        <fieldset><legend>📦 ${t("Collection")}</legend><div class="form"><label class="chk"><input name="owned" type="checkbox" ${s.owned ? "checked" : ""}> ${t("I own this set")}</label></div>
          <div class="form" id="collf" style="${s.owned ? "" : "opacity:.45;pointer-events:none"}"><label>${t("Quantity")}<input name="qty" type="number" min="1" value="${c.qty ?? 1}"></label><label>${t("Paid (€ each)")}<input name="paid" type="number" min="0" step="0.01" value="${c.paid ?? ""}"></label>
          <label>${t("Purchase date")}<input name="added" type="date" max="${new Date().toISOString().slice(0, 10)}" value="${esc(c.added || "")}"></label><label>${t("Condition")}<select name="condition"><option value="">–</option>${CONDITIONS.map((x) => `<option value="${x}" ${c.condition === x ? "selected" : ""}>${t(x)}</option>`).join("")}</select></label><label>${t("Current value (€, import)")}<input name="current_value" type="number" min="0" step="0.01" value="${c.current_value ?? ""}"></label><label>${t("Location")}<input name="location" value="${esc(c.location || "")}"></label></div></fieldset>
        <datalist id="dthemes">${this.state.data.themes.map((th) => `<option value="${esc(th)}">`).join("")}</datalist>
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="save" type="submit">${t("Save")}</button><button class="btn ghost" id="find" type="button">🔎 ${t("Find shops")}</button><button class="btn ghost" id="rf" type="button">↻ ${t("Fetch prices")}</button><span class="hsp" style="flex:1"></span><button class="btn danger" id="rm" type="button">${t("Delete")}</button></div></form></div>`;
    const q = (id) => dlg.querySelector("#" + id);
    dlg.scrollTop = scroll;
    q("x").onclick = () => this.closeDialog();
    q("lg").onclick = () => this.showLogFor(num);
    dlg.querySelectorAll("[data-goto]").forEach((el) => { el.onclick = () => { const [a, b] = el.dataset.goto.split("/"); this.state.section = a; this.state.sub[a] = b; this.closeDialog(true); this.persist(); this.render(true); }; });
    dlg.querySelectorAll("[data-range]").forEach((ch) => ch.onclick = () => { this.state.range = +ch.dataset.range; this.persist(); this.openSet(num); });
    // ---- shop table: manual links and prices
    const rows = [...dlg.querySelectorAll("tr.orow")];
    const changes = () => rows.map((tr) => {
      const u = tr.querySelector(".ou"), p = tr.querySelector(".op"), ch = {};
      if (u.value.trim() !== u.dataset.orig) ch.url = u.value.trim();
      if (p.value.trim() !== p.dataset.orig) ch.manual_price = p.value.trim() === "" ? null : p.value.trim();
      return [tr, ch];
    }).filter(([, ch]) => Object.keys(ch).length);
    const mark = () => {
      const list = changes(), dirty = new Set(list.map(([tr]) => tr));
      rows.forEach((tr) => { tr.classList.toggle("dirty", dirty.has(tr)); const p = tr.querySelector(".op"); if (p.disabled && tr.querySelector(".ou").value.trim()) { p.disabled = false; p.title = ""; } });
      if (q("osave")) { q("osave").disabled = !list.length; q("odirty").textContent = list.length ? t(list.length === 1 ? "{n} shop changed" : "{n} shops changed", { n: list.length }) : ""; }
    };
    const saveShops = () => {
      const list = changes(); if (!list.length) return;
      for (const [, ch] of list) {
        if (ch.manual_price != null && !(+ch.manual_price > 0 && +ch.manual_price <= 10000)) return this.toast(t("Invalid price"), "err");
      }
      const odd = list.find(([, ch]) => ch.manual_price != null && s.rrp && (+ch.manual_price < s.rrp * 0.2 || +ch.manual_price > s.rrp * 4));
      if (odd && !confirm(t("{price} is very different from the RRP {rrp}. Save anyway?", { price: EUR(+odd[1].manual_price), rrp: EUR(s.rrp) }))) return;
      const cleared = list.filter(([tr, ch]) => ch.url === "" && tr.querySelector(".ou").dataset.orig);
      if (cleared.length && !confirm(t("Remove the link of {shops}? The shop may be searched automatically again.", { shops: cleared.map(([tr]) => retailers[tr.dataset.rid]).join(", ") }))) return;
      this.busy(q("osave"), t("Saving…"), async () => {
        for (const [tr, ch] of list) {
          const msg = { type: "lego_tracker/offer/update", set_number: num, retailer: tr.dataset.rid };
          if ("url" in ch) msg.url = ch.url;
          if ("manual_price" in ch && !(ch.url === "")) msg.manual_price = ch.manual_price === null ? null : +ch.manual_price;
          await this._hass.callWS(msg);
        }
        await this.load(); this.toast(t("Shop links and prices saved"), "ok"); this.openSet(num);
      });
    };
    rows.forEach((tr) => tr.querySelectorAll("input").forEach((i) => { i.addEventListener("input", mark); i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveShops(); } }); }));
    if (q("osave")) q("osave").onclick = saveShops;
    if (focusShop) { const i = dlg.querySelector(`tr.orow[data-rid="${focusShop}"] .ou`); if (i) { i.focus(); i.select(); } }
    // ---- details form: only changed fields are sent, so untouched automatic values stay automatic
    const ef = q("ef");
    const snapshot = () => { const fd = new FormData(ef), o = {}; for (const [k, v] of fd.entries()) o[k] = v.toString().trim(); o.retiring = ef.retiring.checked; o.owned = ef.owned.checked; return o; };
    const orig = snapshot();
    ef.owned.addEventListener("change", () => { const cf = q("collf"); cf.style.opacity = ef.owned.checked ? "" : ".45"; cf.style.pointerEvents = ef.owned.checked ? "" : "none"; });
    ef.addEventListener("submit", (e) => {
      e.preventDefault();
      const now = snapshot(), f = {};
      for (const k of Object.keys(now)) if (now[k] !== orig[k]) f[k] = k === "priority" ? +now[k] : now[k];
      if (now.owned && !s.owned) for (const k of ["qty", "paid", "added", "condition", "location", "current_value"]) if (now[k] !== "") f[k] = now[k];
      if (!Object.keys(f).length && !changes().length) { this.closeDialog(); return; }
      if (f.qty !== undefined && f.qty !== "" && !(+f.qty >= 1)) return this.toast(t("Quantity must be at least 1"), "err");
      if (s.owned && !now.owned && !confirm(t("Remove this set from your collection? (it stays on your watchlist)"))) return;
      if (changes().length && !confirm(t("You also changed shop links or prices. Save those too?"))) return;
      this.busy(q("save"), t("Saving…"), async () => {
        for (const [tr, ch] of changes()) { const msg = { type: "lego_tracker/offer/update", set_number: num, retailer: tr.dataset.rid, ...ch }; if (ch.url === "") delete msg.manual_price; if (msg.manual_price != null) msg.manual_price = +msg.manual_price; await this._hass.callWS(msg); }
        if (Object.keys(f).length) await this._hass.callWS({ type: "lego_tracker/update_set", set_number: num, fields: f });
        await this.load(); this.toast(t("Saved"), "ok"); this.closeDialog();
      });
    });
    const find = async (btn) => this.busy(btn, t("Searching…"), async () => { const r = await this.svc("discover_offers", { set_number: num }, true); await this.load(); this.toast(t("{n} shop links found", { n: r.response.found }), r.response.found ? "ok" : ""); this.openSet(num); });
    q("find").onclick = (e) => find(e.currentTarget);
    q("rf").onclick = (e) => this.busy(e.currentTarget, t("Fetching…"), async () => { await this.svc("refresh", { set_number: num }); await this.load(); this.openSet(num); });
    dlg.querySelectorAll(".okb").forEach((btn) => btn.onclick = () => this.busy(btn, "", async () => { await this.svc("confirm_offer", { set_number: num, retailer: btn.dataset.rid }); await this.load(); this.openSet(num); }));
    dlg.querySelectorAll(".rmb").forEach((btn) => btn.onclick = () => { if (!confirm(t("Remove this link? It will not be linked automatically again."))) return; this.busy(btn, "", async () => { await this.svc("remove_offer", { set_number: num, retailer: btn.dataset.rid }); await this.load(); this.toast(t("Link removed"), "ok"); this.openSet(num); }); });
    q("rm").onclick = () => { if (!confirm(t(s.owned ? "Delete set {number} with its whole price history, and remove it from your collection?" : "Delete set {number} with its whole price history?", { number: num }))) return; this.busy(q("rm"), "", async () => { await this.svc("remove_set", { set_number: num }); this.closeDialog(); await this.load(); this.toast(t("Set {number} deleted", { number: num }), "ok"); }); };
    this.hookCharts(dlg);
  }
}
if (!customElements.get("lego-tracker-panel")) customElements.define("lego-tracker-panel", LegoTrackerPanel);
