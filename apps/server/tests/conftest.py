from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

# The library's execution fakes (fake 1Tx client, signer spy) live with its tests.
LIBRARY_TESTS = Path(__file__).resolve().parents[3] / "tests"
sys.path.insert(0, str(LIBRARY_TESTS))


@pytest.fixture(autouse=True)
def isolate_ambient_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real .env and keychain out of the tests, as the
    library's own suite does."""
    monkeypatch.setenv("OPEN_ALLOCATOR_SECRET_BACKEND", "none")
    monkeypatch.setenv("OPEN_ALLOCATOR_ENV_FILE", "/nonexistent/open-allocator/.env")


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A Postgres server: OA_TEST_DATABASE_URL, else a throwaway container."""
    configured = os.environ.get("OA_TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer("postgres:17-alpine", driver="psycopg")
        container.start()
    except Exception as error:  # Docker missing or not running.
        pytest.skip(f"no Postgres: set OA_TEST_DATABASE_URL or start Docker ({error})")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def database_url(postgres_url: str) -> Iterator[str]:
    """An empty database of its own for one test."""
    name = f"oa_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield (
            make_url(postgres_url)
            .set(database=name)
            .render_as_string(hide_password=False)
        )
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    """A migrated database."""
    from oa_server.db.session import make_engine
    from oa_server.launcher import migrate

    migrate(database_url)
    engine = make_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
