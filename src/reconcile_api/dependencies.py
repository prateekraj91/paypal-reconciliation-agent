"""Dependency injection providers for FastAPI routes."""

from __future__ import annotations

from typing import Generator, Optional
from fastapi import Request
from sqlalchemy.orm import Session

from reconcile.paypal import PayPalClient
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from .config import APIConfig

_db_engine: Optional[DatabaseEngine] = None


def get_db_engine() -> DatabaseEngine:
    """Retrieve or initialize the global DatabaseEngine."""
    global _db_engine
    if _db_engine is None:
        config = APIConfig.from_env()
        db_config = DatabaseConfig(url=config.database_url) if config.database_url else DatabaseConfig.from_env()
        _db_engine = DatabaseEngine(db_config)
    return _db_engine


def set_db_engine(engine: Optional[DatabaseEngine]) -> None:
    """Explicitly set or reset the DatabaseEngine (useful for test isolation)."""
    global _db_engine
    _db_engine = engine


def get_db_session() -> Generator[Session, None, None]:
    """Yield a database session within a transactional context."""
    engine = get_db_engine()
    with engine.session() as session:
        yield session


def get_paypal_client() -> PayPalClient:
    """Instantiate a PayPalClient configured for the active environment."""
    config = APIConfig.from_env()
    base_url = config.paypal_base_url
    if not base_url:
        base_url = (
            "https://api-m.paypal.com"
            if config.paypal_environment == "live"
            else "https://api-m.sandbox.paypal.com"
        )

    return PayPalClient(
        client_id=config.paypal_client_id,
        client_secret=config.paypal_client_secret,
        base_url=base_url,
        scopes=(
            "https://uri.paypal.com/services/invoicing "
            "https://uri.paypal.com/services/reporting/search/read "
            "https://uri.paypal.com/services/applications/webhooks"
        ),
    )
