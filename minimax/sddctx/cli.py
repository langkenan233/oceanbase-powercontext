# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Command line entry point of the SDD context module.

Commands
--------
``init``
    Create the seekdb schema and import the workload material.
``context``
    Build the development context for a task and print it.
``state save``
    Write the task state into PowerContext.
``state show``
    Read the task state back from PowerContext.
``patch``
    Ask MiniMax to produce the code change for the assembled context.
``apply``
    Apply the patch that MiniMax produced to the business project.
``verify``
    Run the target and regression tests and record the result.

Run ``python -m sddctx --help`` for the full option list.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sddctx.config import load_settings
from sddctx.context_builder import ContextBuilder
from sddctx.importer import import_workload
from sddctx.material_store import SeekDBMaterialStore
from sddctx.minimax_client import MiniMaxClient, MiniMaxError
from sddctx.patch_apply import apply_patch
from sddctx.state_store import PowerContextStateStore, TaskState

DEFAULT_TASK = "sdd-task-001"
DEFAULT_TRACE_DIR = "minimax/logs"

SYSTEM_PROMPT = (
    "You are a careful software engineer working on an existing project.\n"
    "Change only the code that the specification requires and keep every "
    "existing behaviour that the specification does not change.\n"
    "Answer with one unified diff and nothing else. The diff must use the "
    "git format, so that it starts with a line 'diff --git a/<path> b/<path>', "
    "followed by the '---' and '+++' headers and the hunks. Use repository "
    "relative paths. Do not add any explanation before or after the diff."
)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _load_state(settings: Any, task_id: str) -> TaskState | None:
    store = PowerContextStateStore(settings)
    if not store.ping():
        return None
    return store.load_state(task_id)


def command_init(arguments: argparse.Namespace) -> int:
    """Create the schema, import the material and report the stored row counts."""

    settings = load_settings(arguments.env_file)
    with SeekDBMaterialStore(settings) as store:
        if arguments.reset:
            store.reset()
        result = import_workload(store, settings.workspace_dir)
        counts = store.count()
    _print({"imported": result.as_dict(), "stored": counts})
    return 0


def command_context(arguments: argparse.Namespace) -> int:
    """Build the development context and print it as JSON."""

    settings = load_settings(arguments.env_file)
    with SeekDBMaterialStore(settings) as store:
        builder = ContextBuilder(store)
        state = _load_state(settings, arguments.task_id)
        package = builder.build(
            arguments.task_id,
            arguments.objective,
            keywords=arguments.keyword,
            state=state,
            limit=arguments.limit,
        )
        payload = package.to_dict()
    if arguments.prompt:
        payload["prompt"] = package.render()
    _print(payload)
    return 0


def command_state_save(arguments: argparse.Namespace) -> int:
    """Write the task state into PowerContext."""

    settings = load_settings(arguments.env_file)
    store = PowerContextStateStore(settings)
    state = TaskState(
        task_id=arguments.task_id,
        objective=arguments.objective,
        completed=list(arguments.completed),
        todo=list(arguments.todo),
        verification=[json.loads(item) for item in arguments.verification],
        context_digest=arguments.digest,
    )
    response = store.save_state(state)
    _print({"scope_id": store.scope_id, "saved": True, "response": response})
    return 0


def command_state_show(arguments: argparse.Namespace) -> int:
    """Read the task state back from PowerContext."""

    settings = load_settings(arguments.env_file)
    store = PowerContextStateStore(settings)
    state = store.load_state(arguments.task_id)
    _print({"scope_id": store.scope_id, "state": state.to_dict() if state else None})
    return 0


