# Task sdd-task-001

## Objective
Implement SPEC-001: report a clamped line range when the requested end exceeds the content length

## Progress recorded earlier

- [done] Imported the specification, the code and the tests into seekdb
- [done] Produced the patch with MiniMax and applied it with the module
- [open] Re-import the updated specification and rebuild the context

## Verification results recorded earlier
- target_tests_before: 6 failed, exit code 1
- target_tests_after: 12 passed, exit code 0

## Specification entries that apply

### SPEC-001-read-range-clamp (revision 1)
source: specs/SPEC-001-read-range-clamp.md

# SPEC-001 代码读取范围超出文件行数时必须报告截断

revision: 1

## 适用范围

`linewindow.reader.read_range`，即业务工程 `linewindow` 的行区间读取函数。

## 要改什么

当一次读取请求的 `end` 大于目标内容实际拥有的行数时，函数必须把读取范围截断到
内容末尾，**并且**在返回结果的 `limitations` 中加入 `source_range_clamped` 标记，
同时把 `truncated` 置为 `True`。

截断后的 `end_line` 必须等于内容的实际行数。

## 哪些原有行为必须保持

以下行为必须保持不变：

1. `start` 超过内容实际行数时，仍然抛出 `RangeError`，且 `code` 为
   `code_range_missing`。
2. `start` 不超过内容实际行数时，读取到的内容与修改前完全一致。
3. 请求范围完全落在内容之内时，不加入 `source_range_clamped`。
4. 请求的 `end` 恰好等于实际行数时，不加入 `source_range_clamped`。
5. 因为单行内容过长而截断内容时，仍然使用既有的 `source_truncated` 标记。
6. 因为单行内容过长而无法返回任何内容时，仍然使用既有的
   `source_line_too_large` 标记。
7. `source_range_clamped` 与 `source_truncated` 可以同时出现。

## 理由

修改前，请求第 100 到 300 行而内容只有 150 行时，函数静默地把 `end_line` 改成
150，既不报错也不留下任何限制标记。调用方无法区分「内容本身只有 150 行」和
「我请求的范围被截断了」，因此会在基于不完整证据的前提下继续工作。


## Code to change

### app/linewindow/__init__.py (revision 1)
source: app/linewindow/__init__.py

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

"""A small library that reads a line range out of a text file.

The package is the business project of this competition entry. It exists so
that the specification driven development task has a runnable project with its
own test suite:

``linewindow.reader``
    The range reader under change.
``tests/test_reader.py``
    The behaviour that must survive the change.
``tests/test_reader_range_clamp.py``
    The new behaviour demanded by SPEC-001.

The range reader is derived from the ``GraphQuery.read`` method of
PowerContext (``src/powercontext/builtin/code/query.py``, Apache-2.0). The
line splitting, the byte budget and the limitation vocabulary are kept; the
module boundary is rewritten so that the project runs on its own without the
PowerContext package. The change applied for this task is described by the
specification ``specs/SPEC-001-read-range-clamp.md``.
"""

from linewindow.indexer import Definition, find_definition, index_definitions, index_file
from linewindow.reader import RangeResult, count_lines, read_file, read_range, split_lines

__all__ = [
    "Definition",
    "RangeResult",
    "count_lines",
    "find_definition",
    "index_definitions",
    "index_file",
    "read_file",
    "read_range",
    "split_lines",
]


### app/linewindow/reader.py (revision 1)
source: app/linewindow/reader.py

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

"""Read a line range out of a text file.

The reader splits the content into physical lines, returns the requested
range and reports the limitations that affected the result.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_TRUNCATED = "source_truncated"
SOURCE_LINE_TOO_LARGE = "source_line_too_large"


class RangeError(RuntimeError):
    """Raised when a requested range cannot be served.

    The ``code`` attribute carries a stable machine readable identifier, the
    same way the PowerContext code query reports ``code_range_missing``.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def split_lines(content: str) -> list[str]:
    """Split the content into physical LF lines.

    A trailing newline does not open an extra empty line, and a file without a
    trailing newline still reports its last line.
    """

    parts = content.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def count_lines(content: str) -> int:
    """Return the number of physical lines in the content."""

    return len(split_lines(content))


