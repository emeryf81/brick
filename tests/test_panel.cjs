const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(join(__dirname, "../custom_components/lego_tracker/panel/lego-tracker-panel.js"), "utf8");

function panelFor(events) {
  let Panel;
  const context = vm.createContext({
    HTMLElement: class {},
    customElements: { get: () => undefined, define: (_name, cls) => { Panel = cls; } },
  });
  vm.runInContext(source, context);
  // Exercise the real timeline and renderContent HTML sink without browser lifecycle hooks.
  const panel = Object.create(Panel.prototype);
  const content = {};
  panel.state = { section: "deals", sub: { deals: "today" }, data: { events, retailers: {} } };
  panel.shadowRoot = { getElementById: () => content };
  panel.vToday = panel.timeline;
  panel.bindContent = panel.hookCharts = () => {};
  return { panel, content, context };
}

function event(kind) {
  return { kind, set_number: "10281", name: "Bonsai", price: 30, ts: 1700000000 };
}

test("stored event kinds cannot inject HTML through renderContent", () => {
  const { panel, content } = panelFor([
    event('<img src=x onerror="alert(1)">'),
    event("<svg onload='alert(2)'></svg>"),
    event("future_kind & details"),
  ]);
  panel.renderContent();
  assert.ok(content.innerHTML.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"));
  assert.ok(content.innerHTML.includes("&lt;svg onload=&#39;alert(2)&#39;&gt;&lt;/svg&gt;"));
  assert.ok(content.innerHTML.includes("future_kind &amp; details"));
  assert.doesNotMatch(content.innerHTML, /<(?:img|svg)\b/i);
});

test("supported event kinds keep their labels and icons", () => {
  const { panel, content } = panelFor([event("is_all_time_low"), event("high_discount"), event("target_hit")]);
  panel.renderContent();
  for (const label of ["🔻", "lowest price ever", "🏷️", "high discount", "🎯", "target price reached", "Bonsai"]) {
    assert.ok(content.innerHTML.includes(label));
  }
});

test("event labels are escaped after translation", () => {
  const { panel, content, context } = panelFor([event("high_discount")]);
  vm.runInContext('DICT = { "high discount": "Discount <special> & savings" }', context);
  panel.renderContent();
  assert.ok(content.innerHTML.includes("Discount &lt;special&gt; &amp; savings"));
  assert.doesNotMatch(content.innerHTML, /<special>/);
});
