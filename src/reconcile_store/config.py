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

    @classmethod
    def from_env(cls, default_url: Optional[str] = None) -> "DatabaseConfig":
        """Resolve database configuration from environment variables."""
        from reconcile.paypal import load_dotenv
        load_dotenv()
        url = (
            os.environ.get("DATABASE_URL")
            or os.environ.get("RECONCILE_DB_URL")
            or default_url
            or "sqlite:///recon.db"
        )
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        echo = os.environ.get("DATABASE_ECHO", "").lower() in ("1", "true", "yes")
        return cls(url=url, echo=echo)


    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith("postgresql") or self.url.startswith("postgres")

    @property
    def is_memory(self) -> bool:
        return ":memory:" in self.url
