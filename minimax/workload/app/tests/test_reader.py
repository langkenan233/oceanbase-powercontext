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
