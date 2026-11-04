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

"""Pi session branch Sources with resumable, immutable event capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_CHECKPOINT_VERSION = 1

_VISIBLE_EVENT_TYPES = frozenset({"message", "compaction", "branch_summary", "custom_message"})
_UNKNOWN = "unknown"


class PiSessionInput(BaseModel):
    """One complete Pi v3 session archive supplied by bytes."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its original record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured Pi event that keeps the complete original record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_name(session_id: str, event_id: str, /) -> str:
    """Build the fixed identity of one Pi event from session and event ID."""

    canonical_session_id = _require_text("session_id", session_id)
    canonical_event_id = _require_text("event_id", event_id)
    name = f"pi:{quote(canonical_session_id, safe='')}:{quote(canonical_event_id, safe='')}"
    if len(name) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"Pi event Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return name


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse the selected branch of one Pi session without partial results."""

    return _parse_session(value.content, value.leaf_id)[1]


class PiEventSourceAdapter:
    """Resolve and read immutable Pi event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_name(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi event through the standard text-evidence projection."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Import only the unseen visible events of one selected session branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self._path.read_bytes)
        session_id, events = _parse_session(content, self._leaf_id)
        imported = _imported_digests(session.checkpoint, session_id)
        digests = {event.event_id: _record_digest(event.record) for event in events}
        for event_id, digest in digests.items():
            known = imported.get(event_id)
            if known is not None and known != digest:
                raise ValueError(  # noqa: TRY003
                    f"Pi event {event_id!r} was modified after it was imported"
                )
        for event in events:
            if event.event_id in imported:
                continue
            await session.submit(
                pi_event_source_name(event.session_id, event.event_id),
                PI_EVENT_SOURCE_NAME,
                event,
            )
        merged = {**imported, **digests}
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={
                "version": PI_CHECKPOINT_VERSION,
                "session_id": session_id,
                "events": dict(sorted(merged.items())),
            },
        )


def _parse_session(content: bytes, leaf_id: str | None, /) -> tuple[str, tuple[PiEventInput, ...]]:
    rows = _load_records(_decode(content))
    session_id = _session_id(rows[0])
    nodes = _build_graph(rows[1:], session_id)
    selected = nodes[-1]["id"] if leaf_id is None else _leaf_id(leaf_id)
    events: list[PiEventInput] = []
    for node in _ancestor_chain(nodes, selected, session_id):
        event = _visible_event(session_id, node)
        if event is not None:
            events.append(event)
    return session_id, tuple(events)


def _decode(content: bytes, /) -> str:
    if not isinstance(content, bytes):
        raise ValueError("Pi session content must be bytes")  # noqa: TRY003, TRY004
    try:
        return content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid strictly decoded UTF-8") from error  # noqa: TRY003


def _load_records(text: str, /) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Pi session lines must be valid JSON") from error  # noqa: TRY003
        if not isinstance(record, dict):
            raise ValueError("Pi session lines must be JSON objects")  # noqa: TRY003, TRY004
        records.append(record)
    if not records:
        raise ValueError("Pi session must contain a session header")  # noqa: TRY003
    return records


def _session_id(header: dict[str, JsonValue], /) -> str:
    if header.get("type") != "session":
        raise ValueError("Pi session must start with a session header")  # noqa: TRY003
    if header.get("version") != 3:
        raise ValueError("Pi session header must declare version 3")  # noqa: TRY003
    return _require_text("session id", header.get("id"))


def _build_graph(records: list[dict[str, JsonValue]], session_id: str, /) -> list[dict[str, JsonValue]]:
    if not records:
        raise ValueError("Pi session must contain at least one node after the header")  # noqa: TRY003
    known = {session_id}
    nodes: list[dict[str, JsonValue]] = []
    for record in records:
        event_id = _require_text("event id", record.get("id"))
        if event_id in known:
            raise ValueError(f"Pi session event id {event_id!r} is duplicated")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None:
            parent_id = _require_text("parentId", parent_id)
            if parent_id not in known:
                raise ValueError(  # noqa: TRY003
                    f"Pi session event {event_id!r} references an unknown parent {parent_id!r}"
                )
        known.add(event_id)
        nodes.append(record)
    return nodes


def _leaf_id(value: object, /) -> str:
    return _require_text("leaf_id", value)


def _ancestor_chain(nodes: list[dict[str, JsonValue]], leaf_id: str, session_id: str, /) -> list[dict[str, JsonValue]]:
    by_id = {node["id"]: node for node in nodes}
    if leaf_id not in by_id:
        raise ValueError(f"Pi session leaf {leaf_id!r} does not exist")  # noqa: TRY003
    chain: list[dict[str, JsonValue]] = []
    visited: set[str] = set()
    current: str | None = leaf_id
    while current is not None and current != session_id:
        node = by_id.get(current)
        if node is None or current in visited:
            raise ValueError(f"Pi session branch from {leaf_id!r} is not connected")  # noqa: TRY003
        visited.add(current)
        chain.append(node)
        parent = node.get("parentId")
        current = parent if isinstance(parent, str) else None
    chain.reverse()
    return chain


def _visible_event(session_id: str, record: dict[str, JsonValue], /) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str) or event_type not in _VISIBLE_EVENT_TYPES:
        return None
    role, body = _event_text(event_type, record)
    if role is None or not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record.get("parentId") if isinstance(record.get("parentId"), str) else None,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _event_text(event_type: str, record: dict[str, JsonValue], /) -> tuple[str | None, str]:
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None, ""
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            return None, ""
        return role, _message_body(role, message)
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        return event_type, summary if isinstance(summary, str) else ""
    content = record.get("content")
    return "custom", _content_text(content)


def _message_body(role: str, message: dict[str, JsonValue], /) -> str:
    if role == "bashExecution":
        command = _as_text(message.get("command"))
        output = _as_text(message.get("output"))
        body = f"$ {command}".rstrip()
        return f"{body}\n{output}" if output else body
    if role == "toolResult":
        name = _as_text(message.get("toolName")) or _UNKNOWN
        call_id = _as_text(message.get("toolCallId")) or _UNKNOWN
        text = _content_text(message.get("content"))
        return f"[tool_result {name} {call_id}] {text}"
    return _content_text(message.get("content"))


def _content_text(content: object, /) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get("type")
            if kind == "text":
                parts.append(_as_text(block.get("text")))
            elif kind == "toolCall":
                parts.append(_tool_call_text(block))
    return "\n".join(parts)


def _tool_call_text(block: dict[str, JsonValue], /) -> str:
    name = _as_text(block.get("name")) or _UNKNOWN
    call_id = _as_text(block.get("id")) or _UNKNOWN
    return f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments'))}"


def _compact_json(value: object, /) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _as_text(value: object, /) -> str:
    return value if isinstance(value, str) else ""


def _require_text(field: str, value: object, /) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Pi session {field} must be a non-empty string")  # noqa: TRY003
    if value != value.strip():
        raise ValueError(f"Pi session {field} must not have surrounding whitespace")  # noqa: TRY003
    return value


def _record_digest(record: dict[str, JsonValue], /) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _imported_digests(checkpoint: JsonValue | None, session_id: str, /) -> dict[str, str]:
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != PI_CHECKPOINT_VERSION:
        return {}
    if checkpoint.get("session_id") != session_id:
        return {}
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        return {}
    return {key: value for key, value in events.items() if isinstance(key, str) and isinstance(value, str)}


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_name",
]
