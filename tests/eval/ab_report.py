#!/usr/bin/env python3
"""Build the A/B comparison HTML table from .ab/*.json.

Columns: alert | local (new code) report | deployed (current) report | chars each
| judge accuracy score each. Reports render as collapsible <details> so the table
stays scannable.

Usage: ab_report.py <out_html>
"""
import html
import json
import os
import sys

AB = os.path.join(os.path.dirname(__file__), "..", "..", ".ab")
FENCE = "`" * 3

TITLES = {
    "alert_01": "WebCore PROD - admin/login error rate",
    "alert_02": "Hangfire recurring job stall after an Octopus deploy",
    "alert_03": "webcore-dotnet - AppSqlException on budget recalculation",
    "alert_04": "CoreSvc PROD - gRPC DeadlineExceeded on orderitems",
    "alert_05": "search-service - SearchException from the Elastic cluster",
}


def load(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def cell_report(text: str) -> str:
    if not text:
        return '<span class="bad">— no report —</span>'
    esc = html.escape(text)
    head = html.escape(text.strip().splitlines()[0][:110]) if text.strip() else ""
    return f'<details><summary>{head}…</summary><pre>{esc}</pre></details>'


def score_class(s):
    if s is None:
        return "na"
    return "good" if s >= 95 else ("ok" if s >= 80 else ("mid" if s >= 60 else "bad"))


def main():
    out_html = sys.argv[1]
    stems = sorted(TITLES)
    rows, agg = [], {"l_chars": [], "d_chars": [], "l_score": [], "d_score": [],
                     "l_tldr": 0, "d_tldr": 0, "l_fence": 0, "d_fence": 0, "n": 0}

    # Read the engine the run actually used rather than asserting one in prose. The
    # hardcoded "reasoningEngines/1000000000000000002 (current prod)" in the header was
    # wrong on two counts — that id is DEV, and prod has no IR engine at all — and it
    # misinformed silently even when the run itself was fine. deployed_run.py now stamps
    # `engine` into every result, so the label is derived from the data it describes.
    engines = set()

    for stem in stems:
        loc = load(os.path.join(AB, f"{stem}.local.json"))
        dep = load(os.path.join(AB, f"{stem}.deployed.json"))
        if dep.get("engine"):
            engines.add(dep["engine"])
        jud = load(os.path.join(AB, f"{stem}.judge.json"))
        lt, dt = loc.get("report_text", "") or "", dep.get("report_text", "") or ""
        ls = (jud.get("a") or {}).get("root_cause_score")
        ds = (jud.get("b") or {}).get("root_cause_score")

        agg["n"] += 1
        if lt:
            agg["l_chars"].append(len(lt))
            agg["l_tldr"] += lt.lstrip().startswith("**TL;DR:**")
            agg["l_fence"] += FENCE in lt
        if dt:
            agg["d_chars"].append(len(dt))
            agg["d_tldr"] += dt.lstrip().startswith("**TL;DR:**")
            agg["d_fence"] += FENCE in dt
        if isinstance(ls, int):
            agg["l_score"].append(ls)
        if isinstance(ds, int):
            agg["d_score"].append(ds)

        why_l = html.escape(((jud.get("a") or {}).get("why") or "")[:300])
        why_d = html.escape(((jud.get("b") or {}).get("why") or "")[:300])
        truth = html.escape((jud.get("true_cause_assessment") or "")[:400])

        rows.append(f"""<tr>
  <td class="alert"><b>{html.escape(TITLES[stem])}</b><div class="meta">{html.escape(stem)}</div>
      <div class="truth"><b>Judge — what the evidence shows:</b> {truth or '—'}</div></td>
  <td>{cell_report(lt)}
      <div class="meta">{loc.get('wall_seconds','?')}s · {loc.get('tools','?')} tools ·
      TL;DR {'YES' if lt.lstrip().startswith('**TL;DR:**') else 'no'} ·
      fence {'YES' if FENCE in lt else 'no'}</div></td>
  <td>{cell_report(dt)}
      <div class="meta">{dep.get('wall_seconds','?')}s · {dep.get('tools','?')} tools ·
      TL;DR {'YES' if dt.lstrip().startswith('**TL;DR:**') else 'no'} ·
      fence {'YES' if FENCE in dt else 'no'}</div></td>
  <td class="num">{len(lt) or '—'}</td>
  <td class="num">{len(dt) or '—'}</td>
  <td class="num {score_class(ls)}">{ls if ls is not None else '—'}<div class="why">{why_l}</div></td>
  <td class="num {score_class(ds)}">{ds if ds is not None else '—'}<div class="why">{why_d}</div></td>
</tr>""")

    def mean(xs):
        return round(sum(xs) / len(xs)) if xs else "—"

    if not engines:
        engine_label = ("UNKNOWN — no result file recorded an engine. Re-run with the "
                        "current deployed_run.py; a score with no recorded target is "
                        "not attributable to a build.")
    elif len(engines) == 1:
        engine_label = next(iter(engines))
    else:
        engine_label = ("MIXED — results span more than one engine, so the 'Deployed' "
                        "column is not one variant: " + ", ".join(sorted(engines)))

    summary = f"""<table class="sum">
<tr><th></th><th>Local (new code)</th><th>Deployed (current)</th></tr>
<tr><td>Reports produced</td><td>{len(agg['l_chars'])}/{agg['n']}</td><td>{len(agg['d_chars'])}/{agg['n']}</td></tr>
<tr><td>Mean chars</td><td>{mean(agg['l_chars'])}</td><td>{mean(agg['d_chars'])}</td></tr>
<tr><td>Mean accuracy (judge)</td><td>{mean(agg['l_score'])}</td><td>{mean(agg['d_score'])}</td></tr>
<tr><td>Has TL;DR</td><td>{agg['l_tldr']}/{agg['n']}</td><td>{agg['d_tldr']}/{agg['n']}</td></tr>
<tr><td>Code-fenced (breaks Teams)</td><td>{agg['l_fence']}/{agg['n']}</td><td>{agg['d_fence']}/{agg['n']}</td></tr>
</table>"""

    doc = f"""<!doctype html><meta charset="utf-8">
<title>IR Agent A/B — new code vs deployed</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:24px;color:#1a1a1a}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;margin-bottom:18px}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border:1px solid #d5d5d5;padding:8px;vertical-align:top;text-align:left}}
 th{{background:#f3f4f6;position:sticky;top:0}}
 td.alert{{width:17%}} .num{{text-align:right;width:5%;font-variant-numeric:tabular-nums;font-weight:600}}
 pre{{white-space:pre-wrap;font:12px/1.45 ui-monospace,Consolas,monospace;background:#fafafa;
      padding:8px;border-radius:4px;max-height:460px;overflow:auto;margin:6px 0 0}}
 summary{{cursor:pointer;color:#0b5cad}}
 .meta{{color:#777;font-size:11px;margin-top:5px}}
 .why{{font-weight:400;font-size:11px;color:#555;text-align:left;margin-top:4px}}
 .truth{{font-size:11px;color:#444;margin-top:6px;background:#fffbe6;padding:5px;border-radius:3px}}
 .good{{background:#e7f6e7}} .ok{{background:#eef7dd}} .mid{{background:#fff3cd}}
 .bad{{background:#fde2e1}} .na{{background:#f5f5f5}}
 table.sum{{width:auto;margin-bottom:22px}} table.sum td,table.sum th{{padding:6px 14px}}
</style>
<h1>Incident Response Agent — A/B: new code vs deployed</h1>
<div class="sub">11 live alerts (SpecOps uptime + 10 most recent SRE - Alerts). Local = this branch
(ado-200002: TL;DR + no-fence prompt, report agent on claude-opus-5 via prod Bifrost).
Deployed = {engine_label} (report agent on Haiku).
Judge = independent Sonnet, blind to variant, scoring root cause only against the frozen rubric.</div>
{summary}
<table>
<tr><th>Alert</th><th>Local (new code) report</th><th>Deployed (current) report</th>
    <th>Local chars</th><th>Dep chars</th><th>Local score</th><th>Dep score</th></tr>
{''.join(rows)}
</table>"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"wrote {out_html}")


if __name__ == "__main__":
    main()
