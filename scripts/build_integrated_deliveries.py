from __future__ import annotations

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

LOG = logging.getLogger("pressureiq.integration")
RAW = ROOT / "data" / "raw"
OUT_DATA = ROOT / "data" / "processed"
OUT_REPORTS = ROOT / "reports" / "generated"
START_DATE = "2022-01-01"
MATCH_FIELDS = ["match_id", "comp_id", "match_number", "match_date", "stage", "venue_id", "venue_country",
    "home_team_id", "home_team", "away_team_id", "away_team", "toss_win_team_id", "toss_opted", "result",
    "first_inn_revised_overs", "second_inn_revised_overs", "revised_target", "knock_out", "win_team_id"]
EXTRA_CLASS = {
    "none": "no_extra", "wide": "illegal_ball", "overstep": "illegal_ball",
    "byes": "legal_ball", "legbyes": "legal_ball", "other": "unresolved", "penalty": "unresolved",
}

def load_csv(name: str) -> list[dict[str, str]]:
    with (RAW / name).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def as_int(value: str | None) -> int:
    if value is None or value.strip() in {"", "None", "null"}:
        return 0
    return int(float(value))

def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}

def main() -> int:
    configure_logging(ROOT / "logs")
    comps = load_csv("competitions.csv")
    matches = load_csv("matches.csv")
    deliveries = load_csv("ball_by_ball.csv")
    comp_by_id = {c["comp_id"]: c for c in comps}
    match_by_id = {m["match_id"]: m for m in matches}
    eligible_comp: dict[str, str] = {}
    for c in comps:
        name = c["comp_name"].strip()
        fmt = c["match_format"].strip().upper()
        if fmt == "T20I":
            eligible_comp[c["comp_id"]] = "T20I"
        elif name.upper().startswith("IPL "):
            eligible_comp[c["comp_id"]] = "IPL"
    eligible_matches: dict[str, dict[str, str]] = {}
    for m in matches:
        if m["comp_id"] in eligible_comp and m["match_date"] >= START_DATE:
            eligible_matches[m["match_id"]] = m
    comp_counts, ball_counts, extra_types, innings_counts = Counter(), Counter(), Counter(), Counter()
    flag_counts = Counter()
    output_fields = list(deliveries[0].keys()) + [
        "competition_name", "match_format", "season", *[f"match_{f}" for f in MATCH_FIELDS if f != "match_id"],
        "is_legal_ball", "legal_ball_status", "delivery_total_runs_provisional", "bowler_conceded_runs_provisional", "accounting_quality_flag"]
    # match_date and other context arrive prefixed to keep source delivery fields intact.
    OUT_DATA.mkdir(parents=True, exist_ok=True)
    OUT_REPORTS.mkdir(parents=True, exist_ok=True)
    output_path = OUT_DATA / "integrated_deliveries.csv.gz"
    matched_ids, eligible_rows, unresolved_rows, concession_unknown_rows, legal_known, illegal_known = set(), 0, 0, 0, 0, 0
    total_runs_provisional = 0
    duplicate_delivery_keys = 0
    seen_delivery_keys = set()
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        for ball in deliveries:
            m = eligible_matches.get(ball["match_id"])
            if m is None:
                continue
            comp = comp_by_id[m["comp_id"]]
            kind = eligible_comp[m["comp_id"]]
            matched_ids.add(m["match_id"])
            eligible_rows += 1
            comp_counts[kind] += 1
            innings_counts[int(ball["innings"])] += 1
            extra_type = (ball.get("extras_type") or "").strip().lower()
            if extra_type in {"", "none", "null"}:
                extra_type = "none"
            extra_types[extra_type] += 1
            striker_runs = as_int(ball.get("runs"))
            extra_1, extra_2 = as_int(ball.get("extras1_runs")), as_int(ball.get("extras_2_runs"))
            extras = extra_1 + extra_2
            total_runs = striker_runs + extras
            total_runs_provisional += total_runs
            status = EXTRA_CLASS.get(extra_type, "unresolved")
            flag = ""
            # An unlabeled extra is not silently treated as a standard legal delivery.
            if status == "no_extra" and extras != 0:
                status, flag = "unresolved", "extras_amount_without_extras_type"
            elif extra_type in {"byes", "legbyes"} and striker_runs != 0:
                flag = "batter_runs_with_byes_or_legbyes_type"
            if status == "unresolved":
                flag = "unresolved_extras_type"
                unresolved_rows += 1
            if status == "legal_ball" or status == "no_extra":
                legal = True
                legal_known += 1
            elif status == "illegal_ball":
                legal = False
                illegal_known += 1
            else:
                legal = ""
            # Byes/leg-byes and penalty extras are not charged to the bowler. For
            # 'other' and malformed no-type extras, attribution remains unresolved.
            if status == "unresolved":
                bowler_conceded: int | str = ""
                concession_unknown_rows += 1
            elif extra_type in {"byes", "legbyes", "penalty"}:
                if extra_type == "penalty" or (extra_type in {"byes", "legbyes"} and striker_runs != 0):
                    bowler_conceded = ""
                    concession_unknown_rows += 1
                else:
                    bowler_conceded = 0
            else:
                bowler_conceded = striker_runs + extras
            if extra_type == "penalty":
                flag = "penalty_run_delivery_semantics_unresolved"
            key = (ball["match_id"], ball["innings"], ball["innings_ball_num"])
            if key in seen_delivery_keys:
                duplicate_delivery_keys += 1
                flag = (flag + ";" if flag else "") + "duplicate_match_innings_ball_number"
            seen_delivery_keys.add(key)
            four_mismatch = (striker_runs == 4) != truthy(ball.get("is_four"))
            six_mismatch = (striker_runs == 6) != truthy(ball.get("is_six"))
            if four_mismatch:
                flag_counts["four_run_flag_mismatch"] += 1
                flag = (flag + ";" if flag else "") + "four_flag_disagrees_with_runs"
            if six_mismatch:
                flag_counts["six_run_flag_mismatch"] += 1
                flag = (flag + ";" if flag else "") + "six_flag_disagrees_with_runs"
            if truthy(ball.get("is_wicket")):
                flag_counts["wicket_deliveries"] += 1
            out = dict(ball)
            out.update({"competition_name": comp["comp_name"], "match_format": comp["match_format"], "season": comp["season"]})
            out.update({f"match_{f}": m[f] for f in MATCH_FIELDS if f != "match_id"})
            out.update({"is_legal_ball": legal, "legal_ball_status": status,
                "delivery_total_runs_provisional": total_runs,
                "bowler_conceded_runs_provisional": bowler_conceded,
                "accounting_quality_flag": flag})
            writer.writerow(out)
    for mid in matched_ids:
        m = eligible_matches[mid]
        ball_counts[eligible_comp[m["comp_id"]]] += 1
    absent = set(eligible_matches) - matched_ids
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection_rule": "T20I format or competition name beginning 'IPL '; match_date on/after 2022-01-01",
        "input_delivery_rows": len(deliveries), "eligible_match_rows": len(eligible_matches),
        "eligible_matches_with_delivery_data": len(matched_ids), "eligible_matches_without_delivery_data": len(absent),
        "eligible_delivery_rows": eligible_rows, "delivery_rows_by_competition_type": dict(comp_counts),
        "eligible_match_counts_by_competition_type": dict(ball_counts),
        "innings_number_counts": {str(k): v for k, v in sorted(innings_counts.items())},
        "extras_type_counts": dict(extra_types), "known_legal_delivery_rows": legal_known,
        "known_illegal_delivery_rows": illegal_known, "unresolved_extra_classification_rows": unresolved_rows,
        "bowler_concession_unknown_rows": concession_unknown_rows,
        "provisional_delivery_total_runs": total_runs_provisional, "duplicate_match_innings_ball_number_rows_after_first": duplicate_delivery_keys,
        "source_flag_checks": dict(flag_counts), "output_file": str(output_path.relative_to(ROOT)),
        "accounting_notes": [
            "Source field meanings are not fully documented in the CSVs. Runs plus extras1_runs plus extras_2_runs is labeled provisional until reconciled to independent innings totals.",
            "Wide and Overstep are marked illegal-ball categories; byes, legbyes and no-extra rows are marked legal where extra fields are internally consistent.",
            "The 'other' and 'Penalty' extra categories, and malformed unlabeled extras, remain unresolved rather than being forced into legal-ball or bowler-concession formulas.",
            "Bowler-conceded runs are provisional. Byes/legbyes are excluded; unresolved cases are blank. Validate source-specific penalty and no-ball conventions before player metrics.",
            "Innings 3 and 4 are retained; likely super-over context should be confirmed from match/innings rules before defining states.",
            "The `is_four` flag is false on 20 rows with runs=4 and `is_six` is false on 3 rows with runs=6. Numeric `runs` and source boundary flags must not be assumed interchangeable; the integrated file flags these rows.",
            "The supplied matches table has no innings scorecard totals, so provisional delivery sums cannot yet be reconciled to an independent innings total from these six files.",
        ],
    }
    (OUT_REPORTS / "integration_accounting.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Integrated delivery and accounting audit", "", f"Generated: {report['generated_at_utc']}", "",
        "## Scope and join", "", f"- Eligible match rows: **{len(eligible_matches):,}**",
        f"- Eligible matches with delivery data: **{len(matched_ids):,}**",
        f"- Eligible matches without delivery data: **{len(absent):,}**",
        f"- Integrated delivery rows: **{eligible_rows:,}**", f"- Output: `{report['output_file']}`", "",
        "| Competition type | Delivery rows |", "|---|---:|"]
    for kind, count in sorted(comp_counts.items()):
        lines.append(f"| {kind} | {count:,} |")
    lines += ["", "## Delivery and extras accounting diagnostics", "",
        f"- Known legal-ball rows: **{legal_known:,}**",
        f"- Known illegal-ball rows (wide/Overstep): **{illegal_known:,}**",
        f"- Unresolved extra-classification rows: **{unresolved_rows:,}**",
        f"- Rows with bowler-concession attribution unresolved: **{concession_unknown_rows:,}**",
        f"- Duplicate `(match_id, innings, innings_ball_num)` records after first: **{duplicate_delivery_keys:,}**",
        f"- Provisional sum of `runs + extras1_runs + extras_2_runs`: **{total_runs_provisional:,}**", "",
        "| Extras type | Rows |", "|---|---:|"]
    for label, count in sorted(extra_types.items()):
        lines.append(f"| {label} | {count:,} |")
    lines += ["", "## Validation flags", ""]
    if flag_counts:
        for label, count in sorted(flag_counts.items()):
            lines.append(f"- `{label}`: {count:,}")
    else:
        lines.append("No four/six source-flag mismatches were detected.")
    lines += ["", "## Interpretation and unresolved decisions", ""]
    lines.extend(f"- {note}" for note in report["accounting_notes"])
    lines += ["", "This dataset is integrated and useful for subsequent analysis, but run attribution and pressure-state features must not treat provisional totals as fully reconciled scorecards until the unresolved extra semantics and innings totals have been checked.", ""]
    (OUT_REPORTS / "INTEGRATION_ACCOUNTING.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote %s eligible delivery rows to %s", eligible_rows, output_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
