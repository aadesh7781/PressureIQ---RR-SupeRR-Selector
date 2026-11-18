"""Run the full reproducible PressureIQ workflow from the project root."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = [
    "audit_data.py",
    "audit_competition_scope.py",
    "build_integrated_deliveries.py",
    "validate_scorecard_accounting.py",
    "validate_entity_joins.py",
    "build_match_states.py",
    "explore_pressure_candidates.py",
    "compare_first_innings_par.py",
    "score_pressure_performance.py",
    "analyze_candidate_phase_fit.py",
    "audit_batter_eligibility.py",
    "validate_ranking_stability.py",
    "analyze_rr_2026.py",
    "build_methodology_report.py",
    "build_dashboard.py",
]


def main() -> int:
    for number, filename in enumerate(STEPS, start=1):
        print(f"\n[{number}/{len(STEPS)}] {filename}", flush=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / filename)], cwd=ROOT)
        if result.returncode:
            print(f"Stopped at {filename} (exit code {result.returncode}).", file=sys.stderr)
            return result.returncode
    print("\nPressureIQ workflow complete. Reports are in reports/generated/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
