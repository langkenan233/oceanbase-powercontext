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

"""Build the development context for one task.

The context is assembled from the material that seekdb actually holds:

* the specification entries that constrain the change,
* the code files and symbols that have to be modified,
* the tests that verify the new behaviour and guard the existing behaviour.

The assembled context also carries the task state read back from
PowerContext, so a run after a restart knows what has already been done.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sddctx.material_store import Material, SeekDBMaterialStore
from sddctx.state_store import TaskState


@dataclass
class ContextPackage:
    """The development context handed to the coding assistant."""

    task_id: str
    objective: str
    specifications: list[Material] = field(default_factory=list)
    code: list[Material] = field(default_factory=list)
    tests: list[Material] = field(default_factory=list)
    state: TaskState | None = None

    @property
    def digest(self) -> str:
        """Return a stable digest of the assembled context."""

        payload = json.dumps(
            {
                "specifications": [(item.identifier, item.revision) for item in self.specifications],
                "code": [(item.identifier, item.revision) for item in self.code],
                "tests": [(item.identifier, item.revision) for item in self.tests],
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the context."""

        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "digest": self.digest,
            "specifications": [item.to_dict() for item in self.specifications],
            "code": [item.to_dict() for item in self.code],
            "tests": [item.to_dict() for item in self.tests],
            "state": self.state.to_dict() if self.state else None,
        }

    def render(self) -> str:
        """Render the context as the prompt text for the coding assistant."""

        sections: list[str] = [f"# Task {self.task_id}", "", "## Objective", self.objective, ""]
        if self.state:
            sections.extend(["## Progress recorded earlier", ""])
            for item in self.state.completed:
                sections.append(f"- [done] {item}")
            for item in self.state.todo:
                sections.append(f"- [open] {item}")
            if self.state.verification:
                sections.append("")
                sections.append("## Verification results recorded earlier")
                for record in self.state.verification:
                    sections.append(f"- {record.get('name', 'test')}: {record.get('result', 'unknown')}")
            sections.append("")
        sections.extend(["## Specification entries that apply", ""])
        for item in self.specifications:
            sections.extend(_material_section(item))
        sections.extend(["## Code to change", ""])
        for item in self.code:
            sections.extend(_material_section(item))
        sections.extend(["## Tests that verify the change", ""])
        for item in self.tests:
            sections.extend(_material_section(item))
        return "\n".join(sections)


def _material_section(item: Material) -> list[str]:
    """Return the rendered lines of one material entry."""

    return [
        f"### {item.identifier} (revision {item.revision})",
        f"source: {item.source_path}",
        "",
        item.body,
        "",
    ]


class ContextBuilder:
    """Select the material that applies to a task and assemble the context."""

    def __init__(self, store: SeekDBMaterialStore) -> None:
        self._store = store

    def build(
        self,
        task_id: str,
        objective: str,
        *,
        keywords: list[str],
        state: TaskState | None = None,
        limit: int = 8,
    ) -> ContextPackage:
        """Return the context package for one task.

        The material comes from a seekdb query. A specification whose text
        changed after a re-import is therefore picked up, because the query
        reads the current rows.
        """

        matched = self._store.search(keywords, limit=limit * 3)
        specifications = [item for item in matched if item.kind == "spec"]
        code = [item for item in matched if item.kind == "code"]
        tests = [item for item in matched if item.kind == "test"]
        specifications = _rank(specifications, keywords, limit)
        code = _rank(code, keywords, limit)
        tests = _rank(tests, keywords, limit)
        return ContextPackage(
            task_id=task_id,
            objective=objective,
            specifications=specifications,
            code=code,
            tests=tests,
            state=state,
        )


def _rank(materials: list[Material], keywords: list[str], limit: int) -> list[Material]:
    """Order the material by how many keywords it mentions, then by identifier."""

    terms = [term.lower() for term in keywords if term.strip()]

    def score(item: Material) -> tuple[int, str]:
        haystack = f"{item.identifier}\n{item.title}\n{item.body}".lower()
        hits = sum(1 for term in terms if term in haystack)
        return (-hits, item.identifier)

    return sorted(materials, key=score)[:limit]


__all__ = ["ContextBuilder", "ContextPackage"]
