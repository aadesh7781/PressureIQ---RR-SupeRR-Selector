from __future__ import annotations

import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

LOG = logging.getLogger("pressureiq.audit")
FILES = ["ball_by_ball.csv", "matches.csv", "playing_eleven.csv", "competitions.csv", "teams.csv", "competition_team_player_mapping.csv"]

def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")

def inspect_csv(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        n, missing, distinct = 0, Counter(), {c: set() for c in columns}
        samples, row_hashes, duplicates = {c: [] for c in columns}, set(), 0
        for row in reader:
            n += 1
            for c in columns:
                value = (row.get(c) or "").strip()
                if not value:
                    missing[c] += 1
                elif len(distinct[c]) < 50000:
                    distinct[c].add(value)
                if value and len(samples[c]) < 5 and value not in samples[c]:
                    samples[c].append(value)
            fingerprint = tuple((row.get(c) or "").strip() for c in columns)
            if fingerprint in row_hashes:
                duplicates += 1
            row_hashes.add(fingerprint)
    return {"file": path.name, "rows": n, "columns": columns,
        "missing": {c: {"count": missing[c], "pct": round(100 * missing[c] / n, 3) if n else 0} for c in columns},
        "distinct_up_to_50000": {c: len(distinct[c]) for c in columns}, "samples": samples,
        "full_row_duplicates": duplicates}

def read_values(path: Path, column: str) -> set[str]:
    values = set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            value = (row.get(column) or "").strip()
            if value:
                values.add(value)
    return values

def main() -> int:
    configure_logging(ROOT / "logs")
    raw = ROOT / "data" / "raw"
    missing_files = [name for name in FILES if not (raw / name).exists()]
    if missing_files:
        LOG.error("Missing expected files in %s: %s", raw, ", ".join(missing_files))
        return 2
    audits = [inspect_csv(raw / name) for name in FILES]
    by_name = {a["file"]: a for a in audits}
    available = {a["file"]: set(a["columns"]) for a in audits}
    join_candidates = {a["file"]: [c for c in a["columns"] if norm(c).endswith("_id") or norm(c) == "id"] for a in audits}
    match_cols = {norm(c): c for c in by_name["matches.csv"]["columns"]}
    patterns = {
        "venue/stadium name": r"^(venue|stadium|ground|venue_name|stadium_name)$",
        "venue identifier": r"venue_id|stadium_id|ground_id", "venue country": r"venue_country",
        "city": r"city|location", "date": r"match_date|date|start_time|datetime",
        "format": r"format|match_type|gender", "competition id/name": r"comp_id|competition|tournament|series",
        "toss winner": r"toss.*win|toss.*team", "toss decision": r"toss.*opt|toss.*decision",
        "result": r"result|winner|win_by|outcome", "innings/team context": r"innings|first_innings|second_innings|team_1|team_2|home_team|away_team",
        "time of day": r"time|start_time|datetime",
    }
    match_context = {label: [col for key, col in match_cols.items() if re.search(pattern, key)] for label, pattern in patterns.items()}
    ball_cols = {norm(c): c for c in by_name["ball_by_ball.csv"]["columns"]}
    ball_context = {
        "venue_related": [col for key, col in ball_cols.items() if re.search(r"venue|stadium|ground|city", key)],
        "timestamp_related": [col for key, col in ball_cols.items() if re.search(r"time|date|timestamp", key)],
    }
    candidates = [("ball_by_ball.csv", "matches.csv", "match_id"), ("playing_eleven.csv", "matches.csv", "match_id"),
        ("matches.csv", "competitions.csv", "comp_id"), ("teams.csv", "competitions.csv", "comp_id"),
        ("competition_team_player_mapping.csv", "competitions.csv", "comp_id"),
        ("competition_team_player_mapping.csv", "teams.csv", "team_id")]
    joins = []
    for left, right, key in candidates:
        if key not in available[left] or key not in available[right]:
            joins.append({"left": left, "right": right, "key": key, "status": "key column absent"})
            continue
        lv, rv = read_values(raw / left, key), read_values(raw / right, key)
        joins.append({"left": left, "right": right, "key": key, "left_distinct": len(lv), "right_distinct": len(rv),
            "left_unmatched": len(lv - rv), "right_unmatched": len(rv - lv),
            "left_coverage_pct": round(100 * len(lv & rv) / len(lv), 3) if lv else None})
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": audits,
        "join_key_candidates": join_candidates, "candidate_join_checks": joins, "matches_context_fields": match_context,
        "ball_by_ball_venue_and_timestamp_fields": ball_context,
        "venue_name_present_in_matches": bool(match_context["venue/stadium name"]),
        "venue_id_present_in_matches": bool(match_context["venue identifier"]),
        "venue_field_present_in_ball_by_ball": bool(ball_context["venue_related"])}
    out_dir = ROOT / "reports" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Dataset audit", "", f"Generated: {report['generated_at_utc']}", "", "## File inventory", "",
        "| File | Rows | Columns | Full-row duplicates |", "|---|---:|---:|---:|"]
    for a in audits:
        lines.append(f"| `{a['file']}` | {a['rows']:,} | {len(a['columns'])} | {a['full_row_duplicates']:,} |")
    lines += ["", "## Match context and venue availability", "",
        f"`matches.csv` venue/stadium name field present: **{report['venue_name_present_in_matches']}**",
        f"`matches.csv` venue identifier present: **{report['venue_id_present_in_matches']}**",
        f"`ball_by_ball.csv` venue field present: **{report['venue_field_present_in_ball_by_ball']}**", "", "Fields detected in `matches.csv`:"]
    for label, cols in match_context.items():
        lines.append(f"- **{label}:** {', '.join(f'`{c}`' for c in cols) if cols else 'not found'}")
    lines += ["", "Fields detected in `ball_by_ball.csv`:",
        f"- **Venue-related:** {', '.join(f'`{c}`' for c in ball_context['venue_related']) if ball_context['venue_related'] else 'not found'}",
        f"- **Date/time-related:** {', '.join(f'`{c}`' for c in ball_context['timestamp_related']) if ball_context['timestamp_related'] else 'not found'}",
        "", "No stadium-name or city lookup file is present among the six CSVs. `venue_id` cannot be translated to Jaipur/Sawai Mansingh Stadium from these supplied files alone. `match_date` is available, but there is no match start-time field. `delivery_ist_time` is present in the ball-by-ball file; it is a delivery timestamp, not a dew measurement.",
        "", "## Candidate join coverage", "", "| Left | Right | Key | Left distinct | Right distinct | Left unmatched | Right unmatched | Left coverage |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for j in joins:
        cov = f"{j['left_coverage_pct']}%" if j.get("left_coverage_pct") is not None else "—"
        lines.append(f"| {j['left']} | {j['right']} | `{j['key']}` | {j.get('left_distinct','—')} | {j.get('right_distinct','—')} | {j.get('left_unmatched','—')} | {j.get('right_unmatched','—')} | {cov} |")
    lines += ["", "## Schema and data-quality notes", "", "See `data_audit.json` for every column, missing-value count/rate, distinct-value count and example values. Distinct counts are capped at 50,000 per field to keep this audit lightweight. Join results are candidate key coverage diagnostics; they do not prove semantic correctness or uniqueness.", ""]
    (out_dir / "DATA_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Audited %d input files; report written to reports/generated/DATA_AUDIT.md", len(audits))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
