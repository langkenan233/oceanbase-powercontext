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
PowerContext package. The change applied for this task is listed in
``minimax/docs/technical_notes.md``.
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
