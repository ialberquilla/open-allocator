"""Plans in Postgres, and the human approval that applies one.

The MCP server mounted in this process stores what its execution tools propose
here. `approve` is the only path to applying a plan, and only the HTTP route a
human calls reaches it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from oa_server.db.models import PlanRow
from open_allocator.service import ServiceError
from open_allocator.service import execution as execution_service
from open_allocator.service.plan_store import (
    DEFAULT_PLAN_TTL,
    PlanStore,
    StoredPlan,
    plan_hash,
)

JsonObject = dict[str, Any]

# Refusals that leave the plan as it was: this approval did not take it.
_NOT_TAKEN = frozenset({"plan_not_found", "plan_expired", "plan_used"})


def _stored(row: PlanRow) -> StoredPlan:
    return StoredPlan(
        plan_hash=row.hash,
        kind=row.kind,
        plan=row.plan,
        created_at=row.created_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
    )


class PostgresPlanStore:
    """A `PlanStore` shared by every request and MCP session of the server.

    `take` is one conditional UPDATE, so two approvals of the same hash racing
    each other cannot both get the plan.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        ttl: timedelta = DEFAULT_PLAN_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._ttl = ttl
        self._clock = clock

    def _session(self) -> Session:
        # Rows are read after their transaction commits.
        return Session(self._engine, expire_on_commit=False)

    def put(self, kind: str, plan: Mapping[str, Any]) -> StoredPlan:
        document = json.loads(json.dumps(plan))
        now = self._clock()
        values = {
            "hash": plan_hash(kind, document),
            "kind": kind,
            "plan": document,
            "created_at": now,
            "expires_at": now + self._ttl,
        }
        statement = insert(PlanRow).values(**values)
        # The same plan proposed again gets a fresh expiry, unless it ran.
        statement = statement.on_conflict_do_update(
            index_elements=[PlanRow.hash],
            set_={"created_at": now, "expires_at": values["expires_at"]},
            where=PlanRow.used_at.is_(None),
        )
        with self._session() as session, session.begin():
            session.execute(statement)
            row = session.get(PlanRow, values["hash"])
            assert row is not None
            return _stored(row)

    def get(self, plan_hash: str) -> StoredPlan | None:
        with self._session() as session:
            row = session.get(PlanRow, plan_hash)
            return None if row is None else _stored(row)

    def take(self, plan_hash: str) -> StoredPlan:
        now = self._clock()
        statement = (
            update(PlanRow)
            .where(
                PlanRow.hash == plan_hash,
                PlanRow.used_at.is_(None),
                PlanRow.expires_at > now,
            )
            .values(used_at=now)
            .returning(PlanRow)
        )
        with self._session() as session, session.begin():
            taken = session.execute(statement).scalar_one_or_none()
            if taken is not None:
                return _stored(taken)
            row = session.execute(
                select(PlanRow).where(PlanRow.hash == plan_hash)
            ).scalar_one_or_none()
        if row is None:
            raise ServiceError("plan_not_found", f"no plan {plan_hash}")
        if row.used_at is not None:
            raise ServiceError("plan_used", f"plan {plan_hash} was already applied")
        raise ServiceError("plan_expired", f"plan {plan_hash} expired; plan again")

    def record(
        self,
        plan_hash: str,
        *,
        result: JsonObject | None = None,
        error: str | None = None,
    ) -> None:
        """What applying the plan returned, or why it was refused or failed."""
        with self._session() as session, session.begin():
            row = session.get(PlanRow, plan_hash)
            if row is not None:
                row.result = result
                row.error = error


def approve(store: PlanStore, approved_hash: str, *, policy: Path) -> JsonObject:
    """Apply the stored plan with this hash, once, after re-checking `policy`.

    The plan is marked used first; a refusal or a failure leaves it used and
    needs a new plan. The outcome is recorded on the plan when the store keeps
    outcomes.
    """
    record = getattr(store, "record", None)
    try:
        result = execution_service.apply_approved(store, approved_hash, policy=policy)
    except ServiceError as error:
        if callable(record) and error.code not in _NOT_TAKEN:
            record(approved_hash, error=f"{error.code}: {error.detail}")
        raise
    except Exception as error:
        if callable(record):
            record(approved_hash, error=str(error))
        raise
    if callable(record):
        record(approved_hash, result=result)
    return result
