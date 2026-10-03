"""Migrations build the schema from empty and match the models."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from oa_server.launcher import MIGRATIONS, migrate


def test_upgrade_head_on_an_empty_database(database_url: str) -> None:
    migrate(database_url)

    engine = create_engine(database_url)
    try:
        assert "plan" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_models_and_migrations_agree(database_url: str) -> None:
    """`alembic check`: a model change without its migration fails here."""
    migrate(database_url)
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)

    command.check(config)
