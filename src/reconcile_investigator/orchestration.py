"""Orchestration layer coordinating tools, prompt construction, model inference, and safety verification."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from .models import (
    InvestigationClassification,
    InvestigationResult,
    VerifierStatus,
    EvidenceReference,
)
from .tools.evidence_tools import EvidenceToolKit
from .tools.financial_tools import FinancialAnalyzer
from .providers.base import (
    InvestigationContext,
    InvestigationModelProvider,
    InvestigationRawResponse,
)
from .providers.mock import MockInvestigationProvider
from .prompts import SYSTEM_PROMPT, format_user_prompt
from .verifier import IndependentSafetyVerifier, SafetyVerificationResult

logger = logging.getLogger(__name__)


class InvestigationOrchestrator:
    """Coordinates context assembly, model invocation, and independent safety verification."""

    def __init__(
        self,
        session: Session,
        tenant_id: str,
        provider: Optional[InvestigationModelProvider] = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.toolkit = EvidenceToolKit(session, tenant_id)
        self.financial_analyzer = FinancialAnalyzer(self.toolkit)
        self.verifier = IndependentSafetyVerifier(session, tenant_id)
        self.provider = provider or MockInvestigationProvider()

    def build_context(self, investigation_id: str, target_type: str, target_id: str) -> InvestigationContext:
        """Assemble all authoritative evidence across the system for the investigation target."""
        target_data: Dict[str, Any] = {}
        candidate_invoices: List[Dict[str, Any]] = []
        evidence_records: List[Dict[str, Any]] = []
        prompt_injection_warning = False

        if target_type == "payment":
            pay_detail = self.toolkit.get_payment_details(target_id)
            if pay_detail:
                target_data = pay_detail
                if pay_detail.get("evidence"):
                    evidence_records.append(pay_detail["evidence"])
                if pay_detail.get("has_prompt_injection_flag"):
                    prompt_injection_warning = True

                # Search candidate invoices
                candidate_invoices = self.toolkit.get_candidate_invoices(
                    customer_name=pay_detail.get("payer_name"),
                    customer_email=pay_detail.get("payer_email"),
                    amount_cents=pay_detail.get("amount_cents"),
                    currency=pay_detail.get("currency"),
                    reference=pay_detail.get("reference"),
                )
                for cand in candidate_invoices:
                    if cand.get("evidence"):
                        evidence_records.append(cand["evidence"])

        elif target_type == "proposal":
            ambig = self.toolkit.get_ambiguity_certificate(target_id)
            if ambig:
                target_data = ambig
                if ambig.get("evidence"):
                    evidence_records.append(ambig["evidence"])
            # Also get payment details if available
            pay_detail = self.toolkit.get_payment_details(target_id)
            if pay_detail:
                if pay_detail.get("evidence"):
                    evidence_records.append(pay_detail["evidence"])

        elif target_type == "invoice":
            inv_detail = self.toolkit.get_invoice_details(target_id)
            if inv_detail:
                target_data = inv_detail
                if inv_detail.get("evidence"):
                    evidence_records.append(inv_detail["evidence"])
                if inv_detail.get("has_prompt_injection_flag"):
                    prompt_injection_warning = True

        related_events = self.toolkit.get_related_financial_events(target_id)
        missing_evidence = self.toolkit.get_missing_evidence(target_type, target_id)

        # Append related settlements, refunds, reversals to evidence records
        for s in related_events.get("settlements") or []:
            evidence_records.append(s)
        for r in related_events.get("refunds") or []:
            evidence_records.append(r)
        for rev in related_events.get("reversals") or []:
            evidence_records.append(rev)

        return InvestigationContext(
            investigation_id=investigation_id,
            tenant_id=self.tenant_id,
            target_type=target_type,
            target_id=target_id,
            target_data=target_data,
            evidence_records=evidence_records,
            candidate_invoices=candidate_invoices,
            related_events=related_events,
            missing_evidence=missing_evidence,
            suspected_prompt_injection=prompt_injection_warning,
        )

    def execute_investigation(
        self,
        investigation_id: str,
        target_type: str,
        target_id: str,
    ) -> tuple[InvestigationResult, SafetyVerificationResult, InvestigationRawResponse]:
        """Assemble context, invoke LLM, parse response, and run independent safety verification."""
        context = self.build_context(investigation_id, target_type, target_id)
        user_prompt = format_user_prompt(context)

        # Invoke model
        raw_resp = self.provider.generate_investigation(
            context=context,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        # Parse JSON output fail-closed
        try:
            parsed = json.loads(raw_resp.raw_json_str)
        except Exception as exc:
            # Model output malformed JSON -> Fail-closed invalid result
            invalid_result = InvestigationResult(
                investigation_id=investigation_id,
                tenant_id=self.tenant_id,
                target_type=target_type,
                target_id=target_id,
                classification=InvestigationClassification.UNKNOWN_EXCEPTION,
                executive_summary="Model returned unparseable or malformed JSON.",
                verifier_status=VerifierStatus.INVALID,
                verifier_notes=[f"Malformed JSON output: {exc}"],
                risk_flags=["MALFORMED_MODEL_OUTPUT"],
                requires_human_review=True,
                model_info={
                    "provider": raw_resp.provider_name,
                    "model": raw_resp.model_name,
                    "prompt_version": raw_resp.prompt_version,
                },
                completed_at=datetime.now(timezone.utc),
            )
            ver_fail = SafetyVerificationResult(
                status=VerifierStatus.INVALID,
                passed=False,
                issues=(f"Malformed JSON output from model: {exc}",),
                summary="Rejected: Model output failed JSON parsing.",
            )
            return invalid_result, ver_fail, raw_resp

        # Inject mandatory identifiers if missing
        parsed.setdefault("investigation_id", investigation_id)
        parsed.setdefault("tenant_id", self.tenant_id)
        parsed.setdefault("target_type", target_type)
        parsed.setdefault("target_id", target_id)
        parsed["model_info"] = {
            "provider": raw_resp.provider_name,
            "model": raw_resp.model_name,
            "prompt_version": raw_resp.prompt_version,
            "latency_ms": raw_resp.latency_ms,
        }
        parsed["completed_at"] = datetime.now(timezone.utc)

        # Validate into Pydantic model
        try:
            result = InvestigationResult.model_validate(parsed)
        except Exception as exc:
            invalid_result = InvestigationResult(
                investigation_id=investigation_id,
                tenant_id=self.tenant_id,
                target_type=target_type,
                target_id=target_id,
                classification=InvestigationClassification.UNKNOWN_EXCEPTION,
                executive_summary="Model returned structured output that violates schema constraints.",
                verifier_status=VerifierStatus.INVALID,
                verifier_notes=[f"Schema validation error: {exc}"],
                risk_flags=["SCHEMA_VALIDATION_ERROR"],
                requires_human_review=True,
                model_info=parsed.get("model_info", {}),
                completed_at=datetime.now(timezone.utc),
            )
            ver_fail = SafetyVerificationResult(
                status=VerifierStatus.INVALID,
                passed=False,
                issues=(f"Schema validation failed: {exc}",),
                summary="Rejected: Structured output does not conform to Pydantic model schema.",
            )
            return invalid_result, ver_fail, raw_resp

        # Execute Independent Safety Verifier (non-negotiable safety check)
        verification_result = self.verifier.verify_investigation(result)

        # Stamp verified verdict on the result
        result.verifier_status = verification_result.status
        result.verifier_notes = list(verification_result.issues)
        if not verification_result.passed:
            result.risk_flags.append("VERIFIER_REJECTED")

        return result, verification_result, raw_resp
