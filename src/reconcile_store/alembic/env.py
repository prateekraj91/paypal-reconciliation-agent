import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import _configure_sqlite_connection
from reconcile_store.schema import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# Check if environment explicitly specifies database URL
env_url = os.environ.get("RECONCILE_DB_URL") or os.environ.get("DATABASE_URL")
if env_url:
    config.set_main_option("sqlalchemy.url", env_url)
elif not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", "sqlite:///recon.db")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    url = config.get_main_option("sqlalchemy.url") or ""
    is_sqlite = url.startswith("sqlite")

    connectable = config.attributes.get("connection", None)

    if connectable is None:
        from sqlalchemy import create_engine
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        connectable = create_engine(
            url,
            poolclass=pool.NullPool,
            connect_args=connect_args,
        )
        if is_sqlite:
            from sqlalchemy import event
            event.listen(connectable, "connect", _configure_sqlite_connection)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
