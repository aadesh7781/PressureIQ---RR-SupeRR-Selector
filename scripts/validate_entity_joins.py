from __future__ import annotations

import csv
import json
import logging
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

LOG = logging.getLogger("pressureiq.entity_joins")
RAW = ROOT / "data" / "raw"
OUT = ROOT / "reports" / "generated"
START_DATE = "2022-01-01"

def load(name: str) -> list[dict[str, str]]:
    with (RAW / name).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def clean_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", (value or "")).casefold().split())

def main() -> int:
    configure_logging(ROOT / "logs")
    competitions = load("competitions.csv")
    matches_all = load("matches.csv")
    teams = load("teams.csv")
    player_map = load("competition_team_player_mapping.csv")
    playing = load("playing_eleven.csv")
    comp_by_id = {c["comp_id"]: c for c in competitions}
    eligible_comp = {c["comp_id"] for c in competitions if c["match_format"].strip().upper() == "T20I" or c["comp_name"].strip().upper().startswith("IPL ")}
    matches = {m["match_id"]: m for m in matches_all if m["comp_id"] in eligible_comp and m["match_date"] >= START_DATE}
    teams = [t for t in teams if t["comp_id"] in eligible_comp]
    player_map = [p for p in player_map if p["comp_id"] in eligible_comp]
    team_names = {(t["comp_id"], t["team_id"]): t["team_name"] for t in teams}
    player_scope = {(p["comp_id"], p["player_id"]) for p in player_map}
    player_team_scope = {(p["comp_id"], p["team_id"], p["player_id"]) for p in player_map}
    player_names = defaultdict(set)
    player_teams = defaultdict(set)
    for p in player_map:
        player_names[(p["comp_id"], p["player_id"])].add(clean_name(p["player_name"]))
        player_teams[(p["comp_id"], p["player_id"])].add(p["team_id"])

    match_team_refs = Counter()
    for m in matches.values():
        match_team_refs["home_team"] += 1
        if (m["comp_id"], m["home_team_id"]) not in team_names:
            match_team_refs["home_team_unmatched"] += 1
        match_team_refs["away_team"] += 1
        if (m["comp_id"], m["away_team_id"]) not in team_names:
            match_team_refs["away_team_unmatched"] += 1
    player_event_stats = defaultdict(Counter)
    team_event_stats = Counter()
    name_stats = defaultdict(Counter)
    name_mismatch_examples = defaultdict(list)
    name_mismatch_seen = defaultdict(set)
    delivery_rows, eligible_balls = load("ball_by_ball.csv"), 0
    for b in delivery_rows:
        m = matches.get(b["match_id"])
        if not m:
            continue
        eligible_balls += 1
        comp_id = m["comp_id"]
        for role, team_col, player_col, name_col in [
            ("batter", "batting_team_id", "striker_id", "striker_name"),
            ("non_striker", "batting_team_id", "non_striker_id", "non_striker_name"),
            ("bowler", "bowling_team_id", "bowler_id", "bowler_name"),
        ]:
            team_id, player_id, player_name = b[team_col], b[player_col], b[name_col]
            team_event_stats[f"{role}_events"] += 1
            if (comp_id, team_id) not in team_names:
                team_event_stats[f"{role}_team_unmatched"] += 1
            stat = player_event_stats[role]
            stat["events"] += 1
            if (comp_id, player_id) in player_scope:
                stat["competition_player_matched"] += 1
            else:
                stat["competition_player_unmatched"] += 1
            if (comp_id, team_id, player_id) in player_team_scope:
                stat["competition_team_player_matched"] += 1
            else:
                stat["competition_team_player_unmatched"] += 1
            expected = player_names.get((comp_id, player_id), set())
            if expected:
                name_stats[role]["compared"] += 1
                if clean_name(player_name) in expected:
                    name_stats[role]["name_matched_normalized"] += 1
                else:
                    name_stats[role]["name_mismatch"] += 1
                    example_key = (player_id, player_name, tuple(sorted(expected)))
                    if example_key not in name_mismatch_seen[role] and len(name_mismatch_examples[role]) < 10:
                        name_mismatch_seen[role].add(example_key)
                        name_mismatch_examples[role].append({"comp_id": comp_id, "player_id": player_id,
                            "delivery_name": player_name, "mapping_names": sorted(expected)})
        dismissed = b["dismissed_player_id"].strip()
        if dismissed and dismissed not in {"0", "None"}:
            stat = player_event_stats["dismissed_player"]
            stat["events"] += 1
            if (comp_id, dismissed) in player_scope:
                stat["competition_player_matched"] += 1
            else:
                stat["competition_player_unmatched"] += 1
            team_id = b["batting_team_id"]
            if (comp_id, team_id, dismissed) in player_team_scope:
                stat["competition_team_player_matched"] += 1
            else:
                stat["competition_team_player_unmatched"] += 1

    playing_stats = Counter()
    for p in playing:
        m = matches.get(p["match_id"])
        if not m:
            continue
        playing_stats["eligible_rows"] += 1
        comp_id = m["comp_id"]
        key = (comp_id, p["team_id"], p["player_id"])
        if (comp_id, p["team_id"]) not in team_names:
            playing_stats["team_unmatched"] += 1
        if (comp_id, p["player_id"]) not in player_scope:
            playing_stats["competition_player_unmatched"] += 1
        if key not in player_team_scope:
            playing_stats["competition_team_player_unmatched"] += 1

    # Check whether a competition-level player map assigns a player to multiple teams.
    multi_team_assignments = sum(len(teams_for_player) > 1 for teams_for_player in player_teams.values())
    match_comp_unmatched = sum(m["comp_id"] not in comp_by_id for m in matches_all)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection_rule": "T20I format or IPL competition name, from 2022-01-01",
        "eligible_match_rows": len(matches), "eligible_delivery_rows": eligible_balls,
        "eligible_playing_eleven_rows": playing_stats["eligible_rows"],
        "matches_with_competition_unmatched_all_source_rows": match_comp_unmatched,
        "match_team_reference_checks": dict(match_team_refs), "delivery_team_reference_checks": dict(team_event_stats),
        "player_event_join_checks": {role: {key: counts[key] for key in ("events", "competition_player_matched", "competition_player_unmatched", "competition_team_player_matched", "competition_team_player_unmatched")} | {
            "competition_player_coverage_pct": round(100 * counts["competition_player_matched"] / counts["events"], 4) if counts["events"] else None,
            "competition_team_player_coverage_pct": round(100 * counts["competition_team_player_matched"] / counts["events"], 4) if counts["events"] else None}
            for role, counts in player_event_stats.items()},
        "delivery_name_checks": {role: {key: counts[key] for key in ("compared", "name_matched_normalized", "name_mismatch")} | {"normalized_name_match_pct": round(100 * counts["name_matched_normalized"] / counts["compared"], 4) if counts["compared"] else None}
            for role, counts in name_stats.items()},
        "delivery_name_mismatch_examples": dict(name_mismatch_examples),
        "playing_eleven_join_checks": dict(playing_stats),
        "mapping_players_assigned_to_multiple_teams_within_competition": multi_team_assignments,
        "notes": [
            "All checks are restricted to supplied IPL and T20I competitions from 2022 onward, except the all-source match-to-competition count.",
            "Name matching normalizes Unicode form, whitespace and case only; different spellings are reported for review, not silently rewritten.",
            "Competition-player match does not guarantee team match. Player transfers or provider data conventions may explain team-level unmatched cases.",
            "Dismissed-player joins are checked when a dismissed_player_id is present; this can differ from striker_id for run-outs and other dismissals.",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "entity_join_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Entity join validation", "", f"Generated: {report['generated_at_utc']}", "",
        f"Scope: **{len(matches):,} eligible match records** and **{eligible_balls:,} eligible deliveries**.", "",
        "## Match and team joins", "", "| Check | Count |", "|---|---:|"]
    for key, value in match_team_refs.items():
        lines.append(f"| {key} | {value:,} |")
    for role in ("batter", "bowler"):
        lines.append(f"| {role}_delivery_team_events | {team_event_stats[f'{role}_events']:,} |")
        lines.append(f"| {role}_delivery_team_unmatched | {team_event_stats[f'{role}_team_unmatched']:,} |")
    lines.append(f"| All-source matches with no competition ID match | {match_comp_unmatched:,} |")
    lines += ["", "## Delivery player joins", "", "| Role | Events | Comp-player matched | Unmatched | Comp-team-player matched | Unmatched | Comp-player coverage | Team-player coverage |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for role, values in sorted(report["player_event_join_checks"].items()):
        lines.append(f"| {role} | {values['events']:,} | {values['competition_player_matched']:,} | {values['competition_player_unmatched']:,} | {values['competition_team_player_matched']:,} | {values['competition_team_player_unmatched']:,} | {values['competition_player_coverage_pct']}% | {values['competition_team_player_coverage_pct']}% |")
    lines += ["", "## Delivery name consistency", "", "| Role | Compared | Normalized name matches | Mismatches | Match rate |", "|---|---:|---:|---:|---:|"]
    for role, values in sorted(report["delivery_name_checks"].items()):
        lines.append(f"| {role} | {values['compared']:,} | {values['name_matched_normalized']:,} | {values['name_mismatch']:,} | {values['normalized_name_match_pct']}% |")
    if name_mismatch_examples:
        lines += ["", "Sample name variants (IDs remain the join authority):", ""]
        for role, examples in sorted(name_mismatch_examples.items()):
            for example in examples[:5]:
                lines.append(f"- {role}, player_id `{example['player_id']}`: delivery `{example['delivery_name']}`; mapping name(s): {', '.join(f'`{n}`' for n in example['mapping_names'])}")
    lines += ["", "## Playing-eleven joins", "", f"Eligible playing-eleven rows: **{playing_stats['eligible_rows']:,}**; team references unmatched: **{playing_stats['team_unmatched']:,}**; comp-player rows unmatched: **{playing_stats['competition_player_unmatched']:,}**; comp-team-player rows unmatched: **{playing_stats['competition_team_player_unmatched']:,}**.", "",
        f"Player mappings assigned to multiple teams within one competition: **{multi_team_assignments:,}** distinct competition/player mappings.", "", "## Interpretation", ""]
    lines.extend(f"- {note}" for note in report["notes"])
    lines += ["", "This is a validation report. It does not alter source identifiers or silently drop records.", ""]
    (OUT / "ENTITY_JOIN_VALIDATION.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Validated player/team joins over %d eligible deliveries", eligible_balls)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
