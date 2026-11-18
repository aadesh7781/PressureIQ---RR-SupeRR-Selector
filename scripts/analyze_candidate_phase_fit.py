from __future__ import annotations

import csv
import gzip
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

REPORTS = ROOT / "reports" / "generated"
STATE_PATH = ROOT / "data" / "processed" / "delivery_pressure_states.csv.gz"
INDEX_PATH = ROOT / "data" / "processed" / "pressure_candidate_states.csv.gz"
CREDITED = {"bowled", "caught", "caught and bowled", "lbw", "stump", "stumped", "hit wicket"}
B = 500
SEED = 20261112


def read_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def as_int(value) -> int:
    try:
        return int(float(value or 0))
    except (ValueError, TypeError):
        return 0


def new_stats():
    return {"balls": 0, "runs": 0, "dismissals": 0, "legal": 0,
            "conceded": 0, "wickets": 0, "matches": set()}


def add_stats(target, source, multiplier=1):
    for field in ("balls", "runs", "dismissals", "legal", "conceded", "wickets"):
        target[field] += source[field] * multiplier
    if multiplier:
        target["matches"].update(source["matches"])


def get_metrics(role, player_id, phase, groups, population):
    weight_score = weight_survive = pressure_exp = baseline_exp = 0.0
    pressure_match_ids = set()
    phase_contexts = set()
    for key in set(k for k in groups if k[0] == role and k[1] == player_id and k[3] == phase):
        _, pid, fmt, ph, inning, state = key
        if state != "pressure":
            continue
        p = groups[key]
        n = groups.get((role, pid, fmt, ph, inning, "normal"))
        if not n:
            continue
        pop = population.get((role, fmt, ph, inning))
        if not pop or not pop["exposure"]:
            continue
        if role == "batter":
            if p["balls"] == 0 or n["balls"] == 0:
                continue
            prior_run = pop["runs"] / pop["exposure"]
            prior_out = pop["dismissals"] / pop["exposure"]
            p_run = (p["runs"] + prior_run * 60) / (p["balls"] + 60)
            n_run = (n["runs"] + prior_run * 60) / (n["balls"] + 60)
            p_out = (p["dismissals"] + prior_out * 60) / (p["balls"] + 60)
            n_out = (n["dismissals"] + prior_out * 60) / (n["balls"] + 60)
            exposure, baseline = p["balls"], n["balls"]
            score_lift, survive_lift = (p_run - n_run) * 100, (n_out - p_out) * 100
        else:
            if p["legal"] == 0 or n["legal"] == 0:
                continue
            prior_econ = pop["conceded"] * 6 / pop["exposure"]
            prior_wicket = pop["wickets"] / pop["exposure"]
            p_econ = (p["conceded"] + prior_econ * 8) * 6 / (p["legal"] + 48)
            n_econ = (n["conceded"] + prior_econ * 8) * 6 / (n["legal"] + 48)
            p_w = (p["wickets"] + prior_wicket * 48) * 24 / (p["legal"] + 48)
            n_w = (n["wickets"] + prior_wicket * 48) * 24 / (n["legal"] + 48)
            exposure, baseline = p["legal"], n["legal"]
            score_lift, survive_lift = n_econ - p_econ, p_w - n_w
        weight_score += score_lift * exposure
        weight_survive += survive_lift * exposure
        pressure_exp += exposure
        baseline_exp += baseline
        pressure_match_ids.update(p["matches"])
        phase_contexts.add((fmt, inning))
    if not pressure_exp:
        return None
    return {"scoring_lift": weight_score / pressure_exp,
            "survival_wicket_lift": weight_survive / pressure_exp,
            "pressure_balls": int(pressure_exp), "baseline_balls": int(baseline_exp),
            "pressure_matches": len(pressure_match_ids), "format_innings_contexts": len(phase_contexts)}


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    x = (len(values) - 1) * p
    lo, hi = int(x), min(int(x) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (x - lo)


def main() -> int:
    configure_logging(ROOT / "logs")
    scoring = json.loads((REPORTS / "player_scoring_draft.json").read_text(encoding="utf-8"))
    with (ROOT / "data" / "raw" / "competition_team_player_mapping.csv").open(encoding="utf-8-sig", newline="") as f:
        roster_rows = list(csv.DictReader(f))
    ipl26 = {str(r["player_id"]): r["team_name"] for r in roster_rows if r["comp_name"] == "IPL 2026"}
    primary = scoring["top10_by_candidate"]["current_rate|0.5|0.75"]
    candidates = {"batter": {str(r["player_id"]): r["player_name"] for r in primary["batters"]},
                  "bowler": {str(r["player_id"]): r["player_name"] for r in primary["bowlers"]}}
    index = {(r["match_id"], r["innings"], r["innings_ball_num"]): r for r in read_gz(INDEX_PATH)}
    # keys: role, player, format, phase, innings, pressure/normal; each match owns these groups
    by_match = defaultdict(lambda: defaultdict(new_stats))
    population = defaultdict(lambda: {"exposure": 0, "runs": 0, "dismissals": 0, "conceded": 0, "wickets": 0})
    phase_set = set()
    match_ids = set()
    for d in read_gz(STATE_PATH):
        match_id = d["match_id"]
        idx = index.get((match_id, d["innings"], d["innings_ball_num"]))
        if idx is None:
            continue
        phase = idx["phase"]
        phase_set.add(phase)
        pressure = float(idx["score_weight_50"]) >= 0.75
        state = "pressure" if pressure else "normal"
        fmt, inning = idx["format"], int(d["innings"])
        match_ids.add(match_id)
        ext_type = (d["extras_type"] or "").strip().lower()
        if ext_type in {"", "none", "null"}:
            ext_type = "none"
        extra_total = as_int(d["extras1_runs"]) + as_int(d["extras_2_runs"])
        legal_text = (d["is_legal_ball"] or "").strip().lower()
        legal_ok = legal_text in {"true", "false"}
        if ext_type not in {"wide", "other", "penalty"} and not (ext_type == "none" and extra_total) and legal_ok:
            pid = str(d["striker_id"])
            if state == "normal":
                pop = population[("batter", fmt, phase, inning)]
                pop["exposure"] += 1
                pop["runs"] += as_int(d["runs"])
                pop["dismissals"] += int((d["is_wicket"] or "").lower() == "true" and d["dismissed_player_id"] == pid)
            if pid in candidates["batter"]:
                key = ("batter", pid, fmt, phase, inning, state)
                s = by_match[match_id][key]
                s["balls"] += 1
                s["runs"] += as_int(d["runs"])
                if (d["is_wicket"] or "").lower() == "true" and d["dismissed_player_id"] == pid:
                    s["dismissals"] += 1
                s["matches"].add(match_id)
        conceded = d["bowler_conceded_runs_provisional"]
        if conceded not in {"", None} and legal_ok:
            pid = str(d["bowler_id"])
            dismissal = (d["dismissal_type"] or "").strip().lower()
            credited = int((d["is_wicket"] or "").lower() == "true" and dismissal in CREDITED)
            if state == "normal":
                pop = population[("bowler", fmt, phase, inning)]
                pop["exposure"] += int(legal_text == "true")
                pop["conceded"] += as_int(conceded)
                pop["wickets"] += credited
            if pid in candidates["bowler"]:
                key = ("bowler", pid, fmt, phase, inning, state)
                s = by_match[match_id][key]
                s["conceded"] += as_int(conceded)
                s["legal"] += int(legal_text == "true")
                s["wickets"] += credited
                s["matches"].add(match_id)
    full_groups = defaultdict(new_stats)
    for match_id, groups in by_match.items():
        for key, stats in groups.items():
            full_groups[key]["match_id"] = match_id
            add_stats(full_groups[key], stats)
    phases = sorted(phase_set, key=lambda x: {"powerplay": 0, "middle": 1, "death": 2}.get(x.casefold(), 9))
    full = {}
    for role in ("batter", "bowler"):
        for pid in candidates[role]:
            for phase in phases:
                metric = get_metrics(role, pid, phase, full_groups, population)
                if metric:
                    full[(role, pid, phase)] = metric

    rng = random.Random(SEED)
    match_list = sorted(match_ids)
    boots = defaultdict(lambda: {"scoring": [], "survival": []})
    for _ in range(B):
        selected = Counter(rng.choices(match_list, k=len(match_list)))
        groups = defaultdict(new_stats)
        for match_id, multiplicity in selected.items():
            for key, stats in by_match[match_id].items():
                dest = groups[key]
                dest["match_id"] = match_id
                add_stats(dest, stats, multiplicity)
        for (role, pid, phase) in full:
            metric = get_metrics(role, pid, phase, groups, population)
            if metric:
                boots[(role, pid, phase)]["scoring"].append(metric["scoring_lift"])
                boots[(role, pid, phase)]["survival"].append(metric["survival_wicket_lift"])

    rows = []
    for role in ("batter", "bowler"):
        for pid, name in candidates[role].items():
            for phase in phases:
                key = (role, pid, phase)
                metric = full.get(key)
                if not metric:
                    continue
                boot = boots[key]
                rows.append({"role": role, "player_id": pid, "player_name": name, "phase": phase,
                    **metric,
                    "scoring_lift_95ci": [round(percentile(boot["scoring"], .025), 3), round(percentile(boot["scoring"], .975), 3)] if boot["scoring"] else None,
                    "survival_wicket_lift_95ci": [round(percentile(boot["survival"], .025), 3), round(percentile(boot["survival"], .975), 3)] if boot["survival"] else None,
                    "bootstrap_valid_replicates": min(len(boot["scoring"]), len(boot["survival"]))})
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pressure_rule": "current-rate demand/resource index, equal blend, p75 threshold",
        "bootstrap": {"method": "match-cluster bootstrap with replacement", "replicates": B, "seed": SEED,
            "interval": "percentile 95% intervals for pressure-minus-baseline rate components; the population prior is fixed at the full-sample estimate"},
        "eligible_matches_resampled": len(match_list), "candidate_phase_rows": rows,
        "ipl_2026_team_mapping": {role: [{"player_id": pid, "player_name": name,
            "mapped_team": ipl26.get(pid), "present_in_ipl_2026_mapping": pid in ipl26}
            for pid, name in candidates[role].items()] for role in ("batter", "bowler")},
        "limitations": [
            "These are phase-specific component diagnostics for the current overall top tens, not a newly optimized phase-specific ranking.",
            "Intervals describe uncertainty in each pressure-minus-baseline component, not uncertainty in a player's final rank.",
            "A phase interval crossing zero means this sample does not clearly distinguish pressure improvement from no change under this method.",
            "Player role labels, current team membership, availability and competition-selector eligibility are absent from the supplied CSVs and must be verified separately.",
            "The pressure and run attribution inputs remain provisional because some extra types and innings totals are unresolved.",
        ]}
    (REPORTS / "candidate_phase_fit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Candidate player phase fit and uncertainty", "",
        f"Generated: {report['generated_at_utc']}", "",
        "This report checks whether overall pressure-ranking candidates show phase-specific pressure performance. It uses the current equal-weight p75 pressure rule and the same role-specific smoothed rates as the scoring draft.", "",
        f"Uncertainty ranges are 95% match-cluster bootstrap intervals from {B} resamples of {len(match_list)} matches. They are component intervals, not rank intervals; the population prior is held at its full-sample value.", "",
        "## Batters", "", "Positive scoring lift means higher runs per 100 balls in pressure states than in the player's non-pressure baseline. Positive survival lift means fewer dismissals per 100 balls.", "",
        "| Player | Phase | Pressure balls | Baseline balls | Pressure matches | Scoring lift/100 (95% CI) | Survival lift/100 (95% CI) |", "|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["role"] == "batter":
            lines.append(f"| {r['player_name']} | {r['phase']} | {r['pressure_balls']:,} | {r['baseline_balls']:,} | {r['pressure_matches']} | {r['scoring_lift']:.2f} [{r['scoring_lift_95ci'][0]:.2f}, {r['scoring_lift_95ci'][1]:.2f}] | {r['survival_wicket_lift']:.2f} [{r['survival_wicket_lift_95ci'][0]:.2f}, {r['survival_wicket_lift_95ci'][1]:.2f}] |")
    lines += ["", "## Bowlers", "", "Positive economy lift means fewer runs conceded per over in pressure states than in the player's non-pressure baseline. Positive wicket lift means more credited wickets per 24 legal balls.", "",
        "| Player | Phase | Pressure legal balls | Baseline legal balls | Pressure matches | Economy saved/over (95% CI) | Wicket lift/24 (95% CI) |", "|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["role"] == "bowler":
            lines.append(f"| {r['player_name']} | {r['phase']} | {r['pressure_balls']:,} | {r['baseline_balls']:,} | {r['pressure_matches']} | {r['scoring_lift']:.2f} [{r['scoring_lift_95ci'][0]:.2f}, {r['scoring_lift_95ci'][1]:.2f}] | {r['survival_wicket_lift']:.2f} [{r['survival_wicket_lift_95ci'][0]:.2f}, {r['survival_wicket_lift_95ci'][1]:.2f}] |")
    lines += ["", "## IPL 2026 roster mapping cross-check", "",
        "This checks whether a candidate ID appears in the supplied IPL 2026 team/player mapping. Presence is not proof of registration or official selector eligibility; absence means only that this CSV has no IPL 2026 team mapping for the ID.", "",
        "| Candidate | Category | Supplied IPL 2026 team mapping |", "|---|---|---|"]
    for role in ("batter", "bowler"):
        for r in report["ipl_2026_team_mapping"][role]:
            lines.append(f"| {r['player_name']} | {role} | {r['mapped_team'] or 'No IPL 2026 mapping found'} |")
    lines += ["", "## Interpretation and eligibility limits", ""]
    lines.extend(f"- {note}" for note in report["limitations"])
    lines += ["", "Phase-specific stats should be read with both exposure columns and confidence intervals. Overall rank alone is not evidence that a player fills a particular phase role.", ""]
    (REPORTS / "CANDIDATE_PHASE_FIT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {REPORTS / 'CANDIDATE_PHASE_FIT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
