from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

LOG = logging.getLogger("pressureiq.rr2026")
RR_ID = "3"
TARGET_COMPETITION = "IPL 2026"
PHASES = [("Powerplay", 1, 6), ("Middle", 7, 15), ("Death", 16, 20)]

def phase_for(over_id: int) -> str:
    for label, lower, upper in PHASES:
        if lower <= over_id <= upper:
            return label
    return "Outside standard T20 overs"

def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None

def main() -> int:
    configure_logging(ROOT / "logs")
    integrated_path = ROOT / "data" / "processed" / "integrated_deliveries.csv.gz"
    matches_path = ROOT / "data" / "raw" / "matches.csv"
    with matches_path.open("r", encoding="utf-8-sig", newline="") as f:
        matches = list(csv.DictReader(f))
    rr_matches = {m["match_id"]: m for m in matches
        if m["home_team_id"] == RR_ID or m["away_team_id"] == RR_ID
        if m["comp_id"] and m["match_date"].startswith("2026-")}
    # Restrict by joining the requested season's exact competition label.
    with (ROOT / "data" / "raw" / "competitions.csv").open("r", encoding="utf-8-sig", newline="") as f:
        comp_by_id = {c["comp_id"]: c for c in csv.DictReader(f)}
    rr_matches = {mid: m for mid, m in rr_matches.items() if comp_by_id.get(m["comp_id"], {}).get("comp_name") == TARGET_COMPETITION}
    if not rr_matches:
        raise RuntimeError("No Rajasthan Royals matches found for IPL 2026 in supplied data.")

    innings = defaultdict(lambda: {"runs": 0, "wickets": 0, "known_legal_balls": 0, "rows": 0, "team_id": "", "by_phase": defaultdict(lambda: {"runs": 0, "wickets": 0, "legal_balls": 0})})
    selected_ids = set()
    extra_innings = 0
    with gzip.open(integrated_path, "rt", encoding="utf-8", newline="") as f:
        for d in csv.DictReader(f):
            mid = d["match_id"]
            if mid not in rr_matches or d["competition_name"] != TARGET_COMPETITION:
                continue
            selected_ids.add(mid)
            inn_no = int(d["innings"])
            if inn_no not in (1, 2):
                extra_innings += 1
                continue
            team_id = d["batting_team_id"]
            phase = phase_for(int(d["overid"]))
            key = (mid, inn_no, team_id)
            item = innings[key]
            item["team_id"] = team_id
            item["rows"] += 1
            runs = int(d["delivery_total_runs_provisional"] or 0)
            wicket = (d["is_wicket"] or "").strip().lower() in {"true", "1", "yes"}
            legal = (d["is_legal_ball"] or "").strip().lower() == "true"
            item["runs"] += runs
            item["wickets"] += int(wicket)
            item["known_legal_balls"] += int(legal)
            item["by_phase"][phase]["runs"] += runs
            item["by_phase"][phase]["wickets"] += int(wicket)
            item["by_phase"][phase]["legal_balls"] += int(legal)

    # There should be one batting and one bowling innings for each recorded match;
    # fill a missing phase as zero only for an innings that ended before that phase.
    match_summary, group_innings = [], defaultdict(list)
    match_phase_diffs = defaultdict(lambda: defaultdict(float))
    for mid, match in sorted(rr_matches.items(), key=lambda pair: (pair[1]["match_date"], int(pair[0]))):
        winner = (match["win_team_id"] or "").strip()
        outcome = "Win" if winner == RR_ID else "Loss" if winner not in {"", "0", "None"} else "No result/unknown"
        innings_lines = []
        for inn_no in (1, 2):
            relevant = [(key, data) for key, data in innings.items() if key[0] == mid and key[1] == inn_no]
            if not relevant:
                continue
            key, data = relevant[0]
            team_id = data["team_id"]
            batting_team = match["home_team"] if team_id == match["home_team_id"] else match["away_team"]
            is_rr_batting = team_id == RR_ID
            opp = match["away_team"] if match["home_team_id"] == RR_ID else match["home_team"]
            rr_role = "RR batting" if is_rr_batting else "RR bowling"
            innings_lines.append({"innings": inn_no, "team": batting_team, "team_id": team_id, "rr_role": rr_role,
                "runs": data["runs"], "wickets": data["wickets"], "known_legal_balls": data["known_legal_balls"]})
            for phase, _, _ in PHASES:
                s = data["by_phase"].get(phase, {"runs": 0, "wickets": 0, "legal_balls": 0})
                group_innings[(outcome, rr_role, phase)].append({**s, "match_id": mid, "legal_rate": s["runs"] * 6 / s["legal_balls"] if s["legal_balls"] else None})
                if phase != "Outside standard T20 overs":
                    role_sign = -1 if is_rr_batting else 1
                    match_phase_diffs[(mid, outcome)][phase] += role_sign * s["runs"]
        rr_innings = next((x for x in innings_lines if x["team"] == "Rajasthan Royals"), None)
        opp_innings = next((x for x in innings_lines if x["team"] != "Rajasthan Royals"), None)
        first = next((x for x in innings_lines if x["innings"] == 1), None)
        second = next((x for x in innings_lines if x["innings"] == 2), None)
        margin = re.search(r"won by\s+(\d+)\s+(runs?|wickets?)", match["result"], re.IGNORECASE)
        result_check = "not_comparable"
        if first and second and margin:
            margin_value = int(margin.group(1))
            if margin.group(2).lower().startswith("run"):
                winner_score = first["runs"] if first["team_id"] == winner else second["runs"]
                loser_score = second["runs"] if first["team_id"] == winner else first["runs"]
                result_check = "matches_result_margin" if winner_score - loser_score == margin_value else "does_not_match_result_margin"
            else:
                wickets_left = 10 - second["wickets"]
                chased = second["runs"] > first["runs"]
                result_check = "matches_result_margin" if winner == second["team_id"] and chased and wickets_left == margin_value else "does_not_match_result_margin"
        match_summary.append({"match_id": mid, "date": match["match_date"], "stage": match["stage"],
            "opponent": opp, "outcome": outcome, "toss_winner_id": match["toss_win_team_id"],
            "toss_decision": match["toss_opted"], "venue_id": match["venue_id"],
            "rr_innings": rr_innings, "opponent_innings": opp_innings, "result": match["result"],
            "delivery_score_vs_result_check": result_check})

    phase_summary = []
    for (outcome, role, phase), rows in sorted(group_innings.items()):
        appeared = [r for r in rows if r["legal_balls"] > 0]
        runs = sum(r["runs"] for r in rows)
        legal_balls = sum(r["legal_balls"] for r in rows)
        wickets = sum(r["wickets"] for r in rows)
        phase_summary.append({"outcome": outcome, "rr_role": role, "phase": phase,
            "innings": len(rows), "innings_with_phase_balls": len(appeared),
            "runs": runs, "wickets": wickets, "known_legal_balls": legal_balls,
            "run_rate": runs * 6 / legal_balls if legal_balls else None,
            "wickets_per_phase_appearance": wickets / len(appeared) if appeared else None})
    wins = sum(m["outcome"] == "Win" for m in match_summary)
    losses = sum(m["outcome"] == "Loss" for m in match_summary)
    no_result = len(match_summary) - wins - losses
    comparable = [m for m in match_summary if m["delivery_score_vs_result_check"] != "not_comparable"]
    reconciliation = {"comparable_results": len(comparable),
        "matching_result_margins": sum(m["delivery_score_vs_result_check"] == "matches_result_margin" for m in comparable),
        "nonmatching_result_margins": sum(m["delivery_score_vs_result_check"] == "does_not_match_result_margin" for m in comparable)}
    phase_index = {(r["outcome"], r["rr_role"], r["phase"]): r for r in phase_summary}
    phase_gaps = []
    for role in ("RR batting", "RR bowling"):
        for phase, _, _ in PHASES:
            win = phase_index.get(("Win", role, phase))
            loss = phase_index.get(("Loss", role, phase))
            if not win or not loss or win["run_rate"] is None or loss["run_rate"] is None:
                continue
            # Positive run-rate gap always denotes the unfavorable direction for RR.
            gap = (win["run_rate"] - loss["run_rate"]) if role == "RR batting" else (loss["run_rate"] - win["run_rate"])
            phase_gaps.append({"role": role, "phase": phase, "win_run_rate": win["run_rate"], "loss_run_rate": loss["run_rate"],
                "unfavorable_run_rate_gap": gap, "win_innings": win["innings_with_phase_balls"],
                "loss_innings": loss["innings_with_phase_balls"],
                "win_wickets_per_phase_appearance": win["wickets_per_phase_appearance"],
                "loss_wickets_per_phase_appearance": loss["wickets_per_phase_appearance"]})
    ranked_phase_gaps = sorted(phase_gaps, key=lambda r: r["unfavorable_run_rate_gap"], reverse=True)
    largest_gap = ranked_phase_gaps[0] if ranked_phase_gaps else None
    archetypes = {
        ("RR batting", "Powerplay"): "top-order batter who can score efficiently while preserving early wickets",
        ("RR batting", "Middle"): "middle-order stabilizer who rotates strike and accelerates through the middle overs",
        ("RR batting", "Death"): "end-overs finisher with repeatable boundary output under pressure",
        ("RR bowling", "Powerplay"): "new-ball bowler who can take early wickets without leaking extras",
        ("RR bowling", "Middle"): "middle-overs bowler who controls scoring and creates wickets",
        ("RR bowling", "Death"): "death specialist with reliable yorker and slower-ball execution",
    }
    primary_hypothesis = ({"phase": largest_gap["phase"], "role": largest_gap["role"],
        "observed_unfavorable_run_rate_gap": largest_gap["unfavorable_run_rate_gap"],
        "suggested_profile": archetypes[(largest_gap["role"], largest_gap["phase"])],
        "interpretation": "Descriptive win/loss signal only; confirm with match-level context, pressure metrics, role availability and the complete season sample."} if largest_gap and largest_gap["unfavorable_run_rate_gap"] > 0 else None)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": TARGET_COMPETITION, "team": "Rajasthan Royals", "matches_in_file": len(rr_matches),
        "matches_with_delivery_data": len(selected_ids), "wins": wins, "losses": losses,
        "no_result_or_unknown": no_result, "additional_innings_3_or_4_rows_excluded_from_standard_phase_tables": extra_innings,
        "match_results": match_summary, "phase_comparison": phase_summary, "result_margin_reconciliation": reconciliation,
        "phase_gaps_win_vs_loss": ranked_phase_gaps, "largest_unfavorable_run_rate_gap_hypothesis": primary_hypothesis,
        "limitations": [
            "This review uses only the IPL 2026 matches present in the supplied files; verify that the file contains the complete season before treating the record as final.",
            "The dataset has venue_id but no venue lookup, so this review cannot label stadiums or attribute outcomes to Jaipur.",
            "Delivery total runs use the provisional runs-plus-extras fields; extras types other and Penalty remain unresolved. Result-margin agreement is a partial cross-check, not an independent innings scorecard audit.",
            "Phase comparisons are descriptive across a small season sample; a phase association does not prove it caused a result.",
            "Only innings 1 and 2 are used for standard T20 phase tables; innings 3 and 4 are reported but excluded as likely tie-breakers until confirmed.",
            "The final player type recommendations are scouting hypotheses to test against player rankings, availability and the actual squad; this dataset does not include a complete RR squad/auction constraint model.",
        ],
    }
    out_dir = ROOT / "reports" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rr_2026_review.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Rajasthan Royals 2026 performance review", "", f"Generated: {report['generated_at_utc']}", "",
        "## Scope", "", f"The supplied data contains **{len(rr_matches)} Rajasthan Royals IPL 2026 match records**: **{wins} wins, {losses} losses, {no_result} no-result/unknown**. Delivery data is present for {len(selected_ids)} of these matches. This is a separate scouting analysis; it does not change the required IPL/T20I pressure rankings.", "",
        "## Match-by-match record", "", "| Date | Opponent | Result | RR innings | Opponent innings | Stage | Venue ID | Score/result check |", "|---|---|---|---|---|---|---|---|"]
    for m in match_summary:
        def fmt(i):
            return f"{i['team']} {i['runs']}/{i['wickets']} ({i['known_legal_balls']} known legal balls)" if i else "delivery data missing"
        lines.append(f"| {m['date']} | {m['opponent']} | {m['outcome']} — {m['result']} | {fmt(m['rr_innings'])} | {fmt(m['opponent_innings'])} | {m['stage']} | {m['venue_id']} | {m['delivery_score_vs_result_check']} |")
    lines += ["", "## Match score cross-check", "", f"Delivery score totals match the winner margins stated in `matches.csv` for {reconciliation['matching_result_margins']} of {reconciliation['comparable_results']} comparable results. This is a consistency check, not a substitute for a full innings scorecard source.", "",
        "## Phase comparison", "", "Run rate is provisional delivery runs per six known legal balls. Wickets are the batting side's team wickets (not only bowler-credit dismissals). Each row is grouped by match outcome and RR's role.", "",
        "| RR outcome | RR role | Phase | Innings | Runs | Batting-team wickets | Known legal balls | Runs/over | Wickets per phase appearance |", "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in phase_summary:
        rr = f"{r['run_rate']:.2f}" if r["run_rate"] is not None else "—"
        wp = f"{r['wickets_per_phase_appearance']:.2f}" if r["wickets_per_phase_appearance"] is not None else "—"
        lines.append(f"| {r['outcome']} | {r['rr_role']} | {r['phase']} | {r['innings']} | {r['runs']:,} | {r['wickets']} | {r['known_legal_balls']:,} | {rr} | {wp} |")
    lines += ["", "### Largest win-loss phase gaps", "", "A positive gap means the phase run rate is worse for RR in losses: lower scoring while RR bats, or higher scoring by the opponent while RR bowls.", "",
        "| Gap rank | RR role | Phase | Runs/over in wins | Runs/over in losses | Unfavorable gap | Batting wickets/phase: wins vs losses |", "|---:|---|---|---:|---:|---:|---|"]
    for rank, g in enumerate(ranked_phase_gaps, start=1):
        sign = "+" if g["unfavorable_run_rate_gap"] >= 0 else ""
        lines.append(f"| {rank} | {g['role']} | {g['phase']} | {g['win_run_rate']:.2f} | {g['loss_run_rate']:.2f} | {sign}{g['unfavorable_run_rate_gap']:.2f} | {g['win_wickets_per_phase_appearance']:.2f} vs {g['loss_wickets_per_phase_appearance']:.2f} |")
    lines += ["", "## What player profile could address the observed problem?", "",
        "Use the phase table to locate whether the recurring shortfall appears while RR bats or bowls, and in which phase. Any role suggestion must remain a hypothesis: a lower run rate, extra wickets, or higher conceded rate in losses can guide what to test, but this small observational sample does not establish causation.", "",
        "### Data-led lead to test", ""]
    if primary_hypothesis:
        lines.append(f"The largest descriptive run-rate gap between wins and losses is in **{primary_hypothesis['phase']} while RR is {primary_hypothesis['role'].replace('RR ', '').lower()}**: {largest_gap['win_run_rate']:.2f} runs/over in wins versus {largest_gap['loss_run_rate']:.2f} in losses (an unfavorable {largest_gap['unfavorable_run_rate_gap']:+.2f} runs/over gap). Batting-side wickets in this phase were {largest_gap['win_wickets_per_phase_appearance']:.2f} per phase appearance in wins versus {largest_gap['loss_wickets_per_phase_appearance']:.2f} in losses. This points to testing the profile: **{primary_hypothesis['suggested_profile']}**.")
    else:
        lines.append("The phase run-rate comparison does not identify an unfavorable loss gap in this sample. Use the wicket and match tables below as context and avoid naming a role need until a clearer signal is found.")
    lines += ["", "### Other role hypotheses to evaluate", "",
        "- **If powerplay batting is the shortfall:** test a reliable top-order player who preserves wicket resources while maintaining scoring pressure.",
        "- **If middle-overs batting is the shortfall:** test a spin-capable middle-order stabilizer who can rotate strike and accelerate against spin.",
        "- **If death-over batting is the shortfall:** test a finisher with repeatable boundary output and low dismissal risk at the end of an innings.",
        "- **If powerplay bowling is the shortfall:** test a new-ball bowler with early-wicket threat and control of extras.",
        "- **If middle-overs bowling is the shortfall:** test a wicket-taking, run-controlling spin option matched to opposition handedness and venue once venue labels are available.",
        "- **If death bowling is the shortfall:** test a specialist who reliably executes yorkers/slower balls and limits boundary leakage under pressure.",
        "",
        "These are role archetypes, not player-signing claims. Do not infer that a specific player is available from this dataset alone.",
        "", "## Limits", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    lines += ["", "The structured match and phase summaries are in `rr_2026_review.json`.", ""]
    (out_dir / "RR_2026_PERFORMANCE_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote RR 2026 review for %d matches", len(rr_matches))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
