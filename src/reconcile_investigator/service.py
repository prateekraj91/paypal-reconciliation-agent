"""InvestigationService coordinating database persistence, worker queuing, and execution."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple
from sqlalchemy.orm import Session

from reconcile_store.repositories.investigation_repo import InvestigationRepository
from reconcile_store.repositories.job_repo import JobRepository
from reconcile_store.repositories.audit_repo import AuditRepository
from reconcile_store.schema import InvestigationRecord, JobRecord, _gen_id

from .models import InvestigationResult, VerifierStatus
from .providers.base import InvestigationModelProvider
from .providers.ollama import OllamaProvider
from .providers.mock import MockInvestigationProvider
from .orchestration import InvestigationOrchestrator
from .verifier import SafetyVerificationResult

logger = logging.getLogger(__name__)


def resolve_default_provider() -> InvestigationModelProvider:
    """Resolve model provider based on environment and local availability.
    
    Defaults to MockInvestigationProvider for fast, deterministic, offline operation.
    When MODEL_PROVIDER=ollama is specified, delegates to local Ollama instance.
    """
    provider_type = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    model = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

    if provider_type == "ollama":
        return OllamaProvider(base_url=base_url, model=model)

    return MockInvestigationProvider()


class InvestigationService:
    """Service handling submission, queuing, execution, and audit logging of AI investigations."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.investigation_repo = InvestigationRepository(session)
        self.job_repo = JobRepository(session)
        self.audit_repo = AuditRepository(session)

    def enqueue_investigation(
        self,
        tenant_id: str,
        target_type: str,
        target_id: str,
    ) -> Tuple[InvestigationRecord, JobRecord]:
        """Enqueue an investigation for background execution by the worker."""
        inv_rec = self.investigation_repo.create_investigation(
            tenant_id=tenant_id,
            target_type=target_type,
            target_id=target_id,
            status="QUEUED",
        )

        idempotency_key = f"investigate:{tenant_id}:{target_type}:{target_id}:{inv_rec.id}"
        job = self.job_repo.enqueue_job(
            tenant_id=tenant_id,
            job_type="INVESTIGATE_EXCEPTION",
            idempotency_key=idempotency_key,
            payload={
                "investigation_id": inv_rec.id,
                "target_type": target_type,
                "target_id": target_id,
            },
        )
        self.session.commit()
        return inv_rec, job

    def run_investigation(
        self,
        tenant_id: str,
        target_type: str,
        target_id: str,
        investigation_id: Optional[str] = None,
        provider: Optional[InvestigationModelProvider] = None,
    ) -> Tuple[InvestigationRecord, InvestigationResult, SafetyVerificationResult]:
        """Synchronously execute an investigation and persist the verified result."""
        active_provider = provider or resolve_default_provider()

        # Retrieve or create investigation record
        if investigation_id:
            inv_rec = self.investigation_repo.get_investigation(tenant_id, investigation_id)
            if not inv_rec:
                inv_rec = self.investigation_repo.create_investigation(
                    tenant_id=tenant_id,
                    target_type=target_type,
                    target_id=target_id,
                    status="INVESTIGATING",
                )
            else:
                inv_rec.status = "INVESTIGATING"
                self.session.flush()
        else:
            inv_rec = self.investigation_repo.create_investigation(
                tenant_id=tenant_id,
                target_type=target_type,
                target_id=target_id,
                status="INVESTIGATING",
            )

        orchestrator = InvestigationOrchestrator(
            session=self.session,
            tenant_id=tenant_id,
            provider=active_provider,
        )

        try:
            result, ver_result, raw_resp = orchestrator.execute_investigation(
                investigation_id=inv_rec.id,
                target_type=target_type,
                target_id=target_id,
            )

            # Map status
            final_status = "COMPLETED"
            if ver_result.status == VerifierStatus.INVALID:
                final_status = "INVALID"
            elif ver_result.status == VerifierStatus.NEEDS_REVIEW or result.requires_human_review:
                final_status = "NEEDS_REVIEW"

            self.investigation_repo.record_investigation_result(
                tenant_id=tenant_id,
                investigation_id=inv_rec.id,
                status=final_status,
                classification=result.classification.value,
                summary=result.executive_summary,
                result_dict=result.model_dump(mode="json"),
                evidence_references=[ev.model_dump(mode="json") for ev in result.supporting_evidence],
                verifier_status=ver_result.status.value,
                verifier_details=ver_result.to_dict(),
                model_provider=raw_resp.provider_name,
                model_name=raw_resp.model_name,
                prompt_version=raw_resp.prompt_version,
            )

            # Append tamper-evident audit event
            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="INVESTIGATION_COMPLETED",
                actor=f"ai_investigator:{raw_resp.provider_name}",
                subject_type="investigation",
                subject_id=inv_rec.id,
                details={
                    "target_type": target_type,
                    "target_id": target_id,
                    "classification": result.classification.value,
                    "verifier_status": ver_result.status.value,
                    "verifier_passed": ver_result.passed,
                    "evidence_count": len(result.supporting_evidence),
                    "risk_flags": result.risk_flags,
                },
            )

            self.session.commit()
            return inv_rec, result, ver_result

        except Exception as exc:
            logger.error("Investigation %s failed: %s", inv_rec.id, exc, exc_info=True)
            self.investigation_repo.update_status(
                tenant_id=tenant_id,
                investigation_id=inv_rec.id,
                status="FAILED",
                error_message=str(exc),
            )
            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="INVESTIGATION_FAILED",
                actor="ai_investigator",
                subject_type="investigation",
                subject_id=inv_rec.id,
                details={"error": str(exc)},
            )
            self.session.commit()
            raise
