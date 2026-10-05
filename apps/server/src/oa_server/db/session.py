from __future__ import annotations

from sqlalchemy import Engine, create_engine


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)
