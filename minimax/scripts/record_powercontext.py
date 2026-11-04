#!/usr/bin/env python
"""Record the PowerContext state write and the reuse of the stored state.

The script shows the raw HTTP requests against the PowerContext Server: the
health check, the scope creation, the memory write and the memory read. The
last step builds a development context and shows that the stored progress is
part of it.

Usage:
    python minimax/scripts/record_powercontext.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "minimax"
LOG_PATH = MODULE_ROOT / "logs" / "powercontext_state.log"

sys.path.insert(0, str(MODULE_ROOT))

from sddctx.config import load_settings  # noqa: E402
from sddctx.context_builder import ContextBuilder  # noqa: E402
from sddctx.material_store import SeekDBMaterialStore  # noqa: E402
from sddctx.state_store import PowerContextStateStore, TaskState  # noqa: E402

TASK_ID = "sdd-task-001"


def main() -> int:
    """Write the state, read it back and build a context that uses it."""

    settings = load_settings(MODULE_ROOT / "sddctx.env")
    store = PowerContextStateStore(settings)
    lines = [
        "# PowerContext state record",
        f"# timestamp: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        f"# server: {settings.powercontext_url}",
        "",
    ]

    lines.append("## health check")
    lines.append(f"GET {settings.powercontext_url}/health/live -> {store.ping()}")
    lines.append("")

    scope_id = store.ensure_scope()
    lines.append("## scope")
    lines.append(f"scope_id: {scope_id}")
    lines.append("")

    state = TaskState(
        task_id=TASK_ID,
        objective="Implement SPEC-001: report a clamped line range when the requested end exceeds the content length",
        completed=[
            "Imported the specification, the code and the tests into seekdb",
            "Produced the patch with MiniMax and applied it with the module",
        ],
        todo=["Re-import the updated specification and rebuild the context"],
        verification=[
            {"name": "target_tests_before", "result": "6 failed, exit code 1"},
            {"name": "target_tests_after", "result": "12 passed, exit code 0"},
        ],
        context_digest="207b405b4f8f5227",
    )
    lines.append("## write")
    lines.append(f"POST {settings.powercontext_url}/v1/memory/remember")
    lines.append(f"scope_id: {scope_id}")
    lines.append("kind: sdd_task_state")
    lines.append(f"text: {state.to_text()}")
    response = store.save_state(state)
    lines.append(f"response: {json.dumps(response, ensure_ascii=False)[:400]}")
    lines.append("")

    lines.append("## read")
    lines.append(f"POST {settings.powercontext_url}/v1/memory/entries/list")
    restored = store.load_state(TASK_ID)
    lines.append(f"restored: {json.dumps(restored.to_dict(), ensure_ascii=False) if restored else None}")
    lines.append("")

    lines.append("## the stored state is part of the next context")
    with SeekDBMaterialStore(settings) as material_store:
        package = ContextBuilder(material_store).build(
            TASK_ID,
            state.objective,
            keywords=["read_range", "source_range_clamped", "clamp", "limitations"],
            state=restored,
            limit=5,
        )
    rendered = package.render()
    for line in rendered.splitlines():
        if line.startswith("- [done]") or line.startswith("- [open]") or line.startswith("## Progress"):
            lines.append(line)
    lines.append("")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"log: {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
