"""Database configuration for SQLite and PostgreSQL backends."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional


@dataclass(frozen=True)
class DatabaseConfig:
    """Configuration settings for the reconciliation persistence layer."""

    url: str
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: float = 30.0
    null_pool: bool = False

    @classmethod
    def from_env(
        cls,
        default_url: Optional[str] = None,
        primary_env_var: Optional[str] = None,
    ) -> "DatabaseConfig":
        """Resolve database configuration from environment variables."""
        from reconcile.paypal import load_dotenv
        load_dotenv()
        url = (
            (os.environ.get(primary_env_var) if primary_env_var else None)
            or os.environ.get("RECONCILE_DB_URL")
            or os.environ.get("DATABASE_URL")
            or default_url
            or "sqlite:///recon.db"
        )
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        echo = os.environ.get("DATABASE_ECHO", "").lower() in ("1", "true", "yes")
        null_pool = (
            os.environ.get("DATABASE_NULL_POOL", "").lower() in ("1", "true", "yes")
            or ":6543" in url
        )
        return cls(url=url, echo=echo, null_pool=null_pool)

    @classmethod
    def for_worker(cls, default_url: Optional[str] = None) -> "DatabaseConfig":
        """Resolve database configuration for the background worker daemon.

        Prioritizes WORKER_DB_URL (session pooler, port 5432 on Supabase)
        before falling back to RECONCILE_DB_URL or DATABASE_URL.
        """
        return cls.from_env(default_url=default_url, primary_env_var="WORKER_DB_URL")


    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith("postgresql") or self.url.startswith("postgres")

    @property
    def is_memory(self) -> bool:
        return ":memory:" in self.url
