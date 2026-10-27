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

"""Propagate Source availability through a registered evidence graph.

Callers register Sources together with the Memory entries and Topic revisions
that depend on them, then publish one lifecycle event per Source. Availability
is recomputed from the Sources down through each node's ``all``/``any`` policy:
Memory entries leave or rejoin the active projections, Topic revisions leave or
rejoin search, and every registered reference stays readable for generation and
audit purposes.

The graph itself never changes after registration, so one scope has one writer.
Availability is materialised in graph order, which makes a retry after a failed
write complete the remaining nodes instead of starting over.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.context import BuiltinArtifacts, BuiltinSources
from powercontext.builtin.persistence.source_lifecycle import (
    SourceLifecycleRepository,
    StoredSourceLifecycleNode,
)
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.runtime.protocols import BuiltinTriggers
from powercontext.builtin.sources import validate_scope_id
from powercontext.builtin.statistics import ModelUsagePurpose
from powercontext.context import PowerContext
from powercontext.limits import MAX_ARTIFACT_ID_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.persistence.database import AsyncDatabase
    from powercontext.builtin.runtime.application import BuiltinRuntime

_PURPOSES = ("generation", "audit")
_FAMILIES = ("source", "memory", "topic")
_POLICIES = ("all", "any")
_REASON = "source lifecycle"


class SourceLifecycleError(ValueError):
    """A lifecycle request that cannot be applied to the registered graph."""


class SourceLifecycleEvent(BaseModel):
    """One Source availability change identified by revision and sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


