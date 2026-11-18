from __future__ import annotations

import csv
import gzip
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pressureiq.logging_config import configure_logging  # noqa: E402

STATE_PATH = ROOT / "data" / "processed" / "delivery_pressure_states.csv.gz"
INDEX_PATH = ROOT / "data" / "processed" / "pressure_candidate_states.csv.gz"
REPORTS = ROOT / "reports" / "generated"
B = 300
SEED = 20261112
BATTER_PRIOR = 60.0
BOWLER_PRIOR = 48.0
BATTER_MIN = 60
BOWLER_MIN = 48
BATTER_TOTAL_MIN = 250
MATCH_MIN = 5
CREDITED = {"bowled", "caught", "caught and bowled", "lbw", "stump", "stumped", "hit wicket"}


def read_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def yes(value):
    return (value or "").strip().lower() in {"true", "1", "yes"}


def as_int(value):
    try:
        return int(float(value or 0))
    except (ValueError, TypeError):
        return 0


def empty():
    # balls, runs, dismissed, conceded, legal balls, bowler credited wickets
    return [0, 0, 0, 0, 0, 0]


def add(a, b, mult=1):
    for i in range(6):
        a[i] += b[i] * mult


def get_population(groups):
    pop = defaultdict(lambda: [0, 0, 0, 0, 0])  # exposure, runs, outs, conceded, wickets
    for (role, _pid, fmt, phase, inning, state), vals in groups.items():
        if state != "normal":
            continue
        p = pop[(role, fmt, phase, inning)]
        if role == "batter":
            p[0] += vals[0]
            p[1] += vals[1]
            p[2] += vals[2]
        else:
            p[0] += vals[4]
            p[3] += vals[3]
            p[4] += vals[5]
    return pop


