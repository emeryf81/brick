"""List every translatable English string of the integration (backend T()/tr()/LocalizedError, panel t(),
userscript {{t:}} tokens, label tables). Usage: python scripts/i18n_extract.py [out.json]
Without an argument it prints the strings missing from each panel/i18n/<lang>.json."""
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
B = str(ROOT / "custom_components/lego_tracker") + "/"
out = []
def add(s):
    if s and s not in out and re.search(r"[A-Za-z]", s): out.append(s)
STR = r'"((?:[^"\\\n]|\\.)*)"'
js = open(B + "panel/lego-tracker-panel.js").read()
for m in re.finditer(r'\bt\(\s*' + STR, js): add(json.loads('"' + m.group(1) + '"'))
# t(cond ? "a" : "b")
for m in re.finditer(r'\bt\(\s*[^"()]*?\?\s*' + STR + r'\s*:\s*' + STR, js):
    add(json.loads('"' + m.group(1) + '"')); add(json.loads('"' + m.group(2) + '"'))
# SECTIONS + label tables translated at render time
sec = js[js.index("const SECTIONS = {"):js.index("class LegoTrackerPanel")]
for m in re.finditer(r'(?:label|hint): ' + STR, sec): add(m.group(1))
for m in re.finditer(r'\["\w+", ' + STR + r'\]', sec): add(m.group(1))
for blk in re.findall(r'sorts: \[(.*?)\]\s*\}', js): 
    for m in re.finditer(r'\["\w+", ' + STR + r'\]', blk): add(m.group(1))
for m in re.finditer(r'(?:srcLabel|SOURCE_WORDS) = \{(.*?)\};', js, re.S):
    for x in re.finditer(r': ' + STR, m.group(1)): add(x.group(1))
for m in re.finditer(r'const ic = \{(.*?)\};', js):
    for x in re.finditer(r'\["[^"]*", ' + STR + r'\]', m.group(1)): add(x.group(1))
leg = js[js.index("  legalHtml() {"):js.index("return sec.map")]      # the legal terms: headings and paragraphs
for m in re.finditer(STR, leg): add(json.loads('"' + m.group(1) + '"'))
for c in ["Sealed", "Opened", "Built", "Incomplete", "Unknown", "Job"]: add(c)
for m in re.finditer(r'\["\w+", ' + STR + r', \[', js[js.index("const COLOR_THEMES"):js.index("const COLOR_THEMES") + 1200]): add(m.group(1))
# backend
for f in ["coordinator.py", "models.py", "parsers.py", "lab.py", "client.py", "csv_import.py", "notifications.py", "__init__.py", "websocket_api.py", "shops.py", "sensor.py", "partner_api.py", "compare.py"]:
    src = open(B + f).read()
    for m in re.finditer(r'\b(?:T|tr|LocalizedError)\(\s*' + STR, src): add(ast.literal_eval('"' + m.group(1) + '"'))
    for m in re.finditer(r'\b(?:T|tr|LocalizedError)\(\s*\n\s*' + STR, src): add(ast.literal_eval('"' + m.group(1) + '"'))
us = open(B + "userscript.template.js").read()
for m in re.finditer(r"\{\{tj?:(.*?)\}\}", us): add(m.group(1))
from custom_components.lego_tracker import csv_import, models, notifications  # noqa: E402
for v in models.ACTIVITY_KINDS.values(): add(v)
for v in notifications.TRIGGERS.values(): add(v[0] if isinstance(v, tuple) else v)
for v in csv_import.FIELD_LABELS.values(): add(v)


def extract() -> list[str]:
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1:
        json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        print(len(out), "strings")
    else:
        for f in sorted((ROOT / "custom_components/lego_tracker/panel/i18n").glob("*.json")):
            d = json.loads(f.read_text("utf-8"))
            missing = [s for s in out if s not in d]
            print(f"{f.stem}: {len(out) - len(missing)}/{len(out)} translated", *missing[:20], sep="\n  ")
