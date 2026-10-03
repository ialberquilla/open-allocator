"""Server settings, read from the environment once at start."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from open_allocator.service.allocation import DEFAULT_POLICY_PATH

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://open_allocator:open_allocator@127.0.0.1:5442/open_allocator"
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


@dataclass(frozen=True)
class Settings:
    # Required on every request except the health check.
    token: str
    database_url: str = DEFAULT_DATABASE_URL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # The operator's policy. Approval re-checks every plan against it, whatever
    # policy the model planned under.
    policy_path: Path = DEFAULT_POLICY_PATH
    extra_allowed_hosts: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, token: str, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "database_url": os.environ.get("OA_DATABASE_URL", DEFAULT_DATABASE_URL),
            "host": os.environ.get("OA_HOST", DEFAULT_HOST),
            "port": int(os.environ.get("OA_PORT", DEFAULT_PORT)),
            "policy_path": Path(
                os.environ.get("OA_POLICY_PATH", str(DEFAULT_POLICY_PATH))
            ),
        }
        values.update({key: value for key, value in overrides.items() if value})
        return cls(token=token, **values)  # type: ignore[arg-type]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        hosts = {f"{self.host}:{self.port}", f"localhost:{self.port}"}
        return tuple(sorted(hosts)) + self.extra_allowed_hosts

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(f"http://{host}" for host in self.allowed_hosts)
