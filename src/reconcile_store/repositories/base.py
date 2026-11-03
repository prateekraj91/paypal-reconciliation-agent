"""Base repository and common repository exceptions."""

from __future__ import annotations

from typing import Generic, TypeVar, Optional, Sequence
from sqlalchemy.orm import Session
from sqlalchemy import select

T = TypeVar("T")


class RepositoryError(Exception):
    """Base exception for all repository operations."""


class EntityNotFoundError(RepositoryError):
    """Raised when an expected entity does not exist."""


class ConflictError(RepositoryError):
    """Raised when an operation encounters an integrity conflict or duplicate."""


class StaleVersionError(ConflictError):
    """Raised when optimistic concurrency checks fail due to concurrent modification."""


class TenantIsolationError(RepositoryError):
    """Raised when an operation attempts to breach tenant boundaries."""


class BaseRepository(Generic[T]):
    """Base repository providing tenant-scoped query helpers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _ensure_tenant(self, entity_tenant_id: str, expected_tenant_id: str) -> None:
        if entity_tenant_id != expected_tenant_id:
            raise TenantIsolationError(
                f"Tenant mismatch: entity belongs to '{entity_tenant_id}', expected '{expected_tenant_id}'"
            )
