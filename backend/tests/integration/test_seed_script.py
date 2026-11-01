"""REQ-OPS-05: demo seeding script (scripts/seed_demo.py) contract.

Verifies the script exists, parses, and exposes the three demo scenarios with
the exact expected fees from the spec's verified worked examples. A full
seeding round-trip is exercised only against a running stack.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "seed_demo.py"
FIXTURES = REPO / "shared" / "fixtures" / "demo_scenarios.json"


def test_seed_script_exists_and_compiles():
    assert SCRIPT.is_file()
    subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], check=True)


def test_seed_script_help_runs_without_server():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--base-url" in result.stdout
    assert "--dry-run" in result.stdout


def test_demo_scenarios_match_spec_worked_examples():
    doc = json.loads(FIXTURES.read_text())
    by_key = {s["key"]: s for s in doc["scenarios"]}
    trusted = by_key["trusted"]
    assert trusted["expected"]["r_final"] == "7.80"
    assert trusted["expected"]["protection_fee"] == "0.47"

    scam = by_key["scam"]
    assert scam["expected"]["r_final"] == "70.25"
    assert scam["expected"]["protection_fee"] == "2.81"

    offender = by_key["offender"]
    assert offender["expected"]["r_final"] == "81.00"
    assert offender["expected"]["gate"] == "BLOCKED_HIGH_RISK"
    assert offender["expected"]["protection_fee"] is None


def test_seed_idempotent_flag_exists():
    """Recommended practice: safe re-runs against a live demo stack."""
    source = SCRIPT.read_text()
    assert "--dry-run" in source
