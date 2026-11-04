#!/usr/bin/env python
"""Show that a specification change reaches the next context build.

The script builds the context with the specification at revision 1, replaces the
specification with revision 2, imports it again, builds the context a second time
and writes the two results next to each other. The second context must carry
revision 2 and the text that only revision 2 contains.

The demonstration starts from the state before the code change, so that both
contexts are built from the same baseline as the context that was handed to
MiniMax. The patch is applied again at the end, which leaves the business
project in the delivered state.

Usage:
    python minimax/scripts/verify_spec_change.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "minimax"
LOG_DIR = MODULE_ROOT / "logs"
LOG_PATH = LOG_DIR / "spec_change.log"
SPEC = MODULE_ROOT / "workload" / "specs" / "SPEC-001-read-range-clamp.md"
SPEC_V2 = MODULE_ROOT / "workload" / "snapshots" / "spec-v2" / "specs" / "SPEC-001-read-range-clamp.md"
SPEC_V1 = MODULE_ROOT / "workload" / "snapshots" / "before" / "specs" / "SPEC-001-read-range-clamp.md"

KEYWORDS = ["read_range", "source_range_clamped", "clamp", "limitations"]
MARKER = "报告形式"


def environment() -> dict[str, str]:
    """Return the process environment with the module on the search path."""

    return {**os.environ, "PYTHONPATH": str(MODULE_ROOT)}


def run_module(*arguments: str) -> tuple[int, str]:
    """Run one module command and return its exit code and output."""

    completed = subprocess.run(
        [sys.executable, "-m", "sddctx", *arguments],
        cwd=REPO_ROOT,
        env=environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def run_script(relative: str, *arguments: str) -> tuple[int, str]:
    """Run one script of the module and return its exit code and output."""

    completed = subprocess.run(
        [sys.executable, str(MODULE_ROOT / relative), *arguments],
        cwd=REPO_ROOT,
        env=environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def build_context() -> dict[str, object]:
    """Build the development context and return the parsed result."""

    code, output = run_module(
        "context",
        "--env-file",
        "minimax/sddctx.env",
        "--task-id",
        "sdd-task-001",
        "--objective",
        "Implement SPEC-001: report a clamped line range when the requested end exceeds the content length",
        *[argument for keyword in KEYWORDS for argument in ("--keyword", keyword)],
        "--limit",
        "5",
    )
    if code != 0:
        raise RuntimeError(f"context build failed with {code}: {output}")  # noqa: TRY003
    return json.loads(output)


def main() -> int:
    """Run the specification change demonstration."""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# specification change check",
        f"# timestamp: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        "",
    ]

    shutil_copy(SPEC_V1, SPEC)
    code, output = run_script("scripts/reset_workload.py", "--state", "before")
    if code != 0:
        print(output, file=sys.stderr)
        return 1
    code, output = run_module("init", "--reset", "--env-file", "minimax/sddctx.env")
    if code != 0:
        print(output, file=sys.stderr)
        return 1
    first = build_context()
    first_spec = first["specifications"][0]
    lines.extend(
        [
            "## before the specification change",
            f"revision: {first_spec['revision']}",
            f"contains_marker: {MARKER in first_spec['body']}",
            f"digest: {first['digest']}",
            "",
        ]
    )

    shutil_copy(SPEC_V2, SPEC)
    code, output = run_module("init", "--reset", "--env-file", "minimax/sddctx.env")
    if code != 0:
        print(output, file=sys.stderr)
        return 1
    second = build_context()
    second_spec = second["specifications"][0]
    lines.extend(
        [
            "## after the specification change",
            f"revision: {second_spec['revision']}",
            f"contains_marker: {MARKER in second_spec['body']}",
            f"digest: {second['digest']}",
            "",
        ]
    )

    shutil_copy(SPEC_V1, SPEC)
    code, output = run_module("apply", "--patch", "minimax/logs/minimax_patch.diff")
    if code != 0:
        print(output, file=sys.stderr)
        return 1
    code, output = run_module("init", "--reset", "--env-file", "minimax/sddctx.env")
    if code != 0:
        print(output, file=sys.stderr)
        return 1

    passed = (
        first_spec["revision"] == 1
        and second_spec["revision"] == 2
        and MARKER not in first_spec["body"]
        and MARKER in second_spec["body"]
        and first["digest"] != second["digest"]
    )
    lines.append(f"# result: {'passed' if passed else 'failed'}")
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"log: {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0 if passed else 1


def shutil_copy(source: Path, target: Path) -> None:
    """Copy one file over another."""

    target.write_bytes(source.read_bytes())


if __name__ == "__main__":
    raise SystemExit(main())
