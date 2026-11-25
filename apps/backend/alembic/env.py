"""Alembic configuration. Credentials come only from DATABASE_URL/settings."""

from logging.config import fileConfig

from alembic import context
from app.db import make_engine
from app.models import Base

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    # Embedded callers retain their host logging configuration. CLI migrations
    # also preserve existing application loggers rather than silencing notices.
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def database_url() -> str:
    # Explicit in-memory configuration is useful for isolated migration tests.
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    from app.config import Settings

    return str(Settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=target_metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
