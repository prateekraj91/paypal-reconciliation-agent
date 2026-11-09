"""Hermetic test configuration.

Tests must never read the developer's `.env` files or reach a shared database.
This module neutralises the repository's dotenv files before anything loads them
and refuses PostgreSQL connections to anything but a disposable local database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

import reconcile.paypal as paypal

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PROTECTED_DATABASES = {"reconcile_demo", "postgres"}

for _name in ("RECONCILE_DB_URL", "DATABASE_URL", "WORKER_DB_URL", "MIGRATION_DB_URL"):
    os.environ.pop(_name, None)

os.environ["ADMIN_API_KEY"] = "test-only-admin-key"
os.environ["PAYPAL_CLIENT_ID"] = "test-only-client-id"
os.environ["PAYPAL_CLIENT_SECRET"] = "test-only-client-secret"
os.environ.pop("PAYPAL_BASE_URL", None)

_real_load_dotenv = paypal._load_dotenv


def _hermetic_load_dotenv(filepath: str = ".env") -> dict[str, str]:
    path = Path(filepath).resolve()
    if path.parent == _REPO_ROOT and path.name.startswith(".env"):
        return {}
    return _real_load_dotenv(filepath)


paypal._load_dotenv = _hermetic_load_dotenv


@event.listens_for(Engine, "do_connect")
def _refuse_shared_databases(dialect, conn_rec, cargs, cparams):
    if dialect.name != "postgresql":
        return
    host = str(cparams.get("host") or "localhost")
    database = str(cparams.get("dbname") or cparams.get("database") or "")
    if host not in _LOCAL_HOSTS or database in _PROTECTED_DATABASES:
        raise RuntimeError(f"Test refused a connection to a shared database ({database!r}).")


@pytest.fixture(autouse=True)
def _restore_environment():
    """Stop environment changes made by one test from reaching the next."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