@dataclass
class RangeResult:
    """The lines that were read, together with the limitations that applied."""

    path: str
    start_line: int
    end_line: int
    content: str
    limitations: list[str] = field(default_factory=list)
    truncated: bool = False
    snippet_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable view of the result."""

        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "limitations": sorted(self.limitations),
            "truncated": self.truncated,
            "snippet_sha256": self.snippet_sha256,
        }


def read_range(
    content: str,
    start: int,
    end: int,
    *,
    path: str = "<memory>",
    maximum: int = 24_000,
) -> RangeResult:
    """Return the lines between ``start`` and ``end``, both inclusive.

    Line numbers are 1-based. A ``start`` beyond the last line of the content
    is rejected. An ``end`` beyond the last line of the content is clamped to
    the last line.
    """

    lines = split_lines(content)
    if not lines:
        lines = [""]
    if start > len(lines):
        raise RangeError("code_range_missing", f"line {start} is beyond the end of {path}")
    end = min(end, len(lines))
    selected = bytearray()
    limitations: list[str] = []
    truncated = False
    last = start - 1
    for offset, line in enumerate(lines[start - 1 : end], start):
        encoded = line.encode("utf-8")
        if len(selected) + len(encoded) > maximum:
            limitations.append(SOURCE_TRUNCATED)
            truncated = True
            break
        selected.extend(encoded)
        last = offset
    if last < start:
        return RangeResult(
            path=path,
            start_line=start,
            end_line=last,
            content="",
            limitations=[SOURCE_LINE_TOO_LARGE],
            truncated=True,
        )
    return RangeResult(
        path=path,
        start_line=start,
        end_line=last,
        content=selected.decode("utf-8"),
        limitations=limitations,
        truncated=truncated,
        snippet_sha256=hashlib.sha256(bytes(selected)).hexdigest(),
    )


def read_file(path: str | Path, start: int, end: int, *, maximum: int = 24_000) -> RangeResult:
    """Read the given line range out of a file."""

    resolved = Path(path)
    return read_range(resolved.read_text(encoding="utf-8"), start, end, path=str(resolved), maximum=maximum)


__all__ = [
    "SOURCE_LINE_TOO_LARGE",
    "SOURCE_TRUNCATED",
    "RangeError",
    "RangeResult",
    "count_lines",
    "read_file",
    "read_range",
    "split_lines",
]


### app/linewindow/__main__.py (revision 1)
source: app/linewindow/__main__.py

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

"""Command line of the linewindow library.

Usage:
    python -m linewindow show <file> <start> <end>
    python -m linewindow index <file>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from linewindow.indexer import index_file
from linewindow.reader import RangeError, read_file


