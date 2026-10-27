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

"""Chunked, digest-verified Context Packs and prefix-reusing deltas.

A receiver only needs two wire objects to rebuild a prepared context: the full pack it already
holds and the delta that arrived afterwards. Neither object refers to sender process state, so a
rebuild after serialization returns exactly the bytes a plain ``prepare`` would have sent.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PREFIX = "sha256:"


class ContextPackError(ValueError):
    """A Context Pack or delta violated the wire protocol or the chunking bounds."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete context body split into independently transportable chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build a full pack from already chunked bytes."""

        normalized = _normalize_items(items)
        unsigned = _unsigned_payload(normalized)
        digest = _digest(unsigned)
        return cls(items=normalized, digest=digest, byte_count=_settle_byte_count(unsigned, digest))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk prepared context deterministically, keeping every chunk valid UTF-8."""

        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if content is None:
            return cls.build(())
        if not isinstance(content, str):
            raise ContextPackError("content must be text or None")  # noqa: TRY003
        return cls.build(_split_utf8(content, chunk_bytes))

    def to_bytes(self) -> bytes:
        """Serialize the complete pack, including its digest and settled byte count."""

        return _canonical({**_unsigned_payload(self.items), "digest": self.digest, "byte_count": self.byte_count})

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        """Decode and verify a complete pack."""

        payload = _decode_object(data, _PACK_FIELDS, "pack")
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("unexpected context pack schema")  # noqa: TRY003
        items = _decode_items(payload["items"], "items")
        digest = payload["digest"]
        if not isinstance(digest, str):
            raise ContextPackError("context pack digest must be text")  # noqa: TRY003
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count != len(data):
            raise ContextPackError("context pack byte_count does not match its payload")  # noqa: TRY003
        if _digest(_unsigned_payload(items)) != digest:
            raise ContextPackError("context pack digest does not match its items")  # noqa: TRY003
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """The retained prefix of a base pack plus the target suffix that must follow it."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Carry the longest shared chunk prefix and the remaining target chunks."""

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("a delta needs two context packs")  # noqa: TRY003
        prefix = 0
        shared = min(len(base.items), len(target.items))
        while prefix < shared and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        """The exact wire size of this delta, including digest and protocol fields."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the delta; ``byte_count`` is derived and never written."""

        return _canonical(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": [_encode_item(item) for item in self.appended],
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        """Decode and validate a delta."""

        payload = _decode_object(data, _DELTA_FIELDS, "delta")
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("unexpected context pack delta schema")  # noqa: TRY003
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not isinstance(base_digest, str) or not isinstance(target_digest, str):
            raise ContextPackError("context pack delta digests must be text")  # noqa: TRY003
        prefix_items = payload["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("context pack delta prefix_items must be a non-negative integer")  # noqa: TRY003
        appended = _decode_items(payload["appended"], "appended")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from a held base and a received delta."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("a rebuild needs a base pack and a delta")  # noqa: TRY003
    if base.digest != delta.base_digest:
        raise ContextPackError("delta was built against a different base pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base chunk count")  # noqa: TRY003
    target = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("delta does not rebuild its declared target digest")  # noqa: TRY003
    return target


def _split_utf8(content: str, chunk_bytes: int) -> tuple[bytes, ...]:
    chunks: list[bytes] = []
    pending: list[str] = []
    size = 0
    for char in content:
        encoded = char.encode("utf-8")
        if len(encoded) > chunk_bytes:
            raise ContextPackError("chunk_bytes is too small for one UTF-8 character")  # noqa: TRY003
        if size + len(encoded) > chunk_bytes:
            chunks.append("".join(pending).encode("utf-8"))
            pending.clear()
            size = 0
        pending.append(char)
        size += len(encoded)
    if pending:
        chunks.append("".join(pending).encode("utf-8"))
    return tuple(chunks)


def _normalize_items(items: Iterable[bytes]) -> tuple[bytes, ...]:
    if isinstance(items, (bytes, bytearray, memoryview, str)):
        raise ContextPackError("items must be an iterable of byte chunks")  # noqa: TRY003
    normalized: list[bytes] = []
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("each chunk must be bytes")  # noqa: TRY003
        normalized.append(item)
    return tuple(normalized)


def _unsigned_payload(items: tuple[bytes, ...]) -> dict[str, Any]:
    return {"schema": CONTEXT_PACK_SCHEMA, "items": [_encode_item(item) for item in items]}


def _encode_item(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _decode_items(value: object, field: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{field} must be a list of Base64 strings")  # noqa: TRY003
    decoded: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError(f"{field} must be a list of Base64 strings")  # noqa: TRY003
        try:
            decoded.append(base64.b64decode(entry, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError(f"{field} contains an invalid Base64 chunk") from error  # noqa: TRY003
    return tuple(decoded)


def _decode_object(data: bytes, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ContextPackError(f"{label} payload must be bytes")  # noqa: TRY003
    try:
        payload = json.loads(bytes(data))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError(f"{label} payload is not valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict):
        raise ContextPackError(f"{label} payload must be a JSON object")  # noqa: TRY003
    if set(payload) != fields:
        raise ContextPackError(f"{label} payload must contain exactly {sorted(fields)}")  # noqa: TRY003
    return payload


def _digest(unsigned: Mapping[str, Any]) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _settle_byte_count(unsigned: Mapping[str, Any], digest: str) -> int:
    """Find the byte count that equals the length of the payload carrying it."""

    size = 0
    for _ in range(8):
        candidate = len(_canonical({**unsigned, "digest": digest, "byte_count": size}))
        if candidate == size:
            return size
        size = candidate
    raise ContextPackError("context pack byte_count did not settle")  # noqa: TRY003


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
