from __future__ import annotations

import bisect
import csv
import gzip
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

LOG = logging.getLogger("pressureiq.pressure_candidates")
STATE_PATH = ROOT / "data" / "processed" / "delivery_pressure_states.csv.gz"
REPORT_DIR = ROOT / "reports" / "generated"
WEIGHTS = [0.25, 0.50, 0.75]
PERCENTILES = [0.70, 0.75, 0.80]

def percentile_rank(sorted_values: list[float], value: float) -> float:
    left = bisect.bisect_left(sorted_values, value)
    right = bisect.bisect_right(sorted_values, value)
    return ((left + right) / 2) / len(sorted_values) if sorted_values else 0.5

def as_num(value: str) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)

def main() -> int:
    configure_logging(ROOT / "logs")
    rows = []
    with gzip.open(STATE_PATH, "rt", encoding="utf-8", newline="") as f:
        for idx, r in enumerate(csv.DictReader(f)):
            phase = r["match_phase"]
            if phase not in {"Powerplay", "Middle", "Death"}:
                continue
            exact_remaining = as_num(r["balls_remaining_exact"])
            bucket = int(exact_remaining // 12) if exact_remaining is not None else None
            fmt = "IPL" if r["competition_name"].upper().startswith("IPL ") else "T20I"
            rows.append({"idx": idx, "match_id": r["match_id"], "batter_id": r["striker_id"],
                "bowler_id": r["bowler_id"], "batter_name": r["striker_name"], "bowler_name": r["bowler_name"],
                "ball_num": int(r["innings_ball_num"]), "format": fmt, "phase": phase, "bucket": bucket,
                "innings": int(r["innings"]), "wickets_lost": float(r["pre_delivery_wickets_lost"]),
                "required_rr": as_num(r["required_run_rate"]),
                "current_rr": as_num(r["current_run_rate_provisional"]),
                "remaining_exact": exact_remaining is not None})
    groups_chase, groups_first = defaultdict(lambda: {"rr": [], "wickets": []}), defaultdict(lambda: {"rr": [], "wickets": []})
    for r in rows:
        key = (r["format"], r["phase"], r["bucket"])
        if r["innings"] == 2 and r["required_rr"] is not None and key[2] is not None:
            groups_chase[key]["rr"].append(r["required_rr"])
            groups_chase[key]["wickets"].append(r["wickets_lost"])
        elif r["innings"] == 1 and r["current_rr"] is not None and key[2] is not None:
            groups_first[key]["rr"].append(r["current_rr"])
            groups_first[key]["wickets"].append(r["wickets_lost"])
    for group in list(groups_chase.values()) + list(groups_first.values()):
        group["rr"].sort()
        group["wickets"].sort()
    # Each pressure score is an equal or sensitivity-weighted average of two
    # within-format, phase, remaining-ball-band percentile ranks.
    indexed = []
    for r in rows:
        key = (r["format"], r["phase"], r["bucket"])
        if key[2] is None:
            continue
        if r["innings"] == 2 and r["required_rr"] is not None:
            g = groups_chase[key]
            rr_stress = percentile_rank(g["rr"], r["required_rr"])
            resource_stress = percentile_rank(g["wickets"], r["wickets_lost"])
            indexed.append({**r, "kind": "chase", "demand_stress": rr_stress, "resource_stress": resource_stress})
        elif r["innings"] == 1 and r["current_rr"] is not None:
            g = groups_first[key]
            scoring_stress = 1 - percentile_rank(g["rr"], r["current_rr"])
            resource_stress = percentile_rank(g["wickets"], r["wickets_lost"])
            indexed.append({**r, "kind": "first_innings", "demand_stress": scoring_stress, "resource_stress": resource_stress})

    variants = []
    for weight in WEIGHTS:
        for threshold in PERCENTILES:
            selected = []
            for r in indexed:
                score = weight * r["demand_stress"] + (1 - weight) * r["resource_stress"]
                if score >= threshold:
                    selected.append(r)
            by_kind = Counter(r["kind"] for r in selected)
            by_format_kind = Counter((r["format"], r["kind"]) for r in selected)
            variants.append({"demand_weight": weight, "resource_weight": round(1-weight, 2),
                "within_group_percentile_cutoff": threshold,
                "pressure_delivery_rows": len(selected),
                "eligible_indexed_rows": len(indexed),
                "pressure_share_pct": round(100 * len(selected) / len(indexed), 3) if indexed else None,
                "unique_matches": len({r["match_id"] for r in selected}),
                "unique_batters": len({r["batter_id"] for r in selected}),
                "unique_bowlers": len({r["bowler_id"] for r in selected}),
                "first_innings_rows": by_kind["first_innings"], "chase_rows": by_kind["chase"],
                "by_format_and_type": {f"{fmt}_{kind}": n for (fmt, kind), n in by_format_kind.items()}})
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state_rows_in_phase_groups": len(rows), "rows_with_valid_pressure_components": len(indexed),
        "grouping": "competition type (IPL or T20I) x innings phase x 12-ball remaining quota band",
        "candidate_method": {
            "chase_demand_component": "within-group percentile rank of pre-delivery required run rate (higher is harder)",
            "first_innings_demand_component": "reverse within-group percentile rank of pre-delivery current run rate (lower is more constrained)",
            "resource_component": "within-group percentile rank of wickets already lost (more wickets lost is greater resource stress)",
            "pressure_score": "weighted mean of demand and resource components; neither component uses the current delivery outcome or player identity",
            "thresholds": PERCENTILES, "demand_weights": WEIGHTS,
        },
        "sensitivity_variants": variants,
        "recommended_provisional_setting": {"demand_weight": 0.5, "resource_weight": 0.5, "within_group_percentile_cutoff": 0.75},
        "limitations": [
            "This is a candidate pressure-state definition, not a final rating method or ranking.",
            "A within-group percentile identifies unusually demanding/resource-constrained states relative to the supplied historical mix; it does not estimate win probability or prove psychological pressure.",
            "First-innings current run rate is only available when prior ball-count history is exact; scoring par/expected-final-score models should be assessed as an alternative.",
            "Small or tied groups can yield coarse percentile ranks; report group sizes and test alternate group bands before finalizing labels.",
            "The 70/75/80 percentile and 25/50/75 demand weights are sensitivity settings, not competition-prescribed cutoffs.",
            "The threshold is selected on deliveries; player rankings must be checked for stability across variants and match-level resampling before final use.",
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "pressure_candidate_sensitivity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    indexed_path = ROOT / "data" / "processed" / "pressure_candidate_states.csv.gz"
    with gzip.open(indexed_path, "wt", encoding="utf-8", newline="") as f:
        fields = ["match_id", "innings", "innings_ball_num", "format", "phase", "remaining_ball_band",
            "pressure_type", "batter_id", "batter_name", "bowler_id", "bowler_name",
            "demand_stress_percentile", "resource_stress_percentile", "score_weight_25", "score_weight_50", "score_weight_75"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in indexed:
            writer.writerow({"match_id": r["match_id"], "innings": r["innings"], "innings_ball_num": r["ball_num"],
                "format": r["format"], "phase": r["phase"], "remaining_ball_band": r["bucket"],
                "pressure_type": r["kind"], "batter_id": r["batter_id"], "batter_name": r["batter_name"],
                "bowler_id": r["bowler_id"], "bowler_name": r["bowler_name"],
                "demand_stress_percentile": round(r["demand_stress"], 6),
                "resource_stress_percentile": round(r["resource_stress"], 6),
                **{f"score_weight_{int(weight*100)}": round(weight*r["demand_stress"]+(1-weight)*r["resource_stress"], 6) for weight in WEIGHTS}})
    report["indexed_state_output"] = str(indexed_path.relative_to(ROOT))
    (REPORT_DIR / "pressure_candidate_sensitivity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Pressure definition candidate and sensitivity", "", f"Generated: {report['generated_at_utc']}", "",
        "## Candidate, not final", "", "Use pre-delivery state only. Compare demand stress and depleted resources within competition type, phase and a two-over remaining-ball band. The current delivery outcome and player identity do not affect the state score.", "",
        "- **Chases:** percentile rank of required run rate (higher means harder) plus percentile rank of wickets lost.",
        "- **First innings:** reverse percentile rank of current run rate (lower means more constrained) plus percentile rank of wickets lost.",
        "- **Candidate pressure score:** weighted average of those two components.", "",
        f"Rows with both components available: **{len(indexed):,}** out of **{len(rows):,}** phase-group rows.", "",
        "## Sensitivity grid", "", "| Demand weight | Resource weight | High-pressure cutoff | Pressure deliveries | Share of candidate rows | First innings | Chases | Matches | Batters | Bowlers |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for v in variants:
        lines.append(f"| {v['demand_weight']:.2f} | {v['resource_weight']:.2f} | p{int(v['within_group_percentile_cutoff']*100)} | {v['pressure_delivery_rows']:,} | {v['pressure_share_pct']:.2f}% | {v['first_innings_rows']:,} | {v['chase_rows']:,} | {v['unique_matches']:,} | {v['unique_batters']:,} | {v['unique_bowlers']:,} |")
    lines += ["", "## Provisional setting for the next analysis", "", "Start with equal demand/resource weights and the 75th percentile. This gives the two components equal influence and selects the upper quarter of comparable states. Keep 70th and 80th percentiles and 25/75 versus 75/25 weights as sensitivity alternatives.", "",
        "This setting is not locked until group sizes, pressure-volume balance, and player-score/rank stability are reviewed. It also needs a comparison with a first-innings par/expected-score approach before final methodology sign-off.", "", "## Limitations", ""]
    lines.extend(f"- {note}" for note in report["limitations"])
    lines += ["", "The structured sensitivity results are in `pressure_candidate_sensitivity.json`.", ""]
    (REPORT_DIR / "PRESSURE_CANDIDATE_SENSITIVITY.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Evaluated %d pressure candidates across %d comparable state rows", len(variants), len(indexed))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
