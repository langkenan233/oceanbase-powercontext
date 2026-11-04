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

"""Index the definitions of a Python source file by line number.

The index is used to point a reader at the place where a symbol is defined,
which is the same purpose the symbol index of the PowerContext code query
serves.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Definition:
    """One top level definition found in a source file."""

    name: str
    kind: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable view of the definition."""

        return {
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


def index_definitions(source: str) -> list[Definition]:
    """Return the top level functions and classes of the given source."""

    tree = ast.parse(source)
    found: list[Definition] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(Definition(node.name, "function", node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.ClassDef):
            found.append(Definition(node.name, "class", node.lineno, node.end_lineno or node.lineno))
    return found


def find_definition(source: str, name: str) -> Definition | None:
    """Return the definition of ``name``, or ``None`` when it is absent."""

    for definition in index_definitions(source):
        if definition.name == name:
            return definition
    return None


def index_file(path: str | Path) -> list[Definition]:
    """Return the definitions of the given source file."""

    resolved = Path(path)
    return index_definitions(resolved.read_text(encoding="utf-8"))


__all__ = ["Definition", "find_definition", "index_definitions", "index_file"]
