"""Allow ``python -m sddctx`` to run the module command line."""

from __future__ import annotations

from sddctx.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
