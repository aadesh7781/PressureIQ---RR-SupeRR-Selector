from __future__ import annotations

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

LOG = logging.getLogger("pressureiq.match_states")
IN_PATH = ROOT / "data" / "processed" / "integrated_deliveries.csv.gz"
OUT_PATH = ROOT / "data" / "processed" / "delivery_pressure_states.csv.gz"
REPORT_DIR = ROOT / "reports" / "generated"
STANDARD_BALL_LIMIT = 120

def to_int(value: str | None) -> int | None:
    if value is None or value.strip() in {"", "None", "null"}:
        return None
    return int(float(value))

def phase(over_id: int) -> str:
    if 1 <= over_id <= 6:
        return "Powerplay"
    if 7 <= over_id <= 15:
        return "Middle"
    if 16 <= over_id <= 20:
        return "Death"
    return "Outside standard T20 overs"

def main() -> int:
    configure_logging(ROOT / "logs")
    # Pass 1: obtain first-innings totals for chase targets and check the revised
    # innings-ball-limit field before using it as a quota.
    first_totals = defaultdict(int)
    match_meta = {}
    innings_known_legal = Counter()
    invalid_revised_values = Counter()
    order_breaks = 0
    previous_key = None
    with gzip.open(IN_PATH, "rt", encoding="utf-8", newline="") as f:
        for d in csv.DictReader(f):
            key = (int(d["match_id"]), int(d["innings"]), int(d["innings_ball_num"]))
            if previous_key is not None and key < previous_key:
                order_breaks += 1
            previous_key = key
            mid, inn_no = d["match_id"], int(d["innings"])
            match_meta[mid] = {"name": d["competition_name"], "date": d["match_match_date"],
                "revised_target": d["match_revised_target"],
                "first_limit": d["match_first_inn_revised_overs"],
                "second_limit": d["match_second_inn_revised_overs"]}
            if inn_no == 1:
                first_totals[mid] += int(d["delivery_total_runs_provisional"] or 0)
            if (d["is_legal_ball"] or "").strip().lower() == "true":
                innings_known_legal[(mid, inn_no)] += 1
    if order_breaks:
        raise RuntimeError(f"Integrated data is not ordered by match/innings/ball number ({order_breaks} breaks); refusing to emit states.")

    quota_by_innings, quota_source = {}, {}
    explicit_quota_count = assumed_quota_count = 0
    for mid, meta in match_meta.items():
        for inn_no, field in ((1, "first_limit"), (2, "second_limit")):
            raw = to_int(meta[field])
            if raw is not None:
                if raw <= 0 or raw > STANDARD_BALL_LIMIT or raw % 6 != 0:
                    invalid_revised_values[raw] += 1
                    quota_by_innings[(mid, inn_no)] = None
                    quota_source[(mid, inn_no)] = "invalid_source_value"
                else:
                    quota_by_innings[(mid, inn_no)] = raw
                    quota_source[(mid, inn_no)] = "explicit_revised_ball_limit_inferred"
                    explicit_quota_count += 1
            else:
                quota_by_innings[(mid, inn_no)] = STANDARD_BALL_LIMIT
                quota_source[(mid, inn_no)] = "assumed_standard_t20_120_balls"
                assumed_quota_count += 1
    quota_overruns = []
    for key, known in innings_known_legal.items():
        quota = quota_by_innings.get(key)
        if quota is not None and known > quota:
            quota_overruns.append({"match_id": key[0], "innings": key[1], "known_legal_balls": known, "quota": quota})

    # Pass 2: stream pre-delivery states in validated source order.
    header = None
    output_path = OUT_PATH
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    state_rows = Counter()
    target_sources, schedule_sources, innings_rows = Counter(), Counter(), Counter()
    state_flag_counts = Counter()
    running = {}
    previous_key = None
    with gzip.open(IN_PATH, "rt", encoding="utf-8", newline="") as source, gzip.open(output_path, "wt", encoding="utf-8", newline="") as dest:
        reader = csv.DictReader(source)
        header = list(reader.fieldnames or []) + [
            "match_phase", "pre_delivery_score_runs_provisional", "pre_delivery_wickets_lost",
            "pre_delivery_wickets_remaining", "known_legal_balls_before", "unresolved_deliveries_before",
            "scheduled_innings_balls", "scheduled_balls_source", "known_balls_remaining_upper_bound",
            "balls_remaining_exact", "target_runs", "target_source", "runs_required",
            "current_run_rate_provisional", "required_run_rate", "state_quality_flag"]
        writer = csv.DictWriter(dest, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for d in reader:
            mid, inn_no = d["match_id"], int(d["innings"])
            if inn_no not in (1, 2):
                continue
            order_key = (int(mid), inn_no, int(d["innings_ball_num"]))
            if previous_key is not None and order_key < previous_key:
                raise RuntimeError("Source order changed between state passes.")
            previous_key = order_key
            key = (mid, inn_no)
            state = running.setdefault(key, {"runs": 0, "wickets": 0, "known_legal": 0, "unresolved": 0})
            quota = quota_by_innings.get(key)
            sched_source = quota_source.get(key, "unknown")
            if inn_no == 1:
                target, target_source = None, "not_applicable_first_innings"
            else:
                revised_target = to_int(d["match_revised_target"])
                if revised_target is not None and revised_target > 0:
                    target, target_source = revised_target, "match_revised_target_field"
                elif mid in first_totals:
                    target, target_source = first_totals[mid] + 1, "first_innings_delivery_total_plus_one_provisional"
                else:
                    target, target_source = None, "unavailable_first_innings_total"
            remaining_upper = quota - state["known_legal"] if quota is not None else None
            exact_remaining = remaining_upper if state["unresolved"] == 0 else None
            runs_required = max(0, target - state["runs"]) if target is not None else None
            current_rate = (state["runs"] * 6 / state["known_legal"]
                if state["known_legal"] > 0 and state["unresolved"] == 0 else None)
            required_rate = (runs_required * 6 / exact_remaining
                if inn_no == 2 and runs_required is not None and exact_remaining is not None and exact_remaining > 0 and runs_required > 0 else None)
            flags = []
            if quota is None:
                flags.append("invalid_scheduled_ball_limit")
            if remaining_upper is not None and remaining_upper < 0:
                flags.append("known_legal_balls_exceed_scheduled_limit")
            if state["unresolved"] > 0:
                flags.append("prior_delivery_legal_status_unresolved")
            if inn_no == 2 and target is None:
                flags.append("chase_target_unavailable")
            if flags:
                state_flag_counts.update(flags)
            row = dict(d)
            row.update({
                "match_phase": phase(int(d["overid"])),
                "pre_delivery_score_runs_provisional": state["runs"],
                "pre_delivery_wickets_lost": state["wickets"],
                "pre_delivery_wickets_remaining": max(0, 10 - state["wickets"]),
                "known_legal_balls_before": state["known_legal"],
                "unresolved_deliveries_before": state["unresolved"],
                "scheduled_innings_balls": quota if quota is not None else "",
                "scheduled_balls_source": sched_source,
                "known_balls_remaining_upper_bound": remaining_upper if remaining_upper is not None else "",
                "balls_remaining_exact": exact_remaining if exact_remaining is not None else "",
                "target_runs": target if target is not None else "",
                "target_source": target_source, "runs_required": runs_required if runs_required is not None else "",
                "current_run_rate_provisional": current_rate if current_rate is not None else "",
                "required_run_rate": required_rate if required_rate is not None else "",
                "state_quality_flag": ";".join(flags),
            })
            writer.writerow(row)
            state_rows["total"] += 1
            state_rows[f"innings_{inn_no}"] += 1
            state_rows[f"phase_{phase(int(d['overid']))}"] += 1
            if target is not None:
                target_sources[target_source] += 1
            schedule_sources[sched_source] += 1
            innings_rows[key] += 1
            if target is not None and inn_no == 2:
                state_rows["chase_target_available"] += 1
            if required_rate is not None:
                state_rows["required_run_rate_available"] += 1
            if exact_remaining is not None:
                state_rows["exact_balls_remaining_available"] += 1
            # Advance the state only after writing the pre-delivery row.
            state["runs"] += int(d["delivery_total_runs_provisional"] or 0)
            state["wickets"] += int((d["is_wicket"] or "").strip().lower() in {"true", "1", "yes"})
            legal_status = (d["is_legal_ball"] or "").strip().lower()
            if legal_status == "true":
                state["known_legal"] += 1
            elif legal_status not in {"false"}:
                state["unresolved"] += 1

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_file": str(IN_PATH.relative_to(ROOT)), "output_file": str(output_path.relative_to(ROOT)),
        "eligible_match_count": len(match_meta), "state_row_counts": dict(state_rows),
        "explicit_revised_ball_limit_innings": explicit_quota_count,
        "standard_120_ball_assumption_innings": assumed_quota_count,
        "invalid_revised_ball_limit_values": dict(invalid_revised_values),
        "known_legal_ball_quota_overruns": quota_overruns,
        "quota_semantics_note": "Non-empty revised_overs fields are interpreted as scheduled legal-ball limits because observed values are multiples of six (30–114). Blank values default to 120 balls for IPL/T20I; this is an explicit standard-format assumption.",
        "target_source_counts_by_delivery_row": dict(target_sources),
        "scheduled_balls_source_counts_by_delivery_row": dict(schedule_sources),
        "state_quality_flag_counts": dict(state_flag_counts),
        "feature_timing": "Every feature ending in pre_delivery or named as a state is computed before the current row's outcome is added.",
        "pressure_thresholds": "Not assigned in this step; this script emits state features for later, documented threshold/baseline analysis.",
        "limitations": [
            "Delivery totals and first-innings-derived chase targets remain provisional because source extra semantics are unresolved.",
            "Required run rate is emitted only when the chase target and exact remaining balls are available; prior unresolved deliveries make remaining balls inexact.",
            "First-innings pressure requires a comparison baseline or expected-score model; current run rate and resources are supplied as features, not a pressure verdict.",
            "The current scheduled-ball limit is inferred from source revised fields and standard 120-ball T20 rules; inspect any overruns or exceptions before modeling.",
            "Innings 3 and 4 are excluded from the standard match-state file pending super-over identification.",
        ],
    }
    (REPORT_DIR / "match_state_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Pre-delivery match-state feature audit", "", f"Generated: {report['generated_at_utc']}", "",
        "## Coverage", "", f"- Eligible matches in integrated data: **{len(match_meta):,}**",
        f"- Standard innings 1 and 2 state rows: **{state_rows['total']:,}**",
        f"- First-innings rows: **{state_rows['innings_1']:,}**; second-innings rows: **{state_rows['innings_2']:,}**",
        f"- Chase rows with a target: **{state_rows['chase_target_available']:,}**",
        f"- Chase rows with exact remaining-ball count: **{state_rows['exact_balls_remaining_available']:,}**",
        f"- Chase rows with computed required run rate: **{state_rows['required_run_rate_available']:,}**", "",
        "## State fields", "", "Every row describes the state before that delivery. The output includes provisional score, wickets lost/remaining, known legal balls bowled, unresolved prior-delivery count, scheduled-ball limit, remaining-ball upper bound and exact count where available, target/runs required, current run rate, required run rate, and powerplay/middle/death phase.", "",
        "## Scheduled-ball checks", "", report["quota_semantics_note"], "",
        f"- Innings quotas from non-empty revised fields: **{explicit_quota_count}**",
        f"- Innings quotas assumed at 120 balls: **{assumed_quota_count}**",
        f"- Invalid revised values: **{sum(invalid_revised_values.values())}**",
        f"- Known-legal-ball quota overruns: **{len(quota_overruns)}**", "",
        "## Assumptions and limitations", ""]
    lines.extend(f"- {note}" for note in report["limitations"])
    lines += ["", "Pressure thresholds are deliberately left for the next methodology step; the state table does not label an innings or player as under pressure by itself.", ""]
    (REPORT_DIR / "MATCH_STATE_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote %d pre-delivery state rows to %s", state_rows["total"], output_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
