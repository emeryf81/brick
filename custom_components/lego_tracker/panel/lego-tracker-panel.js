/* LEGO Price Tracker panel – vanilla web component, no build step.
 * Two halves: "Deals & watchlist" (sets you keep an eye on) and "Mijn collectie" (what you own),
 * plus "Beheer" (adding, validated import, shop health, backups). */

// ------------------------------------------------------------------ helpers
const NF = new Intl.NumberFormat("nl-BE", { style: "currency", currency: "EUR" });
const NF0 = new Intl.NumberFormat("nl-BE", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
const EUR = (v) => (v == null || isNaN(v) ? "–" : NF.format(v));
const EUR0 = (v) => (v == null || isNaN(v) ? "–" : NF0.format(v));
const INT = (v) => (v == null ? "–" : Math.round(v).toLocaleString("nl-BE"));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const signPct = (v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${v.toLocaleString("nl-BE", { maximumFractionDigits: 1 })}%`);
const DATE = (ts, o = { day: "2-digit", month: "short" }) => new Date(ts * 1000).toLocaleDateString("nl-BE", o);
const ago = (ts) => {
  if (!ts) return "nooit";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "zojuist";
  if (s < 3600) return `${Math.round(s / 60)} min geleden`;
  if (s < 86400) return `${Math.round(s / 3600)} u geleden`;
  return `${Math.round(s / 86400)} d geleden`;
};
const COLORS = ["#d01012", "#0057a6", "#00852b", "#f5a800", "#7a3c9e", "#00a3da", "#e76318", "#6c6e68"];
const CONDITIONS = ["Sealed", "Geopend", "Gebouwd", "Incompleet"];
const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const lastAt = (pts, t) => { let r = null; for (const p of pts) { if (p[0] <= t) r = p; else break; } return r; };

// ------------------------------------------------------------------ charts (inline SVG)
const CHARTS = {}; let CHART_N = 0;
function lineChart(series, { height = 240, money = true, area = true } = {}) {
  const W = 760, H = height, P = { l: 56, r: 14, t: 14, b: 26 };
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) return `<div class="empty small">📈 Nog te weinig meetpunten voor een grafiek. Na een paar verversingen verschijnt hier de prijsgeschiedenis.</div>`;
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
  for (let i = 0; i <= 4; i++) {
    const t = x0 + ((x1 - x0) * i) / 4;
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
  if (!total) return `<div class="empty small">Geen gegevens.</div>`;
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
  return `<div class="ring" style="width:${size}px;height:${size}px" title="Dealscore ${v}/100"><svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="${r}" class="ring-bg"/><circle cx="22" cy="22" r="${r}" class="ring-fg" stroke="${col}" stroke-dasharray="${C}" style="--off:${C * (1 - v / 100)};--c:${C}" transform="rotate(-90 22 22)"/></svg><span>${v}</span></div>`;
}

const BRICK = `<svg viewBox="0 0 64 44" class="logo" aria-hidden="true"><rect x="2" y="12" width="60" height="30" rx="4" fill="#d01012"/><rect x="9" y="4" width="14" height="10" rx="3" fill="#d01012"/><rect x="41" y="4" width="14" height="10" rx="3" fill="#d01012"/><rect x="9" y="4" width="14" height="4" rx="2" fill="#ff5a4d" opacity=".7"/><rect x="41" y="4" width="14" height="4" rx="2" fill="#ff5a4d" opacity=".7"/><rect x="2" y="12" width="60" height="6" rx="3" fill="#ff5a4d" opacity=".45"/></svg>`;

// ------------------------------------------------------------------ styles
const STYLE = `
:host{--lt-bg:var(--primary-background-color,#f3f4f7);--lt-card:var(--card-background-color,#fff);--lt-text:var(--primary-text-color,#1b1c20);--lt-muted:var(--secondary-text-color,#6b6f7a);
--lt-line:var(--divider-color,#e1e3e8);--lt-accent:var(--primary-color,#0057a6);--lt-on-accent:var(--text-primary-color,#fff);--lt-soft:color-mix(in srgb,var(--lt-accent) 10%,var(--lt-card));
--lt-red:#d01012;--lt-green:#00852b;--lt-yellow:#f5a800;--lt-purple:#7a3c9e;--lt-radius:16px;--lt-shadow:var(--ha-card-box-shadow,0 1px 2px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.06));
display:block;background:var(--lt-bg);color:var(--lt-text);min-height:100vh;font-family:var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif);-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}
.wrap{max-width:1280px;margin:0 auto;padding:12px 16px 48px}
header{display:flex;align-items:center;gap:12px;padding:6px 0 14px}
.logo{width:40px;height:28px;flex:none;filter:drop-shadow(0 2px 3px rgba(208,16,18,.3))}
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
@media(max-width:640px){.wrap{padding:8px 10px 40px}h1{font-size:17px}header{gap:8px}header .lbl{display:none}.logo{width:32px;height:22px}
.seg{gap:2px;padding:4px}.seg button{font-size:13px;padding:9px 4px}.seg small{display:none}
.kpis{grid-template-columns:1fr 1fr;gap:8px}.kpi{padding:10px 12px}.kpi b{font-size:19px}
.grid{grid-template-columns:1fr 1fr;gap:10px}.card .img{height:96px}.card .img img{max-height:88px}.card .p{font-size:17px}.card .ring{width:34px!important;height:34px!important}
.hero .img{height:130px}.hero .p{font-size:28px}.fbar .search{min-width:100%}.panel{padding:12px}}
@media (prefers-reduced-motion: reduce){*,*::before{animation:none!important;transition:none!important}.ln{stroke-dashoffset:0}}
`;

// ------------------------------------------------------------------ component
const SECTIONS = {
  deals: { label: "🏷️ Deals & watchlist", hint: "sets in het oog houden", subs: [["today", "Vandaag"], ["watch", "Watchlist"], ["all", "Alle prijzen"]] },
  collection: { label: "📦 Mijn collectie", hint: "wat je al hebt", subs: [["overview", "Overzicht"], ["sets", "Sets"]] },
  manage: { label: "⚙️ Beheer", hint: "toevoegen, import, winkels", subs: [["add", "Toevoegen"], ["import", "Importeren"], ["shops", "Winkels"], ["backup", "Back-up"]] },
};

class LegoTrackerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      section: "deals", sub: { deals: "today", collection: "overview", manage: "add" },
      f: { q: "", theme: null, subtheme: null, sort: "score", cond: null }, threshold: null,
      cview: "grid", csort: { key: "value", dir: -1 }, range: 90, addMode: "watch",
      imp: { text: "", name: "", analysis: null, only: false, replace: false, track: true },
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
      this.state.data = d; this.state.coll = c; this.state.err = null;
      if (this.state.threshold == null) this.state.threshold = d.threshold;
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
    try { return await fn(); } catch (e) { this.toast(e.message || String(e), "err"); return undefined; } finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
  }

  // ---------------------------------------------------------------- derived
  get sets() { return this.state.data ? this.state.data.sets : []; }
  isDeal(s) {
    return s.best_price != null && (s.is_all_time_low || s.target_hit || s.deal_score >= 70 || (s.discount_rrp != null && s.discount_rrp >= this.state.threshold));
  }
  filtered(list) {
    const { q, theme, subtheme, cond } = this.state.f;
    let out = list.filter((s) => (!theme || s.theme === theme) && (!subtheme || s.subtheme === subtheme) && (!cond || (s.collection && (s.collection.condition || "Onbekend") === cond)));
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
    root.innerHTML = `<style>${STYLE}</style><div class="wrap">
      <header>${this._narrow ? `<ha-menu-button></ha-menu-button>` : ""}${BRICK}<div><h1>LEGO Price Tracker</h1><div class="meta">${d ? `v${esc(d.version)} · ${d.sets.length} sets gevolgd` : "laden…"}</div></div>
        <div class="hsp"></div>${status}<button class="btn ghost sm" id="refresh" title="Alle prijzen nu ophalen">↻ <span class="lbl">Verversen</span></button></header>
      <nav class="seg" role="tablist">${Object.entries(SECTIONS).map(([k, v]) => `<button role="tab" data-sec="${k}" class="${k === s.section ? "on" : ""}">${v.label}<small>${v.hint}</small></button>`).join("")}<span class="ind"></span></nav>
      <div class="sub">${sec.subs.map(([k, l]) => `<button data-sub="${k}" class="${k === sub ? "on" : ""}">${l}${this.subCount(s.section, k)}</button>`).join("")}</div>
      <div id="content"></div></div><div class="toasts"></div><dialog id="dlg"></dialog>`;
    const menu = root.querySelector("ha-menu-button"); if (menu) { menu.hass = this._hass; menu.narrow = this._narrow; }
    this.placeIndicator(false);
    root.querySelectorAll("[data-sec]").forEach((b) => b.addEventListener("click", () => { if (s.section === b.dataset.sec) return; s.section = b.dataset.sec; this.resetFilters(); this.persist(); this.render(true); }));
    root.querySelectorAll(".sub [data-sub]").forEach((b) => b.addEventListener("click", () => { s.sub[s.section] = b.dataset.sub; this.resetFilters(); this.persist(); this.render(true); }));
    root.getElementById("refresh").addEventListener("click", (e) => this.busy(e.currentTarget, "Bezig…", async () => { await this.svc("refresh"); await this.load(); this.toast("Prijzen ververst", "ok"); }));
    const dlg = root.getElementById("dlg");
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); this.closeDialog(); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) this.closeDialog(); });
    if (!this._keys) { this._keys = true; this.addEventListener("keydown", (e) => { if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test((e.composedPath()[0] || {}).tagName || "")) { const q = this.shadowRoot.getElementById("q"); if (q) { e.preventDefault(); q.focus(); } } }); }
    this.renderContent(animate);
    if (animate) requestAnimationFrame(() => this.placeIndicator(true));
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
    if (sec === "manage" && sub === "shops") n = d.health ? d.health.errors : 0;
    return n ? `<span class="count">${n}</span>` : "";
  }
  healthDot() {
    const h = this.state.data.health || {}; const paused = Object.keys(h.paused_hours || {}).length;
    const cls = paused ? "bad" : h.errors ? "warn" : "";
    const txt = paused ? `${paused} winkel${paused > 1 ? "s" : ""} gepauzeerd` : h.errors ? `${h.errors} fout${h.errors > 1 ? "en" : ""}` : "alle winkels ok";
    return `<span class="pill" title="${esc(this.state.data.transport)}" style="cursor:pointer" data-goto="manage/shops"><span class="dotst ${cls}"></span>${txt}</span>`;
  }

  renderContent(animate = false) {
    const s = this.state, el = this.shadowRoot.getElementById("content"); if (!el) return;
    if (s.err) { el.innerHTML = `<div class="empty"><span class="big">⚠️</span>Kon gegevens niet laden: ${esc(s.err)}<br><br><button class="btn" id="retry">Opnieuw proberen</button></div>`; el.querySelector("#retry").onclick = () => this.load(true); return; }
    if (!s.data) { el.innerHTML = `<div class="kpis">${"<div class='skel' style='height:86px'></div>".repeat(4)}</div><div class="grid">${"<div class='skel' style='height:260px'></div>".repeat(8)}</div>`; return; }
    const view = { deals: { today: this.vToday, watch: this.vWatch, all: this.vAll }, collection: { overview: this.vCollOverview, sets: this.vCollSets }, manage: { add: this.vAdd, import: this.vImport, shops: this.vShops, backup: this.vBackup } }[s.section][s.sub[s.section]];
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
    const shown = fmt === "eur" ? EUR0(value) : fmt === "pct" ? signPct(value) : INT(value);
    return `<div class="kpi${color ? " hl" : ""}" style="${color ? `--c:${color}` : ""}"><small>${icon} ${label}</small><b data-to="${value ?? ""}" data-fmt="${fmt}">${shown}</b>${sub ? `<div class="sub2">${sub}</div>` : ""}</div>`;
  }
  countUp(root) {
    if (REDUCED) return;
    root.querySelectorAll("[data-to]").forEach((el) => {
      const to = parseFloat(el.dataset.to); if (isNaN(to) || to === 0) return;
      const fmt = el.dataset.fmt, t0 = performance.now(), dur = 700;
      const f = fmt === "eur" ? EUR0 : fmt === "pct" ? signPct : INT;
      const step = (t) => { const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3); el.textContent = f(fmt === "pct" ? Math.round(to * e * 10) / 10 : to * e); if (p < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
  }
  filterBar({ sorts, threshold = false, cond = false, viewToggle = false, list }) {
    const f = this.state.f;
    const counts = {}; list.forEach((s) => { const t = s.theme || "Onbekend"; counts[t] = (counts[t] || 0) + 1; });
    const themes = Object.keys(counts).sort();
    const subs = {}; if (f.theme) list.filter((s) => s.theme === f.theme && s.subtheme).forEach((s) => { subs[s.subtheme] = (subs[s.subtheme] || 0) + 1; });
    const conds = {}; if (cond) list.forEach((s) => { const c = (s.collection && s.collection.condition) || "Onbekend"; conds[c] = (conds[c] || 0) + 1; });
    return `<div class="fbar"><div class="search"><input id="q" placeholder="Zoek op nummer, naam, thema…  ( / )" value="${esc(f.q)}" autocomplete="off"></div>
      <select id="sort" aria-label="Sorteren">${sorts.map(([k, l]) => `<option value="${k}" ${f.sort === k ? "selected" : ""}>${l}</option>`).join("")}</select>
      ${threshold ? `<label class="thr">Korting ≥ <b id="thrv">${this.state.threshold}%</b><input id="thr" type="range" min="5" max="70" step="5" value="${this.state.threshold}"></label>` : ""}
      ${viewToggle ? `<div class="toggle"><button data-cview="grid" class="${this.state.cview === "grid" ? "on" : ""}" title="Tegels">▦</button><button data-cview="table" class="${this.state.cview === "table" ? "on" : ""}" title="Tabel">☰</button></div>` : ""}</div>
      ${themes.length > 1 ? `<div class="chips"><span class="chip ${!f.theme ? "on" : ""}" data-theme="">Alle thema's</span>${themes.map((t) => `<span class="chip ${f.theme === t ? "on" : ""}" data-theme="${esc(t)}">${esc(t)} <span class="muted">${counts[t]}</span></span>`).join("")}</div>` : ""}
      ${Object.keys(subs).length ? `<div class="chips"><span class="chip sm ${!f.subtheme ? "on" : ""}" data-subtheme="">Alle subthema's</span>${Object.keys(subs).sort().map((t) => `<span class="chip sm ${f.subtheme === t ? "on" : ""}" data-subtheme="${esc(t)}">${esc(t)} <span class="muted">${subs[t]}</span></span>`).join("")}</div>` : ""}
      ${Object.keys(conds).length > 1 ? `<div class="chips"><span class="chip sm ${!f.cond ? "on" : ""}" data-cond="">Elke staat</span>${Object.keys(conds).map((c) => `<span class="chip sm ${f.cond === c ? "on" : ""}" data-cond="${esc(c)}">${esc(c)} <span class="muted">${conds[c]}</span></span>`).join("")}</div>` : ""}`;
  }
  img(s, big = false) { return s.image ? `<img loading="lazy" src="${esc(s.image)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ph',textContent:'🧱'}))">` : `<span class="ph"${big ? ' style="font-size:60px"' : ""}>🧱</span>`; }
  badges(s) {
    return [
      s.is_all_time_low ? `<span class="badge">🔻 Laagste ooit</span>` : "",
      s.discount_rrp != null && s.discount_rrp >= this.state.threshold && s.best_price != null ? `<span class="badge red">−${Math.round(s.discount_rrp)}%</span>` : "",
      s.target_hit ? `<span class="badge purple">🎯 Streefprijs</span>` : "",
      s.retiring_soon ? `<span class="badge yellow">⏳ Verdwijnt${s.retires_in_days != null ? ` ${s.retires_in_days} d` : ""}</span>` : s.retired ? `<span class="badge grey">Uit productie</span>` : "",
      s.owned ? `<span class="badge blue">In bezit${s.collection && s.collection.qty > 1 ? ` ×${s.collection.qty}` : ""}</span>` : "",
    ].join("");
  }
  card(s, i = 0, mode = "deal") {
    const store = s.best_retailer ? this.state.data.retailers[s.best_retailer] : "";
    const tr = s.change_30d == null ? "" : `<span class="trend ${s.change_30d <= 0 ? "up" : "down"}">${s.change_30d <= 0 ? "▼" : "▲"} ${Math.abs(s.change_30d)}%</span>`;
    const stars = s.priority ? `<span class="stars">${"★".repeat(s.priority)}</span>` : "";
    if (mode === "coll") {
      const c = s.collection || {}, now = s.best_price ?? c.current_value ?? s.rrp, g = c.paid && now ? ((now - c.paid) / c.paid) * 100 : null;
      return `<div class="card" tabindex="0" data-set="${esc(s.set_number)}" style="--i:${i}"><div class="badges">${c.condition ? `<span class="badge grey">${esc(c.condition)}</span>` : ""}${s.retiring_soon ? `<span class="badge yellow">⏳</span>` : ""}</div>
        <div class="img">${this.img(s)}</div><div class="n">${esc(s.name || "Set " + s.set_number)}</div>
        <div class="m">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.year ? ` · ${s.year}` : ""}${c.qty > 1 ? ` · ×${c.qty}` : ""}</div>
        <div class="row"><span class="p">${EUR(now)}</span>${g != null ? `<span class="trend ${g >= 0 ? "up" : "down"}">${signPct(Math.round(g))}</span>` : ""}</div>
        <div class="m">${c.paid ? `betaald ${EUR(c.paid)}` : "aankoopprijs onbekend"}${c.location ? ` · 📍 ${esc(c.location)}` : ""}</div></div>`;
    }
    return `<div class="card" tabindex="0" data-set="${esc(s.set_number)}" style="--i:${i}"><div class="badges">${this.badges(s)}</div>${s.best_price != null ? ring(s.deal_score, 40) : ""}
      <div class="img">${this.img(s)}</div><div class="n">${esc(s.name || "Set " + s.set_number)}</div>
      <div class="m">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.subtheme ? ` / ${esc(s.subtheme)}` : ""} ${stars}</div>
      <div class="row"><span class="p">${EUR(s.best_price)}</span>${s.rrp && s.best_price != null && s.best_price < s.rrp ? `<s>${EUR(s.rrp)}</s>` : ""}${tr}</div>
      <div class="m">${store ? `bij ${esc(store)}` : s.offers_live === 0 && Object.keys(s.offers || {}).length ? "⚠ geen prijs gevonden" : Object.keys(s.offers || {}).length ? "geen prijs" : "nog geen winkel-links"}${s.price_per_piece ? ` · ${(s.price_per_piece * 100).toFixed(1)} ct/steen` : ""}${s.target_price ? ` · 🎯 ${EUR0(s.target_price)}` : ""}</div>
      ${spark(s.spark)}</div>`;
  }
  gridOf(list, mode) { return `<div class="grid">${list.map((s, i) => this.card(s, i, mode)).join("")}</div>`; }
  emptyState(icon, text, action = "") { return `<div class="empty"><span class="big">${icon}</span>${text}${action ? `<br><br>${action}` : ""}</div>`; }
  banner() {
    const h = this.state.data.health; if (!h || !h.errors) return "";
    const paused = Object.entries(h.paused_hours || {}).map(([k, v]) => `${esc(k)} (${v} u)`).join(", ");
    return `<div class="banner">⚠️ <span>${h.errors} winkelprijs${h.errors === 1 ? "" : "zen"} kon${h.errors === 1 ? "" : "den"} niet opgehaald worden${paused ? `; gepauzeerd na blokkade: ${paused}` : ""}. <a data-goto="manage/shops">Bekijk winkelstatus</a></span></div>`;
  }

  // ---------------------------------------------------------------- DEALS
  vToday() {
    const watched = this.sets.filter((s) => s.watched);
    if (!this.sets.length) return this.emptyState("🧱", "Nog geen sets. Voeg sets toe om prijzen te volgen, of importeer je collectie.", `<button class="btn" data-goto="manage/add">＋ Set toevoegen</button> <button class="btn ghost" data-goto="manage/import">⇪ Collectie importeren</button>`);
    const deals = this.sorted(watched.filter((s) => this.isDeal(s)), "score");
    const top = deals[0];
    const lows = watched.filter((s) => s.is_all_time_low).length, targets = watched.filter((s) => s.target_hit).length, retiring = watched.filter((s) => s.retiring_soon);
    const w = this.state.data.wishlist;
    const kpis = `<div class="kpis">${this.kpi("Deals nu", deals.length, { icon: "🏷️", color: "var(--lt-red)", sub: `van ${watched.length} gevolgde sets` })}${this.kpi("Laagste prijs ooit", lows, { icon: "🔻", color: "var(--lt-green)" })}${this.kpi("Streefprijs bereikt", targets, { icon: "🎯", color: "var(--lt-purple)" })}${this.kpi("Verdwijnt binnenkort", retiring.length, { icon: "⏳", color: "var(--lt-yellow)" })}${this.kpi("Watchlist nu", w.cost, { fmt: "eur", icon: "🛒", sub: w.saving > 0 ? `<span class="up">${EUR0(w.saving)} onder adviesprijs</span>` : "" })}</div>`;
    const hero = top ? `<div class="hero" data-set="${esc(top.set_number)}"><div class="img">${this.img(top, true)}</div><div><div class="k">⭐ Deal van de dag</div><h2>${esc(top.name || top.set_number)}</h2><div class="m muted">${esc(top.set_number)} · ${esc(top.theme || "")}</div>
      <div style="display:flex;align-items:baseline;gap:10px;margin:8px 0"><span class="p">${EUR(top.best_price)}</span>${top.rrp ? `<s class="muted">${EUR(top.rrp)}</s>` : ""}${top.discount_rrp > 0 ? `<span class="badge red">−${Math.round(top.discount_rrp)}%</span>` : ""}</div>
      <div class="muted" style="font-size:13px">bij ${esc(this.state.data.retailers[top.best_retailer] || "")}${top.is_all_time_low ? " · laagste prijs ooit" : ""}${top.target_hit ? " · onder je streefprijs" : ""}</div>
      ${top.best_url ? `<a class="btn sm" style="margin-top:12px" href="${esc(top.best_url)}" target="_blank" rel="noopener noreferrer" data-stop>Naar de winkel ↗</a>` : ""}</div>${ring(top.deal_score, 64)}</div>` : "";
    const drops = this.sorted(watched.filter((s) => !this.isDeal(s) && s.change_30d != null && s.change_30d <= -8), "drop").slice(0, 8);
    const main = `${hero}${deals.length > 1 ? `<h2 class="sec">🏷️ Alle deals <span class="n">${deals.length}</span></h2>${this.gridOf(deals.slice(1))}` : ""}
      ${!deals.length ? this.emptyState("😴", `Vandaag geen deals bij je ${watched.length} gevolgde sets. Tip: verlaag de kortingsdrempel of stel streefprijzen in.`) : ""}
      ${retiring.length ? `<h2 class="sec">⏳ Verdwijnt binnenkort <span class="n">nu nog kopen?</span></h2>${this.gridOf(this.sorted(retiring, "score"))}` : ""}
      ${drops.length ? `<h2 class="sec">📉 Sterk gedaald <span class="n">laatste 30 dagen</span></h2>${this.gridOf(drops)}` : ""}`;
    return `${this.banner()}${kpis}<div class="cols"><div>${main}</div><aside>${this.timeline()}${this.thresholdPanel()}</aside></div>`;
  }
  thresholdPanel() {
    return `<div class="panel"><h3>🎚️ Wanneer is het een deal?</h3><p>Korting t.o.v. adviesprijs vanaf <b id="thrv">${this.state.threshold}%</b>, of laagste prijs ooit, streefprijs bereikt, of dealscore ≥ 70.</p><input id="thr" type="range" min="5" max="70" step="5" value="${this.state.threshold}" style="width:100%"><p style="margin-top:8px;font-size:12px">De standaarddrempel stel je in via de integratie-opties (nu ${this.state.data.threshold}%).</p></div>`;
  }
  timeline() {
    const ev = this.state.data.events || [];
    const ic = { is_all_time_low: ["🔻", "laagste prijs ooit"], high_discount: ["🏷️", "hoge korting"], target_hit: ["🎯", "streefprijs bereikt"] };
    return `<div class="panel"><h3>🕒 Recente deals</h3>${ev.length ? `<ul class="tl">${ev.slice(0, 12).map((e) => { const [i, t] = ic[e.kind] || ["•", e.kind]; return `<li data-set="${esc(e.set_number)}"><span class="ic">${i}</span><div><b>${esc(e.name || e.set_number)}</b><div>${t} · ${EUR(e.price)}${e.retailer ? ` · ${esc(this.state.data.retailers[e.retailer] || e.retailer)}` : ""}</div><div class="t">${DATE(e.ts, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</div></div></li>`; }).join("")}</ul>` : `<p>Nog niets gemeld. Nieuwe laagste prijzen, hoge kortingen en bereikte streefprijzen verschijnen hier.</p>`}</div>`;
  }
  vWatch() {
    const list = this.sets.filter((s) => s.watched);
    if (!list.length) return this.emptyState("👀", "Je watchlist is leeg. Voeg sets toe die je nog niet hebt; je krijgt een seintje bij een goede prijs.", `<button class="btn" data-goto="manage/add">＋ Set toevoegen</button>`);
    const w = this.state.data.wishlist;
    return `${this.banner()}<div class="kpis">${this.kpi("Sets op watchlist", w.sets, { icon: "👀" })}${this.kpi("Totaal nu", w.cost, { fmt: "eur", icon: "🛒", sub: `${w.priced} met prijs` })}${this.kpi("Adviesprijs", w.rrp, { fmt: "eur", icon: "🏷️" })}${this.kpi("Besparing", w.saving, { fmt: "eur", icon: "💰", color: "var(--lt-green)" })}</div>
      ${this.filterBar({ list, sorts: [["priority", "Prioriteit"], ["score", "Dealscore"], ["discount", "Hoogste korting"], ["price", "Laagste prijs"], ["ppp", "Prijs per steen"], ["drop", "Grootste daling"], ["name", "Naam"]] })}
      <div id="results" data-fn="rWatch">${this.rWatch()}</div>`;
  }
  rWatch() { const l = this.sorted(this.filtered(this.sets.filter((s) => s.watched)), this.state.f.sort); return l.length ? this.gridOf(l) : this.emptyState("🔍", "Niets gevonden met deze filters."); }
  vAll() {
    return `${this.banner()}${this.filterBar({ list: this.sets, threshold: true, sorts: [["score", "Dealscore"], ["discount", "Hoogste korting"], ["price", "Laagste prijs"], ["ppp", "Prijs per steen"], ["drop", "Grootste daling"], ["name", "Naam"], ["number", "Setnummer"]] })}<div id="results" data-fn="rAll">${this.rAll()}</div>`;
  }
  rAll() { const l = this.sorted(this.filtered(this.sets), this.state.f.sort); return l.length ? this.gridOf(l) : this.emptyState("🔍", "Niets gevonden met deze filters."); }

  // ---------------------------------------------------------------- COLLECTION
  vCollOverview() {
    const c = this.state.coll, sum = c.summary, a = this.state.data.analytics || {};
    if (!sum.sets) return this.emptyState("📦", "Je collectie is nog leeg. Importeer een CSV (BrickEconomy, Brickset, Rebrickable of eigen sheet) of markeer sets als ‘in bezit’.", `<button class="btn" data-goto="manage/import">⇪ Collectie importeren</button>`);
    const growth = sum.cost ? sum.growth_pct : null;
    const kpis = `<div class="kpis">${this.kpi("Collectiewaarde", sum.value, { fmt: "eur", icon: "💎", color: "var(--lt-accent)" })}${this.kpi("Aankoopkost", sum.cost, { fmt: "eur", icon: "🧾" })}${this.kpi("Groei", growth, { fmt: "pct", icon: "📈", color: growth >= 0 ? "var(--lt-green)" : "var(--lt-red)", sub: sum.cost ? `<span class="${sum.growth >= 0 ? "up" : "down"}">${sum.growth >= 0 ? "+" : ""}${EUR0(sum.growth)}</span>` : "" })}${this.kpi("Sets", sum.sets, { icon: "🧱", sub: `${INT(sum.pieces)} stenen` })}${this.kpi("Betaald per steen", a.avg_paid_per_piece != null ? a.avg_paid_per_piece * 100 : null, { icon: "🔢", sub: "cent, gemiddeld" })}</div>`;
    const ser = c.series || [];
    const chart = lineChart([{ name: "Waarde", color: COLORS[1], points: ser.map((p) => [p.ts, p.value]) }, { name: "Aankoopkost", color: COLORS[0], points: ser.map((p) => [p.ts, p.cost]), dashed: true }]);
    const themes = Object.entries(a.by_theme || {}); const topT = themes.slice(0, 7), rest = themes.slice(7).reduce((x, [, v]) => x + v.value, 0);
    const donutItems = topT.map(([k, v]) => ({ label: `${k} (${v.count})`, value: v.value })).concat(rest ? [{ label: "Overige", value: rest, color: "#9aa0a6" }] : []);
    const years = Object.entries(a.by_year || {}).map(([k, v]) => ({ label: k, value: v.count }));
    const mv = (list, cls) => list.length ? `<table class="tbl">${list.map((m) => `<tr class="click" data-set="${esc(m.set_number)}"><td>${esc(m.name || m.set_number)}<div class="muted" style="font-size:12px">${EUR(m.paid)} → ${EUR(m.value)}</div></td><td class="num ${cls}"><b>${signPct(m.pct)}</b></td></tr>`).join("")}</table>` : `<p>Nog geen gegevens (aankoopprijs nodig).</p>`;
    const conds = Object.entries(a.by_condition || {});
    return `${kpis}<div class="panel"><h3>📈 Collectiegroei<span class="hsp"></span><span class="muted" style="font-size:12px;font-weight:400">waarde = laagste nieuwprijs × aantal</span></h3>${chart}</div>
      <div class="two"><div class="panel"><h3>🎨 Waarde per thema</h3>${donut(donutItems, { label: "totaal" })}</div><div class="panel"><h3>📅 Sets per uitgavejaar</h3>${years.length > 1 ? bars(years) : `<p>Jaar onbekend voor de meeste sets; vul Brickset in de opties in om dit aan te vullen.</p>`}</div></div>
      <div class="two"><div class="panel"><h3>🚀 Grootste stijgers</h3>${mv(a.top_gainers || [], "up")}</div><div class="panel"><h3>📉 Onder aankoopprijs</h3>${mv(a.top_losers || [], "down")}</div></div>
      ${conds.length ? `<div class="panel"><h3>📦 Staat van je sets</h3><div class="chips">${conds.map(([k, v]) => `<span class="chip">${esc(k)} <b>${v}</b></span>`).join("")}</div></div>` : ""}`;
  }
  vCollSets() {
    const list = this.sets.filter((s) => s.owned);
    if (!list.length) return this.emptyState("📦", "Nog geen sets in je collectie.", `<button class="btn" data-goto="manage/import">⇪ Importeren</button> <button class="btn ghost" data-goto="manage/add">＋ Toevoegen</button>`);
    return `${this.filterBar({ list, cond: true, viewToggle: true, sorts: [["value", "Hoogste waarde"], ["gain", "Grootste winst"], ["name", "Naam"], ["number", "Setnummer"], ["year", "Jaar"]] })}
      <div class="fbar" style="justify-content:flex-end"><button class="btn ghost sm" id="expcsv">⬇ Exporteer CSV</button></div><div id="results" data-fn="rColl">${this.rColl()}</div>`;
  }
  collSort(list) {
    const v = (s) => { const c = s.collection || {}; return (s.best_price ?? c.current_value ?? s.rrp ?? 0) * (c.qty || 1); };
    const g = (s) => { const c = s.collection || {}, now = s.best_price ?? c.current_value ?? s.rrp; return c.paid && now ? (now - c.paid) / c.paid : -1e9; };
    const key = { value: (s) => -v(s), gain: (s) => -g(s), name: (s) => (s.name || "").toLowerCase(), number: (s) => +s.set_number, year: (s) => -(s.year || 0), score: (s) => -v(s) }[this.state.f.sort] || ((s) => -v(s));
    return [...list].sort((a, b) => { const x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
  }
  rColl() {
    const list = this.collSort(this.filtered(this.sets.filter((s) => s.owned)));
    if (!list.length) return this.emptyState("🔍", "Niets gevonden met deze filters.");
    if (this.state.cview === "grid") return this.gridOf(list, "coll");
    const rows = list.map((s) => {
      const c = s.collection || {}, now = s.best_price ?? c.current_value ?? s.rrp, g = c.paid && now ? ((now - c.paid) / c.paid) * 100 : null;
      return `<tr class="click" data-set="${esc(s.set_number)}"><td>${esc(s.set_number)}</td><td>${esc(s.name || "")}</td><td>${esc(s.theme || "")}</td><td>${s.year || ""}</td><td class="num">${c.qty || 1}</td><td class="num">${EUR(c.paid)}</td><td class="num">${EUR(now)}</td><td class="num ${g == null ? "" : g >= 0 ? "up" : "down"}">${g == null ? "–" : signPct(Math.round(g))}</td><td>${esc(c.condition || "")}</td><td>${esc(c.location || "")}</td></tr>`;
    }).join("");
    return `<div class="panel tscroll"><table class="tbl"><tr><th>#</th><th>Naam</th><th>Thema</th><th>Jaar</th><th class="num">Aantal</th><th class="num">Betaald</th><th class="num">Nu</th><th class="num">+/−</th><th>Staat</th><th>Locatie</th></tr>${rows}</table></div>`;
  }

  // ---------------------------------------------------------------- MANAGE
  vAdd() {
    const m = this.state.addMode;
    const themes = this.state.data.themes.map((t) => `<option value="${esc(t)}">`).join("");
    return `<div class="panel"><h3>＋ Set toevoegen</h3>
      <div class="radio"><label class="${m === "watch" ? "on" : ""}"><input type="radio" name="mode" value="watch" ${m === "watch" ? "checked" : ""}><b>👀 In het oog houden</b><span>Prijzen volgen en melding bij een deal</span></label>
      <label class="${m === "own" ? "on" : ""}"><input type="radio" name="mode" value="own" ${m === "own" ? "checked" : ""}><b>📦 Aan mijn collectie toevoegen</b><span>Ik heb deze set al</span></label></div>
      <div class="form"><label>Setnummer *<input id="a_num" inputmode="numeric" placeholder="bv. 10281" autocomplete="off"><div class="hint" id="a_hint"></div></label><label>Naam<input id="a_name" placeholder="optioneel, wordt aangevuld"></label>
      <label>Thema<input id="a_theme" list="themes" placeholder="Botanicals, Technic, Icons…"></label><label>Subthema<input id="a_sub"></label><label>Adviesprijs (€)<input id="a_rrp" type="number" min="0" step="0.01"></label><label>Stenen<input id="a_pcs" type="number" min="0"></label></div>
      ${m === "watch" ? `<div class="form"><label>Streefprijs (€) – melding als de prijs hieronder zakt<input id="a_target" type="number" min="0" step="0.01"></label><label>Prioriteit<select id="a_prio"><option value="0">–</option><option value="1">★</option><option value="2">★★</option><option value="3">★★★</option></select></label></div>`
        : `<div class="form"><label>Aantal<input id="a_qty" type="number" value="1" min="1"></label><label>Betaald (€ per stuk)<input id="a_paid" type="number" min="0" step="0.01"></label><label>Aankoopdatum<input id="a_date" type="date" max="${new Date().toISOString().slice(0, 10)}"></label><label>Staat<select id="a_cond"><option value="">–</option>${CONDITIONS.map((c) => `<option>${c}</option>`).join("")}</select></label><label>Locatie<input id="a_loc" placeholder="bv. zolder, kast 2"></label></div>`}
      <datalist id="themes">${themes}</datalist><button class="btn" id="add">＋ Toevoegen & winkels zoeken</button> <span class="muted" style="font-size:12px">Winkel-links worden automatisch gezocht; dat duurt even.</span></div>
      <div class="panel"><h3>⚡ Meerdere sets tegelijk</h3><p>Plak setnummers, gescheiden door spaties, komma's of nieuwe regels.</p><textarea id="bulk" rows="3" style="width:100%" placeholder="10281 10311 42143"></textarea><div class="hint" id="bulk_hint"></div>
      <label class="chk" style="display:flex;gap:8px;align-items:center;margin:6px 0 10px"><input type="checkbox" id="bulk_owned"> alle als "in bezit" markeren</label><button class="btn" id="bulkadd">Toevoegen</button> <button class="btn ghost" id="discover">🔎 Winkels zoeken voor sets zonder link</button></div>
      <div class="panel"><h3>🔗 Winkel-link koppelen</h3><p>Als de automatische zoekopdracht de verkeerde pagina vond of niets vond.</p><div class="form"><label>Setnummer<input id="o_num" list="nums"></label><label>Winkel<select id="o_ret">${Object.entries(this.state.data.retailers).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label><label>Product-URL of ASIN<input id="o_url" placeholder="https://… of B0…"></label></div>
      <datalist id="nums">${this.sets.map((s) => `<option value="${esc(s.set_number)}">${esc(s.name || "")}</option>`).join("")}</datalist><button class="btn" id="offer">Koppelen</button></div>`;
  }
  vImport() {
    const imp = this.state.imp, a = imp.analysis;
    const steps = `<div class="steps"><span class="${!a ? "on" : ""}">1 · Bestand kiezen</span><span class="${a ? "on" : ""}">2 · Controleren</span><span>3 · Importeren</span></div>`;
    if (!a) return `${steps}<div class="panel"><label class="drop" id="drop"><span class="big">📄</span><b>Sleep je CSV hierheen</b> of klik om te kiezen<br><span class="muted" style="font-size:13px">BrickEconomy, Brickset, Rebrickable of een eigen spreadsheet (komma, puntkomma of tab)</span><input type="file" id="csvfile" accept=".csv,.tsv,.txt,text/csv,text/plain" hidden></label>
      <p style="margin-top:14px">…of plak de inhoud:</p><textarea id="csvtext" rows="6" style="width:100%" placeholder="Number;Name;Theme;Qty;Paid;Purchase Date&#10;10281;Bonsai Tree;Botanicals;1;39,99;24/12/2023">${esc(imp.text)}</textarea><br><br><button class="btn" id="analyze">Controleren →</button>
      <p style="margin-top:14px;font-size:12px">Herkende kolommen: setnummer, naam, thema, subthema, jaar, stenen, adviesprijs, aantal, betaald, waarde, aankoopdatum, staat, locatie, notitie (Engels en Nederlands). Er wordt pas iets opgeslagen na stap 3.</p></div>`;
    if (a.fatal && !a.rows.length) return `${steps}<div class="panel"><h3 class="err">✕ Dit bestand kan niet geïmporteerd worden</h3><p>${esc(a.fatal)}</p><button class="btn ghost" id="impback">← Ander bestand</button></div>`;
    const sm = a.summary, n = sm.ok + sm.warning;
    const rows = a.rows.filter((r) => !imp.only || r.status !== "ok").slice(0, 500);
    const tbl = rows.map((r) => `<tr><td class="muted">${r.line}</td><td><span class="st ${r.status}">${r.status === "ok" ? "✓" : r.status === "warning" ? "!" : "✕"}</span></td><td><b>${esc(r.set_number || r.raw_number)}</b></td><td>${esc(r.name || "")}</td><td class="num">${r.qty ?? ""}</td><td class="num">${r.paid != null ? EUR(r.paid) : ""}</td><td>${esc(r.added || "")}</td><td>${esc(r.condition || "")}</td><td class="iss">${r.issues.map((i) => `<div class="${i.level}">${esc(i.text)}</div>`).join("")}</td></tr>`).join("");
    return `${steps}<div class="panel"><h3>🔎 Controle van ${esc(imp.name || "geplakte gegevens")}<span class="hsp"></span><button class="btn ghost sm" id="impback">← Ander bestand</button></h3>
      ${a.fatal ? `<div class="banner">⚠️ ${esc(a.fatal)}</div>` : ""}
      <div class="sumpills"><span class="ok">✓ ${sm.ok} in orde</span><span style="color:#9a6a00">! ${sm.warning} met waarschuwing</span><span class="err">✕ ${sm.error} worden overgeslagen</span><span>＋ ${sm.new} nieuw</span><span>↻ ${sm.update} bijgewerkt</span>${sm.merged ? `<span>⧉ ${sm.merged} samengevoegd</span>` : ""}</div>
      <p><b>Herkende kolommen:</b> ${Object.entries(a.columns).map(([k, v]) => `<span class="chip sm">${esc(k)} → ${esc(v)}</span>`).join(" ")}${a.ignored_columns.length ? `<br><span class="muted">Genegeerd: ${a.ignored_columns.map(esc).join(", ")}</span>` : ""}</p>
      <div class="fbar"><label class="chk" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="imp_only" ${imp.only ? "checked" : ""}> alleen regels met opmerkingen</label>
      <label class="chk" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="imp_replace" ${imp.replace ? "checked" : ""}> huidige collectie vervangen</label>
      <label class="chk" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="imp_track" ${imp.track ? "checked" : ""}> prijzen gaan volgen</label><span class="hsp"></span>
      <button class="btn" id="impgo" ${n ? "" : "disabled"}>Importeer ${n} regel${n === 1 ? "" : "s"} →</button></div>
      <div class="tscroll"><table class="tbl"><tr><th>Regel</th><th></th><th>Set</th><th>Naam</th><th class="num">Aantal</th><th class="num">Betaald</th><th>Datum</th><th>Staat</th><th>Opmerkingen</th></tr>${tbl}</table></div>
      ${a.rows.length > 500 ? `<p class="muted">Eerste 500 regels getoond.</p>` : ""}</div>`;
  }
  vShops() {
    const st = this.state.data.retailer_stats || {};
    const cards = Object.entries(st).map(([rid, r], i) => {
      const ratio = r.offers ? r.ok / r.offers : 0;
      const state = !r.enabled ? ["", "uitgeschakeld"] : r.paused_hours > 0 ? ["bad", `gepauzeerd (${r.paused_hours} u)`] : r.errors ? ["warn", `${r.errors} fout${r.errors > 1 ? "en" : ""}`] : ["", r.offers ? "werkt" : "geen links"];
      return `<div class="shop" style="--i:${i}"><h4><span class="dotst ${state[0]}"></span>${esc(r.label)}</h4><div class="muted" style="font-size:13px">${state[1]}</div>
        <div class="meter"><i style="width:${Math.round(ratio * 100)}%"></i></div>
        <div class="kv"><span>Links</span><b>${r.offers}</b></div><div class="kv"><span>Met prijs</span><b>${r.ok}</b></div><div class="kv"><span>Goedkoopste voor</span><b>${r.cheapest} sets</b></div><div class="kv"><span>Laatst gelukt</span><b>${ago(r.last_ok)}</b></div></div>`;
    }).join("");
    const failing = Object.values(st).flatMap((r) => r.failing.map((f) => ({ ...f, shop: r.label })));
    return `<div class="panel"><h3>🏪 Winkelstatus<span class="hsp"></span><span class="pill">${esc(this.state.data.transport)}</span></h3><p>Na een blokkade wordt een winkel automatisch een tijd gepauzeerd (1 → 3 → 6 → 12 → 24 u) om de bescherming niet strenger te maken.</p></div>
      <div class="shops">${cards}</div>
      <div class="panel" style="margin-top:16px"><h3>⚠️ Aanbiedingen zonder prijs <span class="muted" style="font-weight:400">${failing.length}</span></h3>${failing.length ? `<div class="tscroll"><table class="tbl"><tr><th>Set</th><th>Winkel</th><th>Melding</th></tr>${failing.map((f) => `<tr class="click" data-set="${esc(f.set_number)}"><td><b>${esc(f.set_number)}</b> ${esc(f.name || "")}</td><td>${esc(f.shop)}</td><td class="err">${esc(f.error)}</td></tr>`).join("")}</table></div>` : `<p class="ok">Alles in orde.</p>`}</div>
      <div class="panel"><h3>🧩 Blijft een winkel blokkeren?</h3><p>1. Vul de prijs handmatig in (klik op een set → Winkels → Handmatig).<br>2. Installeer het userscript <code>tools/lego-tracker.user.js</code> in Tampermonkey: jouw eigen browser stuurt de prijs door wanneer je een productpagina bezoekt.<br>3. Gebruik de actie <code>lego_tracker.report_price</code> vanuit een automation of n8n.</p></div>`;
  }
  vBackup() {
    return `<div class="two"><div class="panel"><h3>⬇ Exporteren</h3><p>Volledige back-up (sets, winkel-links, prijshistoriek, collectie, tijdlijn) als JSON, of alleen je collectie als CSV (opnieuw te importeren).</p><button class="btn" id="expjson">⬇ Back-up (JSON)</button> <button class="btn ghost" id="expcsv">⬇ Collectie (CSV)</button></div>
      <div class="panel"><h3>⬆ Terugzetten</h3><p>De back-up wordt eerst volledig gecontroleerd (structuur, setnummers, URL's, prijshistoriek) voordat er iets verandert.</p><label class="drop" id="jdrop" style="padding:18px"><span class="big" style="font-size:26px">🗂️</span><span id="jname">Kies of sleep een back-upbestand (.json)</span><input type="file" id="jsonfile" accept=".json,application/json" hidden></label>
      <label class="chk" style="display:flex;gap:8px;align-items:center;margin:10px 0"><input type="checkbox" id="merge" checked> samenvoegen met huidige gegevens (anders: alles vervangen)</label><button class="btn" id="impjson" disabled>Terugzetten</button></div></div>`;
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
    this.shadowRoot.querySelectorAll("[data-goto]").forEach((el) => { el.onclick = () => { const [a, b] = el.dataset.goto.split("/"); s.section = a; s.sub[a] = b; this.resetFilters(); this.persist(); this.render(true); }; });
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
      hint.innerHTML = !num.value ? "" : !n ? `<span class="err">Een setnummer bestaat uit 3–7 cijfers</span>` : ex ? `<span style="color:#9a6a00">Al gevolgd${ex.owned ? " en in je collectie" : ""}: ${esc(ex.name || "")} – gegevens worden bijgewerkt</span>` : `<span class="ok">✓ nieuw</span>`;
    });
    const add = $("add"); if (add) add.addEventListener("click", () => {
      const v = (id) => (root.querySelector("#" + id) || {}).value?.trim?.() || "";
      const n = (v("a_num").match(/\d{3,7}/) || [""])[0]; if (!n) return this.toast("Vul een geldig setnummer in (3–7 cijfers)", "err");
      const d = { set_number: n, owned: s.addMode === "own" };
      for (const [k, id] of [["name", "a_name"], ["theme", "a_theme"], ["subtheme", "a_sub"], ["purchase_date", "a_date"]]) if (v(id)) d[k] = v(id);
      for (const [k, id] of [["rrp", "a_rrp"], ["pieces", "a_pcs"], ["paid", "a_paid"], ["target_price", "a_target"]]) if (v(id)) { const x = +v(id); if (!(x >= 0)) return this.toast(`${k}: ongeldig getal`, "err"); d[k] = x; }
      if (v("a_qty")) d.quantity = Math.max(1, +v("a_qty") || 1);
      this.busy(add, "Toevoegen en winkels zoeken…", async () => {
        await this.svc("add_set", d);
        const extra = {}; if (v("a_prio") && +v("a_prio")) extra.priority = +v("a_prio"); if (v("a_cond")) extra.condition = v("a_cond"); if (v("a_loc")) extra.location = v("a_loc");
        if (Object.keys(extra).length) await this._hass.callWS({ type: "lego_tracker/update_set", set_number: n, fields: extra });
        await this.load(); this.toast(`Set ${n} toegevoegd`, "ok"); this.openSet(n);
      });
    });
    const bulk = $("bulk"); if (bulk) bulk.addEventListener("input", () => { const ns = [...new Set(bulk.value.match(/\d{3,7}/g) || [])], known = ns.filter((n) => this.sets.some((x) => x.set_number === n)).length; $("bulk_hint").textContent = ns.length ? `${ns.length} setnummer${ns.length > 1 ? "s" : ""} herkend${known ? `, waarvan ${known} al gevolgd` : ""}` : ""; });
    const ba = $("bulkadd"); if (ba) ba.addEventListener("click", () => { if (!bulk.value.trim()) return; this.busy(ba, "Bezig… (winkels zoeken duurt even)", async () => { const r = await this.svc("add_sets", { set_numbers: bulk.value, owned: $("bulk_owned").checked }, true); await this.load(); this.toast(`${r.response.added} sets toegevoegd`, "ok"); }); });
    const dc = $("discover"); if (dc) dc.addEventListener("click", () => this.busy(dc, "Zoeken…", async () => { const r = await this.svc("discover_offers", {}, true); await this.load(); this.toast(`${r.response.found} nieuwe winkel-links gevonden`, "ok"); }));
    const of = $("offer"); if (of) of.addEventListener("click", () => this.busy(of, "Koppelen…", async () => { await this.svc("set_offer", { set_number: $("o_num").value.trim(), retailer: $("o_ret").value, url: $("o_url").value.trim() }); await this.load(); this.toast("Link gekoppeld. Ververs om de prijs op te halen.", "ok"); }));
    // import wizard
    const imp = s.imp;
    const readFile = async (f) => { if (!f) return; if (f.size > 2_000_000) return this.toast("Bestand te groot (max 2 MB)", "err"); imp.text = await f.text(); imp.name = f.name; await analyze(); };
    const analyze = async () => { if (!imp.text.trim()) return this.toast("Kies een bestand of plak CSV-tekst", "err"); try { imp.analysis = await this._hass.callWS({ type: "lego_tracker/import_preview", csv_text: imp.text, replace: imp.replace }); this.renderContent(true); } catch (e) { this.toast(e.message, "err"); } };
    const drop = $("drop"); if (drop) {
      ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
      drop.addEventListener("drop", (e) => readFile(e.dataTransfer.files[0]));
      $("csvfile").addEventListener("change", (e) => readFile(e.target.files[0]));
    }
    const an = $("analyze"); if (an) an.addEventListener("click", () => { imp.text = $("csvtext").value; imp.name = ""; this.busy(an, "Controleren…", analyze); });
    const back = $("impback"); if (back) back.addEventListener("click", () => { imp.analysis = null; this.renderContent(true); });
    const only = $("imp_only"); if (only) only.addEventListener("change", (e) => { imp.only = e.target.checked; this.renderContent(); });
    const rep = $("imp_replace"); if (rep) rep.addEventListener("change", async (e) => { imp.replace = e.target.checked; await analyze(); });
    const trk = $("imp_track"); if (trk) trk.addEventListener("change", (e) => { imp.track = e.target.checked; });
    const go = $("impgo"); if (go) go.addEventListener("click", () => {
      if (imp.replace && !confirm("Je huidige collectie wordt volledig vervangen door dit bestand. Doorgaan?")) return;
      this.busy(go, "Importeren…", async () => {
        const r = (await this.svc("import_collection", { csv_text: imp.text, replace: imp.replace, track_prices: imp.track }, true)).response;
        s.imp = { text: "", name: "", analysis: null, only: false, replace: false, track: true };
        await this.load(); this.toast(`Import klaar: ${r.added} nieuw, ${r.updated} bijgewerkt${r.skipped ? `, ${r.skipped} overgeslagen` : ""}`, "ok");
        s.section = "collection"; s.sub.collection = "sets"; this.persist(); this.render(true);
      });
    });
    // backup
    const dl = (name, text, mime) => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: mime })); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000); };
    const ec = $("expcsv"); if (ec) ec.addEventListener("click", () => this.busy(ec, "…", async () => { const x = await this.svc("export_collection", {}, true); dl("lego_collectie.csv", x.response.csv, "text/csv;charset=utf-8"); }));
    const ej = $("expjson"); if (ej) ej.addEventListener("click", () => this.busy(ej, "…", async () => { const x = await this.svc("export_data", {}, true); dl(`lego_backup_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(x.response, null, 1), "application/json"); this.toast("Back-up gedownload", "ok"); }));
    const jd = $("jdrop"); if (jd) {
      let payload = null;
      const pick = async (f) => { if (!f) return; try { payload = JSON.parse(await f.text()); $("jname").textContent = `${f.name} · ${Object.keys(payload.sets || {}).length} sets`; $("impjson").disabled = false; } catch (e) { payload = null; $("impjson").disabled = true; this.toast("Geen geldig JSON-bestand", "err"); } };
      ["dragenter", "dragover"].forEach((ev) => jd.addEventListener(ev, (e) => { e.preventDefault(); jd.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => jd.addEventListener(ev, (e) => { e.preventDefault(); jd.classList.remove("over"); }));
      jd.addEventListener("drop", (e) => pick(e.dataTransfer.files[0])); $("jsonfile").addEventListener("change", (e) => pick(e.target.files[0]));
      const ij = $("impjson"); ij.addEventListener("click", () => {
        const merge = $("merge").checked; if (!merge && !confirm("Alle huidige gegevens worden vervangen. Doorgaan?")) return;
        this.busy(ij, "Controleren en terugzetten…", async () => { const r = await this.svc("import_data", { data: payload, merge }, true); await this.load(); this.toast(`Back-up teruggezet: ${r.response.sets} sets, ${r.response.collection} in collectie`, "ok"); });
      });
    }
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
  closeDialog() { const d = this.shadowRoot.getElementById("dlg"); if (!d.open) return; d.classList.add("closing"); setTimeout(() => { d.classList.remove("closing"); d.close(); }, REDUCED ? 0 : 170); }
  async openSet(num) {
    const dlg = this.shadowRoot.getElementById("dlg");
    if (!dlg.open) { dlg.innerHTML = `<div class="dbody"><div class="skel" style="height:110px;margin-bottom:12px"></div><div class="skel" style="height:240px"></div></div>`; dlg.showModal(); }
    let s; try { s = await this._hass.callWS({ type: "lego_tracker/set", set_number: num }); } catch (e) { dlg.innerHTML = `<div class="dbody"><div class="empty">${esc(e.message)}</div><br><button class="btn" id="x2">Sluiten</button></div>`; dlg.querySelector("#x2").onclick = () => this.closeDialog(); return; }
    const days = this.state.range, cut = days ? Date.now() / 1000 - days * 86400 : 0;
    const win = (h) => { if (!cut) return h; const keep = h.filter((p) => p[0] >= cut), prev = lastAt(h, cut); return prev && (!keep.length || keep[0][0] > cut) ? [[cut, prev[1]], ...keep] : keep; };
    const series = Object.entries(s.history).filter(([, h]) => h.length).map(([rid, h], i) => ({ name: s.offers[rid]?.label || rid, color: COLORS[i % COLORS.length], points: win(h) })).filter((x) => x.points.length);
    const allT = series.flatMap((x) => x.points.map((p) => p[0]));
    if (s.rrp && allT.length) series.push({ name: "Adviesprijs", color: "#9aa0a6", dashed: true, points: [[Math.min(...allT), s.rrp], [Math.max(...allT), s.rrp]] });
    if (s.target_price && allT.length) series.push({ name: "Streefprijs", color: "#7a3c9e", dashed: true, points: [[Math.min(...allT), s.target_price], [Math.max(...allT), s.target_price]] });
    const offers = Object.entries(s.offers);
    const cheapest = Math.min(...offers.map(([, o]) => o.price ?? Infinity));
    const orows = offers.map(([rid, o]) => `<tr><td><b>${esc(o.label)}</b>${o.error ? `<div class="err">${esc(o.error)}</div>` : ""}</td><td class="num">${o.price != null ? `<b class="${o.price === cheapest ? "ok" : ""}">${EUR(o.price)}</b>` : "–"}</td><td class="num">${EUR(o.low)}</td><td class="muted">${ago(o.checked)}</td><td>${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">open ↗</a>` : ""}</td><td style="white-space:nowrap"><input class="mp" data-rid="${rid}" type="number" min="0" step="0.01" placeholder="prijs" style="width:88px;padding:6px 8px"> <button class="btn ghost sm mpb" data-rid="${rid}" title="Handmatige prijs opslaan">✓</button></td></tr>`).join("");
    const c = s.collection || {};
    const stat = (l, v) => `<div class="stat"><small>${l}</small><b>${v}</b></div>`;
    dlg.innerHTML = `<div class="dhead"><div class="img">${this.img(s, true)}</div><div><h2>${esc(s.name || "Set " + s.set_number)}</h2><div class="muted">${esc(s.set_number)} · ${esc(s.theme || "?")}${s.subtheme ? " / " + esc(s.subtheme) : ""}${s.year ? " · " + s.year : ""}${s.pieces ? " · " + INT(s.pieces) + " stenen" : ""}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${this.badges(s)}</div>
        <div style="margin-top:8px;display:flex;align-items:baseline;gap:6px 10px;flex-wrap:wrap"><span style="font-size:26px;font-weight:800">${EUR(s.best_price)}</span>${s.best_retailer ? `<span class="muted">bij ${esc(this.state.data.retailers[s.best_retailer])}</span>` : ""}${s.best_url ? `<a class="btn sm" href="${esc(s.best_url)}" target="_blank" rel="noopener noreferrer">Kopen ↗</a>` : ""}</div></div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:10px"><button class="x" id="x" aria-label="Sluiten">✕</button>${s.best_price != null ? ring(s.deal_score, 56) : ""}</div></div>
      <div class="dbody"><div class="chips">${[[30, "30 d"], [90, "90 d"], [365, "1 jaar"], [0, "Alles"]].map(([d, l]) => `<span class="chip sm ${this.state.range === d ? "on" : ""}" data-range="${d}">${l}</span>`).join("")}</div>
        ${lineChart(series, { area: true })}
        <div class="stats">${stat("Laagste ooit", EUR(s.all_time_low))}${stat("Adviesprijs", EUR(s.rrp))}${stat("Korting", s.discount_rrp != null ? `${s.discount_rrp > 0 ? "−" : "+"}${Math.abs(s.discount_rrp)}%` : "–")}${stat("Per steen", s.price_per_piece ? (s.price_per_piece * 100).toFixed(1) + " ct" : "–")}${stat("7 dagen", signPct(s.change_7d))}${stat("30 dagen", signPct(s.change_30d))}${s.retires_in_days != null ? stat(s.retired ? "Uit productie sinds" : "Verdwijnt over", s.retired ? esc(s.exit_date) : s.retires_in_days + " d") : ""}</div>
        <h3 style="margin:16px 0 6px">🏪 Winkels</h3>${offers.length ? `<div class="tscroll"><table class="tbl"><tr><th>Winkel</th><th class="num">Nu</th><th class="num">Laagste</th><th>Gecontroleerd</th><th></th><th>Handmatig</th></tr>${orows}</table></div>` : `<div class="empty small">Nog geen winkel-links. <a id="find2" style="cursor:pointer">Nu zoeken</a> of koppel er een via Beheer.</div>`}
        <form id="ef" style="margin-top:16px" autocomplete="off">
        <fieldset><legend>👀 Volgen</legend><div class="form"><label>Streefprijs (€)<input name="target_price" type="number" min="0" step="0.01" value="${s.target_price ?? ""}"></label>
          <label>Prioriteit<select name="priority">${[0, 1, 2, 3].map((p) => `<option value="${p}" ${(s.priority || 0) === p ? "selected" : ""}>${p ? "★".repeat(p) : "–"}</option>`).join("")}</select></label>
          <label>Verdwijnt op<input name="exit_date" type="date" value="${esc((s.exit_date || "").slice(0, 10))}"></label><label class="chk"><input name="retiring" type="checkbox" ${s.retiring ? "checked" : ""}> verdwijnt binnenkort</label></div>
          <div class="form"><label>Thema<input name="theme" value="${esc(s.theme || "")}" list="dthemes"></label><label>Subthema<input name="subtheme" value="${esc(s.subtheme || "")}"></label><label>Adviesprijs (€)<input name="rrp" type="number" min="0" step="0.01" value="${s.rrp ?? ""}"></label><label>Stenen<input name="pieces" type="number" min="0" value="${s.pieces ?? ""}"></label></div>
          <div class="form"><label style="grid-column:1/-1">Notitie<input name="notes" value="${esc(s.notes || "")}" maxlength="200"></label></div></fieldset>
        <fieldset><legend>📦 Collectie</legend><div class="form"><label class="chk"><input name="owned" type="checkbox" ${s.owned ? "checked" : ""}> ik heb deze set</label></div>
          <div class="form" id="collf" style="${s.owned ? "" : "opacity:.45;pointer-events:none"}"><label>Aantal<input name="qty" type="number" min="1" value="${c.qty ?? 1}"></label><label>Betaald (€/stuk)<input name="paid" type="number" min="0" step="0.01" value="${c.paid ?? ""}"></label>
          <label>Aankoopdatum<input name="added" type="date" max="${new Date().toISOString().slice(0, 10)}" value="${esc(c.added || "")}"></label><label>Staat<select name="condition"><option value="">–</option>${CONDITIONS.map((x) => `<option ${c.condition === x ? "selected" : ""}>${x}</option>`).join("")}</select></label><label>Locatie<input name="location" value="${esc(c.location || "")}"></label></div></fieldset>
        <datalist id="dthemes">${this.state.data.themes.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="save" type="submit">Opslaan</button><button class="btn ghost" id="find" type="button">🔎 Winkels zoeken</button><button class="btn ghost" id="rf" type="button">↻ Prijs ophalen</button><span class="hsp" style="flex:1"></span><button class="btn danger" id="rm" type="button">Verwijderen</button></div></form></div>`;
    const q = (id) => dlg.querySelector("#" + id);
    q("x").onclick = () => this.closeDialog();
    dlg.querySelectorAll("[data-range]").forEach((ch) => ch.onclick = () => { this.state.range = +ch.dataset.range; this.persist(); this.openSet(num); });
    const ef = q("ef");
    ef.owned.addEventListener("change", () => { const cf = q("collf"); cf.style.opacity = ef.owned.checked ? "" : ".45"; cf.style.pointerEvents = ef.owned.checked ? "" : "none"; });
    ef.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = {}, fd = new FormData(ef);
      for (const k of ["target_price", "exit_date", "theme", "subtheme", "rrp", "pieces", "notes"]) f[k] = (fd.get(k) || "").toString().trim();
      f.priority = +fd.get("priority"); f.retiring = ef.retiring.checked; f.owned = ef.owned.checked;
      if (f.owned) for (const k of ["qty", "paid", "added", "condition", "location"]) f[k] = (fd.get(k) || "").toString().trim();
      if (f.owned && f.qty !== "" && !(+f.qty >= 1)) return this.toast("Aantal moet minstens 1 zijn", "err");
      if (s.owned && !f.owned && !confirm("Deze set uit je collectie halen? (hij blijft op je watchlist)")) return;
      this.busy(q("save"), "Opslaan…", async () => { await this._hass.callWS({ type: "lego_tracker/update_set", set_number: num, fields: f }); await this.load(); this.toast("Opgeslagen", "ok"); this.closeDialog(); });
    });
    const find = async (btn) => this.busy(btn, "Zoeken…", async () => { const r = await this.svc("discover_offers", { set_number: num }, true); await this.load(); this.toast(`${r.response.found} winkel-links gevonden`, r.response.found ? "ok" : ""); this.openSet(num); });
    q("find").onclick = (e) => find(e.currentTarget); if (q("find2")) q("find2").onclick = () => find(q("find"));
    q("rf").onclick = (e) => this.busy(e.currentTarget, "Ophalen…", async () => { await this.svc("refresh", { set_number: num }); await this.load(); this.openSet(num); });
    dlg.querySelectorAll(".mpb").forEach((btn) => btn.onclick = () => {
      const rid = btn.dataset.rid, val = parseFloat(dlg.querySelector(`.mp[data-rid="${rid}"]`).value);
      if (!(val > 0) || val > 10000) return this.toast("Geef een geldige prijs in", "err");
      if (s.rrp && (val < s.rrp * 0.2 || val > s.rrp * 4) && !confirm(`${EUR(val)} wijkt sterk af van de adviesprijs ${EUR(s.rrp)}. Toch opslaan?`)) return;
      this.busy(btn, "", async () => { await this.svc("report_price", { set_number: num, retailer: rid, price: val }); await this.load(); this.toast("Prijs opgeslagen", "ok"); this.openSet(num); });
    });
    q("rm").onclick = () => { if (!confirm(`Set ${num} met alle prijshistoriek${s.owned ? " én uit je collectie" : ""} verwijderen?`)) return; this.busy(q("rm"), "", async () => { await this.svc("remove_set", { set_number: num }); this.closeDialog(); await this.load(); this.toast(`Set ${num} verwijderd`, "ok"); }); };
    this.hookCharts(dlg);
  }
}
if (!customElements.get("lego-tracker-panel")) customElements.define("lego-tracker-panel", LegoTrackerPanel);
