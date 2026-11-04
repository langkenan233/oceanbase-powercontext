#!/usr/bin/env python
"""Run the whole verification of the code change and write one log.

The sequence is the one the verification sample describes:

1. restore the business project to the state before the change,
2. run the target tests and record that they fail,
3. apply the patch that MiniMax produced,
4. run the target tests again and record that they pass,
5. run the regression tests.

Every step records its command, its exit code and the code version.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / "minimax" / "logs"
LOG_PATH = LOG_DIR / "verification.log"

RUN_TESTS = REPO_ROOT / "minimax" / "scripts" / "run_tests.py"
RESET = REPO_ROOT / "minimax" / "scripts" / "reset_workload.py"
PATCH = REPO_ROOT / "minimax" / "logs" / "minimax_patch.diff"


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

    status = run("status", "--porcelain", "--", "minimax/workload/app")
    return (
        f"commit={run('rev-parse', 'HEAD')}\n"
        f"branch={run('rev-parse', '--abbrev-ref', 'HEAD')}\n"
        f"workload_tree={'clean' if not status else 'dirty'}"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the verification sequence."""

    parser = argparse.ArgumentParser(description="Verify the code change end to end.")
    parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sections: list[str] = [
        "# verification of the SPEC-001 code change",
        f"# timestamp: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        "# code_version:",
        code_version(),
        "",
    ]
    failures: list[str] = []

    steps: list[tuple[str, list[str], bool]] = [
        ("restore the state before the change", [sys.executable, str(RESET), "--state", "before"], True),
        ("target tests before the change", [sys.executable, str(RUN_TESTS), "target-before"], False),
        ("apply the patch produced by MiniMax", [sys.executable, "-m", "sddctx", "apply", "--patch", str(PATCH)], True),
        ("target tests after the change", [sys.executable, str(RUN_TESTS), "target-after"], False),
        ("regression tests after the change", [sys.executable, str(RUN_TESTS), "regression"], False),
    ]

    for title, command, expect_success in steps:
        environment = {"PYTHONPATH": str(REPO_ROOT / "minimax")}
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env={**dict(__import__("os").environ), **environment},
            capture_output=True,
            text=True,
            check=False,
        )
        sections.extend(
            [
                f"## {title}",
                f"# command: {' '.join(command)}",
                f"# exit_code: {completed.returncode}",
                "",
                completed.stdout.strip(),
                completed.stderr.strip(),
                "",
            ]
        )
        if expect_success and completed.returncode != 0:
            failures.append(title)

    sections.append(f"# failed_steps: {failures if failures else 'none'}")
    LOG_PATH.write_text("\n".join(sections) + "\n", encoding="utf-8")
    print(f"verification log: {LOG_PATH.relative_to(REPO_ROOT)}")
    print(f"failed steps: {failures if failures else 'none'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
