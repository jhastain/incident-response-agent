"""Render the reusable IR-batch HTML report.

Usage: python build_html.py <WORKDIR> [TITLE]
Inputs in WORKDIR:
  meta.json          authored per-alert assessment (see schema below)
  extracted.json     from extract.py (has_report, events, report_tokens)
  toolcalls.json     from extract.py (tool name -> count)
  alert_<k>.txt      the verbiage sent to the agent
  report_<k>.md      the full agent report (if it reached report_agent)
Output: ir-agent-batch-report.html in WORKDIR.

meta.json schema — object keyed by alert id ("alert_01" ...), each:
{
  "thread": "<teams msg id>", "src": "Octopus|Sentry|Datadog/PD #..|GCP/PD #..",
  "when": "MM-DD HH:MM CST", "title": "...", "summary": "...",
  "verified": [["fact","source"], ...],
  "notq": "what authoritative system was NOT queried -> what stays unverified",
  "cls": "<category · SEV · env · status>", "root": "...", "do": "...",
  "sub": {"CLS":0-25, "SRC":0-30, "EVID":0-25, "HON":0-20},
  "rat": "one-line score rationale"
}
Score = sum(sub). SRC (authoritative-source coverage) is the dominant lever:
did the agent query the system that OWNS the truth for this alert type
(Octopus for deploy stalls, Sentry for Sentry alerts, Datadog for Datadog,
ADO for pipelines, the actual log line for log alerts)? If not, root cause is a
hypothesis and the score is capped. Accuracy is an ASSESSMENT, not verified vs live systems.
"""
import html
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
TITLE = sys.argv[2] if len(sys.argv) > 2 else "IR Agent — channel alert batch"
OUT = os.path.join(DIR, "ir-agent-batch-report.html")

meta = json.load(open(os.path.join(DIR, "meta.json"), encoding="utf-8"))
ext = json.load(open(os.path.join(DIR, "extracted.json"), encoding="utf-8"))
tc = json.load(open(os.path.join(DIR, "toolcalls.json"), encoding="utf-8"))
def read(p):
    try: return open(p, encoding="utf-8").read()
    except: return ""
def esc(s): return html.escape(s or "")
def col(s): return "#1a7f37" if s>=80 else "#3a7d00" if s>=70 else "#9a6700" if s>=60 else "#b35900"

order = sorted(meta)
for k in order: meta[k]["score"] = sum(meta[k]["sub"].values())
avg = round(sum(meta[k]["score"] for k in order)/len(order))
full = sum(1 for k in order if ext.get(k,{}).get("has_report"))

tr=[]
for i,k in enumerate(order,1):
    m=meta[k]; e=ext.get(k,{}); calls=sorted(tc.get(k,{}).items(),key=lambda x:-x[1])
    sc=m["score"]; c=col(sc); s=m["sub"]
    vf="".join(f"<li>{esc(f)} <span class=srcg>&mdash; {esc(src)}</span></li>" for f,src in m["verified"])
    chips="".join(f"<span class=tc>{esc(n)} <b>{cnt}</b></span>" for n,cnt in calls)
    sublbl=f"CLS {s['CLS']}/25 · SRC {s['SRC']}/30 · EVID {s['EVID']}/25 · HON {s['HON']}/20"
    tr.append(f"""<tr>
<td class=n>{i}</td>
<td><div class=ttl>{esc(m['title'])}</div><div class=meta><span class=pill>{esc(m['src'])}</span><span class="pill alt">{esc(m['when'])}</span></div><div class=sum>{esc(m['summary'])}</div></td>
<td><details><summary>show</summary><pre>{esc(read(os.path.join(DIR,k+'.txt')))}</pre></details></td>
<td><div class=meta><span class="pill alt">{sum(tc.get(k,{}).values())} calls</span></div><details><summary>tools</summary><div class=tcwrap>{chips}</div></details></td>
<td><ul class=vf>{vf}</ul><div class=nq><b>Not queried:</b> {esc(m['notq'])}</div></td>
<td><div class=meta><span class="pill alt">{e.get('events','?')} ev</span><span class="pill alt">{e.get('report_tokens','?')} tok</span></div><details><summary>full report</summary><pre>{esc(read(os.path.join(DIR,'report_'+k+'.md')))}</pre></details></td>
<td class=fin><ul><li><b>Class:</b> {esc(m['cls'])}</li><li><b>Root cause:</b> {esc(m['root'])}</li><li><b>Action:</b> {esc(m['do'])}</li></ul></td>
<td><div class=bar><span style="width:{sc}%;background:{c}"></span></div><div class=sv style="color:{c}">{sc}<span class=p>/100</span></div><div class=subl>{sublbl}</div><div class=rat>{esc(m['rat'])}</div></td>
</tr>""")

