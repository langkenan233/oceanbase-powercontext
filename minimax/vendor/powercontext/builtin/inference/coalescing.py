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

"""Share Embedding work between concurrent callers and reuse successful vectors.

Every text is submitted at most once per in-flight window: a batch is built from the deduplicated
union of the window, so a caller that arrives while another caller's texts are still queued or
still executing joins that work instead of issuing its own request. Each caller keeps its own
ordering, duplicates and usage, and a cancelled caller never cancels the shared batch.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


@dataclass(slots=True)
class _Batch:
    """One underlying request; its usage is charged once per caller that waited on it."""

    usage: InferenceUsage | None = None


@dataclass(slots=True)
class _Pending:
    """One distinct text shared by every caller currently waiting for it."""

    text: str
    future: asyncio.Future[tuple[float, ...]]
    batch: _Batch | None = None


class CoalescingEmbeddingModel:
    """Wrap one Embedding model with cross-call coalescing and a bounded LRU cache."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, int | float) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1_000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, _Pending] = {}
        self._queued: list[_Pending] = []
        self._window: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def profile(self) -> Any:
        """Expose the wrapped model's deployment-fixed profile unchanged."""

        return self._model.profile

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        """Return one vector per input position, reusing cache and shared in-flight work."""

        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        pending = [self._lookup(text) for text in dict.fromkeys(requested)]
        # Shielding keeps a cancelled caller from cancelling the batch every other caller shares.
        settled = await asyncio.gather(*(asyncio.shield(item.future) for item in pending), return_exceptions=True)
        for value in settled:
            if isinstance(value, BaseException):
                raise value
        by_text = {item.text: value for item, value in zip(pending, settled, strict=True)}
        return EmbeddingResult(
            vectors=tuple(by_text[text] for text in requested),
            usage=_total_usage(item.batch for item in pending),
        )

    def _lookup(self, text: str) -> _Pending:
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            settled: asyncio.Future[tuple[float, ...]] = asyncio.get_running_loop().create_future()
            settled.set_result(cached)
            return _Pending(text=text, future=settled)
        existing = self._inflight.get(text)
        if existing is not None:
            return existing
        pending = _Pending(text=text, future=asyncio.get_running_loop().create_future())
        pending.future.add_done_callback(_retrieve_failure)
        self._inflight[text] = pending
        self._queued.append(pending)
        if self._window is None:
            self._window = asyncio.create_task(self._flush_window())
            self._tasks.add(self._window)
            self._window.add_done_callback(self._tasks.discard)
        return pending

    async def _flush_window(self) -> None:
        """Wait for the coalescing window, then send the deduplicated union in bounded batches."""

        if self._coalesce_seconds > 0:
            await asyncio.sleep(self._coalesce_seconds)
        else:
            await asyncio.sleep(0)
        queued, self._queued = self._queued, []
        self._window = None
        for start in range(0, len(queued), self._max_batch_size):
            chunk = tuple(queued[start : start + self._max_batch_size])
            batch = _Batch()
            for item in chunk:
                item.batch = batch
            task = asyncio.create_task(self._run_batch(chunk, batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, chunk: tuple[_Pending, ...], batch: _Batch) -> None:
        texts = tuple(item.text for item in chunk)
        try:
            result = await self._model.embed(texts)
        except BaseException as error:  # noqa: BLE001 - every waiter must observe the same failure
            self._fail(chunk, error)
            return
        vectors = tuple(result.vectors)
        if len(vectors) != len(texts):
            self._fail(chunk, ValueError("embedding vector count does not match the batch"))
            return
        batch.usage = result.usage
        for item, vector in zip(chunk, vectors, strict=True):
            stored = tuple(float(value) for value in vector)
            self._inflight.pop(item.text, None)
            self._remember(item.text, stored)
            if not item.future.done():
                item.future.set_result(stored)

    def _fail(self, chunk: tuple[_Pending, ...], error: BaseException) -> None:
        for item in chunk:
            self._inflight.pop(item.text, None)
            # A failed batch leaves nothing behind, so the same text can be retried.
            item.batch = None
            if not item.future.done():
                item.future.set_exception(error)

    def _remember(self, text: str, vector: tuple[float, ...]) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _retrieve_failure(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _total_usage(batches: Iterable[_Batch | None]) -> InferenceUsage:
    requests = 0
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    for batch in {id(batch): batch for batch in batches if batch is not None}.values():
        usage = batch.usage
        if usage is None:
            continue
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens.append(usage.input_tokens)
        if usage.output_tokens is not None:
            output_tokens.append(usage.output_tokens)
    return InferenceUsage(
        requests=requests,
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
    )


__all__ = ["DEFAULT_CACHE_SIZE", "DEFAULT_COALESCE_MS", "DEFAULT_MAX_BATCH_SIZE", "CoalescingEmbeddingModel"]
