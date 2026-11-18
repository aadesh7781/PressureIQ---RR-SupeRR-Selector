from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "generated"


def read(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def main() -> int:
    scoring = read("player_scoring_draft.json")
    audit = read("data_audit.json")
    scope = read("competition_scope.json")
    pressure = read("pressure_candidate_sensitivity.json")
    par = read("first_innings_par_comparison.json")
    rr = read("rr_2026_review.json")
    phase_fit = read("candidate_phase_fit.json")
    batter_eligibility = read("BATTER_ELIGIBILITY_REVIEW.json")
    rank_stability = read("RANKING_STABILITY.json")
    accounting = read("SCORECARD_ACCOUNTING_RECONCILIATION.json")
    primary = scoring["top10_by_candidate"]["current_rate|0.5|0.75"]
    pressure_rows = primary["pressure_state_rows"]
    stability = scoring["score_assumption_sensitivity"]
    volume = scoring["batter_total_volume_sensitivity"]
    working_batter = next(r for r in volume if r["minimum_total_valid_batting_balls"] == 250)
    # Transparent role review: replace Ashwin with the next score-ranked
    # candidate who has stronger specialist-style batting deployment evidence.
    role_review_replacement = next(r for r in working_batter["next_candidates_after_top10"] if r["player_id"] == "2877")
    working_batter_final = [r for r in working_batter["top10"] if r["player_id"] != "305"] + [role_review_replacement]
    working_batter_final.sort(key=lambda r: (-r["score_adjusted"], -r["pressure_balls"], r["player_name"].casefold()))
    lines = [
        "# PressureIQ — methodology report draft", "",
        f"Generated from the six supplied CSV files. Scoring analysis timestamp: {scoring['generated_at_utc']}.", "",
        "> **Status: methodology and ranking draft; not frozen.** These two ordered top tens are exploratory candidates, not final competition rankings. Delivery accounting still contains unresolved extras and rank-level stability is mixed. Player-selector eligibility and Jaipur/dew analysis are outside the current milestone. No external submission has been made.", "",
        "## Competition requirement and dataset scope", "",
        "The official deliverable is two separate ordered lists of ten players (batters and bowlers) plus a transparent methodology report. Each list contains unique players; the same player can appear in both. The project uses the supplied brief's IPL and T20I scope from 2022 onward; it does not limit the rankings to Rajasthan Royals.", "",
        f"The supplied data covers {scope['eligible_match_rows']:,} eligible match rows and {scope['eligible_ball_rows']:,} eligible ball-by-ball rows: {scope['match_counts_by_kind'].get('IPL', 0):,} IPL matches and {scope['match_counts_by_kind'].get('T20I', 0):,} T20Is. There are {scope['eligible_matches_without_ball_data']} eligible matches without ball data.", "",
        "## Venue, date and match context audit", "",
        "`matches.csv` contains match date, venue ID and country, toss winner/decision, team/innings context and result. It does not contain a stadium name, city or time of day. None of the six supplied tables is a venue lookup. `ball_by_ball.csv` contains `delivery_ist_time` but has no venue field or measured dew observation. The supplied files therefore cannot verify Jaipur/Sawai Mansingh coverage or measured dew; an external venue crosswalk and observational source would be required. Time-of-day or innings indicators may only be reported as proxies.", "",
        "## Delivery processing and current limits", "",
        "The pipeline joins eligible deliveries to match, team and player context, creates pre-delivery match states, and records legal-ball status, provisional score, wickets, phase, target and required rate where supported. Ambiguous extra categories are excluded from role-rate denominators where attribution cannot be established. The source package has unresolved `other`, penalty and unlabeled extras, no independent innings scorecard totals, and source boundary flags that disagree with run totals on a small number of deliveries. Treat derived run totals and metrics as provisional until reconciled.", "",
        "## Pressure definition and checks", "",
        f"The current candidate uses a 50/50 blend of demand and resource stress with a 75th-percentile threshold; demand uses current-rate stress in the first innings and required-rate stress in chases. This identifies {pressure_rows:,} pressure delivery states across formats, phases and remaining-ball bands. It is a transparent candidate rule, not a direct measure of player psychology.", "",
        f"A cross-validated first-innings par method was compared with current-rate stress. Match-level folds trained on {par['training_checkpoints']:,} six-ball checkpoints and generated predictions for {par['cross_validated_state_rows']:,} state rows; held-out expected-final-score MAE was {par['expected_final_score_mae_delivery_states']:.2f} runs (RMSE {par['expected_final_score_rmse_delivery_states']:.2f}). At equal weights and p75, par and current-rate player top tens share {next((r['batters_top10_overlap'] for r in scoring['sensitivity_vs_current_rate_50_75'] if r['method']=='par_first_innings' and r['demand_weight']==0.5 and r['cutoff']==0.75), 'n/a')}/10 batters and {next((r['bowlers_top10_overlap'] for r in scoring['sensitivity_vs_current_rate_50_75'] if r['method']=='par_first_innings' and r['demand_weight']==0.5 and r['cutoff']==0.75), 'n/a')}/10 bowlers. This is method agreement, not out-of-sample validation of player value.", "",
        "## Player scoring method", "",
        "For each player, compare pressure deliveries with that player's non-pressure baseline within the same format, phase and innings type. Batter components are run-rate lift per 100 balls and dismissal-rate reduction per 100 balls. Bowler components are economy saved per over and credited-wicket lift per 24 legal balls. Rates are smoothed toward the format/phase/innings population prior, standardized across eligible players, capped at ±3 z, then weighted and reliability-adjusted. Base eligibility requires 60 batter balls or 48 bowler legal balls in both pressure and comparison exposure, plus five pressure matches. The working batter shortlist also requires 250 total valid batting balls. This is a project evidence screen, not a competition rule. The current draft gives the two components equal weight and uses exposure and match-count shrinkage.", "",
        "### Working ordered batter candidates (250-ball evidence floor)", "",
        "| Rank | Player | Pressure balls | Baseline balls | Pressure matches | Rate lift/100 | Survival lift/100 |", "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(working_batter_final, 1):
        lines.append(f"| {rank} | {row['player_name']} | {row['pressure_balls']:,} | {row['baseline_balls']:,} | {row['pressure_matches']} | {row['scoring_lift_component']:.2f} | {row['survival_lift_component']:.2f} |")
    lines += ["", "### Exploratory ordered bowler candidates", "", "| Rank | Player | Pressure legal balls | Baseline legal balls | Pressure matches | Economy saved/over | Wicket lift/24 |", "|---:|---|---:|---:|---:|---:|---:|"]
    for i, row in enumerate(primary["bowlers"], 1):
        lines.append(f"| {i} | {row['player_name']} | {row['pressure_exposure_balls']:,} | {row['baseline_exposure_balls']:,} | {row['pressure_matches']} | {row['scoring_lift_component']:.2f} | {row['survival_lift_component']:.2f} |")
    lines += ["", "Ashwin was removed after role review: although he clears 250 balls, his median batting position is 7 and 45 of 62 lineup appearances were at positions 7–11. Vijay Shankar is promoted from the next score-ranked candidate: he has 382 counted batting balls, 79 pressure balls across 10 matches, a +15.91 scoring lift and +0.84 survival lift; his supplied batting-order record has him in the top six in 24 of 26 appearances (median position 5). This is a disclosed role-fit override, so the edited top ten is not the untouched score-only ranking.", "", "### Sensitivity interpretation", ""]
    for role in ("batter", "bowler"):
        rows = [r for r in stability if r["role"] == role]
        min_overlap = min(r["top10_overlap_with_default"] for r in rows)
        lines.append(f"- **{role.title()}:** across component weights, base/strict sample rules and reliability settings, top-ten overlap with the default ranges from {min_overlap}/10 to 10/10. The result changes meaningfully under scoring-heavy or stricter variants, so the equal-weight list is not robust enough to call final.")
    lines += ["", "### Batter volume eligibility sensitivity", "",
        "The 250-ball floor is the current working screen. Stricter total-valid-ball thresholds change the top ten:", "",
        "| Minimum total batting balls | Top-ten overlap with current list | Names removed from current top ten |", "|---:|---:|---|"]
    for r in volume:
        lines.append(f"| {r['minimum_total_valid_batting_balls']} | {r['top10_overlap_with_default']}/10 | {', '.join(r['excluded_from_default_top10']) or 'None'} |")
    threehundred = next((r for r in volume if r["minimum_total_valid_batting_balls"] == 300), None)
    if threehundred:
        replacement_names = [r["player_name"] for r in threehundred["top10"] if r["player_id"] not in {x["player_id"] for x in primary["batters"]}]
        lines += ["", f"At the 300-ball screen, the current list retains {threehundred['top10_overlap_with_default']}/10 names; the draft replacement candidates include {', '.join(replacement_names)}. This is a sensitivity scenario, not a locked eligibility rule. The supplied files have batting order but no authoritative specialist-role label, and total exposure alone cannot establish that a player is a specialist batter."]
    shaheen = next(r for r in batter_eligibility["candidate_batters"] if r["player_name"] == "Shaheen Afridi")
    lines += ["", f"**Role-proxy check:** Shaheen Afridi has {shaheen['candidate_total_balls']} counted batting balls and 15 pressure matches. In {shaheen['eligible_xi_appearances_with_order']} eligible lineup appearances with a batting order, his median position is {shaheen['median_batting_order']:g}; {shaheen['lower_order_7_to_11_appearances']} were at positions 7–11. This supports describing him as a lower-order pressure-batting signal rather than a proven specialist batter. The batting order is a deployment proxy, not an official role label."]
    lines += ["", "Full scenario-by-scenario results are in `PLAYER_SCORING_DRAFT.md` and `player_scoring_draft.json`.", "",
        "### Phase-specific evidence for the RR death-overs gap", "",
        "This is a phase diagnostic, not a phase-specific selection list. The table shows current overall bowler candidates with at least 60 pressure legal balls and five pressure matches in the death phase.", "",
        "| Candidate | IPL 2026 team in supplied mapping | Pressure legal balls | Pressure matches | Economy saved/over (95% CI) | Wicket lift/24 (95% CI) |", "|---|---|---:|---:|---:|---:|"]
    death_rows = [r for r in phase_fit["candidate_phase_rows"] if r["role"] == "bowler" and r["phase"].casefold() == "death" and r["pressure_balls"] >= 60 and r["pressure_matches"] >= 5]
    roster_lookup = {r["player_id"]: r["mapped_team"] or "No IPL 2026 mapping" for r in phase_fit["ipl_2026_team_mapping"]["bowler"]}
    for r in sorted(death_rows, key=lambda x: (-x["scoring_lift"], x["player_name"])):
        econ, wicket = r["scoring_lift_95ci"], r["survival_wicket_lift_95ci"]
        lines.append(f"| {r['player_name']} | {roster_lookup.get(r['player_id'], 'No IPL 2026 mapping')} | {r['pressure_balls']:,} | {r['pressure_matches']} | {r['scoring_lift']:.2f} [{econ[0]:.2f}, {econ[1]:.2f}] | {r['survival_wicket_lift']:.2f} [{wicket[0]:.2f}, {wicket[1]:.2f}] |")
    lines += ["", "In this sample, Arshdeep Singh and Varun Chakravarthy show positive death-phase economy lifts with 95% match-bootstrap intervals above zero; their wicket-lift intervals include zero. FazalHaq Farooqi shows a positive death-phase wicket lift with an interval above zero, while his economy interval includes zero. None is mapped to Rajasthan Royals in the supplied IPL 2026 roster table. These results illustrate relevant player profiles, not confirmed RR availability or a recommendation to acquire a specific player.", "",
        "## Rajasthan Royals 2026 postmortem (separate from competition rankings)", "",
        f"The supplied RR season file has {rr['matches_in_file']} matches: {rr['wins']} wins and {rr['losses']} losses. Delivery-derived team scores reconcile with recorded result margins in {rr['result_margin_reconciliation']['matching_result_margins']} of {rr['result_margin_reconciliation']['comparable_results']} comparable results.", "",
        "The largest descriptive win/loss phase gap is death-overs bowling: opponents scored 12.36 runs per over in RR losses versus 9.32 in wins, while RR took fewer wickets per phase appearance in losses. Powerplay bowling is the next clearest gap. This supports evaluating a death specialist with dependable yorkers and slower-ball execution, plus a powerplay wicket-taking option. These are small-sample scouting hypotheses, not causal conclusions, and remain separate from the official player lists.", "",
        "## Rank-level uncertainty and season-held-out stability", "",
        f"A {rank_stability['bootstrap']['replicates']}-replicate match-cluster bootstrap resampled {rank_stability['match_count']:,} eligible matches and recomputed the score-only candidate rankings. The table is updated to the role-reviewed working shortlist; Vijay Shankar's bootstrap frequency is read from those replicates, while the scoring recipe itself has not been recalculated after the role override. Frequencies are not probabilities of intrinsic player quality.", "",
        "### Current top-ten inclusion stability", "",
        "| Role | Working shortlist rank | Player | Bootstrap top-10 frequency | Rank range when included (5th–95th percentile) |", "|---|---:|---|---:|---|",
    ]
    for role, key, shortlist in (("Batter", "batters", working_batter_final), ("Bowler", "bowlers", primary["bowlers"])):
        summary = {r["player_id"]: r for r in rank_stability["bootstrap"]["top10_inclusion_summary"][key]}
        for rank, candidate in enumerate(shortlist, 1):
            r = summary.get(candidate["player_id"])
            freq = f"{r['bootstrap_top10_probability']:.1%}" if r else "<0.3%"
            rank_range = f"{r['rank_05_when_top10']}–{r['rank_95_when_top10']}" if r else "rarely selected"
            lines.append(f"| {role} | {rank} | {candidate['player_name']} | {freq} | {rank_range} |")
    lines += ["", "### Leave-one-calendar-year-out overlap", "", "Each row removes one season, fits the complete candidate list on the remaining seasons, and compares its top ten with the full-period list. This measures season influence; it is not prospective prediction.", "",
        "| Omitted season | Matches held out | Batter overlap | Bowler overlap |", "|---:|---:|---:|---:|"]
    for r in rank_stability["leave_one_calendar_year_out"]:
        lines.append(f"| {r['held_out_calendar_year']} | {r['held_out_matches']} | {r['batters_top10_overlap_with_full_period']}/10 | {r['bowlers_top10_overlap_with_full_period']}/10 |")
    lines += ["", f"**Interpretation:** season-held-out overlap ranges from {min(r['batters_top10_overlap_with_full_period'] for r in rank_stability['leave_one_calendar_year_out'])}/10 to {max(r['batters_top10_overlap_with_full_period'] for r in rank_stability['leave_one_calendar_year_out'])}/10 for batters and {min(r['bowlers_top10_overlap_with_full_period'] for r in rank_stability['leave_one_calendar_year_out'])}/10 to {max(r['bowlers_top10_overlap_with_full_period'] for r in rank_stability['leave_one_calendar_year_out'])}/10 for bowlers. This is mixed stability and does not support freezing the lists yet.", "",
        "## Accounting and validation gates still open", "",
        f"The internal result-margin audit covers {accounting['result_margin_checks']['comparable_results']} comparable matches, with {accounting['result_margin_checks']['matching_margins']} margin matches and {accounting['result_margin_checks']['nonmatching_margins']} mismatch. The one mismatch is traced to a supplied result-text error: match 20190 is recorded as a six-wicket win, while the official IPL match report and delivery totals support five wickets. A separate Cricket West Indies official scorecard spot-check matched one game's totals. These checks do not reconcile all innings against independent scorecards.", "",
        f"There are still {accounting['unresolved_extra_rows']} unresolved extra rows (260 `other`, 1 `Penalty`, and 2 unlabeled). Their type and legal-ball/bowler attribution cannot be inferred from amount patterns. The current pressure-based player rates remain provisional until the source-specific classification and authoritative scorecard reconciliation are resolved.", "",
        "The user's current milestone defers official selector eligibility and Jaipur/venue/dew work. Those items are not treated as completed and are not needed to proceed with the accounting and ranking-stability milestone.", "",
        "## Freeze decision", "",
        "**Do not freeze the methodology or submit these lists yet.** Sensitivity results and season-held-out overlap show meaningful top-ten movement, and unresolved extras can change role-specific rates. Once the accounting source issue and the agreed robustness gates are closed, regenerate the report and optional dashboard from the same machine-readable outputs.", "",
        "## Reproduction", "",
        "From the project root, run `python scripts/run_pipeline.py` to regenerate all upstream data and reports in dependency order, including the rank-stability and scorecard-accounting checks. The detailed views are `RANKING_STABILITY.md`, `SCORECARD_ACCOUNTING_RECONCILIATION.md`, and `CANDIDATE_PHASE_FIT.md`.", "",
    ]
    out = REPORTS / "METHODOLOGY_REPORT_DRAFT.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
