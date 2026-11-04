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

"""Apply the patch that MiniMax produced to the business project.

The patch is a unified diff against the repository root, so the same file
works with ``git apply`` and with the checker below. The checker runs first,
so a patch that does not apply cleanly is reported instead of leaving the
working tree half changed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_HEADER_PATTERN = re.compile(r"^diff --git a/(?P<old>\S+) b/(?P<new>\S+)\s*$", re.MULTILINE)
_TARGET_PATTERN = re.compile(r"^--- a/(?P<old>\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of one patch application."""

    applied: bool
    touched_files: tuple[str, ...]
    check_output: str
    apply_output: str

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable view of the result."""

        return {
            "applied": self.applied,
            "touched_files": list(self.touched_files),
        }


def touched_files(patch_text: str) -> tuple[str, ...]:
    """Return the repository relative files the patch touches."""

    names = [match.group("new") for match in _HEADER_PATTERN.finditer(patch_text)]
    if names:
        return tuple(dict.fromkeys(names))
    return tuple(dict.fromkeys(match.group("old") for match in _TARGET_PATTERN.finditer(patch_text)))


def git_executable() -> str:
    """Return the absolute path of the git executable."""

    return shutil.which("git") or "git"


def apply_patch(repository_root: Path, patch_path: Path) -> ApplyResult:
    """Apply the patch file to the working tree of ``repository_root``.

    The patch is validated first. A patch that does not apply leaves the
    working tree unchanged and the returned result reports ``applied`` as
    ``False``.

    The patch is handed to ``git apply`` as bytes. Text mode would translate
    the line endings of the patch on Windows, and the hunk headers would no
    longer match the files in the working tree.
    """

    if not patch_path.is_file():
        return ApplyResult(
            applied=False,
            touched_files=(),
            check_output=f"patch file not found: {patch_path}",
            apply_output="",
        )

    patch_bytes = patch_path.read_bytes()
    patch_text = patch_bytes.decode("utf-8")
    files = touched_files(patch_text)

    check = subprocess.run(  # noqa: S603 - the command is fixed and the patch is read from a file
        [git_executable(), "apply", "--check", "--verbose", "-"],
        cwd=repository_root,
        input=patch_bytes,
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        return ApplyResult(
            applied=False,
            touched_files=files,
            check_output=_decode(check.stdout) + _decode(check.stderr),
            apply_output="",
        )

    applied = subprocess.run(  # noqa: S603 - the command is fixed and the patch is read from a file
        [git_executable(), "apply", "-"],
        cwd=repository_root,
        input=patch_bytes,
        capture_output=True,
        check=False,
    )
    return ApplyResult(
        applied=applied.returncode == 0,
        touched_files=files,
        check_output=_decode(check.stdout) + _decode(check.stderr),
        apply_output=_decode(applied.stdout) + _decode(applied.stderr),
    )


def _decode(payload: bytes) -> str:
    """Return the decoded text of a command output."""

    return payload.decode("utf-8", errors="replace").strip()


__all__ = ["ApplyResult", "apply_patch", "touched_files"]
