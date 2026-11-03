"""Repositories for reconciliation runs, proposals, verifications, and decisions."""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict, List
from datetime import datetime, timezone
import json
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError
from ..schema import (
    ReconciliationRunRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    VerificationRecord,
    DecisionRecord,
)
from ..canonical import canonical_json


class ReconciliationRunRepository(BaseRepository[ReconciliationRunRecord]):
    """Repository for tracking reconciliation batch runs."""

    def create_run(
        self,
        tenant_id: str,
        input_snapshot_id: str,
        matcher_name: str,
        matcher_version: str = "1.0.0",
        summary_metrics: Optional[Dict[str, Any]] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
        status: str = "COMPLETED",
    ) -> ReconciliationRunRecord:
        run_record = ReconciliationRunRecord(
            tenant_id=tenant_id,
            input_snapshot_id=input_snapshot_id,
            matcher_name=matcher_name,
            matcher_version=matcher_version,
            status=status,
            metrics_json=canonical_json(summary_metrics or {}),
            config_json=canonical_json(config_snapshot or {}),
            run_at=datetime.now(timezone.utc),
        )
        self.session.add(run_record)
        self.session.flush()
        return run_record

    def get_run(self, tenant_id: str, run_id: str) -> Optional[ReconciliationRunRecord]:
        run = self.session.get(ReconciliationRunRecord, run_id)
        if run:
            self._ensure_tenant(run.tenant_id, tenant_id)
        return run

    def list_runs(self, tenant_id: str) -> Sequence[ReconciliationRunRecord]:
        stmt = (
            select(ReconciliationRunRecord)
            .where(ReconciliationRunRecord.tenant_id == tenant_id)
            .order_by(ReconciliationRunRecord.run_at.desc())
        )
        return self.session.scalars(stmt).all()


