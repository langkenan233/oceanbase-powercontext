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

"""Durable evidence-graph nodes for Source lifecycle propagation.

One row keeps the registered binding (reference payload, parents and policy) and
the state derived from the current Source availability. Rows never change their
binding: only ``available`` and the per-Source event counters advance.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE


class StoredSourceLifecycleNode:
    """One registered graph node as persisted for a Scope."""

    __slots__ = (
        "artifact_id",
        "available",
        "binding_hash",
        "entry_id",
        "event_hash",
        "family",
        "lifecycle_revision",
        "node_id",
        "ordinal",
        "parents",
        "payload",
        "policy",
        "scope_id",
        "sequence",
    )

    def __init__(
        self,
        *,
        scope_id: str,
        node_id: str,
        family: str,
        ordinal: int,
        parents: tuple[str, ...],
        policy: str,
        payload: dict[str, Any],
        binding_hash: str,
        available: bool,
        lifecycle_revision: int,
        sequence: int,
        event_hash: str | None,
        artifact_id: str | None,
        entry_id: str | None,
    ) -> None:
        self.scope_id = scope_id
        self.node_id = node_id
        self.family = family
        self.ordinal = ordinal
        self.parents = parents
        self.policy = policy
        self.payload = payload
        self.binding_hash = binding_hash
        self.available = available
        self.lifecycle_revision = lifecycle_revision
        self.sequence = sequence
        self.event_hash = event_hash
        self.artifact_id = artifact_id
        self.entry_id = entry_id

    def replace(self, **changes: Any) -> StoredSourceLifecycleNode:
        values = {name: getattr(self, name) for name in self.__slots__}
        values.update(changes)
        return StoredSourceLifecycleNode(**values)


def _dump_parents(parents: tuple[str, ...]) -> str:
    return json.dumps(list(parents), ensure_ascii=False, separators=(",", ":"))


def _dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_row(row: Any) -> StoredSourceLifecycleNode:
    mapping = dict(row)
    return StoredSourceLifecycleNode(
        scope_id=str(mapping["scope_id"]),
        node_id=str(mapping["node_id"]),
        family=str(mapping["family"]),
        ordinal=int(mapping["ordinal"]),
        parents=tuple(json.loads(mapping["parents"])),
        policy=str(mapping["policy"]),
        payload=json.loads(mapping["payload"]),
        binding_hash=str(mapping["binding_hash"]),
        available=bool(mapping["available"]),
        lifecycle_revision=int(mapping["lifecycle_revision"]),
        sequence=int(mapping["sequence"]),
        event_hash=None if mapping["event_hash"] is None else str(mapping["event_hash"]),
        artifact_id=None if mapping["artifact_id"] is None else str(mapping["artifact_id"]),
        entry_id=None if mapping["entry_id"] is None else str(mapping["entry_id"]),
    )


class SourceLifecycleRepository:
    """Read and write graph nodes inside a caller-owned short transaction."""

    async def list_nodes(self, connection: AsyncConnection, scope_id: str, /) -> tuple[StoredSourceLifecycleNode, ...]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
                .order_by(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.ordinal,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id,
                )
            )
        ).mappings()
        return tuple(_load_row(row) for row in rows)

    async def get_node(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
    ) -> StoredSourceLifecycleNode | None:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _load_row(row)

    async def insert_node(self, connection: AsyncConnection, node: StoredSourceLifecycleNode, /) -> None:
        await connection.execute(
            insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                scope_id=node.scope_id,
                node_id=node.node_id,
                family=node.family,
                ordinal=node.ordinal,
                parents=_dump_parents(node.parents),
                policy=node.policy,
                payload=_dump_payload(node.payload),
                binding_hash=node.binding_hash,
                available=node.available,
                lifecycle_revision=node.lifecycle_revision,
                sequence=node.sequence,
                event_hash=node.event_hash,
                artifact_id=node.artifact_id,
                entry_id=node.entry_id,
            )
        )

    async def update_node(
        self,
        connection: AsyncConnection,
        node: StoredSourceLifecycleNode,
        /,
        **changes: Any,
    ) -> None:
        if not changes:
            return
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == node.scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node.node_id,
            )
            .values(**changes)
        )

    async def suppressed_topic_ids(
        self,
        connection: AsyncConnection,
        scope_id: str,
        /,
    ) -> frozenset[str]:
        """Return Topic artifacts whose registered evidence is no longer available."""

        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE.c.artifact_id).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.family == "topic",
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available.is_(False),
                    SOURCE_LIFECYCLE_NODES_TABLE.c.artifact_id.is_not(None),
                )
            )
        ).scalars()
        return frozenset(str(value) for value in rows)
