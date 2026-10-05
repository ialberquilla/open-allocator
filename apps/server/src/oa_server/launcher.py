"""`open-allocator-ui`: migrate the database, then serve on 127.0.0.1.

Run it from the directory holding the `.env` the CLI uses: the server signs with
the same configuration. `open-allocator-ui approve <hash>` and `reject <hash>`
decide a stored plan from a terminal instead of the approval page;
`open-allocator-ui backfill` fills the NAV history without serving.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from datetime import date
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from oa_server.app import create_app
from oa_server.approval import PostgresPlanStore, approve, reject
from oa_server.auth import load_mcp_token, new_token
from oa_server.dashboard import (
    BACKFILL_INTERVAL_SECONDS,
    SHELF_INTERVAL_SECONDS,
    Dashboard,
)
from oa_server.db.session import make_engine
from oa_server.nav_job import backfill
from oa_server.settings import DEFAULT_MCP_TOKEN_PATH, Settings
from oa_server.state import PostgresStateBackend
from open_allocator.service import ServiceError, use_state_backend

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def migrate(database_url: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _connect(database_url: str) -> Engine:
    engine = make_engine(database_url)
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
    return engine


def _mcp_token() -> str:
    return os.environ.get("OA_MCP_TOKEN") or load_mcp_token(DEFAULT_MCP_TOKEN_PATH)


def serve(args: argparse.Namespace) -> None:
    settings = Settings.from_env(
        new_token(),
        _mcp_token(),
        host=args.host,
        port=args.port,
        database_url=args.database_url,
    )
    engine = _connect(settings.database_url)
    migrate(settings.database_url)
    use_state_backend(PostgresStateBackend(engine))

    app = create_app(
        settings,
        PostgresPlanStore(engine),
        dashboard=Dashboard(engine),
        backfill_every=None if args.no_backfill else BACKFILL_INTERVAL_SECONDS,
        shelf_every=SHELF_INTERVAL_SECONDS,
    )
    login_url = settings.login_url()
    print(
        f"Open Allocator server on {settings.base_url}\n"
        f"  approvals: {login_url}\n"
        f"  MCP:       {settings.base_url}/mcp\n"
        f"  connect Claude Code with:\n"
        f"    claude mcp add --transport http open-allocator "
        f"{settings.base_url}/mcp --header "
        f'"Authorization: Bearer {settings.mcp_token}"',
        file=sys.stderr,
    )
    if not args.no_browser:
        webbrowser.open(login_url)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def decide(args: argparse.Namespace) -> None:
    """Approve or reject one stored plan, printing the outcome as JSON."""
    settings = Settings.from_env("", "", database_url=args.database_url)
    engine = _connect(settings.database_url)
    use_state_backend(PostgresStateBackend(engine))
    store = PostgresPlanStore(engine)
    try:
        if args.command == "approve":
            outcome = {
                "plan_hash": args.plan_hash,
                "result": approve(store, args.plan_hash, policy=settings.policy_path),
            }
        else:
            reject(store, args.plan_hash)
            outcome = {"plan_hash": args.plan_hash, "status": "rejected"}
    except ServiceError as error:
        print(json.dumps({"error": error.detail, "code": error.code}), file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(outcome, indent=2))


def run_backfill(args: argparse.Namespace) -> None:
    """Fill the NAV history up to yesterday, printing what was read as JSON."""
    settings = Settings.from_env("", "", database_url=args.database_url)
    engine = _connect(settings.database_url)
    migrate(settings.database_url)
    since = date.fromisoformat(args.since) if args.since else None
    print(json.dumps(backfill(engine, since=since).payload(), indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="open-allocator-ui")
    parser.add_argument("--database-url", help="Defaults to OA_DATABASE_URL.")
    parser.add_argument("--host", help="Bind address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Port (default 8787).")
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open the approvals page."
    )
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="Do not fill the NAV history on start and hourly.",
    )
    commands = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("approve", "Apply a stored plan, after re-checking the policy."),
        ("reject", "Retire a stored plan without applying it."),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("plan_hash")
    sub = commands.add_parser(
        "backfill", help="Read every closed day the NAV history lacks, then rebuild it."
    )
    sub.add_argument(
        "--since",
        help="First day (YYYY-MM-DD). Defaults to the day the Safe was deployed.",
    )
    args = parser.parse_args(argv)
    if args.command is None:
        serve(args)
    elif args.command == "backfill":
        run_backfill(args)
    else:
        decide(args)
