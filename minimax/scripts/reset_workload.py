#!/usr/bin/env python
"""Restore the business project to the state before the change.

The verification sample needs a repeatable way to go back to the state in
which the target tests fail. The state before the change is kept as a snapshot
next to the project, and this script copies it back.

Usage:
    python minimax/scripts/reset_workload.py
    python minimax/scripts/reset_workload.py --after
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKLOAD = REPO_ROOT / "minimax" / "workload"
SNAPSHOTS = WORKLOAD / "snapshots"


def restore(state: str) -> list[str]:
    """Copy every snapshot file of the given state over the workload.

    A snapshot mirrors the workload layout, so ``snapshots/before/app/...``
    lands on ``workload/app/...`` and ``snapshots/before/specs/...`` lands on
    ``workload/specs/...``.
    """

    source = SNAPSHOTS / state
    if not source.is_dir():
        raise FileNotFoundError(f"snapshot directory not found: {source}")  # noqa: TRY003
    restored: list[str] = []
    for snapshot in sorted(source.rglob("*")):
        if not snapshot.is_file():
            continue
        relative = snapshot.relative_to(source)
        target = WORKLOAD / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot, target)
        restored.append(target.relative_to(REPO_ROOT).as_posix())
    return restored


def main(argv: list[str] | None = None) -> int:
    """Restore the requested state."""

    parser = argparse.ArgumentParser(description="Restore the business project to a snapshot.")
    parser.add_argument("--state", choices=["before", "after"], default="before")
    arguments = parser.parse_args(argv)

    restored = restore(arguments.state)
    for name in restored:
        print(f"restored {name}")
    print(f"state: {arguments.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
