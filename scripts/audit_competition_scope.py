from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "reports" / "generated"
START_DATE = "2022-01-01"

def rows(name: str) -> list[dict[str, str]]:
    with (RAW / name).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def main() -> int:
    comps = rows("competitions.csv")
    matches = rows("matches.csv")
    balls = rows("ball_by_ball.csv")
    comp_lookup = {c["comp_id"]: c for c in comps}
    eligible_comp = {}
    excluded_comp = {}
    for c in comps:
        name = c["comp_name"].strip()
        fmt = c["match_format"].strip().upper()
        is_ipl = name.upper().startswith("IPL ")
        if fmt == "T20I":
            eligible_comp[c["comp_id"]] = "T20I"
        elif is_ipl:
            eligible_comp[c["comp_id"]] = "IPL"
        else:
            excluded_comp[c["comp_id"]] = c
    eligible_matches = {}
    excluded_date = []
    for m in matches:
        if m["comp_id"] not in eligible_comp:
            continue
        if m["match_date"] < START_DATE:
            excluded_date.append(m["match_id"])
            continue
        eligible_matches[m["match_id"]] = (m["comp_id"], eligible_comp[m["comp_id"]])
    match_counts = Counter(kind for _, kind in eligible_matches.values())
    ball_counts = Counter()
    for b in balls:
        joined = eligible_matches.get(b["match_id"])
        if joined:
            _, kind = joined
            ball_counts[kind] += 1
    by_comp_matches = Counter(comp_id for comp_id, _ in eligible_matches.values())
    by_comp_balls = Counter(eligible_matches[b["match_id"]][0] for b in balls if b["match_id"] in eligible_matches)
    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection_rule": "T20I format or competition name beginning 'IPL '; match_date on/after 2022-01-01",
        "eligible_competition_count": len(eligible_comp),
        "eligible_match_rows": len(eligible_matches),
        "eligible_matches_without_ball_data": len(set(eligible_matches) - {b["match_id"] for b in balls}),
        "eligible_ball_rows": sum(ball_counts.values()),
        "match_counts_by_kind": dict(match_counts),
        "ball_rows_by_kind": dict(ball_counts),
        "eligible_competitions": [
            {"comp_id": comp_id, "name": comp_lookup[comp_id]["comp_name"], "format": comp_lookup[comp_id]["match_format"],
             "season": comp_lookup[comp_id]["season"], "matches": by_comp_matches[comp_id], "ball_rows": by_comp_balls[comp_id]}
            for comp_id in sorted(eligible_comp, key=lambda cid: (eligible_comp[cid], comp_lookup[cid]["season"], comp_lookup[cid]["comp_name"]))
        ],
        "excluded_non_ipl_non_t20i_competitions": [
            {"comp_id": c["comp_id"], "name": c["comp_name"], "format": c["match_format"], "season": c["season"]}
            for c in excluded_comp.values()
        ],
        "note": "This is a competition-scope audit, not a player eligibility list or final ranking pool. Validate supplied player dropdown availability before submission.",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "competition_scope.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    lines = ["# Competition scope audit", "", f"Generated: {out['generated_at_utc']}", "",
        "## Rule applied", "", out["selection_rule"], "", "## Eligible supplied data", "",
        f"- Competitions: **{out['eligible_competition_count']}**",
        f"- Match rows: **{out['eligible_match_rows']:,}**",
        f"- Matches without ball-by-ball rows: **{out['eligible_matches_without_ball_data']:,}**",
        f"- Ball-by-ball rows: **{out['eligible_ball_rows']:,}**", "",
        "| Competition type | Match rows | Ball-by-ball rows |", "|---|---:|---:|"]
    for kind in sorted(set(match_counts) | set(ball_counts)):
        lines.append(f"| {kind} | {match_counts[kind]:,} | {ball_counts[kind]:,} |")
    lines += ["", "## IPL seasons in the supplied data", "", "| Season | Competition | Matches | Ball rows |", "|---|---|---:|---:|"]
    for c in out["eligible_competitions"]:
        if c["name"].upper().startswith("IPL "):
            lines.append(f"| {c['season']} | {c['name']} | {c['matches']:,} | {c['ball_rows']:,} |")
    lines += ["", "## Excluded competitions", "", "The official brief specifies IPL and T20I. Other competitions whose format is T20 are excluded from the primary rankings.", ""]
    for c in out["excluded_non_ipl_non_t20i_competitions"]:
        lines.append(f"- {c['name']} ({c['season']}; format `{c['format']}`; comp_id `{c['comp_id']}`)")
    lines += ["", "## Player-pool and submission notes", "", out["note"],
        "The brief requires two independent ranked lists: 10 batters and 10 bowlers, ranks 1 through 10 in descending rating order. It prohibits duplicates within each list. It does not prohibit a player from appearing once in each list.", ""]
    (OUT / "COMPETITION_SCOPE.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT / 'COMPETITION_SCOPE.md'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
