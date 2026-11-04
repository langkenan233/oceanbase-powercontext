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
