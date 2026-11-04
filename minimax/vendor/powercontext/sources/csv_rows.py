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

"""Row-level CSV Sources with exact identity and text-evidence projection."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"


class CSVDocumentInput(BaseModel):
    """One complete offline CSV document supplied by bytes."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed CSV row identified by a document-local primary key."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """Captured CSV row that keeps its original columns and values."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the fixed, delimiter-safe identity of one CSV row Source."""

    canonical_document_id = _require_document_id(document_id)
    canonical_primary_key_value = _require_primary_key_value(primary_key_value)
    source_id = (
        f"csv:{quote(canonical_document_id, safe='')}:{quote(canonical_primary_key_value, safe='')}"
    )
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"CSV row Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return source_id


def parse_csv_document(value: CSVDocumentInput, /) -> tuple[CSVRowInput, ...]:
    """Parse and validate one complete CSV document without partial results."""

    document_id = _require_document_id(value.document_id)
    primary_key = _require_primary_key(value.primary_key)

    try:
        text = value.content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid strictly decoded UTF-8") from error

    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError("CSV content must follow strict quoting rules") from error

    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003

    header = records[0]
    if not header:
        raise ValueError("CSV header must not be empty")  # noqa: TRY003

    seen_columns: set[str] = set()
    for column in header:
        if not column or not column.strip():
            raise ValueError("CSV header fields must be non-empty and not blank")  # noqa: TRY003
        if column in seen_columns:
            raise ValueError("CSV header fields must be unique")  # noqa: TRY003
        seen_columns.add(column)

    try:
        primary_key_index = header.index(primary_key)
    except ValueError as error:
        raise ValueError("primary_key must exactly match one CSV header field") from error

    width = len(header)
    columns = tuple(header)
    rows: list[CSVRowInput] = []
    seen_primary_key_values: set[str] = set()

    for record in records[1:]:
        if not record:
            continue
        if len(record) != width:
            raise ValueError("CSV data records must match the header width")  # noqa: TRY003
        primary_key_value = record[primary_key_index]
        _require_primary_key_value(primary_key_value)
        if primary_key_value in seen_primary_key_values:
            raise ValueError("CSV primary key values must be unique within one document")  # noqa: TRY003
        seen_primary_key_values.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )

    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve and read row-level CSV Sources."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row through the standard text-evidence projection."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _require_document_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if value != value.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003
    return value


def _require_primary_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003
    return value


def _require_primary_key_value(value: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("primary key value must be non-empty and not blank")  # noqa: TRY003
    return value


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
