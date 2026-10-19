"""Extract IR Agent reports + tool-call telemetry from a batch run.

Usage: python extract.py <WORKDIR>
Reads  out_alert_*.jsonl in WORKDIR.
Writes extracted.json, toolcalls.json, and report_alert_*.md (for runs that reached report_agent).
Prints a per-alert status line (report present?, events, tokens, last author).
"""
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()

ext, tc = {}, {}
for path in sorted(glob.glob(os.path.join(DIR, "out_alert_*.jsonl"))):
    base = os.path.basename(path).replace("out_", "").replace(".jsonl", "")
    report_text, report_tokens, total, last_author = None, "?", 0, "unknown"
    authors, calls = [], {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        a = d.get("author")
        if a:
            last_author = a; authors.append(a)
        for part in d.get("content", {}).get("parts", []):
            fc = part.get("function_call")
            if fc and fc.get("name"):
                calls[fc["name"]] = calls.get(fc["name"], 0) + 1
            t = part.get("text")
            if t and a == "report_agent" and (report_text is None or len(t) > len(report_text)):
                report_text = t
                report_tokens = d.get("usage_metadata", {}).get("total_token_count", "?")
    seen = []
    for a in authors:
        if a not in seen:
            seen.append(a)
    ext[base] = dict(events=total, authors=seen, report_tokens=report_tokens,
                     has_report=report_text is not None, report_text=report_text or "",
                     last_author=last_author)
    tc[base] = calls
    if report_text:
        open(os.path.join(DIR, f"report_{base}.md"), "w", encoding="utf-8").write(report_text)

json.dump(ext, open(os.path.join(DIR, "extracted.json"), "w", encoding="utf-8"), indent=1)
json.dump(tc, open(os.path.join(DIR, "toolcalls.json"), "w", encoding="utf-8"), indent=1)
for k in sorted(ext):
    e = ext[k]
    print(f"{k}: report={e['has_report']} events={e['events']} tokens={e['report_tokens']} "
          f"calls={sum(tc[k].values())} last={e['last_author']}")
ok = sum(1 for e in ext.values() if e["has_report"])
print(f"\n{ok}/{len(ext)} reached report_agent. Re-run any that didn't (often a ~600s stream-cap cut).")
