# PressureIQ

PressureIQ is a cricket analytics project for studying player performance in high-pressure T20 match states. It builds separate batter and bowler rankings, publishes the component metrics and sample sizes, and produces a separate Rajasthan Royals 2026 performance review.

## Project scope

- Primary ranking scope: IPL and T20 Internationals (T20I), from 2022 onward.
- The supplied IPL files label the match format as `T20`; the pipeline identifies IPL using the competition name.
- The player pool is not limited to Rajasthan Royals. The RR 2026 team diagnosis is a separate scouting section and does not change the main pressure scores.
- The batter workflow applies a 250-valid-batting-ball minimum. Batter and bowler rankings are separate lists.

## Data access

The competition data pack is intentionally **not included** in this GitHub package. Obtain and use it only under the competition's data-use terms. Place the six authorized CSV files in `data/raw/` with these exact names:

```text
ball_by_ball.csv
matches.csv
playing_eleven.csv
competitions.csv
teams.csv
competition_team_player_mapping.csv
```

Do not commit the data pack, processed delivery files, player-level outputs, generated dashboards or logs. The `.gitignore` excludes these by default. Confirm you have permission before publishing any derived output from competition data.

## Setup

Use Python 3.10 or newer. From the project folder, create an environment and install the listed dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On macOS or Linux, activate the environment with `source .venv/bin/activate`.

## Run the complete workflow

After adding the authorized CSV files under `data/raw/`, run:

```powershell
python scripts/run_pipeline.py
```

The pipeline runs all stages in dependency order and stops if a stage fails. It writes intermediate and processed tables under `data/`, generated reports and the HTML dashboard under `reports/generated/`, and the run log under `logs/pressureiq.log`. Re-run the same command to regenerate outputs from the raw files.

To run one stage, use `python scripts/<script_name>.py` from the project folder. The scripts are:

| Stage | Script |
|---|---|
| Profile input schemas and key coverage | `audit_data.py` |
| Select IPL/T20I scope | `audit_competition_scope.py` |
| Integrate match and delivery data | `build_integrated_deliveries.py` |
| Check score accounting | `validate_scorecard_accounting.py` |
| Validate entity joins | `validate_entity_joins.py` |
| Build pre-delivery match states | `build_match_states.py` |
| Compare pressure-state definitions | `explore_pressure_candidates.py` |
| Compare first-innings par baselines | `compare_first_innings_par.py` |
| Score pressure performance | `score_pressure_performance.py` |
| Analyze candidate phase fit | `analyze_candidate_phase_fit.py` |
| Review batter eligibility and batting-order proxies | `audit_batter_eligibility.py` |
| Check ranking stability | `validate_ranking_stability.py` |
| Review Rajasthan Royals 2026 | `analyze_rr_2026.py` |
| Assemble methodology report | `build_methodology_report.py` |
| Build the dashboard | `build_dashboard.py` |

## How pressure is calculated

Pressure is assigned to a **delivery state before the ball is bowled**. The project groups comparable states by format (IPL or T20I), innings phase (powerplay, middle or death), and a 12-ball remaining band. It calculates two percentile components from that group:

- **Demand stress (D):** in a chase, the percentile rank of required run rate; in the first innings, the reverse percentile rank of current run rate, so a lower scoring rate means more stress.
- **Resource stress (R):** the percentile rank of wickets already lost, so losing more wickets means more stress.

The candidate pressure score is:

```text
Pressure score = w × D + (1 − w) × R
```

The current default uses equal weights (`w = 0.50`) and labels a state high pressure at or above the 75th percentile (`0.75`). The workflow checks alternative demand weights of 0.25, 0.50 and 0.75, and cutoffs of 0.70, 0.75 and 0.80. A cross-validated first-innings par/expected-score approach is also compared with current run rate. These are project analysis settings, not a competition-prescribed rule or a direct measure of psychological pressure. Ranking sensitivity reports show how much player lists change under alternatives.

## How player metrics work

For each role, the scoring workflow compares a player's pressure-state results with their own non-pressure results in the same format, innings type and phase. It calculates:

- **Batters:** runs-per-100-ball lift and dismissals avoided per 100 balls.
- **Bowlers:** runs saved per over and wicket lift per 24 legal balls.

The pipeline smooths small samples toward population baselines, standardizes the components across eligible players, combines them with equal component weights, and applies exposure and match-count reliability adjustments. It checks alternative pressure definitions, weights, sample rules and ranking stability. These choices remain project methodology settings, not official competition rules. The generated methodology report records the settings used in a run.

## Data and interpretation limits

This code is built around the supplied CSV schema. It validates joins and reports missing or unmatched records rather than silently treating unavailable fields as facts. The supplied match file contains venue IDs but no stadium/city lookup; delivery timestamps are not measured dew observations. Do not make stadium or dew claims without an independently verified source.

The current analysis has unresolved extra classifications and incomplete independent innings-scorecard reconciliation. Ranking sensitivity and season-held-out stability also vary. Treat generated player rankings as analytical recommendations with uncertainty, not proof that a player will win a close match or confirmation of selector eligibility, auction availability or squad fit. The competition selector is not accessed by this software.

The RR 2026 review describes phase differences in a small match sample. It does not establish causation. It stays separate from the IPL/T20I pressure scoring methodology.

## Repository layout

```text
config/project.yaml       Analysis scope and documented settings
data/raw/                 Authorized input files (not included)
data/interim/             Intermediate data (generated, ignored)
data/processed/           Processed data (generated, ignored)
logs/                     Runtime logs (ignored)
reports/generated/        Audits, reports and dashboard (ignored)
scripts/                  Reproducible workflow entry points
src/pressureiq/           Shared project utilities
requirements.txt          Python dependencies
```

## License and competition data

No open-source license is included.