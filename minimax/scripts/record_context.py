#!/usr/bin/env python
"""Record the development context and the prompt that were handed to MiniMax.

The script assembles the context from seekdb with the same task, keywords and
limit as the call that produced the patch, and writes the context and the
rendered prompt to the log directory. It does not call the model, so the
artefacts can be rebuilt and checked without a MiniMax key.

The context of this task was assembled while the business project was in the
state before the change. Rebuild it the same way:

    python minimax/scripts/reset_workload.py --state before
    PYTHONPATH=minimax python -m sddctx init --reset --env-file minimax/sddctx.env
    python minimax/scripts/record_context.py

Usage:
    python minimax/scripts/record_context.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "minimax"
LOG_DIR = MODULE_ROOT / "logs"
CONTEXT_FILE = LOG_DIR / "development_context.json"
PROMPT_FILE = LOG_DIR / "development_prompt.md"

sys.path.insert(0, str(MODULE_ROOT))

from sddctx.config import load_settings  # noqa: E402
from sddctx.context_builder import ContextBuilder  # noqa: E402
from sddctx.material_store import SeekDBMaterialStore  # noqa: E402
from sddctx.state_store import PowerContextStateStore  # noqa: E402

TASK_ID = "sdd-task-001"
OBJECTIVE = "Implement SPEC-001: report a clamped line range when the requested end exceeds the content length"
KEYWORDS = ["read_range", "source_range_clamped", "clamp", "limitations"]
LIMIT = 8


def main() -> int:
    """Write the context and the prompt to the log directory."""

    settings = load_settings(MODULE_ROOT / "sddctx.env")
    state_store = PowerContextStateStore(settings)
    state = state_store.load_state(TASK_ID) if state_store.ping() else None

    with SeekDBMaterialStore(settings) as store:
        package = ContextBuilder(store).build(
            TASK_ID,
            OBJECTIVE,
            keywords=KEYWORDS,
            state=state,
            limit=LIMIT,
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_FILE.write_text(
        json.dumps(package.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    PROMPT_FILE.write_text(package.render(), encoding="utf-8")

    print(f"context digest: {package.digest}")
    print(f"specifications: {len(package.specifications)}")
    print(f"code: {len(package.code)}")
    print(f"tests: {len(package.tests)}")
    print(f"state recorded: {bool(state)}")
    print(f"context: {CONTEXT_FILE.relative_to(REPO_ROOT)}")
    print(f"prompt: {PROMPT_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
