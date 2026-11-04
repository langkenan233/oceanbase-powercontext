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

"""Engineering material storage on seekdb.

Three kinds of material are stored and searched:

``spec``
    Normative specification entries that constrain the change.
``code``
    Source files and the symbols defined inside them.
``test``
    Test files that verify the change and guard the existing behaviour.

Every material row carries a ``revision``. Re-importing a specification with a
new revision replaces the previous text, so a later context build observes the
updated specification instead of a fixed answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import pymysql

from sddctx.config import Settings

_SPEC_TABLE = "sdd_spec"
_CODE_TABLE = "sdd_code"
_TEST_TABLE = "sdd_test"

_TABLES = (_SPEC_TABLE, _CODE_TABLE, _TEST_TABLE)


@dataclass(frozen=True)
class Material:
    """One retrieved engineering material with its provenance."""

    kind: str
    identifier: str
    title: str
    body: str
    source_path: str
    revision: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the material."""

        return {
            "kind": self.kind,
            "id": self.identifier,
            "title": self.title,
            "source": self.source_path,
            "revision": self.revision,
            "body": self.body,
        }


class SeekDBMaterialStore:
    """Persist and retrieve engineering material in seekdb."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection: pymysql.Connection | None = None

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Open the connection and make sure the schema exists."""

        arguments = self._settings.seekdb_connection_arguments
        database = str(arguments.pop("database"))
        self._connection = pymysql.connect(**arguments, autocommit=True)
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_bin"
            )
            cursor.execute(f"USE `{database}`")
            self._create_schema(cursor)

    def close(self) -> None:
        """Close the connection if one is open."""

        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> SeekDBMaterialStore:
        self.connect()
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def _create_schema(self, cursor: Any) -> None:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{_SPEC_TABLE}` (
                spec_id VARCHAR(128) NOT NULL,
                revision INT NOT NULL,
                title VARCHAR(512) NOT NULL,
                body TEXT NOT NULL,
                source_path VARCHAR(1024) NOT NULL,
                tags VARCHAR(512) NOT NULL DEFAULT '',
                PRIMARY KEY (spec_id)
            )
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{_CODE_TABLE}` (
                code_id VARCHAR(256) NOT NULL,
                revision INT NOT NULL,
                symbol VARCHAR(256) NOT NULL DEFAULT '',
                title VARCHAR(512) NOT NULL,
                body TEXT NOT NULL,
                source_path VARCHAR(1024) NOT NULL,
                PRIMARY KEY (code_id)
            )
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{_TEST_TABLE}` (
                test_id VARCHAR(256) NOT NULL,
                revision INT NOT NULL,
                title VARCHAR(512) NOT NULL,
                body TEXT NOT NULL,
                source_path VARCHAR(1024) NOT NULL,
                PRIMARY KEY (test_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `sdd_import_log` (
                import_id INT NOT NULL AUTO_INCREMENT,
                kind VARCHAR(32) NOT NULL,
                item_id VARCHAR(256) NOT NULL,
                revision INT NOT NULL,
                imported_at VARCHAR(64) NOT NULL,
                PRIMARY KEY (import_id)
            )
            """
        )

    # -- writing --------------------------------------------------------------

    def import_specs(self, specs: Iterable[dict[str, Any]]) -> int:
        """Upsert specification entries and return the number of stored rows."""

        return self._upsert(
            _SPEC_TABLE,
            "spec_id",
            specs,
            lambda item: (
                item["id"],
                int(item.get("revision", 1)),
                item.get("title", item["id"]),
                item.get("body", ""),
                item.get("source", ""),
                item.get("tags", ""),
            ),
            ("spec_id", "revision", "title", "body", "source_path", "tags"),
        )

    def import_code(self, entries: Iterable[dict[str, Any]]) -> int:
        """Upsert code material and return the number of stored rows."""

        return self._upsert(
            _CODE_TABLE,
            "code_id",
            entries,
            lambda item: (
                item["id"],
                int(item.get("revision", 1)),
                item.get("symbol", ""),
                item.get("title", item["id"]),
                item.get("body", ""),
                item.get("source", ""),
            ),
            ("code_id", "revision", "symbol", "title", "body", "source_path"),
        )

    def import_tests(self, entries: Iterable[dict[str, Any]]) -> int:
        """Upsert test material and return the number of stored rows."""

        return self._upsert(
            _TEST_TABLE,
            "test_id",
            entries,
            lambda item: (
                item["id"],
                int(item.get("revision", 1)),
                item.get("title", item["id"]),
                item.get("body", ""),
                item.get("source", ""),
            ),
            ("test_id", "revision", "title", "body", "source_path"),
        )

    def _upsert(
        self,
        table: str,
        key_column: str,
        entries: Iterable[dict[str, Any]],
        row_builder: Any,
        columns: tuple[str, ...],
    ) -> int:
        stored = 0
        with self._connection.cursor() as cursor:
            for item in entries:
                values = row_builder(item)
                placeholders = ", ".join(["%s"] * len(columns))
                assignments = ", ".join(f"{column} = VALUES({column})" for column in columns[1:])
                columns_sql = ", ".join(columns)
                cursor.execute(
                    f"INSERT INTO `{table}` ({columns_sql}) VALUES ({placeholders}) "  # noqa: S608 - fixed names
                    f"ON DUPLICATE KEY UPDATE {assignments}",
                    values,
                )
                cursor.execute(
                    "INSERT INTO `sdd_import_log` (kind, item_id, revision, imported_at) VALUES (%s, %s, %s, %s)",
                    (table, str(values[0]), int(values[1]), _now()),
                )
                stored += 1
        return stored

    def reset(self) -> None:
        """Remove every stored material row so an import starts from empty state."""

        with self._connection.cursor() as cursor:
            for table in (*_TABLES, "sdd_import_log"):
                cursor.execute(f"DELETE FROM `{table}`")  # noqa: S608 - the table name is a module constant

    # -- reading --------------------------------------------------------------

    def search(self, keywords: Iterable[str], *, limit: int = 8) -> list[Material]:
        """Return the material most relevant to the given keywords.

        The query runs against seekdb, so the returned rows are the rows the
        database actually holds after the most recent import.
        """

        terms = [term.strip() for term in keywords if term.strip()]
        if not terms:
            return []
        like_clause = " OR ".join(["LOWER(body) LIKE %s OR LOWER(title) LIKE %s"] * len(terms))
        term_parameters: list[Any] = []
        for term in terms:
            pattern = f"%{term.lower()}%"
            term_parameters.extend([pattern, pattern])
        statement = (
            "SELECT kind, item_id, title, body, source_path, revision FROM ("  # noqa: S608 - fixed names
            "SELECT 'spec' AS kind, spec_id AS item_id, title, body, source_path, revision FROM `"
            f"{_SPEC_TABLE}` WHERE {like_clause} UNION ALL "
            "SELECT 'code' AS kind, code_id AS item_id, title, body, source_path, revision FROM `"
            f"{_CODE_TABLE}` WHERE {like_clause} UNION ALL "
            "SELECT 'test' AS kind, test_id AS item_id, title, body, source_path, revision FROM `"
            f"{_TEST_TABLE}` WHERE {like_clause}"
            ") AS matched ORDER BY kind, item_id LIMIT %s"
        )
        # The same keyword list is bound once per UNION branch.
        parameters = term_parameters * 3 + [limit]
        with self._connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            rows = cursor.fetchall()
        return [
            Material(
                kind=row[0],
                identifier=row[1],
                title=row[2],
                body=row[3],
                source_path=row[4],
                revision=int(row[5]),
            )
            for row in rows
        ]

    def list_all(self, kind: str) -> list[Material]:
        """Return every stored row of one material kind."""

        table, id_column = {
            "spec": (_SPEC_TABLE, "spec_id"),
            "code": (_CODE_TABLE, "code_id"),
            "test": (_TEST_TABLE, "test_id"),
        }[kind]
        with self._connection.cursor() as cursor:
            columns = f"{id_column}, title, body, source_path, revision"
            select = f"SELECT {columns} FROM `{table}`"  # noqa: S608 - fixed names
            cursor.execute(f"{select} ORDER BY {id_column}")
            rows = cursor.fetchall()
        return [
            Material(kind=kind, identifier=row[0], title=row[1], body=row[2], source_path=row[3], revision=int(row[4]))
            for row in rows
        ]

    def count(self) -> dict[str, int]:
        """Return the number of stored rows per material kind."""

        counts: dict[str, int] = {}
        with self._connection.cursor() as cursor:
            for kind, table in (("spec", _SPEC_TABLE), ("code", _CODE_TABLE), ("test", _TEST_TABLE)):
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")  # noqa: S608 - the table name is a module constant
                counts[kind] = int(cursor.fetchone()[0])
        return counts


def _now() -> str:
    from datetime import datetime

    return datetime.now(tz=UTC).isoformat(timespec="seconds")


__all__ = ["Material", "SeekDBMaterialStore"]