def _show(arguments: list[str]) -> int:
    if len(arguments) != 3:
        print("usage: python -m linewindow show <file> <start> <end>", file=sys.stderr)
        return 2
    target, start, end = arguments[0], int(arguments[1]), int(arguments[2])
    try:
        result = read_file(target, start, end)
    except RangeError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}), file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _index(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: python -m linewindow index <file>", file=sys.stderr)
        return 2
    definitions = index_file(Path(arguments[0]))
    print(json.dumps([item.to_dict() for item in definitions], ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: python -m linewindow <show|index> ...", file=sys.stderr)
        return 2
    command, rest = arguments[0], arguments[1:]
    if command == "show":
        return _show(rest)
    if command == "index":
        return _index(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


### app/linewindow/indexer.py (revision 1)
source: app/linewindow/indexer.py

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


## Tests that verify the change

### app/tests/test_reader_range_clamp.py (revision 1)
source: app/tests/test_reader_range_clamp.py

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

"""Target behaviour demanded by SPEC-001.

A range that reaches past the last line of the content is clamped to the file
length, and the clamp is reported as a limitation so that a caller can tell a
short file from a truncated request.

Every test in this file fails before the change and passes after it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from linewindow.reader import RangeError, read_file, read_range

SOURCE = "alpha\nbeta\ngamma\ndelta\n"

CLAMPED = "source_range_clamped"
TRUNCATED = "source_truncated"


def test_range_beyond_the_last_line_is_clamped_to_the_file_length() -> None:
    result = read_range(SOURCE, 2, 40)

    assert result.end_line == 4
    assert result.content == "beta\ngamma\ndelta\n"


def test_range_beyond_the_last_line_reports_the_clamp() -> None:
    result = read_range(SOURCE, 2, 40)

    assert CLAMPED in result.limitations


def test_range_inside_the_file_reports_no_clamp() -> None:
    result = read_range(SOURCE, 1, 2)

    assert CLAMPED not in result.limitations


def test_range_ending_exactly_at_the_last_line_reports_no_clamp() -> None:
    result = read_range(SOURCE, 1, 4)

    assert result.end_line == 4
    assert CLAMPED not in result.limitations


def test_clamp_and_snippet_truncation_are_reported_together() -> None:
    result = read_range("alpha\n" + ("x" * 100) + "\nbeta\n", 1, 40, maximum=20)

    assert CLAMPED in result.limitations
    assert TRUNCATED in result.limitations


def test_start_beyond_the_last_line_is_still_rejected() -> None:
    with pytest.raises(RangeError) as error:
        read_range(SOURCE, 5, 9)

    assert error.value.code == "code_range_missing"


def test_reading_inside_the_file_is_unchanged() -> None:
    result = read_range(SOURCE, 2, 3)

    assert result.start_line == 2
    assert result.end_line == 3
    assert result.content == "beta\ngamma\n"


def test_reading_a_single_line_is_unchanged() -> None:
    result = read_range(SOURCE, 3, 3)

    assert result.content == "gamma\n"
    assert CLAMPED not in result.limitations


def test_clamp_is_reported_for_a_single_line_file() -> None:
    result = read_range("only\n", 1, 9)

    assert result.end_line == 1
    assert CLAMPED in result.limitations


def test_repeated_reads_keep_reporting_the_clamp() -> None:
    first = read_range(SOURCE, 1, 40)
    second = read_range(SOURCE, 2, 40)

    assert CLAMPED in first.limitations
    assert CLAMPED in second.limitations


def test_empty_content_does_not_raise_and_reports_the_clamp() -> None:
    result = read_range("", 1, 5)

    assert result.end_line == 1
    assert CLAMPED in result.limitations


def test_read_file_reports_the_clamp(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text(SOURCE, encoding="utf-8")

    result = read_file(target, 1, 40)

    assert result.end_line == 4
    assert CLAMPED in result.limitations


### app/tests/test_reader.py (revision 1)
source: app/tests/test_reader.py

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

"""Behaviour of the range reader that must survive the SPEC-001 change.

Every test in this file passes before and after the change. It is the
regression suite of the business project.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from linewindow.reader import (
    SOURCE_LINE_TOO_LARGE,
    SOURCE_TRUNCATED,
    RangeError,
    count_lines,
    read_file,
    read_range,
    split_lines,
)

SOURCE = "alpha\nbeta\ngamma\ndelta\n"


def test_split_lines_keeps_the_line_ending() -> None:
    assert split_lines("alpha\nbeta\n") == ["alpha\n", "beta\n"]


def test_split_lines_does_not_open_an_empty_last_line() -> None:
    assert split_lines("alpha\n") == ["alpha\n"]


def test_split_lines_keeps_a_last_line_without_a_ending() -> None:
    assert split_lines("alpha\nbeta") == ["alpha\n", "beta"]


def test_split_lines_of_empty_content_is_empty() -> None:
    assert split_lines("") == []


def test_count_lines_counts_physical_lines() -> None:
    assert count_lines(SOURCE) == 4
    assert count_lines("") == 0


def test_read_range_returns_the_requested_lines() -> None:
    result = read_range(SOURCE, 2, 3)

    assert result.start_line == 2
    assert result.end_line == 3
    assert result.content == "beta\ngamma\n"


def test_read_range_returns_a_single_line() -> None:
    result = read_range(SOURCE, 3, 3)

    assert result.content == "gamma\n"


def test_read_range_starts_at_the_first_line() -> None:
    result = read_range(SOURCE, 1, 2)

    assert result.content == "alpha\nbeta\n"


def test_read_range_rejects_a_start_beyond_the_last_line() -> None:
    with pytest.raises(RangeError) as error:
        read_range(SOURCE, 5, 9)

    assert error.value.code == "code_range_missing"


def test_read_range_reports_a_truncated_snippet() -> None:
    result = read_range(SOURCE, 1, 4, maximum=len("alpha\n"))

    assert SOURCE_TRUNCATED in result.limitations
    assert result.truncated is True


def test_read_range_reports_a_line_that_does_not_fit() -> None:
    result = read_range(SOURCE, 1, 4, maximum=1)

    assert SOURCE_LINE_TOO_LARGE in result.limitations
    assert result.content == ""


def test_read_range_of_empty_content_returns_one_empty_line() -> None:
    result = read_range("", 1, 1)

    assert result.start_line == 1
    assert result.end_line == 1
    assert result.content == ""


def test_read_file_reads_the_range_from_disk(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text(SOURCE, encoding="utf-8")

    result = read_file(target, 2, 3)

    assert result.content == "beta\ngamma\n"
    assert result.path == str(target)


def test_read_file_rejects_a_start_beyond_the_last_line(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text(SOURCE, encoding="utf-8")

    with pytest.raises(RangeError) as error:
        read_file(target, 9, 12)

    assert error.value.code == "code_range_missing"


def test_read_range_reports_the_snippet_digest() -> None:
    result = read_range(SOURCE, 1, 2)

    assert result.snippet_sha256
    assert result.to_dict()["limitations"] == []


### app/tests/test_indexer.py (revision 1)
source: app/tests/test_indexer.py

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

