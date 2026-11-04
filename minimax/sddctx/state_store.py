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

"""Task state persistence in PowerContext.

The module keeps the state of one development task in a PowerContext Memory
entry. The entry records what is done, what is still open and which
verification results already exist. It is written while the task runs and read
back after the module restarts, so a later run continues from the recorded
progress instead of starting over.

The interaction uses the public PowerContext HTTP contract:

* ``POST /v1/scopes``                 creates the working Scope
* ``POST /v1/memory/remember``        writes the task state
* ``POST /v1/memory/entries/list``    reads the stored task state back
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from sddctx.config import Settings

STATE_KIND = "sdd_task_state"

# The PowerContext Memory contract rejects an entry text longer than 8192 UTF-8 bytes.
MAX_TEXT_BYTES = 8192


@dataclass
class TaskState:
    """The persisted state of one development task."""

    task_id: str
    objective: str
    completed: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    context_digest: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the state."""

        return asdict(self)

    def to_text(self) -> str:
        """Return the state encoded as the Memory entry text."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_text(cls, text: str) -> TaskState:
        """Rebuild the state from a stored Memory entry text."""

        payload = json.loads(text)
        return cls(
            task_id=payload.get("task_id", ""),
            objective=payload.get("objective", ""),
            completed=list(payload.get("completed", [])),
            todo=list(payload.get("todo", [])),
            verification=list(payload.get("verification", [])),
            context_digest=payload.get("context_digest", ""),
            updated_at=payload.get("updated_at", ""),
        )


class PowerContextStateStore:
    """Save and load the task state through the PowerContext HTTP API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scope_id = settings.powercontext_scope_id
        self._token = settings.powercontext_token
        self._base_url = settings.powercontext_url.rstrip("/")

    # -- scope ----------------------------------------------------------------

    def ensure_scope(self) -> str:
        """Return the configured Scope, creating it when the setting is empty."""

        if self._scope_id:
            return self._scope_id
        response = self._request(
            "POST",
            "/v1/scopes",
            json={
                "title": "SDD context module",
                "summary": "Engineering context for the specification driven development task.",
                "idempotency_key": "sdd-context-module-scope",
            },
        )
        self._scope_id = response["scope_id"]
        return self._scope_id

    @property
    def scope_id(self) -> str:
        """Return the Scope currently in use."""

        return self._scope_id

    # -- state ----------------------------------------------------------------

    def save_state(self, state: TaskState) -> dict[str, Any]:
        """Write the task state as a Memory entry and return the server answer."""

        scope_id = self.ensure_scope()
        state.updated_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
        text = state.to_text()
        size = len(text.encode("utf-8"))
        if size > MAX_TEXT_BYTES:
            raise RuntimeError(  # noqa: TRY003
                f"task state is {size} bytes, the Memory contract allows {MAX_TEXT_BYTES}; "
                "shorten the completed, todo or verification lists"
            )
        return self._request(
            "POST",
            "/v1/memory/remember",
            json={
                "scope_id": scope_id,
                "kind": STATE_KIND,
                "text": text,
                "reason": "SDD context module task progress",
            },
        )

    def load_state(self, task_id: str) -> TaskState | None:
        """Read the stored task state, or ``None`` when the task is unknown."""

        scope_id = self.ensure_scope()
        response = self._request(
            "POST",
            "/v1/memory/entries/list",
            json={"scope_id": scope_id, "include_inactive": False},
        )
        entries = response.get("entries", []) if isinstance(response, dict) else []
        newest: TaskState | None = None
        for entry in entries:
            if entry.get("kind") != STATE_KIND:
                continue
            try:
                state = TaskState.from_text(entry.get("text", ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if state.task_id != task_id:
                continue
            if newest is None or state.updated_at >= newest.updated_at:
                newest = state
        return newest

    # -- transport ------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(  # noqa: TRY003
                f"PowerContext request {method} {path} failed with {response.status_code}: {response.text}"
            )
        if not response.content:
            return {}
        return response.json()

    def ping(self) -> bool:
        """Return whether the PowerContext Server answers."""

        try:
            self._request("GET", "/health/live")
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError):
            return False
        return True


__all__ = ["STATE_KIND", "PowerContextStateStore", "TaskState"]