def _event_hash(event: SourceLifecycleEvent) -> str:
    body = json.dumps(
        [event.node_id, event.revision, event.sequence, event.available, event.reason],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _binding_hash(
    family: str,
    payload: Mapping[str, Any],
    parents: Sequence[str],
    policy: str,
) -> str:
    body = json.dumps(
        {
            "family": family,
            "payload": payload,
            "parents": list(parents),
            "policy": policy,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _policy_state(support: Sequence[bool], policy: str) -> bool:
    if not support:
        return False
    return all(support) if policy == "all" else any(support)


class SourceLifecycleService:
    """Register evidence nodes and apply Source lifecycle events in one Scope."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        scope_id: str,
        /,
        *,
        database: AsyncDatabase,
        repository: SourceLifecycleRepository,
    ) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)
        self._database = database
        self._repository = repository

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        /,
        *,
        revision: int,
    ) -> None:
        """Bind one captured Source and its lifecycle version to a node id."""

        self._require_node_id(node_id)
        self._require_positive(revision, "revision")
        if type(source_ref) is not SourceRef:
            raise SourceLifecycleError("source-ref")  # noqa: TRY004
        await self._require_readable_source(source_ref)
        await self._register(
            node_id,
            family="source",
            payload=source_ref.model_dump(mode="json"),
            parents=(),
            policy="none",
            lifecycle_revision=revision,
            artifact_id=None,
            entry_id=None,
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        """Bind one Memory entry version to the nodes that must support it."""

        self._require_node_id(node_id)
        if type(citation) is not MemoryCitation:
            raise SourceLifecycleError("memory-citation")  # noqa: TRY004
        selected_policy = self._require_policy(policy)
        selected_parents = self._require_parents(parents)
        await self._require_readable_memory(citation)
        await self._register(
            node_id,
            family="memory",
            payload=citation.model_dump(mode="json"),
            parents=selected_parents,
            policy=selected_policy,
            lifecycle_revision=0,
            artifact_id=citation.memory_ref.artifact_id,
            entry_id=citation.entry_id,
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        """Bind one exact Topic Memory revision to the nodes that support it."""

        self._require_node_id(node_id)
        if type(artifact_ref) is not ArtifactRef:
            raise SourceLifecycleError("topic-ref")  # noqa: TRY004
        if artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("topic-family")
        selected_policy = self._require_policy(policy)
        selected_parents = self._require_parents(parents)
        await self._require_readable_topic(artifact_ref)
        await self._register(
            node_id,
            family="topic",
            payload=artifact_ref.model_dump(mode="json"),
            parents=selected_parents,
            policy=selected_policy,
            lifecycle_revision=0,
            artifact_id=artifact_ref.artifact_id,
            entry_id=None,
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one event and return every currently available node id."""

        if type(event) is not SourceLifecycleEvent:
            raise SourceLifecycleError("event")  # noqa: TRY004
        async with self._runtime._context(
            self.scope_id,
            embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING,
        ) as context, self._runtime._locked(self.scope_id):
            nodes = await self._load()
            source = nodes.get(event.node_id)
            if source is None or source.family != "source":
                raise SourceLifecycleError("source")
            if event.revision != source.lifecycle_revision:
                raise SourceLifecycleError("revision")
            digest = _event_hash(event)
            if event.sequence == source.sequence:
                if source.event_hash == digest:
                    return _available_ids(nodes)
                raise SourceLifecycleError("sequence")
            if event.sequence < source.sequence:
                raise SourceLifecycleError("sequence")
            desired = _propagate(nodes, source.node_id, event.available)
            ordered = sorted(nodes.values(), key=lambda item: item.ordinal)
            for node in ordered:
                if node.family == "memory":
                    await self._set_memory_state(context, node.artifact_id, node.entry_id, desired[node.node_id])
            await self._persist(ordered, desired, source=source, event=event, digest=digest)
        return sorted(name for name, active in desired.items() if active)

    async def resolve(self, node_id: str, /, *, purpose: str) -> Any:
        """Return the registered reference for generation or audit purposes."""

        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose")
        nodes = await self._load()
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable")
        return _decode_payload(node)

    async def active_nodes(self) -> list[str]:
        """Return every available node id in this Scope."""

        return _available_ids(await self._load())

    async def _register(
        self,
        node_id: str,
        *,
        family: str,
        payload: Mapping[str, Any],
        parents: tuple[str, ...],
        policy: str,
        lifecycle_revision: int,
        artifact_id: str | None,
        entry_id: str | None,
    ) -> None:
        binding = _binding_hash(family, payload, parents, policy)
        async with self._runtime._context(
            self.scope_id,
            embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING,
        ) as context, self._runtime._locked(self.scope_id):
            nodes = await self._load()
            existing = nodes.get(node_id)
            if existing is not None:
                if existing.binding_hash == binding:
                    return
                raise SourceLifecycleError("binding")
            if family == "source":
                available = True
            else:
                self._require_dependencies(node_id, parents, nodes)
                available = _policy_state([nodes[parent].available for parent in parents], policy)
            if family == "memory" and not available:
                await self._set_memory_state(context, artifact_id, entry_id, available)
            node = StoredSourceLifecycleNode(
                scope_id=self.scope_id,
                node_id=node_id,
                family=family,
                ordinal=max((item.ordinal for item in nodes.values()), default=-1) + 1,
                parents=parents,
                policy=policy,
                payload=dict(payload),
                binding_hash=binding,
                available=available,
                lifecycle_revision=lifecycle_revision,
                sequence=0,
                event_hash=None,
                artifact_id=artifact_id,
                entry_id=entry_id,
            )
            async with self._database.transaction() as connection:
                await self._repository.insert_node(connection, node)

    async def _set_memory_state(
        self,
        context: PowerContext[BuiltinSources, BuiltinArtifacts, BuiltinTriggers],
        artifact_id: str | None,
        entry_id: str | None,
        available: bool,
    ) -> None:
        if artifact_id is None or entry_id is None:
            raise SourceLifecycleError("memory-binding")
        service = context.artifacts.memory
        current = await service.head(artifact_id)
        item = next((entry for entry in current.content.manifest.entries if entry.entry_id == entry_id), None)
        if item is None:
            raise SourceLifecycleError("memory-entry")
        if (item.state == "active") is available:
            return
        entry = next((entry for entry in await service.entries(current) if entry.entry_id == entry_id), None)
        if entry is None:
            raise SourceLifecycleError("memory-entry")
        if available:
            await service.reactivate(current, entries=(entry,), reason=_REASON)
        else:
            await service.forget(current, entries=(entry,), reason=_REASON)

    async def _persist(
        self,
        ordered: Sequence[StoredSourceLifecycleNode],
        desired: Mapping[str, bool],
        *,
        source: StoredSourceLifecycleNode,
        event: SourceLifecycleEvent,
        digest: str,
    ) -> None:
        async with self._database.transaction() as connection:
            for node in ordered:
                changes: dict[str, Any] = {}
                if node.available != desired[node.node_id]:
                    changes["available"] = desired[node.node_id]
                if node.node_id == source.node_id:
                    changes.update(
                        available=event.available,
                        sequence=event.sequence,
                        event_hash=digest,
                    )
                if changes:
                    await self._repository.update_node(connection, node, **changes)

    async def _load(self) -> dict[str, StoredSourceLifecycleNode]:
        async with self._database.transaction() as connection:
            rows = await self._repository.list_nodes(connection, self.scope_id)
        return {row.node_id: row for row in rows}

    def _require_node_id(self, node_id: object) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node-id")  # noqa: TRY004
        if len(node_id) > MAX_ARTIFACT_ID_LENGTH:
            raise SourceLifecycleError("node-id")

    def _require_positive(self, value: object, field: str) -> None:
        if type(value) is not int or value <= 0:
            raise SourceLifecycleError(field)  # noqa: TRY004

    def _require_policy(self, policy: object) -> str:
        if policy not in _POLICIES:
            raise SourceLifecycleError("policy")  # noqa: TRY004
        return str(policy)

    def _require_parents(self, parents: object) -> tuple[str, ...]:
        if isinstance(parents, str) or not isinstance(parents, Sequence):
            raise SourceLifecycleError("parents")  # noqa: TRY004
        selected = tuple(parents)
        if not selected:
            raise SourceLifecycleError("parents")
        for parent in selected:
            if not isinstance(parent, str) or not parent.strip():
                raise SourceLifecycleError("parents")  # noqa: TRY004
        if len(set(selected)) != len(selected):
            raise SourceLifecycleError("parents")
        return selected

    def _require_dependencies(
        self,
        node_id: str,
        parents: Sequence[str],
        nodes: Mapping[str, StoredSourceLifecycleNode],
    ) -> None:
        for parent in parents:
            if parent == node_id:
                raise SourceLifecycleError("cycle")
            if parent not in nodes:
                raise SourceLifecycleError("parent")

    async def _require_readable_source(self, source_ref: SourceRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type,
                source_ref.source_id,
            )
        except Exception as error:  # noqa: BLE001 - any reader failure means it is not readable
            raise SourceLifecycleError("source-unreadable") from error

    async def _require_readable_memory(self, citation: MemoryCitation) -> None:
        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:  # noqa: BLE001 - any reader failure means it is not readable
            raise SourceLifecycleError("memory-unreadable") from error

    async def _require_readable_topic(self, artifact_ref: ArtifactRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family,
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except Exception as error:  # noqa: BLE001 - any reader failure means it is not readable
            raise SourceLifecycleError("topic-unreadable") from error


def _decode_payload(node: StoredSourceLifecycleNode) -> Any:
    if node.family == "source":
        return SourceRef.model_validate(node.payload)
    if node.family == "memory":
        return MemoryCitation.model_validate(node.payload)
    return ArtifactRef.model_validate(node.payload)


def _available_ids(nodes: Mapping[str, StoredSourceLifecycleNode]) -> list[str]:
    return sorted(name for name, node in nodes.items() if node.available)


def _propagate(
    nodes: Mapping[str, StoredSourceLifecycleNode],
    source_id: str,
    available: bool,
) -> dict[str, bool]:
    state: dict[str, bool] = {}
    for node in sorted(nodes.values(), key=lambda item: item.ordinal):
        if node.family == "source":
            state[node.node_id] = available if node.node_id == source_id else node.available
        else:
            state[node.node_id] = _policy_state([state[parent] for parent in node.parents], node.policy)
    return state


class SourceLifecycleApplication:
    """Select the lifecycle service for one Scope."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        /,
        *,
        database: AsyncDatabase | None,
        repository: SourceLifecycleRepository,
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._repository = repository

    def for_scope(self, scope_id: str, /) -> SourceLifecycleService:
        if self._database is None:
            raise SourceLifecycleError("lifecycle")  # noqa: TRY003
        return SourceLifecycleService(
            self._runtime,
            scope_id,
            database=self._database,
            repository=self._repository,
        )
