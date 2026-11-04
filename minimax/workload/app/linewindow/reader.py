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
SOURCE_RANGE_CLAMPED = "source_range_clamped"


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
    the last line, and the clamp is reported as ``source_range_clamped`` so a
    caller can tell a short content from a truncated request.
    """

    lines = split_lines(content)
    if not lines:
        lines = [""]
    if start > len(lines):
        raise RangeError("code_range_missing", f"line {start} is beyond the end of {path}")
    limitations: list[str] = []
    truncated = False
    if end > len(lines):
        limitations.append(SOURCE_RANGE_CLAMPED)
        truncated = True
        end = len(lines)
    selected = bytearray()
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
        limitations.append(SOURCE_LINE_TOO_LARGE)
        return RangeResult(
            path=path,
            start_line=start,
            end_line=last,
            content="",
            limitations=limitations,
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
    "SOURCE_RANGE_CLAMPED",
    "SOURCE_TRUNCATED",
    "RangeError",
    "RangeResult",
    "count_lines",
    "read_file",
    "read_range",
    "split_lines",
]