def rank_from_weights(by_match, all_matches, candidate_names, match_weights):
    groups = defaultdict(empty)
    match_sets = defaultdict(set)
    match_occurrences = defaultdict(Counter)
    for mid, mult in match_weights.items():
        if mult <= 0:
            continue
        for key, vals in by_match[mid].items():
            add(groups[key], vals, mult)
            match_sets[key].add(mid)
            match_occurrences[key][mid] += mult
    pop = get_population(groups)
    player_stats = {"batter": defaultdict(lambda: {"score_sum": 0.0, "survive_sum": 0.0,
        "pressure": 0, "baseline": 0, "matches": {}}),
        "bowler": defaultdict(lambda: {"score_sum": 0.0, "survive_sum": 0.0,
        "pressure": 0, "baseline": 0, "matches": {}})}
    contexts = {(k[0], k[1], k[2], k[3], k[4]) for k in groups}
    for role, pid, fmt, phase, inning in contexts:
        p = groups.get((role, pid, fmt, phase, inning, "pressure"))
        n = groups.get((role, pid, fmt, phase, inning, "normal"))
        if p is None or n is None:
            continue
        prior = pop.get((role, fmt, phase, inning))
        if not prior or prior[0] <= 0:
            continue
        stats = player_stats[role][pid]
        if role == "batter":
            if p[0] == 0 or n[0] == 0:
                continue
            prior_run, prior_out = prior[1] / prior[0], prior[2] / prior[0]
            p_run = (p[1] + prior_run * BATTER_PRIOR) / (p[0] + BATTER_PRIOR)
            n_run = (n[1] + prior_run * BATTER_PRIOR) / (n[0] + BATTER_PRIOR)
            p_out = (p[2] + prior_out * BATTER_PRIOR) / (p[0] + BATTER_PRIOR)
            n_out = (n[2] + prior_out * BATTER_PRIOR) / (n[0] + BATTER_PRIOR)
            exp, base = p[0], n[0]
            score_lift, survive_lift = (p_run - n_run) * 100, (n_out - p_out) * 100
        else:
            if p[4] == 0 or n[4] == 0:
                continue
            prior_econ = prior[3] * 6 / prior[0]
            prior_wicket = prior[4] / prior[0]
            p_econ = (p[3] + prior_econ * BOWLER_PRIOR / 6) * 6 / (p[4] + BOWLER_PRIOR)
            n_econ = (n[3] + prior_econ * BOWLER_PRIOR / 6) * 6 / (n[4] + BOWLER_PRIOR)
            p_w = (p[5] + prior_wicket * BOWLER_PRIOR) * 24 / (p[4] + BOWLER_PRIOR)
            n_w = (n[5] + prior_wicket * BOWLER_PRIOR) * 24 / (n[4] + BOWLER_PRIOR)
            exp, base = p[4], n[4]
            score_lift, survive_lift = n_econ - p_econ, p_w - n_w
        stats["score_sum"] += score_lift * exp
        stats["survive_sum"] += survive_lift * exp
        stats["pressure"] += exp
        stats["baseline"] += base
        for mid, count in match_occurrences[(role, pid, fmt, phase, inning, "pressure")].items():
            stats["matches"][mid] = count
    output = {"batters": [], "bowlers": [], "eligible_counts": {}}
    for role, out_role in (("batter", "batters"), ("bowler", "bowlers")):
        raw = []
        for pid, s in player_stats[role].items():
            exp = s["pressure"]
            total_matches = sum(s["matches"].values())
            min_exp = BATTER_MIN if role == "batter" else BOWLER_MIN
            if exp < min_exp or s["baseline"] < min_exp or total_matches < MATCH_MIN:
                continue
            if role == "batter" and exp + s["baseline"] < BATTER_TOTAL_MIN:
                continue
            raw.append({"player_id": pid, "player_name": candidate_names[role].get(pid, pid),
                "scoring_lift": s["score_sum"] / exp, "survival_lift": s["survive_sum"] / exp,
                "pressure_exposure": exp, "baseline_exposure": s["baseline"],
                "pressure_matches": total_matches})
        s_values = [r["scoring_lift"] for r in raw]
        w_values = [r["survival_lift"] for r in raw]
        s_mean = sum(s_values) / len(s_values) if s_values else 0
        w_mean = sum(w_values) / len(w_values) if w_values else 0
        s_sd = pstdev(s_values) if len(s_values) > 1 else 0
        w_sd = pstdev(w_values) if len(w_values) > 1 else 0
        k = BATTER_PRIOR if role == "batter" else BOWLER_PRIOR
        ranked = []
        for r in raw:
            sz = max(-3, min(3, (r["scoring_lift"] - s_mean) / s_sd)) if s_sd else 0
            wz = max(-3, min(3, (r["survival_lift"] - w_mean) / w_sd)) if w_sd else 0
            reliability = r["pressure_exposure"] / (r["pressure_exposure"] + k) * r["pressure_matches"] / (r["pressure_matches"] + 4)
            r["score"] = (sz + wz) / 2 * reliability
            ranked.append(r)
        ranked.sort(key=lambda r: (-r["score"], -r["pressure_exposure"], r["player_name"].casefold()))
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        output[out_role] = ranked
        output["eligible_counts"][role] = len(ranked)
    return output


