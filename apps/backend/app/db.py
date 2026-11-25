"""Database construction; importing this module never opens a connection."""

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def make_engine(url: str) -> Engine:
    options: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url in {"sqlite://", "sqlite+pysqlite://"}:
            options["poolclass"] = StaticPool
    else:
        options.update(pool_size=5, max_overflow=2, pool_timeout=10)
        if url.startswith("postgresql"):
            options["connect_args"] = {
                "connect_timeout": 5,
                "options": "-c statement_timeout=15000 -c idle_in_transaction_session_timeout=30000",
            }
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