class ProposalRepository(BaseRepository[ProposalRecord]):
    """Repository for persisting proposed reconciliation outcomes.

    Invariant I6: Matchers and AI generate PROPOSED outcomes only.
    Neither can directly create APPROVED allocations.
    """

    def create_proposal(
        self,
        tenant_id: str,
        run_id: str,
        payment_id: str,
        outcome: str,
        evidence: Optional[Sequence[Any]] = None,
        source_hashes: Optional[Dict[str, Any]] = None,
        verifier_status: str = "NOT_VERIFIED",
        allocations: Optional[List[Dict[str, Any]]] = None,
    ) -> ProposalRecord:
        if verifier_status.upper() == "APPROVED":
            raise ValueError(
                "Proposals cannot be initialized as APPROVED (Invariant I6). "
                "Only the atomic approval engine can create approved allocations."
            )

        proposal = ProposalRecord(
            tenant_id=tenant_id,
            run_id=run_id,
            payment_id=payment_id,
            outcome=outcome,
            evidence_json=canonical_json(list(evidence or [])),
            source_hashes_json=canonical_json(source_hashes or {}),
            verifier_status=verifier_status,
            version=1,
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(proposal)
        self.session.flush()

        if allocations:
            for alloc_data in allocations:
                amt = alloc_data["amount_minor"]
                if amt <= 0:
                    raise ValueError(f"Proposal allocation amount must be positive, got {amt}")
                prop_alloc = ProposalAllocationRecord(
                    tenant_id=tenant_id,
                    proposal_id=proposal.id,
                    payment_id=payment_id,
                    invoice_id=alloc_data["invoice_id"],
                    amount_minor=amt,
                    currency=alloc_data["currency"].upper(),
                    evidence_json=canonical_json(alloc_data.get("evidence", [])),
                )
                self.session.add(prop_alloc)
            self.session.flush()

        return proposal

    def get_proposal(self, tenant_id: str, proposal_id: str) -> Optional[ProposalRecord]:
        proposal = self.session.get(ProposalRecord, proposal_id)
        if proposal:
            self._ensure_tenant(proposal.tenant_id, tenant_id)
        return proposal

    def list_by_run(self, tenant_id: str, run_id: str) -> Sequence[ProposalRecord]:
        stmt = (
            select(ProposalRecord)
            .where(ProposalRecord.tenant_id == tenant_id, ProposalRecord.run_id == run_id)
            .order_by(ProposalRecord.created_at)
        )
        return self.session.scalars(stmt).all()

    def list_by_payment(self, tenant_id: str, payment_id: str) -> Sequence[ProposalRecord]:
        stmt = (
            select(ProposalRecord)
            .where(
                ProposalRecord.tenant_id == tenant_id,
                ProposalRecord.payment_id == payment_id,
            )
            .order_by(ProposalRecord.created_at.desc())
        )
        return self.session.scalars(stmt).all()


class VerificationRepository(BaseRepository[VerificationRecord]):
    """Repository for independent verifications.

    Invariant: Verification records are strictly append-only.
    """

    def record_verification(
        self,
        tenant_id: str,
        proposal_id: str,
        check_name: str,
        check_inputs: Dict[str, Any],
        passed: bool,
        failure_reason: Optional[str] = None,
        source_hashes: Optional[Dict[str, Any]] = None,
    ) -> VerificationRecord:
        verification = VerificationRecord(
            tenant_id=tenant_id,
            proposal_id=proposal_id,
            check_name=check_name,
            check_inputs_json=canonical_json(check_inputs),
            passed=passed,
            failure_reason=failure_reason,
            source_hashes_json=canonical_json(source_hashes or {}),
            verified_at=datetime.now(timezone.utc),
        )
        self.session.add(verification)
        self.session.flush()
        return verification

    def list_by_proposal(
        self, tenant_id: str, proposal_id: str
    ) -> Sequence[VerificationRecord]:
        stmt = (
            select(VerificationRecord)
            .where(
                VerificationRecord.tenant_id == tenant_id,
                VerificationRecord.proposal_id == proposal_id,
            )
            .order_by(VerificationRecord.verified_at)
        )
        return self.session.scalars(stmt).all()


class DecisionRepository(BaseRepository[DecisionRecord]):
    """Repository for human/authorized decision records."""

    ALLOWED_DECISION_TYPES = {"APPROVED", "REJECTED", "ESCALATED"}

    def record_decision(
        self,
        tenant_id: str,
        proposal_id: str,
        decision_type: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> DecisionRecord:
        decision_type_upper = decision_type.upper()
        if decision_type_upper not in self.ALLOWED_DECISION_TYPES:
            raise ValueError(
                f"Invalid decision type '{decision_type}'. Must be one of {self.ALLOWED_DECISION_TYPES}"
            )

        stmt = select(DecisionRecord).where(
            DecisionRecord.tenant_id == tenant_id,
            DecisionRecord.idempotency_key == idempotency_key,
        )
        existing = self.session.scalars(stmt).first()
        if existing:
            if existing.proposal_id != proposal_id or existing.decision_type != decision_type_upper:
                raise ConflictError(
                    f"Idempotency key '{idempotency_key}' conflict: already used for proposal "
                    f"'{existing.proposal_id}' with decision '{existing.decision_type}'"
                )
            return existing

        decision = DecisionRecord(
            tenant_id=tenant_id,
            proposal_id=proposal_id,
            decision_type=decision_type_upper,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            decided_at=datetime.now(timezone.utc),
        )
        self.session.add(decision)
        self.session.flush()
        return decision

    def get_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> Optional[DecisionRecord]:
        stmt = select(DecisionRecord).where(
            DecisionRecord.tenant_id == tenant_id,
            DecisionRecord.idempotency_key == idempotency_key,
        )
        return self.session.scalars(stmt).first()

    def get_by_proposal(
        self, tenant_id: str, proposal_id: str
    ) -> Sequence[DecisionRecord]:
        stmt = (
            select(DecisionRecord)
            .where(
                DecisionRecord.tenant_id == tenant_id,
                DecisionRecord.proposal_id == proposal_id,
            )
            .order_by(DecisionRecord.decided_at.desc())
        )
        return self.session.scalars(stmt).all()
