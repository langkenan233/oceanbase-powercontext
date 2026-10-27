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

"""Cross-encoder reranking behind a batched HTTP scoring service."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score a coarse candidate pool through one bounded-batch HTTP service.

    Every candidate keeps its position in the **complete** input pool: batches are only a
    transport detail, so a rank sent in the second batch competes against the first batch in
    one global selection. The service never sees or rewrites Memory identity.
    """

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("rerank batch_size must be at least one")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once and select a globally ordered prefix."""

        _validate_bounds(len(candidates), limit)
        scored: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(range(start + 1, start + 1 + len(batch)))
            response = await self._client.post(self._endpoint, json=_request_body(query, ranks, batch))
            response.raise_for_status()
            requests += 1
            scored.update(_parse_scores(response.content, ranks))
        ordered = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _score in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _request_body(query: str, ranks: tuple[int, ...], batch: tuple[MemoryHit, ...]) -> dict[str, Any]:
    return {
        "query": query,
        "candidates": [
            {"rank": rank, "text": candidate.text}
            for rank, candidate in zip(ranks, batch, strict=True)
        ],
    }


def _parse_scores(content: bytes, expected_ranks: tuple[int, ...]) -> dict[int, float]:
    """Validate one batch response against the exact ranks this batch sent."""

    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("rerank response is not valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly the scores field")  # noqa: TRY003
    entries = payload["scores"]
    if not isinstance(entries, list):
        raise ValueError("rerank scores must be a list")  # noqa: TRY003
    parsed: dict[int, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"rank", "score"}:
            raise ValueError("each rerank score must contain exactly rank and score")  # noqa: TRY003
        rank = entry["rank"]
        score = entry["score"]
        if type(rank) is not int or rank not in expected_ranks:
            raise ValueError("rerank score rank is outside the ranks this batch sent")  # noqa: TRY003
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        if rank in parsed:
            raise ValueError("rerank score rank is duplicated")  # noqa: TRY003
        parsed[rank] = float(score)
    if set(parsed) != set(expected_ranks):
        raise ValueError("rerank scores must cover every rank this batch sent")  # noqa: TRY003
    return parsed


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
