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

"""Behaviour of the definition index that must survive the SPEC-001 change."""

from __future__ import annotations

from pathlib import Path

from linewindow.indexer import find_definition, index_definitions, index_file

SOURCE = '''
"""Module docstring."""


def alpha():
    """First."""


class Beta:
    """Second."""


async def gamma():
    """Third."""
'''


def test_index_definitions_finds_the_top_level_definitions() -> None:
    definitions = index_definitions(SOURCE)

    assert [item.name for item in definitions] == ["alpha", "Beta", "gamma"]


def test_index_definitions_reports_the_kind() -> None:
    definitions = index_definitions(SOURCE)
    kinds = {item.name: item.kind for item in definitions}

    assert kinds["alpha"] == "function"
    assert kinds["Beta"] == "class"
    assert kinds["gamma"] == "function"


def test_index_definitions_reports_the_line_numbers() -> None:
    definitions = index_definitions(SOURCE)
    alpha = next(item for item in definitions if item.name == "alpha")

    assert alpha.start_line == 5
    assert alpha.end_line == 6


def test_find_definition_returns_the_definition() -> None:
    definition = find_definition(SOURCE, "Beta")

    assert definition is not None
    assert definition.kind == "class"


def test_find_definition_returns_none_for_an_unknown_name() -> None:
    assert find_definition(SOURCE, "missing") is None


def test_index_file_reads_the_definitions(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(SOURCE, encoding="utf-8")

    definitions = index_file(target)

    assert [item.name for item in definitions] == ["alpha", "Beta", "gamma"]


def test_definition_to_dict_is_json_friendly() -> None:
    definition = index_definitions(SOURCE)[0]

    assert definition.to_dict() == {
        "name": "alpha",
        "kind": "function",
        "start_line": 5,
        "end_line": 6,
    }
