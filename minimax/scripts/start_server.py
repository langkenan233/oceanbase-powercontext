#!/usr/bin/env python
"""Start the PowerContext Server for the SDD context module.

The Server stores its own task state. The state directory is derived from this
file's location so the module runs the same way on any machine.

The Server source is looked up in two places: the repository root ``src``
directory when the module runs inside the full PowerContext repository, and the
copy shipped with the submission under ``minimax/vendor``. The competition
branch holds the vendor copy only, so that one is preferred whenever it exists.

Usage:
    python minimax/scripts/start_server.py [--env-file PATH] [--port PORT]

The Server is reached at http://127.0.0.1:<port>/v1 once it reports ready.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "minimax"
DEFAULT_ENV_FILE = MODULE_ROOT / "powercontext.env"
DATA_DIR = MODULE_ROOT / "data"
SOURCE_ROOTS = (MODULE_ROOT / "vendor", REPO_ROOT / "src")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the PowerContext Server.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Server environment file. Defaults to minimax/powercontext.env.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the HTTP port from the environment file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments, forwarded = build_parser().parse_known_args(argv)

    env_file = arguments.env_file.resolve()
    if not env_file.is_file():
        print(f"environment file not found: {env_file}", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["POWERCONTEXT_HOME"] = str(DATA_DIR)
    os.environ["POWERCONTEXT_SERVER_WORKSPACE"] = str(REPO_ROOT)
    if arguments.port is not None:
        os.environ["POWERCONTEXT_SERVER_HTTP_PORT"] = str(arguments.port)

    source_root = resolve_source_root()
    sys.path.insert(0, str(source_root))

    from powercontext.server.cli import app

    app(args=forwarded or None)
    return 0


def resolve_source_root() -> Path:
    """Return the directory that holds the ``powercontext`` package."""

    for candidate in SOURCE_ROOTS:
        if (candidate / "powercontext" / "server" / "cli.py").is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in SOURCE_ROOTS)
    message = f"the PowerContext source was not found in: {searched}"
    raise SystemExit(message)


if __name__ == "__main__":
    raise SystemExit(main())
