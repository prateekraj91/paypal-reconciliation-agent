"""Investigation repository for storing and querying AI exception investigations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Optional, Sequence
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError
from ..schema import InvestigationRecord, _gen_id, _utcnow


class InvestigationRepository(BaseRepository[InvestigationRecord]):
    """Repository for AI exception investigations."""

    def create_investigation(
        self,
        tenant_id: str,
        target_type: str,
        target_id: str,
        status: str = "QUEUED",
        model_provider: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> InvestigationRecord:
        """Create a new investigation record."""
        record = InvestigationRecord(
            id=_gen_id("invg"),
            tenant_id=tenant_id,
            target_type=target_type,
            target_id=target_id,
            status=status,
            model_provider=model_provider,
            model_name=model_name,
            prompt_version=prompt_version,
            created_at=_utcnow(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_investigation(self, tenant_id: str, investigation_id: str) -> Optional[InvestigationRecord]:
        """Fetch investigation strictly by tenant_id and investigation_id."""
        stmt = select(InvestigationRecord).where(
            InvestigationRecord.tenant_id == tenant_id,
            InvestigationRecord.id == investigation_id,
        )
        return self.session.scalars(stmt).first()

    def list_investigations(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        target_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[InvestigationRecord]:
        """List investigations for a tenant with optional filtering."""
        stmt = select(InvestigationRecord).where(InvestigationRecord.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(InvestigationRecord.status == status)
        if target_type:
            stmt = stmt.where(InvestigationRecord.target_type == target_type)
        stmt = stmt.order_by(desc(InvestigationRecord.created_at)).limit(limit).offset(offset)
        return self.session.scalars(stmt).all()

    def update_status(
        self,
        tenant_id: str,
        investigation_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> InvestigationRecord:
        """Update status of an investigation."""
        rec = self.get_investigation(tenant_id, investigation_id)
        if not rec:
            raise EntityNotFoundError(f"Investigation '{investigation_id}' not found for tenant '{tenant_id}'")
        rec.status = status
        if error_message:
            rec.error_message = error_message
        if status in ("COMPLETED", "FAILED", "NEEDS_REVIEW", "INVALID"):
            rec.completed_at = _utcnow()
        self.session.flush()
        return rec

    def record_investigation_result(
        self,
        tenant_id: str,
        investigation_id: str,
        status: str,
        classification: Optional[str],
        summary: Optional[str],
        result_dict: dict[str, Any],
        evidence_references: list[dict[str, Any]],
        verifier_status: str,
        verifier_details: dict[str, Any],
        model_provider: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> InvestigationRecord:
        """Record the full validated investigation output and verifier results."""
        rec = self.get_investigation(tenant_id, investigation_id)
        if not rec:
            raise EntityNotFoundError(f"Investigation '{investigation_id}' not found for tenant '{tenant_id}'")
        rec.status = status
        rec.classification = classification
        rec.summary = summary
        rec.result_json = json.dumps(result_dict)
        rec.evidence_references_json = json.dumps(evidence_references)
        rec.verifier_status = verifier_status
        rec.verifier_details_json = json.dumps(verifier_details)
        if model_provider:
            rec.model_provider = model_provider
        if model_name:
            rec.model_name = model_name
        if prompt_version:
            rec.prompt_version = prompt_version
        rec.completed_at = _utcnow()
        self.session.flush()
        return rec