def command_patch(arguments: argparse.Namespace) -> int:
    """Ask MiniMax to produce the code change for the assembled context."""

    settings = load_settings(arguments.env_file)
    with SeekDBMaterialStore(settings) as store:
        builder = ContextBuilder(store)
        state = _load_state(settings, arguments.task_id)
        package = builder.build(
            arguments.task_id,
            arguments.objective,
            keywords=arguments.keyword,
            state=state,
            limit=arguments.limit,
        )
    client = MiniMaxClient(settings)
    call = client.complete(package.render(), system=SYSTEM_PROMPT)
    output_path = Path(arguments.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(call.response, encoding="utf-8")

    trace_dir = Path(arguments.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "development_context.json").write_text(
        json.dumps(package.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (trace_dir / "development_prompt.md").write_text(package.render(), encoding="utf-8")
    (trace_dir / "minimax_call.json").write_text(
        json.dumps(call.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    _print(
        {
            "model": call.model,
            "base_url": call.base_url,
            "finish_reason": call.finish_reason,
            "usage": call.usage,
            "latency_ms": call.latency_ms,
            "context_digest": package.digest,
            "patch_file": str(output_path),
            "trace_dir": str(trace_dir),
        }
    )
    return 0


def command_apply(arguments: argparse.Namespace) -> int:
    """Apply the patch that MiniMax produced to the business project."""

    settings = load_settings(arguments.env_file)
    patch_path = Path(arguments.patch)
    if not patch_path.is_file():
        print(json.dumps({"error": {"message": f"patch file not found: {patch_path}"}}), file=sys.stderr)
        return 2
    result = apply_patch(settings.repository_root, patch_path)
    _print(
        {
            "applied": result.applied,
            "touched_files": list(result.touched_files),
            "patch_file": str(patch_path),
        }
    )
    if not result.applied:
        print(json.dumps({"error": {"message": result.check_output or result.apply_output}}), file=sys.stderr)
        return 2
    return 0


def command_verify(arguments: argparse.Namespace) -> int:
    """Run the target and regression tests and record the result."""

    settings = load_settings(arguments.env_file)
    import os
    import subprocess

    environment = dict(os.environ)
    search_path = [str(settings.workspace_dir / "app"), str(settings.repository_root / "minimax")]
    existing = environment.get("PYTHONPATH", "")
    if existing:
        search_path.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(search_path)

    completed = subprocess.run(  # noqa: S603 - the command is given by the operator on purpose
        arguments.command,
        cwd=settings.repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    result = {
        "command": list(arguments.command),
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    _print(result)
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the module."""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Environment file with the module settings.",
    )

    parser = argparse.ArgumentParser(
        prog="sddctx",
        description="SDD context module for engineering tasks.",
    )
    parser.add_argument("--env-file", type=Path, default=None, help="Environment file with the module settings.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", parents=[common], help="Create the schema and import the workload material.")
    init.add_argument("--reset", action="store_true", help="Remove every stored row before importing.")
    init.set_defaults(handler=command_init)

    context = subparsers.add_parser("context", parents=[common], help="Build the development context for a task.")
    context.add_argument("--task-id", default=DEFAULT_TASK)
    context.add_argument("--objective", default="")
    context.add_argument("--keyword", action="append", default=[], help="Keyword used to select material.")
    context.add_argument("--limit", type=int, default=8)
    context.add_argument("--prompt", action="store_true", help="Also print the rendered prompt.")
    context.set_defaults(handler=command_context)

    state = subparsers.add_parser("state", parents=[common], help="Manage the persisted task state.")
    state_subparsers = state.add_subparsers(dest="state_command", required=True)

    state_save = state_subparsers.add_parser("save", parents=[common], help="Write the task state into PowerContext.")
    state_save.add_argument("--task-id", default=DEFAULT_TASK)
    state_save.add_argument("--objective", default="")
    state_save.add_argument("--completed", action="append", default=[])
    state_save.add_argument("--todo", action="append", default=[])
    state_save.add_argument("--verification", action="append", default=[])
    state_save.add_argument("--digest", default="")
    state_save.set_defaults(handler=command_state_save)

    state_show = state_subparsers.add_parser(
        "show", parents=[common], help="Read the task state back from PowerContext."
    )
    state_show.add_argument("--task-id", default=DEFAULT_TASK)
    state_show.set_defaults(handler=command_state_show)

    patch = subparsers.add_parser("patch", parents=[common], help="Ask MiniMax to produce the code change.")
    patch.add_argument("--task-id", default=DEFAULT_TASK)
    patch.add_argument("--objective", default="")
    patch.add_argument("--keyword", action="append", default=[])
    patch.add_argument("--limit", type=int, default=8)
    patch.add_argument("--output", default="minimax/logs/minimax_patch.diff")
    patch.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR)
    patch.set_defaults(handler=command_patch)

    apply_patch_parser = subparsers.add_parser("apply", parents=[common], help="Apply the produced patch.")
    apply_patch_parser.add_argument("--patch", default="minimax/logs/minimax_patch.diff")
    apply_patch_parser.set_defaults(handler=command_apply)

    verify = subparsers.add_parser("verify", parents=[common], help="Run the target and regression tests.")
    verify.add_argument("command", nargs=argparse.REMAINDER, help="Command to run.")
    verify.set_defaults(handler=command_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the module."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except MiniMaxError as error:
        print(json.dumps({"error": {"message": str(error)}}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (OSError, RuntimeError, KeyError, ValueError) as error:
        print(json.dumps({"error": {"message": str(error)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