doc=f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(TITLE)}</title><style>
*{{box-sizing:border-box}}body{{font:13px/1.45 -apple-system,Segoe UI,Roboto,Arial;color:#1f2328;margin:0;padding:20px}}
h1{{font-size:18px;margin:0 0 2px}}.sub{{color:#656d76;font-size:12px;margin-bottom:12px}}
.stats{{display:flex;gap:10px;margin-bottom:12px}}.stat{{border:1px solid #d8dee4;border-radius:8px;padding:8px 14px;background:#f6f8fa}}
.stat .nn{{font-size:20px;font-weight:700}}.stat .ll{{font-size:11px;color:#656d76;text-transform:uppercase}}
.rub{{border-left:3px solid #0969da;background:#ddf4ff55;padding:8px 12px;border-radius:4px;font-size:12px;margin-bottom:14px}}.rub li{{margin:2px 0}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d8dee4;padding:8px 9px;vertical-align:top;text-align:left}}
th{{background:#eaeef2;font-size:11px;text-transform:uppercase}}td.n{{text-align:center;font-weight:700;color:#656d76;width:20px}}
th:nth-child(2),td:nth-child(2){{width:15%}}th:nth-child(5),td:nth-child(5){{width:18%}}th:nth-child(7),td:nth-child(7){{width:16%}}th:nth-child(8),td:nth-child(8){{width:15%}}
.ttl{{font-weight:600;margin-bottom:4px}}.sum{{color:#1f2328}}.meta{{margin:4px 0}}
.pill{{display:inline-block;background:#ddf4ff;color:#0969da;border-radius:20px;padding:1px 8px;font-size:10.5px;margin:1px 3px 1px 0;white-space:nowrap}}.pill.alt{{background:#eef1f4;color:#57606a}}
ul{{margin:2px 0;padding-left:16px}}li{{margin:3px 0}}ul.vf li{{margin:4px 0}}.srcg{{color:#0969da;font-size:11px}}
.nq{{margin-top:6px;color:#8a6d00;background:#fff8e5;border-radius:4px;padding:4px 6px;font-size:11.5px}}
.tcwrap{{margin-top:6px;display:flex;flex-wrap:wrap;gap:3px}}
.tc{{background:#eef1f4;color:#1f2328;border-radius:4px;padding:1px 6px;font:10.5px ui-monospace,Consolas,monospace;white-space:nowrap}}.tc b{{color:#0969da}}
.fin code{{background:#f6f8fa;padding:0 3px;border-radius:3px;font-size:11.5px}}
details>summary{{cursor:pointer;color:#0969da;font-size:11.5px}}
pre{{white-space:pre-wrap;word-wrap:break-word;background:#f6f8fa;border:1px solid #d8dee4;border-radius:5px;padding:9px;font:11px/1.4 ui-monospace,Consolas,monospace;max-height:420px;overflow:auto;margin-top:6px}}
.bar{{height:7px;background:#eef1f4;border-radius:4px;overflow:hidden}}.bar span{{display:block;height:100%}}
.sv{{font-size:17px;font-weight:700;margin:2px 0}}.p{{font-size:11px;color:#656d76}}.subl{{font-size:10.5px;color:#57606a;margin-bottom:3px}}.rat{{color:#656d76;font-size:11.5px}}
</style></head><body>
<h1>{esc(TITLE)}</h1>
<div class=sub>Teams &rsaquo; SRE - Alerts &nbsp;|&nbsp; sent to deployed IR Agent via <code>/investigate</code></div>
<div class=stats>
<div class=stat><div class=nn>{full}/{len(order)}</div><div class=ll>full reports</div></div>
<div class=stat><div class=nn>{avg}/100</div><div class=ll>avg accuracy</div></div>
<div class=stat><div class=nn>~16</div><div class=ll>agents/run</div></div>
</div>
<div class=rub><b>Accuracy = assessment, not verified vs live systems.</b> Four parts:
<ul>
<li><b>CLS /25</b> — category, severity, environment, status correct vs the card.</li>
<li><b>SRC /30 (dominant lever)</b> — did it query the system that <i>owns the truth</i> for this alert type? Octopus for deploy stalls, Sentry for Sentry alerts, Datadog for Datadog, ADO for pipelines, the actual log line for log alerts. When that system wasn't queried, the root cause is a <i>hypothesis</i> and the score is capped.</li>
<li><b>EVID /25</b> — findings backed by real tool output (incl. corroborating GCP signals).</li>
<li><b>HON /20</b> — correct confidence labels, gaps flagged, no fabrication.</li>
</ul>
<b>"Tools called" counts are attempts and include MCP calls that failed</b> (list_services / list_instances / list_instance_group_managers / list_log_entries were largely MCP failures; query_gcp_logs / query_time_series / list_cloud_run_revisions are SDK calls that succeeded).</div>
<table><thead><tr><th>#</th><th>(a) Incident &amp; summary</th><th>(b) Sent</th><th>Tools called</th><th>(c) Verified facts &amp; source</th><th>(d) Agent result (full)</th><th>(e) Final result</th><th>Accuracy</th></tr></thead>
<tbody>{''.join(tr)}</tbody></table>
</body></html>"""
open(OUT,"w",encoding="utf-8").write(doc)
print("wrote",OUT,"| avg",avg,"| full",full,"/",len(order))
