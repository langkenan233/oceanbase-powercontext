#!/usr/bin/env python
"""Record the seekdb queries that produce the development context.

The script shows that seekdb really holds the engineering material: it prints
the schema, the row counts, the retrieval query and the rows the query
returns. The retrieval query is the one the context builder uses.

Usage:
    python minimax/scripts/record_seekdb.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "minimax"
LOG_PATH = MODULE_ROOT / "logs" / "seekdb_queries.log"

sys.path.insert(0, str(MODULE_ROOT))

from sddctx.config import load_settings  # noqa: E402
from sddctx.material_store import SeekDBMaterialStore  # noqa: E402

KEYWORDS = ["read_range", "source_range_clamped", "clamp", "limitations"]


def main() -> int:
    """Print and record the seekdb queries."""

    env_file = MODULE_ROOT / "sddctx.env"
    settings = load_settings(env_file)
    lines = [
        "# seekdb query record",
        f"# timestamp: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        f"# host: {settings.seekdb_host}:{settings.seekdb_port}",
        f"# database: {settings.seekdb_database}",
        "",
    ]

    with SeekDBMaterialStore(settings) as store:
        with store._connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            lines.append(f"server version: {cursor.fetchone()[0]}")
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]
            lines.append(f"tables: {tables}")
            lines.append("")

        counts = store.count()
        lines.append("## row counts")
        for kind, count in sorted(counts.items()):
            lines.append(f"{kind}: {count}")
        lines.append("")

        lines.append("## retrieval query")
        lines.append("SELECT kind, item_id, title, source_path, revision FROM (")
        lines.append("  SELECT 'spec' AS kind, spec_id AS item_id, title, body, source_path, revision")
        lines.append("    FROM sdd_spec WHERE <keyword>")
        lines.append("  UNION ALL")
        lines.append("  SELECT 'code' AS kind, code_id AS item_id, title, body, source_path, revision")
        lines.append("    FROM sdd_code WHERE <keyword>")
        lines.append("  UNION ALL")
        lines.append("  SELECT 'test' AS kind, test_id AS item_id, title, body, source_path, revision")
        lines.append("    FROM sdd_test WHERE <keyword>")
        lines.append(") AS matched ORDER BY kind, item_id LIMIT 24")
        lines.append(f"keywords: {KEYWORDS}")
        lines.append("")

        matched = store.search(KEYWORDS, limit=24)
        lines.append("## rows returned")
        for item in matched:
            lines.append(
                f"{item.kind:5} {item.identifier:42} revision={item.revision} source={item.source_path}"
            )
        lines.append(f"rows: {len(matched)}")

        lines.append("")
        lines.append("## stored specification")
        for item in store.list_all("spec"):
            lines.append(f"{item.identifier} revision={item.revision} source={item.source_path}")
            lines.append(f"  first line: {item.body.splitlines()[0] if item.body else ''}")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"log: {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