def main() -> int:
    configure_logging(ROOT / "logs")
    index = {(r["match_id"], r["innings"], r["innings_ball_num"]): r for r in read_gz(INDEX_PATH)}
    by_match = defaultdict(dict)
    match_year = {}
    candidate_names = {"batter": defaultdict(Counter), "bowler": defaultdict(Counter)}
    for d in read_gz(STATE_PATH):
        mid = d["match_id"]
        idx = index.get((mid, d["innings"], d["innings_ball_num"]))
        if idx is None:
            continue
        year = (d["match_match_date"] or "")[:4]
        if not year:
            continue
        match_year[mid] = year
        high = float(idx["score_weight_50"]) >= 0.75
        state = "pressure" if high else "normal"
        fmt, phase, inning = idx["format"], idx["phase"], int(d["innings"])
        extra_type = (d["extras_type"] or "").strip().lower()
        if extra_type in {"", "none", "null"}:
            extra_type = "none"
        extra_total = as_int(d["extras1_runs"]) + as_int(d["extras_2_runs"])
        legal = (d["is_legal_ball"] or "").strip().lower()
        valid_flag = legal in {"true", "false"}
        if extra_type not in {"wide", "other", "penalty"} and not (extra_type == "none" and extra_total) and valid_flag:
            pid = str(d["striker_id"])
            key = ("batter", pid, fmt, phase, inning, state)
            arr = by_match[mid].setdefault(key, empty())
            arr[0] += 1
            arr[1] += as_int(d["runs"])
            arr[2] += int(yes(d["is_wicket"]) and d["dismissed_player_id"] == pid)
            candidate_names["batter"][pid][d["striker_name"]] += 1
        conceded = d["bowler_conceded_runs_provisional"]
        if conceded not in {"", None} and valid_flag:
            pid = str(d["bowler_id"])
            key = ("bowler", pid, fmt, phase, inning, state)
            arr = by_match[mid].setdefault(key, empty())
            arr[3] += as_int(conceded)
            arr[4] += int(legal == "true")
            arr[5] += int(yes(d["is_wicket"]) and (d["dismissal_type"] or "").strip().lower() in CREDITED)
            candidate_names["bowler"][pid][d["bowler_name"]] += 1
    names = {role: {pid: counts.most_common(1)[0][0] for pid, counts in role_names.items() if counts}
        for role, role_names in candidate_names.items()}
    mids = sorted(by_match)
    full = rank_from_weights(by_match, mids, names, {mid: 1 for mid in mids})
    top_reference = {role: {r["player_id"] for r in full[role][:10]} for role in ("batters", "bowlers")}
    years = sorted({match_year[mid] for mid in mids})
    loso = []
    for year in years:
        train_weights = {mid: 1 for mid in mids if match_year[mid] != year}
        result = rank_from_weights(by_match, mids, names, train_weights)
        row = {"held_out_calendar_year": year, "training_matches": len(train_weights), "held_out_matches": sum(match_year[m] == year for m in mids)}
        for role in ("batters", "bowlers"):
            ids = {r["player_id"] for r in result[role][:10]}
            overlap = len(ids & top_reference[role])
            row[f"{role}_top10_overlap_with_full_period"] = overlap
            row[f"{role}_top10_ids"] = [r["player_id"] for r in result[role][:10]]
        loso.append(row)
    rng = random.Random(SEED)
    boot_counts = {role: defaultdict(list) for role in ("batters", "bowlers")}
    boot_top10_sizes = Counter()
    for _ in range(B):
        counts = Counter(rng.choices(mids, k=len(mids)))
        sample = rank_from_weights(by_match, mids, names, counts)
        for role in ("batters", "bowlers"):
            top = sample[role][:10]
            boot_top10_sizes[role] += len(top)
            for r in top:
                boot_counts[role][r["player_id"]].append(r["rank"])
    bootstrap = {}
    for role in ("batters", "bowlers"):
        out = []
        all_ids = set(top_reference[role]) | set(boot_counts[role])
        full_names = {r["player_id"]: r["player_name"] for r in full[role]}
        for pid in all_ids:
            ranks = boot_counts[role].get(pid, [])
            full_rank = next((r["rank"] for r in full[role] if r["player_id"] == pid), None)
            top_rate = len(ranks) / B
            if top_rate < 0.05 and full_rank is None:
                continue
            sorted_ranks = sorted(ranks)
            def pct(p):
                if not sorted_ranks:
                    return None
                pos = round((len(sorted_ranks) - 1) * p)
                return sorted_ranks[pos]
            out.append({"player_id": pid, "player_name": full_names.get(pid, names["batter" if role == "batters" else "bowler"].get(pid, pid)),
                "full_period_rank": full_rank, "bootstrap_top10_probability": round(top_rate, 3),
                "median_rank_when_top10": pct(.5), "rank_05_when_top10": pct(.05), "rank_95_when_top10": pct(.95),
                "bootstrap_top10_count": len(ranks)})
        out.sort(key=lambda r: (-r["bootstrap_top10_probability"], r["full_period_rank"] or 999, r["player_name"].casefold()))
        bootstrap[role] = out
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "criteria": {"pressure": "equal demand/resource current-rate index at or above p75",
            "batter_minimum": "60 pressure balls, 60 baseline balls, 250 total valid balls, and 5 pressure matches",
            "bowler_minimum": "48 pressure and 48 baseline legal balls, and 5 pressure matches",
            "scoring": "equal-weight z scores for own-baseline scoring lift and survival/wicket lift; reliability shrinkage as documented in scoring report"},
        "match_count": len(mids), "calendar_years": years,
        "full_period_rankings": {role: full[role][:10] for role in ("batters", "bowlers")},
        "bootstrap": {"method": "match-cluster sampling with replacement; full eligible match count sampled per replicate",
            "replicates": B, "seed": SEED, "top10_inclusion_summary": bootstrap},
        "leave_one_calendar_year_out": loso,
        "limitations": [
            "Bootstrap intervals are rank/inclusion stability diagnostics under the current scoring rule, not confidence that a player is intrinsically better.",
            "Leave-one-year-out reports how much each season influences rankings; it is not a predictive holdout because the same historical outcome metric defines both lists.",
            "Population smoothing priors are recomputed inside each bootstrap and each held-out training sample.",
            "Run totals, extras attribution and player availability remain subject to the project accounting and roster limitations.",
        ]}
    (REPORTS / "RANKING_STABILITY.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Rank-level uncertainty and season stability", "",
        f"Generated: {report['generated_at_utc']}", "",
        f"The rank uncertainty check resamples matches as clusters, with {B} replicates and {len(mids)} matches per replicate. The batter sample screen is 250 total valid balls plus the pressure/baseline and match minimums. The bowler minimum is 48 legal balls in both pressure and baseline states plus five pressure matches.", "",
        "## Bootstrap inclusion stability", "",
        "Top-10 probability is the share of match-cluster bootstrap samples in which the player appears in the top ten. Rank percentiles are conditional on appearing in the top ten; they are not a full rank interval.", ""]
    for role, label in (("batters", "Batters"), ("bowlers", "Bowlers")):
        lines += [f"### {label}", "", "| Player | Full-period rank | Bootstrap top-10 probability | Median rank when selected | 5th–95th percentile when selected |", "|---|---:|---:|---:|---:|"]
        for r in bootstrap[role][:20]:
            pct_range = f"{r['rank_05_when_top10']}–{r['rank_95_when_top10']}" if r["rank_05_when_top10"] is not None else "n/a"
            lines.append(f"| {r['player_name']} | {r['full_period_rank'] or '—'} | {r['bootstrap_top10_probability']:.1%} | {r['median_rank_when_top10'] or '—'} | {pct_range} |")
        lines.append("")
    lines += ["## Leave-one-calendar-year-out top-ten overlap", "",
        "Each year is removed in turn and rankings are recomputed from the remaining years. Overlap is against the full-period top ten. This is an influence/stability check, not predictive validation.", "",
        "| Held-out year | Training matches | Held-out matches | Batter overlap | Bowler overlap |", "|---:|---:|---:|---:|---:|"]
    for r in loso:
        lines.append(f"| {r['held_out_calendar_year']} | {r['training_matches']} | {r['held_out_matches']} | {r['batters_top10_overlap_with_full_period']}/10 | {r['bowlers_top10_overlap_with_full_period']}/10 |")
    lines += ["", "## Limitations", ""]
    lines.extend(f"- {x}" for x in report["limitations"])
    lines += ["", "No submission list is finalized by this diagnostic. See the separate data-accounting report for unresolved extras and the current methodology report for eligibility limits.", ""]
    (REPORTS / "RANKING_STABILITY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {REPORTS / 'RANKING_STABILITY.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
