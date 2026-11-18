from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
REPORTS = ROOT / "reports" / "generated"


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    scoring = json.loads((REPORTS / "player_scoring_draft.json").read_text(encoding="utf-8"))
    batters = scoring["top10_by_candidate"]["current_rate|0.5|0.75"]["batters"]
    ids = {str(r["player_id"]): r for r in batters}
    comps = {r["comp_id"] for r in read_csv(RAW / "competitions.csv")
        if r["match_format"].strip().upper() == "T20I" or r["comp_name"].strip().upper().startswith("IPL ")}
    matches = {r["match_id"] for r in read_csv(RAW / "matches.csv")
        if r["comp_id"] in comps and r["match_date"] >= "2022-01-01"}
    orders = {pid: [] for pid in ids}
    for row in read_csv(RAW / "playing_eleven.csv"):
        if row["match_id"] not in matches or row["player_id"] not in orders or row["is_played"].strip().lower() != "true":
            continue
        try:
            orders[row["player_id"]].append(int(row["batting_order"]))
        except (TypeError, ValueError):
            continue
    rows = []
    for pid, candidate in ids.items():
        positions = orders[pid]
        top6 = sum(pos <= 6 for pos in positions)
        lower = sum(pos >= 7 for pos in positions)
        total_balls = candidate["pressure_exposure_balls"] + candidate["baseline_exposure_balls"]
        rows.append({"player_id": pid, "player_name": candidate["player_name"],
            "candidate_pressure_balls": candidate["pressure_exposure_balls"],
            "candidate_baseline_balls": candidate["baseline_exposure_balls"],
            "candidate_total_balls": total_balls,
            "candidate_pressure_matches": candidate["pressure_matches"],
            "eligible_xi_appearances_with_order": len(positions),
            "median_batting_order": median(positions) if positions else None,
            "top_six_appearances": top6, "lower_order_7_to_11_appearances": lower,
            "lower_order_appearance_share": round(lower / len(positions), 4) if positions else None})
    report = {"generated_at_utc": scoring["generated_at_utc"],
        "scope": "Eligible IPL and T20I match lineups from 2022 onward",
        "interpretation": "Batting order is deployment evidence, not an official specialist-role label. A lower-order player may still be a valuable finisher; the current top-batter rank alone cannot distinguish that from a specialist-batter claim.",
        "candidate_batters": rows,
        "volume_sensitivity": scoring["batter_total_volume_sensitivity"],
        "working_batter_rule": {"minimum_total_valid_batting_balls": 250, "minimum_pressure_balls": 60,
            "minimum_baseline_balls": 60, "minimum_pressure_matches": 5},
        "conclusion": "Apply a 250 total valid-ball floor to the working batter shortlist, in addition to the existing pressure, baseline and match minimums. Shaheen Afridi does not qualify and is retained only as a lower-order pressure-batting observation, not a top-ten batter candidate."}
    (REPORTS / "BATTER_ELIGIBILITY_REVIEW.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Batter eligibility and role-proxy review", "",
        "The scoring list measures pressure batting change, not whether a player is a specialist batter. This review adds total valid-ball exposure and batting-order placement from the supplied lineups.", "",
        "| Candidate | Pressure balls | Baseline balls | Total valid balls | Pressure matches | XI appearances with order | Median batting order | Top-six appearances | Positions 7–11 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        med = f"{r['median_batting_order']:g}" if r["median_batting_order"] is not None else "n/a"
        lines.append(f"| {r['player_name']} | {r['candidate_pressure_balls']} | {r['candidate_baseline_balls']} | {r['candidate_total_balls']} | {r['candidate_pressure_matches']} | {r['eligible_xi_appearances_with_order']} | {med} | {r['top_six_appearances']} | {r['lower_order_7_to_11_appearances']} |")
    lines += ["", "## Shaheen Afridi", "",
        "Shaheen qualifies under the draft rule with 111 pressure batting balls, 64 baseline balls and 15 pressure matches (175 counted balls total). In the eligible XI data he appears 48 times with a batting order: median position 9; 47 of 48 appearances were at positions 7–11. His draft score is therefore best described as a possible lower-order survival signal, not evidence that he is a proven top-ten batter.", "",
        "Ravichandran Ashwin is another lower-order case: 284 counted balls, median batting order 7, and 45 of 62 eligible XI appearances at positions 7–11. He clears the 250-ball working floor but should be presented as a lower-order batting contribution, not a top-order specialist. Finisher and specialist role labels are not supplied, so the lineup positions remain a proxy.", "",
        "## Sample-volume sensitivity", "",
        "| Minimum total valid batting balls | Eligible players | Overlap with current top 10 | Removed current names |", "|---:|---:|---:|---|"]
    for r in report["volume_sensitivity"]:
        lines.append(f"| {r['minimum_total_valid_batting_balls']} | {r['eligible_players']} | {r['top10_overlap_with_default']}/10 | {', '.join(r['excluded_from_default_top10']) or 'None'} |")
    lines += ["", "**Working rule:** minimum 250 total valid batting balls, in addition to at least 60 pressure balls, 60 baseline balls and five pressure matches. At this floor, Shaheen is excluded and 9/10 names from the initial list remain. The 300- and 500-ball screens remain stricter sensitivity checks. This is a project methodology choice, not a competition rule; check the official brief/dropdown for any explicit role requirement.", "",
        report["interpretation"], ""]
    (REPORTS / "BATTER_ELIGIBILITY_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {REPORTS / 'BATTER_ELIGIBILITY_REVIEW.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
