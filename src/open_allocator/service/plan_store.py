"""Plans waiting for a human to approve them.

An execution tool stores the plan it built and returns its hash. Only an approval
outside the model's reach applies it, and it applies the stored plan, never a
rebuilt one. A plan is applied at most once and not after it expires.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from open_allocator.service.errors import ServiceError

# Long enough to read a plan and click Approve; short enough that the balances
# and positions it was sized against still hold.
DEFAULT_PLAN_TTL = timedelta(minutes=15)


def plan_hash(kind: str, plan: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of the kind and the plan."""
    encoded = json.dumps(
        {"kind": kind, "plan": plan}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StoredPlan:
    plan_hash: str
    kind: str
    plan: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None


class PlanStore(Protocol):
    def put(self, kind: str, plan: Mapping[str, Any]) -> StoredPlan: ...

    def get(self, plan_hash: str) -> StoredPlan | None: ...

    def take(self, plan_hash: str) -> StoredPlan:
        """Mark the plan used and return it, atomically.

        Raises `ServiceError` with code `plan_not_found`, `plan_expired` or
        `plan_used`; a second take of the same hash always fails.
        """
        ...


class InMemoryPlanStore:
    """A `PlanStore` for one process; the stdio MCP server uses it."""

    def __init__(
        self,
        *,
        ttl: timedelta = DEFAULT_PLAN_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._plans: dict[str, StoredPlan] = {}
        self._lock = threading.Lock()

    def put(self, kind: str, plan: Mapping[str, Any]) -> StoredPlan:
        document = json.loads(json.dumps(plan))
        now = self._clock()
        stored = StoredPlan(
            plan_hash=plan_hash(kind, document),
            kind=kind,
            plan=document,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            existing = self._plans.get(stored.plan_hash)
            # The same plan proposed again gets a fresh expiry, unless it ran.
            if existing is not None and existing.used_at is not None:
                return existing
            self._plans[stored.plan_hash] = stored
        return stored

    def get(self, plan_hash: str) -> StoredPlan | None:
        with self._lock:
            return self._plans.get(plan_hash)

    def take(self, plan_hash: str) -> StoredPlan:
        with self._lock:
            stored = self._plans.get(plan_hash)
            if stored is None:
                raise ServiceError("plan_not_found", f"no plan {plan_hash}")
            if stored.used_at is not None:
                raise ServiceError("plan_used", f"plan {plan_hash} was already applied")
            now = self._clock()
            if now >= stored.expires_at:
                raise ServiceError(
                    "plan_expired", f"plan {plan_hash} expired; plan again"
                )
            taken = replace(stored, used_at=now)
            self._plans[plan_hash] = taken
            return taken
