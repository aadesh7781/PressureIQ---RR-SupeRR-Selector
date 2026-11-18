from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "generated"


def read(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def main() -> int:
    scoring = read("player_scoring_draft.json")
    stability = read("RANKING_STABILITY.json")
    accounting = read("SCORECARD_ACCOUNTING_RECONCILIATION.json")
    primary = scoring["top10_by_candidate"]["current_rate|0.5|0.75"]
    volume = scoring["batter_total_volume_sensitivity"]
    working_batter = next(x for x in volume if x["minimum_total_valid_batting_balls"] == 250)
    bat = [x for x in working_batter["top10"] if x["player_id"] != "305"]
    bat.append(next(x for x in working_batter["next_candidates_after_top10"] if x["player_id"] == "2877"))
    bat.sort(key=lambda x: (-x["score_adjusted"], -x["pressure_balls"], x["player_name"].casefold()))
    prob = {
        role: {r["player_id"]: r["bootstrap_top10_probability"] for r in stability["bootstrap"]["top10_inclusion_summary"][role]}
        for role in ("batters", "bowlers")
    }

    def table(rows, role):
        rendered = []
        for i, row in enumerate(rows, 1):
            p = prob[role].get(row["player_id"], 0)
            exposure = row.get("pressure_balls", row.get("pressure_exposure_balls", 0))
            pressure_matches = row.get("pressure_matches", 0)
            lift = row.get("scoring_lift_component", 0)
            survival = row.get("survival_lift_component", 0)
            rendered.append(
                f"<tr><td>{i}</td><td>{html.escape(row['player_name'])}</td><td>{exposure}</td>"
                f"<td>{pressure_matches}</td><td>{lift:.2f}</td><td>{survival:.2f}</td><td>{p:.1%}</td></tr>"
            )
        return "\n".join(rendered)

    heldout = "\n".join(
        f"<tr><td>{r['held_out_calendar_year']}</td><td>{r['held_out_matches']}</td>"
        f"<td>{r['batters_top10_overlap_with_full_period']}/10</td><td>{r['bowlers_top10_overlap_with_full_period']}/10</td></tr>"
        for r in stability["leave_one_calendar_year_out"]
    )
    checks = accounting["result_margin_checks"]
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PressureIQ draft dashboard</title><style>
:root{{--ink:#172334;--muted:#607083;--blue:#164e83;--line:#dce4ec;--bg:#f4f7fa;--card:#fff;--warn:#fff3d6}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Segoe UI,Arial,sans-serif}}
header{{background:#102d4c;color:white;padding:28px max(22px,calc((100vw - 1160px)/2))}}header h1{{margin:0 0 5px;font-size:28px}}header p{{margin:0;color:#d3e2f1}}
main{{max-width:1160px;margin:24px auto;padding:0 18px}}.notice{{background:var(--warn);border:1px solid #efd596;padding:14px 16px;border-radius:10px;margin-bottom:18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:16px 0}}.card,section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}}
.metric{{font-size:25px;font-weight:700;color:var(--blue)}}.label,.muted{{color:var(--muted)}}h2{{margin:0 0 12px;font-size:20px}}h3{{margin:18px 0 8px;font-size:16px}}
.tablewrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{background:#f3f6f9;position:sticky;top:0}}
input{{padding:9px;border:1px solid var(--line);border-radius:7px;min-width:240px}}.foot{{font-size:12px;color:var(--muted);padding:10px 2px}}
@media(max-width:600px){{header h1{{font-size:23px}}main{{padding:0 10px}}}}
</style></head><body>
<header><h1>PressureIQ · draft decision dashboard</h1><p>SupeRR Selector Season 2 | Generated from the same pipeline data as the methodology report</p></header>
<main><div class="notice"><strong>Provisional — do not submit.</strong> The top tens are exploratory. {accounting['unresolved_extra_rows']} extras remain unclassified, and season-held-out overlap shows roster movement. Methodology has not been frozen.</div>
<div class="grid"><div class="card"><div class="metric">{stability['match_count']:,}</div><div class="label">eligible matches in rank analysis</div></div>
<div class="card"><div class="metric">{stability['bootstrap']['replicates']}</div><div class="label">match-cluster bootstrap replicates</div></div>
<div class="card"><div class="metric">{accounting['unresolved_extra_rows']}</div><div class="label">unresolved extra deliveries</div></div>
<div class="card"><div class="metric">{checks['matching_margins']}/{checks['comparable_results']}</div><div class="label">internal result-margin matches</div></div></div>
<section><h2>Ordered batter candidates</h2><p class="muted">Working screen: at least 250 total valid batting balls. Vijay Shankar replaces Ravichandran Ashwin after role review: Vijay’s supplied batting-order record is top six in 24 of 26 appearances; Ashwin’s median position is 7. This disclosed role-fit override means the list differs from the score-only ranking. Bootstrap frequencies reflect the score-only ranking.</p><input class="filter" placeholder="Filter players" aria-label="Filter batter players"><div class="tablewrap"><table><thead><tr><th>Rank</th><th>Player</th><th>Pressure balls</th><th>Pressure matches</th><th>Scoring lift</th><th>Survival lift</th><th>Bootstrap top-10 frequency</th></tr></thead><tbody>{table(bat, 'batters')}</tbody></table></div></section>
<section><h2>Ordered bowler candidates</h2><p class="muted">Exploratory list under the current equal-weight scoring rule. Inclusion frequency is rank stability, not a probability of player quality.</p><input class="filter" placeholder="Filter players" aria-label="Filter bowler players"><div class="tablewrap"><table><thead><tr><th>Rank</th><th>Player</th><th>Pressure legal balls</th><th>Pressure matches</th><th>Economy saved/over</th><th>Wicket lift</th><th>Bootstrap top-10 frequency</th></tr></thead><tbody>{table(primary['bowlers'], 'bowlers')}</tbody></table></div></section>
<section><h2>Season-held-out stability</h2><p class="muted">Each season is removed in turn, then top tens are compared with the full-period list. This is an influence check, not forward prediction.</p><div class="tablewrap"><table><thead><tr><th>Omitted season</th><th>Matches held out</th><th>Batter overlap</th><th>Bowler overlap</th></tr></thead><tbody>{heldout}</tbody></table></div></section>
<section><h2>Accounting and decision status</h2><p>Internal margins: {checks['matching_margins']} of {checks['comparable_results']} match; the remaining mismatch is traced to a supplied match-result text error and is documented in the reconciliation report. One official scorecard spot-check matched; full independent reconciliation is not complete.</p><p>Unresolved extras: 260 <code>other</code>, one <code>Penalty</code>, and two with no type. Do not infer legal-ball or bowler attribution from numeric amounts alone.</p><p>Next: obtain the source's extra-type field definition or authoritative delivery scorecards, rerun the sensitivity and stability gates, then decide whether the method can be frozen.</p></section>
<p class="foot">Generated {html.escape(scoring['generated_at_utc'])}. This dashboard is a view of reproducible draft outputs; read SCORECARD_ACCOUNTING_RECONCILIATION.md and RANKING_STABILITY.md for limitations.</p></main>
<script>document.querySelectorAll('.filter').forEach(input=>input.addEventListener('input',()=>{{const rows=input.nextElementSibling.querySelectorAll('tbody tr');const q=input.value.toLowerCase();rows.forEach(r=>r.hidden=!r.cells[1].textContent.toLowerCase().includes(q))}}));</script>
</body></html>"""
    out = REPORTS / "PRESSUREIQ_DRAFT_DASHBOARD.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
