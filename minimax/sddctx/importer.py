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

"""Import the engineering material of one task into seekdb.

The workload directory holds the material in plain files:

``specs/<id>.md``
    One specification entry per file.
``app/<package>/<module>.py``
    The source files that have to change.
``app/**/tests/<name>.py``
    The test files that verify the change and guard the existing behaviour.

Every file may declare its own revision in a leading ``revision: <n>`` header.
The revision is stored with the material, so a re-import after a specification
change replaces the previous text and a later context build observes the new
content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sddctx.material_store import SeekDBMaterialStore

_REVISION_PATTERN = re.compile(r"^revision:\s*(\d+)\s*$", re.MULTILINE)
_TEST_DIRECTORY = "tests"


@dataclass(frozen=True)
class ImportResult:
    """The number of stored rows per material kind."""

    specifications: int
    code: int
    tests: int

    def as_dict(self) -> dict[str, int]:
        """Return a JSON-serializable view of the result."""

        return {
            "specifications": self.specifications,
            "code": self.code,
            "tests": self.tests,
        }


def read_revision(text: str, default: int = 1) -> int:
    """Return the revision declared in a material file."""

    match = _REVISION_PATTERN.search(text)
    return int(match.group(1)) if match else default


def import_workload(store: SeekDBMaterialStore, workload_dir: Path) -> ImportResult:
    """Import every material file below ``workload_dir`` into seekdb."""

    specifications = [
        _spec_entry(path, workload_dir) for path in sorted((workload_dir / "specs").rglob("*.md"))
    ]
    code = [_code_entry(path, workload_dir) for path in _application_files(workload_dir) if not _is_test(path)]
    tests = [_test_entry(path, workload_dir) for path in _application_files(workload_dir) if _is_test(path)]
    return ImportResult(
        specifications=store.import_specs(specifications),
        code=store.import_code(code),
        tests=store.import_tests(tests),
    )


def _application_files(workload_dir: Path) -> list[Path]:
    """Return every Python file of the application directory."""

    return sorted((workload_dir / "app").rglob("*.py"))


def _is_test(path: Path) -> bool:
    """Return whether the file belongs to a test directory."""

    return _TEST_DIRECTORY in path.parts


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _spec_entry(path: Path, root: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    identifier = path.stem
    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return {
        "id": identifier,
        "revision": read_revision(text),
        "title": title or identifier,
        "body": text,
        "source": _relative(path, root),
        "tags": "",
    }


def _code_entry(path: Path, root: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    symbol = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("class "):
            symbol = stripped.split("(", 1)[0].replace("def ", "").replace("class ", "").strip()
            break
    return {
        "id": _relative(path, root),
        "revision": read_revision(text),
        "symbol": symbol,
        "title": path.name,
        "body": text,
        "source": _relative(path, root),
    }


def _test_entry(path: Path, root: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return {
        "id": _relative(path, root),
        "revision": read_revision(text),
        "title": path.name,
        "body": text,
        "source": _relative(path, root),
    }


__all__ = ["ImportResult", "import_workload", "read_revision"]
