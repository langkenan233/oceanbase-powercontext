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
