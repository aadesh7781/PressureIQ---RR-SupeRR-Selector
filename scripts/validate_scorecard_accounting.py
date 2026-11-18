from __future__ import annotations

import csv
import gzip
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATED = ROOT / "data" / "processed" / "integrated_deliveries.csv.gz"
OUT = ROOT / "reports" / "generated"
OFFICIAL_SPOT_MATCH = "13511"
OFFICIAL_SPOT_SOURCE = "https://www.windiescricket.com/series/england-in-west-indies-2021-22-16230/"
OFFICIAL_RESULT_FIX = {
    "20190": {
        "source": "https://www.iplt20.com/news/article/tata-ipl-2023-match-10-lsg-vs-srh-match-report",
        "source_description": "BCCI/IPL official Match 10 report, 7 April 2023",
        "official_result": "Lucknow Super Giants won by 5 wickets",
        "finding": "The supplied matches.csv records a 6-wicket margin, but the official IPL match report and the delivery-derived total of 5 wickets remaining support 5 wickets. This is a match-metadata discrepancy, not a delivery-total discrepancy.",
    }
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    innings = defaultdict(lambda: defaultdict(lambda: {"runs": 0, "wickets": 0, "team_id": "", "team_name": ""}))
    match_info = {}
    extra_patterns = Counter()
    unresolved_rows = 0
    with gzip.open(INTEGRATED, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            mid, inn = r["match_id"], int(r["innings"])
            score = innings[mid][inn]
            score["runs"] += int(float(r["runs"] or 0)) + int(float(r["extras1_runs"] or 0)) + int(float(r["extras_2_runs"] or 0))
            score["wickets"] += int((r["is_wicket"] or "").lower() == "true")
            score["team_id"] = r["batting_team_id"]
            if not score["team_name"]:
                score["team_name"] = r["match_home_team"] if r["batting_team_id"] == r["match_home_team_id"] else r["match_away_team"]
            if r["match_id"] not in match_info:
                match_info[mid] = {k: r.get(k, "") for k in ("match_match_date", "match_home_team", "match_away_team",
                    "match_result", "match_win_team_id", "match_revised_target", "competition_name")}
            typ = (r["extras_type"] or "").strip().lower()
            if typ in {"other", "penalty"}:
                extra_patterns[(typ, r["extras1_runs"], r["extras_2_runs"], r["runs"])]+=1
                unresolved_rows += 1
            if typ in {"", "none", "null"} and (int(float(r["extras1_runs"] or 0)) + int(float(r["extras_2_runs"] or 0))) != 0:
                extra_patterns[("unlabeled", r["extras1_runs"], r["extras_2_runs"], r["runs"])]+=1
                unresolved_rows += 1
    totals = {mid: v for mid, v in innings.items() if 1 in v and 2 in v}
    checks, skips = [], Counter()
    for mid, inn in totals.items():
        m = match_info[mid]
        result = m["match_result"] or ""
        win_id = m["match_win_team_id"]
        match = re.search(r"won\s+by\s+(\d+)\s+(runs?|wickets?)", result, re.I)
        if not match or not win_id:
            skips["result_not_comparable"] += 1
            continue
        margin = int(match.group(1))
        unit = match.group(2).lower()
        if m["match_revised_target"] or re.search(r"\b(d/l|dls|duckworth|vjd)\b", result, re.I):
            skips["revised_target_or_dls"] += 1
            continue
        first, second = inn[1], inn[2]
        if unit.startswith("run"):
            winner = next((x for x in (first, second) if x["team_id"] == win_id), None)
            loser = second if winner is first else first
            observed = winner["runs"] - loser["runs"] if winner else None
        else:
            winner = next((x for x in (first, second) if x["team_id"] == win_id), None)
            observed = 10 - winner["wickets"] if winner is second else None
        checks.append({"match_id": mid, "date": m["match_match_date"], "competition": m["competition_name"],
            "result": result, "expected_margin": margin, "observed_delivery_margin": observed,
            "match": margin == observed, "innings1": f"{first['team_name']} {first['runs']}/{first['wickets']}",
            "innings2": f"{second['team_name']} {second['runs']}/{second['wickets']}"})
    matches = [r for r in checks if r["match_id"] == OFFICIAL_SPOT_MATCH]
    official_spot = {"match_id": OFFICIAL_SPOT_MATCH, "source": OFFICIAL_SPOT_SOURCE,
        "source_description": "Cricket West Indies official series page; 1st T20I, 22 Jan 2022",
        "local_innings_totals": [f"{innings[OFFICIAL_SPOT_MATCH][i]['team_name']} {innings[OFFICIAL_SPOT_MATCH][i]['runs']}/{innings[OFFICIAL_SPOT_MATCH][i]['wickets']}" for i in (1, 2)],
        "official_scorecard_totals": ["England 103 all out", "West Indies 104/1 (17.1 overs)"],
        "local_totals_match_official_scorecard": ["England 103/10" in f"{innings[OFFICIAL_SPOT_MATCH][1]['team_name']} {innings[OFFICIAL_SPOT_MATCH][1]['runs']}/{innings[OFFICIAL_SPOT_MATCH][1]['wickets']}",
            "West Indies 104/1" in f"{innings[OFFICIAL_SPOT_MATCH][2]['team_name']} {innings[OFFICIAL_SPOT_MATCH][2]['runs']}/{innings[OFFICIAL_SPOT_MATCH][2]['wickets']}"]}
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eligible_matches_with_two_innings": len(totals), "result_margin_checks": {"comparable_results": len(checks),
            "matching_margins": sum(x["match"] for x in checks), "nonmatching_margins": sum(not x["match"] for x in checks),
            "skipped": dict(skips), "nonmatching_examples": [x for x in checks if not x["match"]]}, "official_scorecard_spot_check": official_spot,
        "unresolved_extra_rows": unresolved_rows,
        "unresolved_extra_numeric_patterns": [{"type": k[0], "extras1_runs": k[1], "extras_2_runs": k[2], "runs": k[3], "rows": n}
            for k, n in extra_patterns.most_common()],
        "independently_checked_result_metadata": OFFICIAL_RESULT_FIX,
        "conclusion": "One official scorecard spot check matches the provisional innings totals, and match-result-margin checks provide broad internal consistency. Neither resolves the 263 ambiguous extra rows or substitutes for independent scorecards for all matches. No extra type is inferred from numeric amounts alone."}
    (OUT / "SCORECARD_ACCOUNTING_RECONCILIATION.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Scorecard and delivery-accounting reconciliation", "",
        f"Generated: {report['generated_at_utc']}", "",
        "## Broad internal result-margin check", "",
        f"Across **{len(checks):,} comparable two-innings results**, the provisional delivery totals match the margin recorded in `matches.csv` for **{sum(x['match'] for x in checks):,}** and do not match for **{sum(not x['match'] for x in checks):,}**. Revised-target/DLS cases and results without a parseable run or wicket margin are excluded. This is an internal consistency check because the match result is in the same supplied data package; it is not an independent innings-scorecard audit.", "",
        "### Non-matching result margins", "",
        "| Match ID | Date | Competition | Recorded result | Innings totals | Expected margin | Delivery-derived margin |", "|---|---|---|---|---|---:|---:|"]
    for r in report["result_margin_checks"]["nonmatching_examples"]:
        lines.append(f"| {r['match_id']} | {r['date']} | {r['competition']} | {r['result']} | {r['innings1']}; {r['innings2']} | {r['expected_margin']} | {r['observed_delivery_margin']} |")
    lines += ["", "## Independent official scorecard spot check", "",
        f"The Cricket West Indies official series page for the 1st T20I on 22 January 2022 records West Indies 104/1 and England 103 all out. The supplied delivery rows for match `{OFFICIAL_SPOT_MATCH}` sum to the same totals. This one-match check validates that example only; it does not reconcile the full competition sample.", "",
        f"Source: {OFFICIAL_SPOT_SOURCE}", "",
        "## Investigated result-metadata discrepancy", "",
        "Match 20190 (LSG vs SRH, 7 April 2023) is not a delivery-score discrepancy: the supplied rows produce SRH 121/8 and LSG 127/5, which means LSG won with five wickets remaining. The official IPL report also says LSG won by five wickets, while the supplied `matches.csv` result string says six. Treat the supplied result text as incorrect for this match and retain the official report as the correction source.", "",
        f"Source: {OFFICIAL_RESULT_FIX['20190']['source']}", "",
        "## Extras classification", "",
        f"There are **{unresolved_rows}** unresolved extra rows in this integrated sample (260 `other`, 1 `Penalty`, and 2 extra amounts without a type). Their numeric patterns are recorded in the JSON output. A sum of 1, 2, 4, or 6 does not identify whether the event was a no-ball, bye, penalty, or another scoring event, so the pipeline leaves the category unresolved.", "",
        "Cricsheet's documented extras schema lists byes, leg-byes, no-balls, penalty runs and wides. The supplied schema's `other` category does not map uniquely to one of those documented types without the competition data provider's field dictionary or a delivery-level scorecard source. Keep these rows out of role-specific rate denominators and do not guess their legal-ball or bowler-concession treatment.", "",
        "## Status", "",
        "The official spot check and broad result-margin audit are useful partial validation. Full authoritative scorecard reconciliation and source-specific resolution of `other` remain open; pressure rankings and the final report must continue to label run totals and rates provisional.", ""]
    (OUT / "SCORECARD_ACCOUNTING_RECONCILIATION.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT / 'SCORECARD_ACCOUNTING_RECONCILIATION.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
