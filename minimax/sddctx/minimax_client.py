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

"""MiniMax chat client used to produce the code change.

MiniMax publishes an OpenAI compatible chat completion endpoint, so the
request below is a standard ``/chat/completions`` call with an ``Authorization``
header. The client returns the raw completion together with the identifiers a
reviewer needs to match the call against the recorded development trace.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from sddctx.config import Settings


@dataclass
class MiniMaxCall:
    """One recorded MiniMax chat completion call."""

    model: str
    base_url: str
    prompt: str
    response: str
    finish_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the call."""

        return {
            "model": self.model,
            "base_url": self.base_url,
            "prompt": self.prompt,
            "response": self.response,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
        }


class MiniMaxError(RuntimeError):
    """Raised when the MiniMax endpoint cannot serve the request."""


class MiniMaxClient:
    """Call the MiniMax chat completion endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.minimax_base_url.rstrip("/")
        self._api_key = settings.minimax_api_key
        self._model = settings.minimax_model

    @property
    def available(self) -> bool:
        """Return whether an API key is configured."""

        return bool(self._api_key)

    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0) -> MiniMaxCall:
        """Send one chat completion request and return the recorded call."""

        if not self._api_key:
            raise MiniMaxError("MINIMAX_API_KEY is not configured")  # noqa: TRY003
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        with httpx.Client(timeout=180.0) as client:
            response = client.post(f"{self._base_url}/chat/completions", headers=headers, json=payload)
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise MiniMaxError(  # noqa: TRY003
                f"MiniMax request failed with {response.status_code}: {response.text}"
            )
        body = response.json()
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason", "")
        except (KeyError, IndexError, TypeError) as error:
            raise MiniMaxError(  # noqa: TRY003
                f"unexpected MiniMax response shape: {json.dumps(body)[:400]}"
            ) from error
        return MiniMaxCall(
            model=body.get("model", self._model),
            base_url=self._base_url,
            prompt=prompt,
            response=content,
            finish_reason=finish_reason,
            usage=body.get("usage", {}) or {},
            latency_ms=latency_ms,
        )

    def complete_json(self, prompt: str, *, system: str = "", temperature: float = 0.0) -> tuple[Any, MiniMaxCall]:
        """Ask for JSON and return the parsed value together with the recorded call."""

        call = self.complete(prompt, system=system, temperature=temperature)
        text = call.response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[: -len("```")]
            text = text.strip()
        try:
            return json.loads(text), call
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1]), call
            raise


__all__ = ["MiniMaxCall", "MiniMaxClient", "MiniMaxError"]
