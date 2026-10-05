"""Execution state in Postgres: the library's `StateBackend` for this server.

Planning reads it (a completed leg is not planned again, a bridge under way is
planned as itself) and approval writes it, so the server sets it for the whole
process at start. The CLI keeps the `.open_allocator/` files; the two do not
share state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_server.db.models import AllocationLogRow, CheckpointRow, IdempotencyKeyRow
from open_allocator.core.state import CheckpointExists, CheckpointNotFound, json_safe

if TYPE_CHECKING:
    from open_allocator.core.checkpoint import AllocationLogEntry, Checkpoint


class PostgresStateBackend:
    """Each write is its own committed transaction, so a step marked completed
    is durable before the next one is sent."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        row = CheckpointRow(
            id=checkpoint.id, checkpoint=checkpoint.model_dump(mode="json")
        )
        try:
            with Session(self._engine) as session, session.begin():
                session.add(row)
        except IntegrityError as error:
            raise CheckpointExists(
                f"checkpoint already exists: {checkpoint.id}"
            ) from error

    def read_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        from open_allocator.core.checkpoint import Checkpoint

        with Session(self._engine) as session:
            row = session.get(CheckpointRow, checkpoint_id)
            if row is None:
                raise CheckpointNotFound(f"no checkpoint {checkpoint_id}")
            return Checkpoint.model_validate(row.checkpoint)

    def append_allocation_log_entry(self, entry: AllocationLogEntry) -> None:
        with Session(self._engine) as session, session.begin():
            session.add(AllocationLogRow(entry=entry.model_dump(mode="json")))

    def read_allocation_log(self) -> tuple[AllocationLogEntry, ...]:
        from open_allocator.core.checkpoint import AllocationLogEntry

        statement = select(AllocationLogRow.entry).order_by(AllocationLogRow.id)
        with Session(self._engine) as session:
            return tuple(
                AllocationLogEntry.model_validate(entry)
                for entry in session.execute(statement).scalars()
            )

    def is_completed(self, scope: str, key: str) -> bool:
        statement = select(
            exists().where(
                IdempotencyKeyRow.scope == scope, IdempotencyKeyRow.key == key
            )
        )
        with Session(self._engine) as session:
            return bool(session.execute(statement).scalar_one())

    def mark_completed(self, scope: str, key: str, value: Any = None) -> None:
        # Overwrites, as the files do: a bridge leg rewrites its state here as
        # the transfer advances.
        stored = None if value is None else json_safe(value)
        statement = insert(IdempotencyKeyRow).values(scope=scope, key=key, value=stored)
        statement = statement.on_conflict_do_update(
            index_elements=[IdempotencyKeyRow.scope, IdempotencyKeyRow.key],
            set_={"value": statement.excluded.value},
        )
        with Session(self._engine) as session, session.begin():
            session.execute(statement)

    def completed_value(self, scope: str, key: str) -> Any:
        with Session(self._engine) as session:
            row = session.get(IdempotencyKeyRow, (scope, key))
            return None if row is None else row.value
