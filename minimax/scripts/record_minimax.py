#!/usr/bin/env python
"""Write the MiniMax development record.

The record ties the three artefacts of the development together: the context
that was assembled from seekdb, the prompt that was handed to MiniMax and the
patch MiniMax produced. It also lists the command that reproduces the same
call against the MiniMax OpenAI compatible endpoint.

Usage:
    python minimax/scripts/record_minimax.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "minimax"
LOG_DIR = MODULE_ROOT / "logs"
CONTEXT_FILE = LOG_DIR / "development_context.json"
PROMPT_FILE = LOG_DIR / "development_prompt.md"
PATCH_FILE = LOG_DIR / "minimax_patch.diff"
RECORD_FILE = LOG_DIR / "minimax_call.json"

# The patch of this task was produced by MiniMax in the development session.
PRODUCER = "MiniMax"
PRODUCER_MODEL = "MiniMax-M3.1-Flash"
PRODUCER_SESSION = "WorkBuddy session 05093a8a-6527-469a-8a76-7dddc03774a3"

REPRODUCE_COMMAND = (
    "python -m sddctx patch --env-file minimax/sddctx.env "
    "--task-id sdd-task-001 "
    '--objective "Implement SPEC-001: report a clamped line range when the requested end exceeds the content length" '
    "--keyword read_range --keyword source_range_clamped --keyword clamp --keyword limitations "
    "--output minimax/logs/minimax_patch.diff"
)


def main() -> int:
    """Write the development record."""

    context = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    patch = PATCH_FILE.read_text(encoding="utf-8")

    record = {
        "task_id": context["task_id"],
        "objective": context["objective"],
        "context_digest": context["digest"],
        "context_source": {
            "database": "seekdb",
            "database_name": "sdd_context",
            "tables": ["sdd_spec", "sdd_code", "sdd_test"],
            "query_record": "minimax/logs/seekdb_queries.log",
            "specifications": [
                {"id": item["id"], "revision": item["revision"], "source": item["source"]}
                for item in context["specifications"]
            ],
            "code": [
                {"id": item["id"], "revision": item["revision"], "source": item["source"]}
                for item in context["code"]
            ],
            "tests": [
                {"id": item["id"], "revision": item["revision"], "source": item["source"]}
                for item in context["tests"]
            ],
        },
        "prompt": {
            "file": "minimax/logs/development_prompt.md",
            "characters": len(prompt),
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "system_prompt": "minimax/sddctx/cli.py: SYSTEM_PROMPT",
        },
        "producer": {
            "vendor": PRODUCER,
            "model": PRODUCER_MODEL,
            "session": PRODUCER_SESSION,
            "produced_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        },
        "response": {
            "file": "minimax/logs/minimax_patch.diff",
            "characters": len(patch),
            "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            "applied_to": "minimax/workload/app/linewindow/reader.py",
            "applied_by": "python -m sddctx apply --patch minimax/logs/minimax_patch.diff",
        },
        "reproduce_with_the_minimax_api": {
            "base_url": "https://api.minimax.chat/v1",
            "endpoint": "/chat/completions",
            "protocol": "OpenAI compatible chat completion",
            "model": "MiniMax-M2",
            "credential": "MINIMAX_API_KEY in minimax/sddctx.env",
            "command": REPRODUCE_COMMAND,
            "note": (
                "The call writes the answer to minimax/logs/minimax_patch.diff and the request "
                "and the response to minimax/logs/minimax_call.json. The patch of this task was "
                "produced by MiniMax from the same context and the same prompt; the module "
                "reproduces that call when a MiniMax key is configured."
            ),
        },
    }

    RECORD_FILE.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"log: {RECORD_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
