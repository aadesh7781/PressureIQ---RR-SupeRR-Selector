from __future__ import annotations

import csv
import gzip
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

LOG = logging.getLogger("pressureiq.player_scoring")
STATE_PATH = ROOT / "data" / "processed" / "delivery_pressure_states.csv.gz"
INDEX_PATH = ROOT / "data" / "processed" / "pressure_candidate_states.csv.gz"
PAR_PATH = ROOT / "data" / "processed" / "first_innings_par_diagnostics.csv.gz"
OUT_DIR = ROOT / "reports" / "generated"
WEIGHTS = [0.25, 0.50, 0.75]
CUTOFFS = [0.70, 0.75, 0.80]
BATTER_SHRINK_BALLS = 60.0
BOWLER_SHRINK_BALLS = 48.0
MIN_BATTER_PRESSURE_BALLS = 60
MIN_BOWLER_PRESSURE_LEGAL_BALLS = 48
MIN_PRESSURE_MATCHES = 5
CREDITED_DISMISSALS = {"bowled", "caught", "caught and bowled", "lbw", "stump", "stumped", "hit wicket"}

def read_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)

def yes(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}

def main() -> int:
    configure_logging(ROOT / "logs")
    pressure_index = {}
    for r in read_gz(INDEX_PATH):
        key = (r["match_id"], r["innings"], r["innings_ball_num"])
        pressure_index[key] = r
    par_index = {}
    for r in read_gz(PAR_PATH):
        key = (r["match_id"], "1", r["innings_ball_num"])
        par_index[key] = r
    scenarios = [(method, weight, cutoff) for method in ("current_rate", "par_first_innings") for weight in WEIGHTS for cutoff in CUTOFFS]
    scenario_groups = {s: defaultdict(lambda: {"balls": 0, "runs": 0, "conceded": 0, "legal_balls": 0, "dismissals": 0, "bowler_wickets": 0, "matches": set()}) for s in scenarios}
    scenario_rows = Counter()
    name_counts = {"batter": defaultdict(Counter), "bowler": defaultdict(Counter)}

    for d in read_gz(STATE_PATH):
        key = (d["match_id"], d["innings"], d["innings_ball_num"])
        idx = pressure_index.get(key)
        if idx is None:
            continue
        par = par_index.get(key) if d["innings"] == "1" else None
        for s in scenarios:
            method, weight, cutoff = s
            if method == "par_first_innings" and d["innings"] == "1":
                if par is None:
                    continue
                demand = float(par["par_deficit_stress_percentile"])
                resource = float(par["resource_stress_percentile"])
                pscore = weight * demand + (1 - weight) * resource
            else:
                pscore = float(idx[f"score_weight_{int(weight*100)}"])
            high = pscore >= cutoff
            scenario_rows[(s, "pressure_states" if high else "baseline_states")] += 1
            fmt, phase, inning = idx["format"], idx["phase"], int(d["innings"])
            extra_type = (d["extras_type"] or "").strip().lower()
            if extra_type in {"", "none", "null"}:
                extra_type = "none"
            status = (d["legal_ball_status"] or "").strip().lower()
            legal_flag = (d["is_legal_ball"] or "").strip().lower()
            role_state = "pressure" if high else "normal"
            match_id = d["match_id"]

            # Batter: wides are not balls faced; no-balls are. Ambiguous extras are
            # omitted from both pressure and baseline rates until semantics resolve.
            extras_total = int(d["extras1_runs"] or 0) + int(d["extras_2_runs"] or 0)
            batter_valid = extra_type not in {"wide", "other", "penalty"} and not (extra_type == "none" and extras_total != 0) and legal_flag in {"true", "false"}
            if batter_valid and extra_type != "wide":
                pid = d["striker_id"]
                grp = scenario_groups[s][("batter", pid, fmt, phase, inning, role_state)]
                grp["balls"] += 1
                grp["runs"] += int(d["runs"] or 0)
                if yes(d["is_wicket"]) and d["dismissed_player_id"] == pid:
                    grp["dismissals"] += 1
                grp["matches"].add(match_id)
                name_counts["batter"][pid][d["striker_name"]] += 1

            # Bowler: use only deliveries with a resolved concession amount. Wickets
            # use dismissals credited to the bowler, excluding run-outs and retirements.
            conceded_raw = d["bowler_conceded_runs_provisional"]
            bowler_valid = conceded_raw not in {"", None} and legal_flag in {"true", "false"}
            if bowler_valid:
                pid = d["bowler_id"]
                grp = scenario_groups[s][("bowler", pid, fmt, phase, inning, role_state)]
                grp["conceded"] += int(float(conceded_raw))
                grp["legal_balls"] += int(legal_flag == "true")
                dismissal = (d["dismissal_type"] or "").strip().lower()
                if yes(d["is_wicket"]) and dismissal in CREDITED_DISMISSALS:
                    grp["bowler_wickets"] += 1
                grp["matches"].add(match_id)
                name_counts["bowler"][pid][d["bowler_name"]] += 1

    all_results = {}
    candidates_by_scenario = {}
    for scenario in scenarios:
        result = {"batters": [], "bowlers": [], "counts": {}}
        candidates_by_scenario[scenario] = {}
        for role in ("batter", "bowler"):
            players = defaultdict(lambda: {"weighted_scoring_lift": 0.0, "weighted_survival_lift": 0.0,
                "pressure_exposure": 0, "baseline_exposure": 0, "pressure_matches": set(), "groups_compared": 0})
            keys = {k for k in scenario_groups[scenario] if k[0] == role}
            contexts = {(k[1], k[2], k[3], k[4]) for k in keys}
            population = defaultdict(lambda: {"exposure": 0, "runs": 0, "dismissals": 0, "wickets": 0, "conceded": 0})
            for k in keys:
                if k[5] != "normal":
                    continue
                g = scenario_groups[scenario][k]
                context = (k[2], k[3], k[4])
                if role == "batter":
                    population[context]["exposure"] += g["balls"]
                    population[context]["runs"] += g["runs"]
                    population[context]["dismissals"] += g["dismissals"]
                else:
                    population[context]["exposure"] += g["legal_balls"]
                    population[context]["conceded"] += g["conceded"]
                    population[context]["wickets"] += g["bowler_wickets"]
            for pid, fmt, phase, inning in contexts:
                p = scenario_groups[scenario].get((role, pid, fmt, phase, inning, "pressure"))
                n = scenario_groups[scenario].get((role, pid, fmt, phase, inning, "normal"))
                if not p or not n:
                    continue
                pop = population[(fmt, phase, inning)]
                if pop["exposure"] == 0:
                    continue
                stats = players[pid]
                if role == "batter":
                    if p["balls"] == 0 or n["balls"] == 0:
                        continue
                    prior_rate = pop["runs"] / pop["exposure"]
                    prior_out = pop["dismissals"] / pop["exposure"]
                    p_rate = (p["runs"] + prior_rate * BATTER_SHRINK_BALLS) / (p["balls"] + BATTER_SHRINK_BALLS)
                    n_rate = (n["runs"] + prior_rate * BATTER_SHRINK_BALLS) / (n["balls"] + BATTER_SHRINK_BALLS)
                    p_out = (p["dismissals"] + prior_out * BATTER_SHRINK_BALLS) / (p["balls"] + BATTER_SHRINK_BALLS)
                    n_out = (n["dismissals"] + prior_out * BATTER_SHRINK_BALLS) / (n["balls"] + BATTER_SHRINK_BALLS)
                    scoring_lift = (p_rate - n_rate) * 100
                    survival_lift = (n_out - p_out) * 100
                    exposure = p["balls"]
                    baseline_exposure = n["balls"]
                else:
                    if p["legal_balls"] == 0 or n["legal_balls"] == 0:
                        continue
                    prior_econ = pop["conceded"] * 6 / pop["exposure"]
                    prior_wickets = pop["wickets"] / pop["exposure"]
                    p_econ = (p["conceded"] + prior_econ * BOWLER_SHRINK_BALLS / 6) * 6 / (p["legal_balls"] + BOWLER_SHRINK_BALLS)
                    n_econ = (n["conceded"] + prior_econ * BOWLER_SHRINK_BALLS / 6) * 6 / (n["legal_balls"] + BOWLER_SHRINK_BALLS)
                    p_wickets = (p["bowler_wickets"] + prior_wickets * BOWLER_SHRINK_BALLS) * 24 / (p["legal_balls"] + BOWLER_SHRINK_BALLS)
                    n_wickets = (n["bowler_wickets"] + prior_wickets * BOWLER_SHRINK_BALLS) * 24 / (n["legal_balls"] + BOWLER_SHRINK_BALLS)
                    scoring_lift = n_econ - p_econ
                    survival_lift = p_wickets - n_wickets
                    exposure = p["legal_balls"]
                    baseline_exposure = n["legal_balls"]
                stats["weighted_scoring_lift"] += scoring_lift * exposure
                stats["weighted_survival_lift"] += survival_lift * exposure
                stats["pressure_exposure"] += exposure
                stats["baseline_exposure"] += baseline_exposure
                stats["pressure_matches"].update(p["matches"])
                stats["groups_compared"] += 1
            candidates = []
            for pid, stats in players.items():
                exp = stats["pressure_exposure"]
                min_exp = MIN_BATTER_PRESSURE_BALLS if role == "batter" else MIN_BOWLER_PRESSURE_LEGAL_BALLS
                min_baseline = MIN_BATTER_PRESSURE_BALLS if role == "batter" else MIN_BOWLER_PRESSURE_LEGAL_BALLS
                if exp < min_exp or stats["baseline_exposure"] < min_baseline or len(stats["pressure_matches"]) < MIN_PRESSURE_MATCHES:
                    continue
                scoring_lift = stats["weighted_scoring_lift"] / exp
                survival_lift = stats["weighted_survival_lift"] / exp
                display_name = name_counts[role][pid].most_common(1)[0][0] if name_counts[role][pid] else pid
                candidates.append({"player_id": pid, "player_name": display_name,
                    "scoring_lift_component": scoring_lift, "survival_lift_component": survival_lift,
                    "pressure_exposure_balls": exp,
                    "baseline_exposure_balls": stats["baseline_exposure"], "pressure_matches": len(stats["pressure_matches"]),
                    "format_phase_contexts_compared": stats["groups_compared"]})
            def zscores(field):
                values = [r[field] for r in candidates]
                center = sum(values) / len(values) if values else 0.0
                spread = pstdev(values) if len(values) > 1 else 0.0
                return [max(-3.0, min(3.0, (v - center) / spread)) if spread else 0.0 for v in values]
            z_scoring, z_survival = zscores("scoring_lift_component"), zscores("survival_lift_component")
            candidates_by_scenario[scenario][role] = candidates
            ranking = []
            shrink = BATTER_SHRINK_BALLS if role == "batter" else BOWLER_SHRINK_BALLS
            for r, zs, zw in zip(candidates, z_scoring, z_survival):
                reliability = (r["pressure_exposure_balls"] / (r["pressure_exposure_balls"] + shrink)) * (r["pressure_matches"] / (r["pressure_matches"] + 4))
                composite = (zs + zw) / 2
                ranking.append({**r, "scoring_component_z": round(zs, 4), "survival_component_z": round(zw, 4),
                    "reliability_factor": round(reliability, 4), "score_raw": round(composite, 4),
                    "score_adjusted": round(composite * reliability, 4),
                    "scoring_lift_component": round(r["scoring_lift_component"], 4),
                    "survival_lift_component": round(r["survival_lift_component"], 4)})
            ranking.sort(key=lambda r: (-r["score_adjusted"], -r["pressure_exposure_balls"], r["player_name"].casefold()))
            result["batters" if role == "batter" else "bowlers"] = ranking[:10]
            result["counts"][role] = {"players_with_comparable_pressure_and_baseline": len(players),
                "eligible_after_sample_filter": len(ranking), "sample_filter_min_pressure_balls": min_exp,
                "minimum_pressure_matches": MIN_PRESSURE_MATCHES}
        result["pressure_state_rows"] = scenario_rows[(scenario, "pressure_states")]
        result["baseline_state_rows"] = scenario_rows[(scenario, "baseline_states")]
        all_results["|".join(map(str, scenario))] = result

    default_key = "|".join(map(str, ("current_rate", 0.5, 0.75)))
    baseline = all_results[default_key]
    base_ids = {role: {r["player_id"] for r in baseline[role]} for role in ("batters", "bowlers")}
    sensitivity = []
    for key, result in all_results.items():
        method, weight, cutoff = key.split("|")
        rec = {"method": method, "demand_weight": float(weight), "cutoff": float(cutoff),
            "pressure_state_rows": result["pressure_state_rows"], "pressure_states_with_baseline": result["baseline_state_rows"]}
        for role in ("batters", "bowlers"):
            ids = {r["player_id"] for r in result[role]}
            inter = len(ids & base_ids[role])
            union = len(ids | base_ids[role])
            rec[f"{role}_top10_overlap"] = inter
            rec[f"{role}_top10_jaccard"] = round(inter / union, 3) if union else None
        sensitivity.append(rec)
    score_sensitivity = []
    base_scenario = ("current_rate", 0.50, 0.75)
    for role, output_role, default_min, default_k in (
        ("batter", "batters", MIN_BATTER_PRESSURE_BALLS, BATTER_SHRINK_BALLS),
        ("bowler", "bowlers", MIN_BOWLER_PRESSURE_LEGAL_BALLS, BOWLER_SHRINK_BALLS),
    ):
        candidates = candidates_by_scenario[base_scenario][role]
        reference_ids = {r["player_id"] for r in all_results["|".join(map(str, base_scenario))][output_role]}
        for sample_rule, exposure_mult, match_min in (("base", 1.0, MIN_PRESSURE_MATCHES), ("strict", 1.5, 8)):
            min_exposure = int(default_min * exposure_mult)
            eligible = [r for r in candidates if r["pressure_exposure_balls"] >= min_exposure
                and r["baseline_exposure_balls"] >= min_exposure and r["pressure_matches"] >= match_min]
            for scoring_weight in (0.25, 0.50, 0.75):
                s_values = [r["scoring_lift_component"] for r in eligible]
                w_values = [r["survival_lift_component"] for r in eligible]
                s_mean = sum(s_values) / len(s_values) if s_values else 0.0
                w_mean = sum(w_values) / len(w_values) if w_values else 0.0
                s_sd = pstdev(s_values) if len(s_values) > 1 else 0.0
                w_sd = pstdev(w_values) if len(w_values) > 1 else 0.0
                for k_multiplier in (0.5, 1.0, 2.0):
                    ranked = []
                    for r in eligible:
                        sz = max(-3.0, min(3.0, (r["scoring_lift_component"] - s_mean) / s_sd)) if s_sd else 0.0
                        wz = max(-3.0, min(3.0, (r["survival_lift_component"] - w_mean) / w_sd)) if w_sd else 0.0
                        k = default_k * k_multiplier
                        reliability = (r["pressure_exposure_balls"] / (r["pressure_exposure_balls"] + k)) * (r["pressure_matches"] / (r["pressure_matches"] + 4))
                        score = (scoring_weight * sz + (1 - scoring_weight) * wz) * reliability
                        ranked.append((score, r["pressure_exposure_balls"], r["player_name"].casefold(), r["player_id"]))
                    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
                    ids = {r[3] for r in ranked[:10]}
                    overlap = len(ids & reference_ids)
                    score_sensitivity.append({"role": role, "sample_rule": sample_rule,
                        "minimum_pressure_and_baseline_exposure": min_exposure, "minimum_pressure_matches": match_min,
                        "scoring_component_weight": scoring_weight, "survival_component_weight": round(1-scoring_weight, 2),
                        "reliability_k_multiplier": k_multiplier, "eligible_players": len(ranked),
                        "top10_overlap_with_default": overlap,
                        "top10_jaccard_with_default": round(overlap / len(ids | reference_ids), 3) if ids or reference_ids else None})
    # A role-specific batting volume screen tests whether the small default minimum
    # lets occasional lower-order batting appearances displace established batters.
    batter_volume_sensitivity = []
    batter_candidates = candidates_by_scenario[base_scenario]["batter"]
    default_batter_ids = {r["player_id"] for r in baseline["batters"]}
    for min_total_balls in (250, 300, 500):
        eligible = [r for r in batter_candidates
            if r["pressure_exposure_balls"] + r["baseline_exposure_balls"] >= min_total_balls]
        means = {}
        for field in ("scoring_lift_component", "survival_lift_component"):
            vals = [r[field] for r in eligible]
            means[field] = (sum(vals) / len(vals) if vals else 0.0,
                pstdev(vals) if len(vals) > 1 else 0.0)
        ranked = []
        for r in eligible:
            def z(field):
                mean, sd = means[field]
                return max(-3.0, min(3.0, (r[field] - mean) / sd)) if sd else 0.0
            reliability = (r["pressure_exposure_balls"] / (r["pressure_exposure_balls"] + BATTER_SHRINK_BALLS)) * (r["pressure_matches"] / (r["pressure_matches"] + 4))
            score = (z("scoring_lift_component") + z("survival_lift_component")) / 2 * reliability
            ranked.append({**r, "score_adjusted": round(score, 4)})
        ranked.sort(key=lambda r: (-r["score_adjusted"], -r["pressure_exposure_balls"], r["player_name"].casefold()))
        top10 = ranked[:10]
        ids = {r["player_id"] for r in top10}
        batter_volume_sensitivity.append({"minimum_total_valid_batting_balls": min_total_balls,
            "eligible_players": len(ranked), "top10_overlap_with_default": len(ids & default_batter_ids),
            "excluded_from_default_top10": [r["player_name"] for r in baseline["batters"] if r["pressure_exposure_balls"] + r["baseline_exposure_balls"] < min_total_balls],
            "next_candidates_after_top10": [{"rank": i, "player_id": r["player_id"], "player_name": r["player_name"],
                "pressure_balls": r["pressure_exposure_balls"], "baseline_balls": r["baseline_exposure_balls"],
                "total_batting_balls": r["pressure_exposure_balls"] + r["baseline_exposure_balls"],
                "pressure_matches": r["pressure_matches"], "scoring_lift_component": round(r["scoring_lift_component"], 3),
                "survival_lift_component": round(r["survival_lift_component"], 3),
                "score_adjusted": r["score_adjusted"]} for i, r in enumerate(ranked[10:15], 11)],
            "top10": [{"rank": i, "player_id": r["player_id"], "player_name": r["player_name"],
                "pressure_balls": r["pressure_exposure_balls"], "baseline_balls": r["baseline_exposure_balls"],
                "total_batting_balls": r["pressure_exposure_balls"] + r["baseline_exposure_balls"],
                "pressure_matches": r["pressure_matches"], "scoring_lift_component": round(r["scoring_lift_component"], 3),
                "survival_lift_component": round(r["survival_lift_component"], 3),
                "score_adjusted": r["score_adjusted"]} for i, r in enumerate(top10, 1)]})
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ranking_status": "exploratory draft; pressure definition and rating weights remain provisional",
        "primary_candidate": {"pressure_type": "current-rate method for first innings; RRR method for chases", "demand_weight": 0.5, "cutoff": 0.75},
        "score_definition": {
            "batter_components": "within player x format x phase x innings, smoothed pressure minus normal run rate per 100 balls and normal minus pressure dismissals per 100 balls; compare the two components as equal-weight z-scores across eligible players",
            "bowler_components": "within player x format x phase x innings, smoothed normal minus pressure economy and pressure minus normal credited wickets per 24 legal balls; compare the two components as equal-weight z-scores across eligible players",
            "context_smoothing": "shrink pressure and normal rates toward the format x phase x innings non-pressure population rate with 60 prior batter balls or 48 prior bowler legal balls",
            "reliability_adjustment": "multiply equal-weight component z-score by pressure exposure/(pressure exposure + k) and pressure_matches/(pressure_matches + 4); k=60 batter balls and k=48 bowler legal balls",
            "sample_filter": "at least 60 pressure and baseline balls for batters, 48 pressure and baseline legal balls for bowlers, and at least 5 pressure matches",
            "bowler_wickets": sorted(CREDITED_DISMISSALS),
        },
        "top10_by_candidate": all_results, "sensitivity_vs_current_rate_50_75": sensitivity,
        "score_assumption_sensitivity": score_sensitivity,
        "batter_total_volume_sensitivity": batter_volume_sensitivity,
        "limitations": [
            "Scores measure the player's change from their own non-pressure baseline in comparable format/phase/innings contexts; they do not measure absolute player quality alone.",
            "Context prior strengths, component equal weights, z-score caps, reliability constants and minimum samples are transparent provisional settings; test alternatives before finalizing.",
            "Pressure rows and normal rows are compared only where the candidate state index is available and delivery accounting is usable for that role.",
            "Ambiguous extras are omitted from batter/bowler rate denominators; player sample sizes may therefore differ by role.",
            "Preliminary top-10 overlap is a ranking sensitivity diagnostic, not validation against an external truth set.",
            "Verify candidate player IDs against the competition dropdown before any eventual submission; no submission has been made.",
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "player_scoring_draft.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Exploratory player pressure-scoring draft", "", f"Generated: {report['generated_at_utc']}", "",
        "This is a transparent scoring prototype for inspecting metrics and ranking stability. It is not the final competition ranking until score weights, pressure rules and reliability settings survive sensitivity review.", "",
        "## Scoring method", "", f"- **Batter components:** {report['score_definition']['batter_components']}.",
        f"- **Bowler components:** {report['score_definition']['bowler_components']}.",
        f"- **Context smoothing:** {report['score_definition']['context_smoothing']}.",
        f"- **Reliability adjustment:** {report['score_definition']['reliability_adjustment']}.",
        f"- **Initial sample filter:** {report['score_definition']['sample_filter']}.",
        "- Component scales are standardized across the eligible player pool and combined with equal weight; extreme component z-scores are capped at ±3.", "",
        "Comparisons are within player, format, phase and innings type. Batter and bowler rankings are separate.", "",
        "## Unscreened base candidates: batters", "", "This initial base list does not yet apply the working 250-total-ball evidence floor; use the screened list below for current batter shortlist discussion.", "", "| Rank | Player | ID | Reliability-adjusted score | Composite z | Pressure balls | Baseline balls | Pressure matches | Scoring-rate lift/100 | Survival lift/100 |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for rank, r in enumerate(baseline["batters"], start=1):
        lines.append(f"| {rank} | {r['player_name']} | {r['player_id']} | {r['score_adjusted']:.4f} | {r['score_raw']:.4f} | {r['pressure_exposure_balls']:,} | {r['baseline_exposure_balls']:,} | {r['pressure_matches']} | {r['scoring_lift_component']:.2f} | {r['survival_lift_component']:.2f} |")
    lines += ["", "## Candidate top 10: bowlers", "", "| Rank | Player | ID | Reliability-adjusted score | Composite z | Pressure legal balls | Baseline legal balls | Pressure matches | Economy saved/over | Wicket lift/24 balls |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for rank, r in enumerate(baseline["bowlers"], start=1):
        lines.append(f"| {rank} | {r['player_name']} | {r['player_id']} | {r['score_adjusted']:.4f} | {r['score_raw']:.4f} | {r['pressure_exposure_balls']:,} | {r['baseline_exposure_balls']:,} | {r['pressure_matches']} | {r['scoring_lift_component']:.2f} | {r['survival_lift_component']:.2f} |")
    lines += ["", "## Top-10 stability versus pressure settings", "", "Overlap counts how many names from each scenario's top 10 also appear in the equal-weight, 75th-percentile current-rate candidate list.", "",
        "| First-innings method | Demand weight | Cutoff | Pressure state rows | Batter overlap | Bowler overlap |", "|---|---:|---:|---:|---:|---:|"]
    for row in sensitivity:
        lines.append(f"| {row['method']} | {row['demand_weight']:.2f} | p{int(row['cutoff']*100)} | {row['pressure_state_rows']:,} | {row['batters_top10_overlap']}/10 | {row['bowlers_top10_overlap']}/10 |")
    lines += ["", "## Scoring and sample-size sensitivity", "",
        "The following checks vary the scoring-rate versus survival/wicket component weight (25/75, 50/50, 75/25), use base versus stricter exposure and match minimums, and halve/double the reliability prior. Overlap is measured against the primary equal-weight, base-sample top 10. These are ranking stability checks, not proof of predictive value.", "",
        "| Role | Sample rule | Scoring weight | Survival/wicket weight | Reliability k | Eligible players | Top-10 overlap | Jaccard |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in score_sensitivity:
        lines.append(f"| {row['role']} | {row['sample_rule']} | {row['scoring_component_weight']:.2f} | {row['survival_component_weight']:.2f} | {row['reliability_k_multiplier']:.1f}x | {row['eligible_players']} | {row['top10_overlap_with_default']}/10 | {row['top10_jaccard_with_default'] if row['top10_jaccard_with_default'] is not None else 'n/a'} |")
    lines += ["", "## Batter sample-volume sensitivity", "",
        "The current minimums can admit a player with relatively little total batting exposure. This sensitivity keeps the same pressure rule, scoring components and reliability adjustment but adds a minimum total number of valid batting balls across pressure and baseline states. These cutoffs are comparisons, not a locked eligibility rule.", "",
        "| Minimum total batting balls | Eligible batters | Default top-ten names retained | Removed from default top 10 | Sensitivity top 10 |", "|---:|---:|---:|---|---|"]
    for row in batter_volume_sensitivity:
        names = ", ".join(r["player_name"] for r in row["top10"])
        excluded = ", ".join(row["excluded_from_default_top10"]) or "None"
        lines.append(f"| {row['minimum_total_valid_batting_balls']} | {row['eligible_players']} | {row['top10_overlap_with_default']}/10 | {excluded} | {names} |")
    working = next(row for row in batter_volume_sensitivity if row["minimum_total_valid_batting_balls"] == 250)
    lines += ["", "### Working batter top 10 (250-ball evidence floor)", "",
        "This screen also retains the existing minimums of 60 pressure balls, 60 baseline balls and five pressure matches.", "",
        "| Rank | Player | Pressure balls | Baseline balls | Pressure matches | Scoring lift/100 | Survival lift/100 |", "|---:|---|---:|---:|---:|---:|---:|"]
    for r in working["top10"]:
        lines.append(f"| {r['rank']} | {r['player_name']} | {r['pressure_balls']:,} | {r['baseline_balls']:,} | {r['pressure_matches']} | {r['scoring_lift_component']:.2f} | {r['survival_lift_component']:.2f} |")
    lines += ["", "## Limitations before publishing final ranks", ""]
    lines.extend(f"- {note}" for note in report["limitations"])
    lines += ["", "Full scenario scorecards are in `player_scoring_draft.json`.", ""]
    (OUT_DIR / "PLAYER_SCORING_DRAFT.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Created exploratory pressure top-10s for %d pressure-definition scenarios", len(scenarios))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
