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

"""Tests of the SDD context module.

The tests that need a running seekdb or PowerContext Server are skipped when
the service is not reachable, so the suite also runs on a machine that only
has the module checked out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sddctx.cli import parse_verification
from sddctx.config import Settings, load_settings
from sddctx.context_builder import ContextBuilder, ContextPackage
from sddctx.importer import import_workload, read_revision
from sddctx.material_store import Material, SeekDBMaterialStore
from sddctx.minimax_client import MiniMaxClient, MiniMaxError
from sddctx.patch_apply import apply_patch, touched_files
from sddctx.state_store import PowerContextStateStore, TaskState

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKLOAD = REPO_ROOT / "minimax" / "workload"


def _settings(**overrides: object) -> Settings:
    base = {
        "seekdb_host": "127.0.0.1",
        "seekdb_port": 2881,
        "seekdb_user": "root",
        "seekdb_password": "",
        "seekdb_database": "sdd_context_test",
        "powercontext_url": "http://127.0.0.1:8000",
        "powercontext_token": "",
        "powercontext_scope_id": "",
        "minimax_base_url": "https://api.minimax.chat/v1",
        "minimax_api_key": "",
        "minimax_model": "MiniMax-M2",
        "repository_root": REPO_ROOT,
        "workspace_dir": WORKLOAD,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# -- configuration ------------------------------------------------------------


def test_load_settings_reads_the_environment_file(tmp_path: Path) -> None:
    env_file = tmp_path / "module.env"
    env_file.write_text(
        "SEEKDB_PORT=3881\nMINIMAX_MODEL=MiniMax-M2\n# comment\nSDD_WORKSPACE_DIR=workload\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.seekdb_port == 3881
    assert settings.minimax_model == "MiniMax-M2"
    assert settings.workspace_dir == Path("workload")


def test_load_settings_prefers_the_process_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "module.env"
    env_file.write_text("SEEKDB_PORT=3881\n", encoding="utf-8")
    monkeypatch.setenv("SEEKDB_PORT", "4881")

    settings = load_settings(env_file)

    assert settings.seekdb_port == 4881


# -- importer -----------------------------------------------------------------


def test_read_revision_returns_the_declared_revision() -> None:
    assert read_revision("# title\n\nrevision: 7\n\nbody") == 7


def test_read_revision_defaults_to_one() -> None:
    assert read_revision("# title\n\nbody") == 1


def test_read_revision_ignores_a_revision_inside_the_body() -> None:
    assert read_revision("text\nrevision: not-a-number\n") == 1


def test_import_workload_counts_every_kind(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "app" / "pkg").mkdir(parents=True)
    (tmp_path / "app" / "tests").mkdir(parents=True)
    (tmp_path / "specs" / "SPEC-1.md").write_text("# SPEC-1\n\nrevision: 2\n\nbody\n", encoding="utf-8")
    (tmp_path / "app" / "pkg" / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app" / "tests" / "test_module.py").write_text("def test_value():\n    pass\n", encoding="utf-8")

    class _RecordingStore:
        def __init__(self) -> None:
            self.specs: list[dict[str, object]] = []
            self.code: list[dict[str, object]] = []
            self.tests: list[dict[str, object]] = []

        def import_specs(self, specs: object) -> int:
            self.specs = list(specs)  # type: ignore[arg-type]
            return len(self.specs)

        def import_code(self, entries: object) -> int:
            self.code = list(entries)  # type: ignore[arg-type]
            return len(self.code)

        def import_tests(self, entries: object) -> int:
            self.tests = list(entries)  # type: ignore[arg-type]
            return len(self.tests)

    store = _RecordingStore()
    result = import_workload(store, tmp_path)  # type: ignore[arg-type]

    assert result.as_dict() == {"specifications": 1, "code": 1, "tests": 1}
    assert store.specs[0]["revision"] == 2
    assert store.code[0]["id"] == "app/pkg/module.py"
    assert store.tests[0]["id"] == "app/tests/test_module.py"


def test_import_workload_keeps_the_specification_revision(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "specs" / "SPEC-1.md").write_text("# SPEC-1\n\nrevision: 3\n\nbody\n", encoding="utf-8")

    class _RecordingStore:
        def __init__(self) -> None:
            self.specs: list[dict[str, object]] = []

        def import_specs(self, specs: object) -> int:
            self.specs = list(specs)  # type: ignore[arg-type]
            return len(self.specs)

        def import_code(self, entries: object) -> int:
            return 0

        def import_tests(self, entries: object) -> int:
            return 0

    store = _RecordingStore()
    import_workload(store, tmp_path)  # type: ignore[arg-type]

    assert store.specs[0]["revision"] == 3


# -- context ------------------------------------------------------------------


def _material(kind: str, identifier: str, body: str, revision: int = 1) -> Material:
    return Material(
        kind=kind,
        identifier=identifier,
        title=identifier,
        body=body,
        source_path=f"{kind}/{identifier}",
        revision=revision,
    )


def test_context_digest_changes_with_the_material_revision() -> None:
    first = ContextPackage(task_id="t", objective="o", specifications=[_material("spec", "S", "a", 1)])
    second = ContextPackage(task_id="t", objective="o", specifications=[_material("spec", "S", "a", 2)])

    assert first.digest != second.digest


def test_context_digest_is_stable_for_the_same_material() -> None:
    first = ContextPackage(task_id="t", objective="o", specifications=[_material("spec", "S", "a", 1)])
    second = ContextPackage(task_id="t", objective="o", specifications=[_material("spec", "S", "a", 1)])

    assert first.digest == second.digest


def test_context_render_names_the_specification_and_its_source() -> None:
    package = ContextPackage(task_id="t", objective="o", specifications=[_material("spec", "SPEC-1", "body")])

    rendered = package.render()

    assert "SPEC-1" in rendered
    assert "source: spec/SPEC-1" in rendered
    assert "body" in rendered


def test_context_render_lists_the_recorded_progress() -> None:
    state = TaskState(task_id="t", objective="o", completed=["done"], todo=["open"])
    package = ContextPackage(task_id="t", objective="o", state=state)

    rendered = package.render()

    assert "- [done] done" in rendered
    assert "- [open] open" in rendered


def test_context_builder_ranks_the_material_by_keyword_hits() -> None:
    class _Store:
        def search(self, keywords: object, limit: int = 8) -> list[Material]:
            return [
                _material("code", "a.py", "read_range end"),
                _material("code", "b.py", "read_range"),
                _material("code", "c.py", "unrelated"),
            ]

    builder = ContextBuilder(_Store())  # type: ignore[arg-type]
    package = builder.build("t", "o", keywords=["read_range", "end"])

    assert next(item.identifier for item in package.code) == "a.py"


# -- state --------------------------------------------------------------------


def test_task_state_round_trips_through_its_text() -> None:
    state = TaskState(
        task_id="t",
        objective="o",
        completed=["a"],
        todo=["b"],
        verification=[{"name": "suite", "result": "passed"}],
        context_digest="d",
    )

    restored = TaskState.from_text(state.to_text())

    assert restored.to_dict() == state.to_dict()


def test_task_state_text_is_valid_json() -> None:
    payload = json.loads(TaskState(task_id="t", objective="o").to_text())

    assert payload["task_id"] == "t"


def test_state_store_rejects_a_state_that_is_too_long() -> None:
    store = PowerContextStateStore(_settings())
    state = TaskState(task_id="t", objective="o", todo=["x" * 9000])

    with pytest.raises(RuntimeError, match="8192"):
        store.save_state(state)


def test_verification_entry_keeps_a_json_object() -> None:
    entry = parse_verification('{"name": "target_tests_before", "result": "6 failed"}')

    assert entry == {"name": "target_tests_before", "result": "6 failed"}


def test_verification_entry_wraps_plain_text() -> None:
    entry = parse_verification("target tests failed before the change")

    assert entry == {"result": "target tests failed before the change"}


def test_verification_entry_wraps_a_json_scalar() -> None:
    entry = parse_verification("42")

    assert entry == {"result": 42}


# -- patch --------------------------------------------------------------------


def test_touched_files_reads_the_git_header() -> None:
    patch = "diff --git a/x/y.py b/x/y.py\n--- a/x/y.py\n+++ b/x/y.py\n@@ -1 +1 @@\n"

    assert touched_files(patch) == ("x/y.py",)


def test_touched_files_falls_back_to_the_file_header() -> None:
    patch = "--- a/x/y.py\n+++ b/x/y.py\n@@ -1 +1 @@\n"

    assert touched_files(patch) == ("x/y.py",)


def test_apply_patch_reports_a_missing_patch_file(tmp_path: Path) -> None:
    result = apply_patch(REPO_ROOT, tmp_path / "missing.diff")

    assert result.applied is False


# -- MiniMax ------------------------------------------------------------------


def test_minimax_client_reports_a_missing_key() -> None:
    client = MiniMaxClient(_settings())

    assert client.available is False
    with pytest.raises(MiniMaxError):
        client.complete("prompt")


# -- integration --------------------------------------------------------------


def _seekdb_available(settings: Settings) -> bool:
    try:
        with SeekDBMaterialStore(settings):
            return True
    except Exception:
        return False


def test_seekdb_stores_and_returns_the_material() -> None:
    settings = _settings()
    if not _seekdb_available(settings):
        pytest.skip("seekdb is not reachable")

    with SeekDBMaterialStore(settings) as store:
        store.reset()
        store.import_specs(
            [{"id": "SPEC-X", "title": "SPEC-X", "body": "clamp the range", "source": "s", "revision": 2}]
        )
        found = store.search(["clamp"])
        counts = store.count()
        store.reset()

    assert counts["spec"] == 1
    assert found
    assert found[0].identifier == "SPEC-X"
    assert found[0].revision == 2


def _powercontext_available(settings: Settings) -> bool:
    return PowerContextStateStore(settings).ping()


def test_powercontext_stores_and_returns_the_task_state() -> None:
    settings = _settings()
    if not _powercontext_available(settings):
        pytest.skip("the PowerContext Server is not reachable")

    store = PowerContextStateStore(settings)
    state = TaskState(task_id="sddctx-test-task", objective="o", completed=["a"], todo=["b"])
    store.save_state(state)

    restored = store.load_state("sddctx-test-task")

    assert restored is not None
    assert restored.completed == ["a"]
    assert restored.todo == ["b"]
