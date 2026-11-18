from __future__ import annotations

import bisect
import csv
import gzip
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

LOG = logging.getLogger("pressureiq.first_innings_par")
STATE_PATH = ROOT / "data" / "processed" / "delivery_pressure_states.csv.gz"
OUT_PATH = ROOT / "data" / "processed" / "first_innings_par_diagnostics.csv.gz"
REPORT_DIR = ROOT / "reports" / "generated"
CUTOFFS = [0.70, 0.75, 0.80]
DEMAND_WEIGHTS = [0.25, 0.50, 0.75]

def num(v: str | None) -> float | None:
    return float(v) if v not in (None, "") else None

def pct(sorted_vals: list[float], value: float) -> float:
    return (bisect.bisect_left(sorted_vals, value) + bisect.bisect_right(sorted_vals, value)) / (2 * len(sorted_vals)) if sorted_vals else 0.5

def main() -> int:
    configure_logging(ROOT / "logs")
    all_rows, final_total, season_by_match = [], defaultdict(int), {}
    with gzip.open(STATE_PATH, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["innings"] != "1":
                continue
            mid = r["match_id"]
            final_total[mid] += int(r["delivery_total_runs_provisional"] or 0)
            season_by_match[mid] = ("IPL" if r["competition_name"].upper().startswith("IPL ") else "T20I", r["season"])
            remaining = num(r["balls_remaining_exact"])
            current_rr = num(r["current_run_rate_provisional"])
            if remaining is None or current_rr is None:
                continue
            all_rows.append({"match_id": mid, "fold": int(mid) % 5,
                "format": season_by_match[mid][0], "season": season_by_match[mid][1],
                "phase": r["match_phase"], "remaining": remaining, "bucket": int(remaining // 12),
                "ball_num": int(r["innings_ball_num"]),
                "wickets_lost": int(r["pre_delivery_wickets_lost"]),
                "wickets_remaining": int(r["pre_delivery_wickets_remaining"]),
                "score": int(r["pre_delivery_score_runs_provisional"]), "current_rr": current_rr,
                "batter_id": r["striker_id"], "bowler_id": r["bowler_id"]})
    # The final score is accumulated in the first pass, then attached to each state.
    # Generate one training checkpoint per innings at each six legal balls to avoid
    # giving long innings dozens of near-identical observations at one state.
    checkpoints, seen = [], set()
    with gzip.open(STATE_PATH, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["innings"] != "1":
                continue
            mid = r["match_id"]
            remaining, rr = num(r["balls_remaining_exact"]), num(r["current_run_rate_provisional"])
            if remaining is None or rr is None:
                continue
            balls_before = int(r["known_legal_balls_before"])
            checkpoint_key = (mid, balls_before)
            if balls_before % 6 != 0 or checkpoint_key in seen:
                continue
            seen.add(checkpoint_key)
            checkpoints.append({"match_id": mid, "fold": int(mid) % 5,
                "format": "IPL" if r["competition_name"].upper().startswith("IPL ") else "T20I",
                "season": r["season"], "phase": r["match_phase"], "bucket": int(remaining // 12),
                "wickets_lost": int(r["pre_delivery_wickets_lost"]),
                "remaining": remaining, "score": int(r["pre_delivery_score_runs_provisional"])})
    for r in checkpoints:
        r["runs_remaining_actual"] = final_total[r["match_id"]] - r["score"]
        r["final_total"] = final_total[r["match_id"]]

    by_fold_predictions = []
    fallback_counts = Counter()
    for fold in range(5):
        train = [r for r in checkpoints if r["fold"] != fold]
        test = [r for r in all_rows if r["fold"] == fold]
        exact, broad, phase_only, fmt_only = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
        season_par, format_par = defaultdict(list), defaultdict(list)
        for r in train:
            exact[(r["format"], r["phase"], r["bucket"], r["wickets_lost"])].append(r["runs_remaining_actual"])
            broad[(r["format"], r["phase"], r["bucket"])].append(r["runs_remaining_actual"])
            phase_only[(r["format"], r["phase"])].append(r["runs_remaining_actual"])
            fmt_only[r["format"]].append(r["runs_remaining_actual"])
        for mid, total in final_total.items():
            if int(mid) % 5 == fold:
                continue
            fmt, season = season_by_match[mid]
            season_par[(fmt, season)].append(total)
            format_par[fmt].append(total)
        def mean(xs):
            return sum(xs) / len(xs) if xs else None
        for r in test:
            k4 = (r["format"], r["phase"], r["bucket"], r["wickets_lost"])
            k3 = (r["format"], r["phase"], r["bucket"])
            k2 = (r["format"], r["phase"])
            choices = [(exact.get(k4, []), "format_phase_bucket_wickets"),
                (broad.get(k3, []), "format_phase_bucket"),
                (phase_only.get(k2, []), "format_phase"),
                (fmt_only.get(r["format"], []), "format")]
            residuals, fallback = next(((values, name) for values, name in choices if len(values) >= 8), ([], "unavailable"))
            expected_remaining = mean(residuals) if residuals else None
            par_values = season_par.get((r["format"], r["season"]), [])
            par_score = mean(par_values) if len(par_values) >= 8 else mean(format_par.get(r["format"], []))
            if expected_remaining is None or par_score is None:
                continue
            r["expected_runs_remaining"] = expected_remaining
            r["expected_final_score"] = r["score"] + expected_remaining
            r["format_season_par_score"] = par_score
            r["par_gap"] = r["expected_final_score"] - par_score
            r["baseline_fallback"] = fallback
            r["validation_actual_final"] = final_total[r["match_id"]]
            r["prediction_error"] = r["expected_final_score"] - r["validation_actual_final"]
            by_fold_predictions.append(r)
            fallback_counts[fallback] += 1

    current_groups, par_groups, resource_groups = defaultdict(lambda: {"current": [], "par": [], "wickets": []}), defaultdict(lambda: {"current": [], "par": [], "wickets": []}), defaultdict(list)
    for r in by_fold_predictions:
        g = (r["format"], r["phase"], r["bucket"])
        current_groups[g]["current"].append(r["current_rr"])
        current_groups[g]["wickets"].append(r["wickets_lost"])
        par_groups[g]["par"].append(r["par_gap"])
        par_groups[g]["wickets"].append(r["wickets_lost"])
    for g in current_groups:
        for key in current_groups[g]:
            current_groups[g][key].sort()
        par_groups[g]["par"].sort()
        par_groups[g]["wickets"].sort()
    for r in by_fold_predictions:
        g = (r["format"], r["phase"], r["bucket"])
        r["current_rate_stress"] = 1 - pct(current_groups[g]["current"], r["current_rr"])
        r["par_deficit_stress"] = 1 - pct(par_groups[g]["par"], r["par_gap"])
        r["resource_stress"] = pct(current_groups[g]["wickets"], r["wickets_lost"])

    variants = []
    for weight in DEMAND_WEIGHTS:
        for cutoff in CUTOFFS:
            current_set = {r["match_id"] + ":" + str(r["match_id"]) + ":" + str(r["score"]) for r in []}
            par_selected, current_selected = [], []
            for r in by_fold_predictions:
                par_score = weight * r["par_deficit_stress"] + (1 - weight) * r["resource_stress"]
                current_score = weight * r["current_rate_stress"] + (1 - weight) * r["resource_stress"]
                if par_score >= cutoff:
                    par_selected.append(r)
                if current_score >= cutoff:
                    current_selected.append(r)
            par_keys = {(r["match_id"], r["ball_num"]) for r in par_selected}
            current_keys = {(r["match_id"], r["ball_num"]) for r in current_selected}
            intersection = len(par_keys & current_keys)
            union = len(par_keys | current_keys)
            variants.append({"demand_weight": weight, "resource_weight": round(1-weight, 2), "cutoff": cutoff,
                "par_method_high_pressure_rows": len(par_selected), "current_rate_method_high_pressure_rows": len(current_selected),
                "jaccard_overlap": round(intersection / union, 4) if union else None,
                "par_method_matches": len({r["match_id"] for r in par_selected}),
                "par_method_batters": len({r["batter_id"] for r in par_selected}),
                "par_method_bowlers": len({r["bowler_id"] for r in par_selected})})
    errors = [r["prediction_error"] for r in by_fold_predictions]
    rmse = math.sqrt(sum(e*e for e in errors) / len(errors)) if errors else None
    mae = sum(abs(e) for e in errors) / len(errors) if errors else None
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eligible_first_innings_delivery_states": len(all_rows), "training_checkpoints": len(checkpoints),
        "cross_validated_state_rows": len(by_fold_predictions), "match_level_fold_count": 5,
        "fold_definition": "integer(match_id) modulo 5; every prediction uses training checkpoints from other matches",
        "baseline_fallback_counts": dict(fallback_counts), "expected_final_score_mae_delivery_states": round(mae, 3) if mae is not None else None,
        "expected_final_score_rmse_delivery_states": round(rmse, 3) if rmse is not None else None,
        "sensitivity_variants": variants,
        "par_method": "expected final score = current provisional score + cross-fitted mean remaining runs for same format/phase/12-ball remaining band/wickets lost, with hierarchical fallback; compare to cross-fitted format-season mean first-innings score",
        "caveats": [
            "This uses first-innings delivery totals from the same provisional runs/extras accounting; source extras categories remain unresolved.",
            "A par deficit indicates a projected below-par state, not a causal measure of psychological pressure.",
            "The par benchmark and remaining-run table are empirical and use equal-weighted six-ball checkpoints from training matches only.",
            "Five folds are deterministic match-level folds, not randomized; check by-season holdouts and sample uncertainty in final validation.",
            "Current-rate and par-gap candidate methods are compared on their common cross-validated states only.",
            "The delivered values in this script are for methodology comparison; no player ranking is produced here.",
        ],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fields = ["match_id", "format", "season", "phase", "remaining_ball_band", "wickets_lost", "score_before",
        "innings_ball_num",
        "current_run_rate", "expected_runs_remaining", "expected_final_score", "format_season_par_score", "par_gap",
        "current_rate_stress_percentile", "par_deficit_stress_percentile", "resource_stress_percentile", "baseline_fallback", "validation_actual_final", "prediction_error"]
    with gzip.open(OUT_PATH, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in by_fold_predictions:
            w.writerow({"match_id": r["match_id"], "format": r["format"], "season": r["season"], "phase": r["phase"],
                "remaining_ball_band": r["bucket"], "wickets_lost": r["wickets_lost"], "score_before": r["score"],
                "innings_ball_num": r["ball_num"],
                "current_run_rate": round(r["current_rr"], 5), "expected_runs_remaining": round(r["expected_runs_remaining"], 5),
                "expected_final_score": round(r["expected_final_score"], 5), "format_season_par_score": round(r["format_season_par_score"], 5),
                "par_gap": round(r["par_gap"], 5), "current_rate_stress_percentile": round(r["current_rate_stress"], 5),
                "par_deficit_stress_percentile": round(r["par_deficit_stress"], 5), "resource_stress_percentile": round(r["resource_stress"], 5),
                "baseline_fallback": r["baseline_fallback"], "validation_actual_final": r["validation_actual_final"],
                "prediction_error": round(r["prediction_error"], 5)})
    report["output_file"] = str(OUT_PATH.relative_to(ROOT))
    (REPORT_DIR / "first_innings_par_comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# First-innings par-method comparison", "", f"Generated: {report['generated_at_utc']}", "",
        "## Method", "", report["par_method"], "", f"The baseline uses **{len(checkpoints):,} six-ball training checkpoints** and emits cross-validated values for **{len(by_fold_predictions):,} first-innings state rows**. Each validation fold holds out whole matches.", "",
        f"- Cross-validated expected final score MAE across states: **{report['expected_final_score_mae_delivery_states']} runs**",
        f"- Cross-validated expected final score RMSE across states: **{report['expected_final_score_rmse_delivery_states']} runs**", "",
        "## Sensitivity versus the current-rate method", "", "Jaccard overlap compares high-pressure delivery states selected by the par-deficit method and the current-run-rate method on the same cross-validated states.", "",
        "| Demand weight | Resource weight | Cutoff | Par method rows | Current-rate rows | State overlap (Jaccard) | Matches | Batters | Bowlers |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for v in variants:
        lines.append(f"| {v['demand_weight']:.2f} | {v['resource_weight']:.2f} | p{int(v['cutoff']*100)} | {v['par_method_high_pressure_rows']:,} | {v['current_rate_method_high_pressure_rows']:,} | {v['jaccard_overlap']} | {v['par_method_matches']:,} | {v['par_method_batters']:,} | {v['par_method_bowlers']:,} |")
    lines += ["", "## Methodology interpretation", "", "The expected-score approach provides an explicit first-innings reference against historical scoring, while the current-rate method measures relative scoring pace. The par method is a stronger candidate for first innings because it accounts for wickets and remaining resources through its conditional baseline. Keep both methods in player-ranking sensitivity analysis; neither is a final pressure label yet.", "", "## Caveats", ""]
    lines.extend(f"- {c}" for c in report["caveats"])
    lines += ["", "The match-fold state-level predictions are saved in `data/processed/first_innings_par_diagnostics.csv.gz`.", ""]
    (REPORT_DIR / "FIRST_INNINGS_PAR_COMPARISON.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Compared cross-validated par states on %d first-innings deliveries", len(by_fold_predictions))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
