"""Admin endpoint to run the test suite and report results.

Runs pytest as a subprocess (so it picks up its own conftest env and
never touches the live DB or LLM). Stores the last run in memory; lost
on app restart, which is fine for a dashboard.
"""
import re
import subprocess
import time
from datetime import datetime, timezone
from flask import jsonify
from flask_login import login_required

from app.admin import admin_bp, admin_required

PROJECT_ROOT = "/opt/Ecolyxis"
PYTEST_BIN = f"{PROJECT_ROOT}/venv/bin/pytest"
PYTEST_TIMEOUT = 120  # seconds — tests normally finish in ~5s

# Parses pytest's final summary line. Captures groups are optional because
# pytest only includes sections that actually occurred.
_SUMMARY_RE = re.compile(
    r"=+ "
    r"(?:(\d+) failed,? ?)?"
    r"(?:(\d+) passed,? ?)?"
    r"(?:(\d+) skipped,? ?)?"
    r"(?:(\d+) xfailed,? ?)?"
    r"(?:(\d+) xpassed,? ?)?"
    r"(?:(\d+) errors?,? ?)?"
    r"(?:(\d+) warnings? )?"
    r"in ([\d.]+)s"
)
_FAILED_RE = re.compile(r"^FAILED (\S+)(?: - (.+))?$", re.MULTILINE)

_last_run = None  # populated by run_tests; read by routes.index for the template


def get_last_run():
    """Return the most recent test-run dict, or None if no run has happened."""
    return _last_run


def _parse_pytest_output(stdout):
    """Extract a summary dict and a list of failures from pytest -v output."""
    summary = {
        "passed": 0, "failed": 0, "skipped": 0,
        "xfailed": 0, "xpassed": 0, "errors": 0,
        "duration": 0.0,
    }
    m = _SUMMARY_RE.search(stdout)
    if m:
        summary["failed"] = int(m.group(1) or 0)
        summary["passed"] = int(m.group(2) or 0)
        summary["skipped"] = int(m.group(3) or 0)
        summary["xfailed"] = int(m.group(4) or 0)
        summary["xpassed"] = int(m.group(5) or 0)
        summary["errors"] = int(m.group(6) or 0)
        summary["duration"] = float(m.group(8))
    failures = [
        {"test": fm.group(1), "message": (fm.group(2) or "").strip()}
        for fm in _FAILED_RE.finditer(stdout)
    ]
    return summary, failures


@admin_bp.route("/tests/run", methods=["POST"])
@login_required
@admin_required
def run_tests():
    """Run the pytest suite and return parsed results as JSON."""
    global _last_run
    started = time.time()
    try:
        proc = subprocess.run(
            [PYTEST_BIN, "tests/", "-v", "--tb=short", "--color=no"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT,
        )
        elapsed = time.time() - started
        summary, failures = _parse_pytest_output(proc.stdout)
        _last_run = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            "summary": summary,
            "failures": failures,
            "stdout_tail": proc.stdout[-8000:],
            "stderr_tail": proc.stderr[-2000:],
            "wall_time_s": round(elapsed, 2),
        }
    except subprocess.TimeoutExpired:
        _last_run = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_code": -1,
            "passed": False,
            "summary": {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "errors": 0, "duration": 0.0},
            "failures": [],
            "stdout_tail": "",
            "stderr_tail": f"Test run timed out after {PYTEST_TIMEOUT}s",
            "wall_time_s": PYTEST_TIMEOUT,
        }
    except FileNotFoundError:
        _last_run = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_code": -1,
            "passed": False,
            "summary": {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "errors": 0, "duration": 0.0},
            "failures": [],
            "stdout_tail": "",
            "stderr_tail": f"pytest not found at {PYTEST_BIN}. Install requirements-dev.txt.",
            "wall_time_s": round(time.time() - started, 2),
        }
    return jsonify(_last_run)
