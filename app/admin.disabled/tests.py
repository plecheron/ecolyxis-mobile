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
PYTEST_TIMEOUT = 120

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

_TEST_LINE_RE = re.compile(
    r"^(tests/\S+?)::(\S+) (PASSED|FAILED|SKIPPED|XFAILED|XPASS(?:ED)?)(?: .*)?$",
    re.MULTILINE,
)
_FAILED_RE = re.compile(r"^FAILED (\S+)(?: - (.+))?$", re.MULTILINE)

_last_run = None


def get_last_run():
    return _last_run


def _parse_pytest_output(stdout):
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

    suites = {}
    for tm in _TEST_LINE_RE.finditer(stdout):
        file_path = tm.group(1)
        test_name = tm.group(2)
        status = tm.group(3)
        if status.startswith("XPASS"):
            status = "xpassed"
        else:
            status = status.lower()

        parts = file_path.replace("tests/", "").split("/")
        suite = parts[0] if len(parts) > 1 else "root"

        if suite not in suites:
            suites[suite] = {"file": file_path, "tests": []}
        suites[suite]["tests"].append({"name": test_name, "status": status})

    return summary, failures, suites


@admin_bp.route("/tests/run", methods=["POST"])
@login_required
@admin_required
def run_tests():
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
        summary, failures, suites = _parse_pytest_output(proc.stdout)
        _last_run = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            "summary": summary,
            "failures": failures,
            "suites": suites,
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
            "suites": {},
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
            "suites": {},
            "stdout_tail": "",
            "stderr_tail": f"pytest not found at {PYTEST_BIN}. Install requirements-dev.txt.",
            "wall_time_s": round(time.time() - started, 2),
        }
    return jsonify(_last_run)
