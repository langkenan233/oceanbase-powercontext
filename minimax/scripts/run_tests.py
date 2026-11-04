#!/usr/bin/env python
"""Run the business project test suites and write a reproducible log.

Every log records the command, the exit code and the code version that was
under test, so that a reviewer can tie a result to a commit.

Usage:
    python minimax/scripts/run_tests.py target-before
    python minimax/scripts/run_tests.py target-after
    python minimax/scripts/run_tests.py regression
    python minimax/scripts/run_tests.py all
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "minimax" / "workload" / "app"
MODULE_ROOT = REPO_ROOT / "minimax"
LOG_DIR = REPO_ROOT / "minimax" / "logs"

TARGET_TESTS = "minimax/workload/app/tests/test_reader_range_clamp.py"
REGRESSION_TESTS = [
    "minimax/workload/app/tests/test_reader.py",
    "minimax/workload/app/tests/test_indexer.py",
]
MODULE_TESTS = ["minimax/tests"]

SUITES = {
    "target-before": ([TARGET_TESTS], APP_ROOT),
    "target-after": ([TARGET_TESTS], APP_ROOT),
    "regression": (REGRESSION_TESTS, APP_ROOT),
    "module": (MODULE_TESTS, MODULE_ROOT),
}


def code_version() -> str:
    """Return the commit and the dirty state of the working tree."""

    def run(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() or completed.stderr.strip()

    commit = run("rev-parse", "HEAD")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    status = run("status", "--porcelain", "--", "minimax/workload/app")
    state = "clean" if not status else "dirty"
    return f"commit={commit}\nbranch={branch}\nworkload_tree={state}"


def run_suite(label: str, targets: list[str], search_path: Path) -> int:
    """Run one test selection and write its log."""

    environment = {
        **dict(split_environment()),
        "PYTHONPATH": str(search_path),
    }
    command = [sys.executable, "-m", "pytest", *targets, "-v"]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{label}.log"
    header = [
        f"# suite: {label}",
        f"# timestamp: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        "# command: " + " ".join(command),
        f"# exit_code: {completed.returncode}",
        "# code_version:",
        code_version(),
        "",
    ]
    log_path.write_text("\n".join(header) + completed.stdout + completed.stderr, encoding="utf-8")
    print(f"{label}: exit_code={completed.returncode} log={log_path.relative_to(REPO_ROOT)}")
    return completed.returncode


def split_environment() -> list[tuple[str, str]]:
    """Return the current process environment as key/value pairs."""

    import os

    return list(os.environ.items())


def main(argv: list[str] | None = None) -> int:
    """Run the requested suites."""

    parser = argparse.ArgumentParser(description="Run the business project test suites.")
    parser.add_argument("suite", choices=[*SUITES, "all"])
    arguments = parser.parse_args(argv)

    if arguments.suite == "all":
        codes = [run_suite(label, targets, search_path) for label, (targets, search_path) in SUITES.items()]
        return max(codes)
    targets, search_path = SUITES[arguments.suite]
    return run_suite(arguments.suite, targets, search_path)


if __name__ == "__main__":
    raise SystemExit(main())
