"""Database engine, connection pooling, SQLite pragmas, and session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import DatabaseConfig
from .schema import Base


def _configure_sqlite_connection(dbapi_connection: Any, connection_record: Any) -> None:
    """Enforce foreign keys, busy timeout, WAL mode, and disable pysqlite deferred begin."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL;")
    except Exception:
        pass
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA busy_timeout = 10000;")
    cursor.close()
    # Disable pysqlite's automatic DML-triggered deferred begin
    dbapi_connection.isolation_level = None


def _sqlite_begin_immediate(conn: Any) -> None:
    """Begin immediate transaction to reserve write lock and prevent write-skew in SQLite."""
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def get_engine(config: Optional[DatabaseConfig] = None) -> Engine:
    """Create and configure a SQLAlchemy Engine according to DatabaseConfig."""
    cfg = config or DatabaseConfig.from_env()

    if cfg.is_sqlite:
        connect_args = {"check_same_thread": False}
        if cfg.is_memory:
            # In-memory SQLite needs a StaticPool so connections share the same memory DB
            engine = create_engine(
                cfg.url,
                connect_args=connect_args,
                poolclass=StaticPool,
                echo=cfg.echo,
            )
        else:
            engine = create_engine(
                cfg.url,
                connect_args=connect_args,
                echo=cfg.echo,
            )

        event.listen(engine, "connect", _configure_sqlite_connection)
        event.listen(engine, "begin", _sqlite_begin_immediate)
        return engine

    # PostgreSQL configuration
    if cfg.null_pool:
        from sqlalchemy.pool import NullPool
        connect_args = {"prepare_threshold": 0} if cfg.is_postgres else {}
        return create_engine(
            cfg.url,
            echo=cfg.echo,
            poolclass=NullPool,
            connect_args=connect_args,
        )

    engine = create_engine(
        cfg.url,
        echo=cfg.echo,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
        pool_timeout=cfg.pool_timeout,
        pool_pre_ping=True,  # Test connections before checkout
    )
    return engine


class DatabaseEngine:
    """Convenience wrapper managing an engine and session factory."""

    def __init__(self, config_or_url: DatabaseConfig | str | None = None) -> None:
        if isinstance(config_or_url, str):
            config = DatabaseConfig(url=config_or_url)
        elif isinstance(config_or_url, DatabaseConfig):
            config = config_or_url
        else:
            config = DatabaseConfig.from_env()

        self.config = config
        self.engine = get_engine(config)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def init_schema(self) -> None:
        """Create all tables in the database (for SQLite test setups / fresh instances)."""
        Base.metadata.create_all(self.engine)

    create_schema = init_schema

    def drop_schema(self) -> None:
        """Drop all tables in the database (for clean test teardowns)."""
        Base.metadata.drop_all(self.engine)

    def dispose(self) -> None:
        """Dispose of the underlying connection pool."""
        self.engine.dispose()

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Provide a transactional session scope."""
        sess: Session = self.session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()


@contextmanager
def session_scope(session_factory_or_engine: sessionmaker[Session] | Engine | DatabaseEngine) -> Generator[Session, None, None]:
    """Context manager for atomic transaction management with automatic rollback."""
    if isinstance(session_factory_or_engine, DatabaseEngine):
        with session_factory_or_engine.session() as sess:
            yield sess
        return

    if isinstance(session_factory_or_engine, Engine):
        factory = sessionmaker(
            bind=session_factory_or_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    else:
        factory = session_factory_or_engine

    sess: Session = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
