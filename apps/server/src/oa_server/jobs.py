"""Background jobs and their `job_run` rows.

Every read the server makes on its own (the NAV backfill, the shelf, the
rewards) leaves a row saying when it ran and how it went, so a page can say how
fresh its numbers are and the Activity page can show what failed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from oa_server.db.models import JobRunRow

JsonObject = dict[str, Any]
T = TypeVar("T")

# The jobs `POST /api/jobs/{name}` can start.
JOBS = ("nav", "shelf", "rewards")


def start_run(engine: Engine, job: str) -> int:
    with Session(engine) as session, session.begin():
        row = JobRunRow(job=job, started_at=datetime.now(UTC), status="running")
        session.add(row)
        session.flush()
        return row.id


def finish_run(
    engine: Engine, run_id: int, status: str, detail: JsonObject | None
) -> None:
    with Session(engine) as session, session.begin():
        row = session.get(JobRunRow, run_id)
        if row is not None:
            row.status, row.detail, row.finished_at = status, detail, datetime.now(UTC)


def recorded(
    engine: Engine, job: str, run: Callable[[], T], detail: Callable[[T], JsonObject]
) -> T:
    """Run ``run`` as one `job_run` row: ok with ``detail`` of its result, or
    failed with the error, which is raised again."""
    run_id = start_run(engine, job)
    try:
        result = run()
    except Exception as error:
        finish_run(engine, run_id, "failed", {"error": str(error)})
        raise
    finish_run(engine, run_id, "ok", detail(result))
    return result
