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

"""Runtime configuration for the SDD context module.

Every value can be supplied through an environment file so that a reviewer can
run the module from a clean checkout without editing source code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = "minimax/sddctx.env"


def _read_env_file(path: Path) -> dict[str, str]:
    """Return the KEY=VALUE pairs declared in a simple environment file."""

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one context module run."""

    seekdb_host: str
    seekdb_port: int
    seekdb_user: str
    seekdb_password: str
    seekdb_database: str
    powercontext_url: str
    powercontext_token: str
    powercontext_scope_id: str
    minimax_base_url: str
    minimax_api_key: str
    minimax_model: str
    repository_root: Path
    workspace_dir: Path

    @property
    def seekdb_connection_arguments(self) -> dict[str, object]:
        """Return the keyword arguments used to open a seekdb connection."""

        return {
            "host": self.seekdb_host,
            "port": self.seekdb_port,
            "user": self.seekdb_user,
            "password": self.seekdb_password,
            "database": self.seekdb_database,
            "charset": "utf8mb4",
        }


def load_settings(env_file: Path | None = None) -> Settings:
    """Build settings from the environment, then from an optional environment file.

    Real process environment variables win over the file so that a reviewer can
    override a single value without editing the file.
    """

    resolved_file = env_file or Path(DEFAULT_ENV_FILE)
    file_values: dict[str, str] = {}
    if resolved_file.is_file():
        file_values = _read_env_file(resolved_file)

    def value(key: str, default: str = "") -> str:
        found = os.environ.get(key)
        if found is not None:
            return found.strip()
        return file_values.get(key, default).strip()

    repository_root = Path(value("SDD_REPOSITORY_ROOT", str(Path.cwd()))).expanduser()
    workspace_dir = Path(value("SDD_WORKSPACE_DIR", str(repository_root / "minimax" / "workload"))).expanduser()
    return Settings(
        seekdb_host=value("SEEKDB_HOST", "127.0.0.1"),
        seekdb_port=int(value("SEEKDB_PORT", "2881") or "2881"),
        seekdb_user=value("SEEKDB_USER", "root"),
        seekdb_password=value("SEEKDB_PASSWORD", ""),
        seekdb_database=value("SEEKDB_DATABASE", "sdd_context"),
        powercontext_url=value("POWERCONTEXT_SERVER_URL", "http://127.0.0.1:8000"),
        powercontext_token=value("POWERCONTEXT_TOKEN", ""),
        powercontext_scope_id=value("POWERCONTEXT_SCOPE_ID", ""),
        minimax_base_url=value("MINIMAX_BASE_URL", "https://api.minimax.chat/v1"),
        minimax_api_key=value("MINIMAX_API_KEY", ""),
        minimax_model=value("MINIMAX_MODEL", "MiniMax-M2"),
        repository_root=repository_root,
        workspace_dir=workspace_dir,
    )


__all__ = ["DEFAULT_ENV_FILE", "Settings", "load_settings"]
