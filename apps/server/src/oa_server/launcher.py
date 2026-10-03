"""`open-allocator-ui`: migrate the database, then serve on 127.0.0.1.

Run it from the directory holding the `.env` the CLI uses: the server signs with
the same configuration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from oa_server.app import create_app
from oa_server.approval import PostgresPlanStore
from oa_server.auth import new_token
from oa_server.db.session import make_engine
from oa_server.settings import Settings

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def migrate(database_url: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="open-allocator-ui")
    parser.add_argument("--host", help="Bind address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Port (default 8787).")
    parser.add_argument("--database-url", help="Defaults to OA_DATABASE_URL.")
    args = parser.parse_args(argv)

    settings = Settings.from_env(
        new_token(), host=args.host, port=args.port, database_url=args.database_url
    )
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except OperationalError as error:
        print(
            f"cannot reach Postgres at {engine.url.render_as_string()}: "
            f"{error.orig}\nStart it with `docker compose up -d` from the "
            "repository root, or set OA_DATABASE_URL.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    migrate(settings.database_url)

    app = create_app(settings, PostgresPlanStore(engine))
    print(
        f"Open Allocator server on {settings.base_url}\n"
        f"  token: {settings.token}\n"
        f"  MCP:   {settings.base_url}/mcp  (Authorization: Bearer <token>)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
