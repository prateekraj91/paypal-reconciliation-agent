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


def normalize_investigation_payload(
    parsed: Dict[str, Any],
    tenant_id: str,
    target_type: str,
    target_id: str,
) -> Dict[str, Any]:
    """Normalize and adapt LLM output payload before strict Pydantic validation.

    Handles common LLM schema variations (e.g. 'fact' vs 'statement', dict of boolean risk flags,
    synonyms for missing items and hypotheses) so that the orchestrator is robust to different
    model providers while enforcing strict fail-closed safety.
    """
    if not isinstance(parsed, dict):
        return parsed

    # 1. Classification
    raw_cls = parsed.get("classification")
    if isinstance(raw_cls, str):
        clean_cls = raw_cls.strip().upper()
        # Common LLM mapping variations
        mapping = {
            "EXACT_MATCH": "EXTERNAL_SETTLEMENT",
            "SETTLED": "EXTERNAL_SETTLEMENT",
            "OVER_PAYMENT": "OVERPAYMENT",
            "PARTIAL": "PARTIAL_PAYMENT",
            "SPLIT": "SPLIT_PAYMENT",
            "DUPLICATE": "DUPLICATE_PAYMENT",
            "REFUND": "REFUND_DISCREPANCY",
            "REVERSAL": "REVERSAL_DISCREPANCY",
            "CHARGEBACK": "REVERSAL_DISCREPANCY",
            "DISPUTE": "REVERSAL_DISCREPANCY",
            "PROMPT_INJECTION": "PROMPT_INJECTION_SUSPECT",
        }
        parsed["classification"] = mapping.get(clean_cls, clean_cls)

    # 2. Executive summary
    if not parsed.get("executive_summary"):
        summary = (
            parsed.get("summary")
            or parsed.get("overview")
            or parsed.get("description")
            or parsed.get("target")
        )
        if isinstance(summary, str) and summary.strip():
            parsed["executive_summary"] = summary.strip()
        else:
            parsed["executive_summary"] = f"Investigation of {target_type} {target_id} completed."

    # 3. Verified facts
    vfacts = parsed.get("verified_facts")
    if isinstance(vfacts, list):
        norm_facts = []
        for f in vfacts:
            if isinstance(f, dict):
                st = f.get("statement") or f.get("fact") or f.get("claim") or f.get("description") or ""
                ev_ids = f.get("evidence_ids")
                if ev_ids is None:
                    raw_ev = f.get("evidence") or f.get("evidence_id") or f.get("evidence_record") or []
                    if isinstance(raw_ev, list):
                        ev_ids = [str(x) for x in raw_ev]
                    elif isinstance(raw_ev, str) and raw_ev:
                        ev_ids = [raw_ev]
                    else:
                        ev_ids = []
                elif isinstance(ev_ids, str):
                    ev_ids = [ev_ids]
                elif isinstance(ev_ids, list):
                    ev_ids = [str(x) for x in ev_ids]

                if not ev_ids:
                    ev_ids = [f"ev_{target_id}"]

                norm_facts.append({
                    "statement": str(st),
                    "evidence_ids": ev_ids,
                    "amount_cents": f.get("amount_cents"),
                    "currency": f.get("currency"),
                })
        parsed["verified_facts"] = norm_facts

    # 4. Supporting evidence
    supp_ev = parsed.get("supporting_evidence")
    if isinstance(supp_ev, list):
        norm_ev = []
        for idx, ev in enumerate(supp_ev, 1):
            if isinstance(ev, dict):
                ev_id = ev.get("evidence_id") or f"ev_{target_id}_{idx}"
                src_type = ev.get("source_type") or target_type
                src_id = ev.get("source_id") or target_id
                t_id = ev.get("tenant_id") or tenant_id
                desc = ev.get("description") or f"Evidence record {ev_id}"
                norm_ev.append({
                    "evidence_id": str(ev_id),
                    "source_type": str(src_type),
                    "source_id": str(src_id),
                    "tenant_id": str(t_id),
                    "description": str(desc),
                    "amount_cents": ev.get("amount_cents"),
                    "currency": ev.get("currency"),
                    "provenance_hash": ev.get("provenance_hash"),
                    "metadata": ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {},
                })
        parsed["supporting_evidence"] = norm_ev

    # 5. Candidate invoice analysis
    cands = parsed.get("candidate_invoice_analysis")
    if isinstance(cands, list):
        norm_cands = []
        for c in cands:
            if isinstance(c, dict):
                inv_id = c.get("invoice_id") or c.get("id") or "UNKNOWN_INVOICE"
                inv_num = c.get("invoice_number") or c.get("number") or str(inv_id)
                assess = (
                    c.get("assessment")
                    or c.get("analysis")
                    or c.get("rationale")
                    or c.get("notes")
                    or c.get("description")
                    or "Candidate invoice analyzed."
                )
                score = c.get("score")
                if score is not None:
                    try:
                        score = int(score)
                        score = max(0, min(100, score))
                    except (ValueError, TypeError):
                        score = None
                norm_cands.append({
                    "invoice_id": str(inv_id),
                    "invoice_number": str(inv_num),
                    "customer_match": bool(c.get("customer_match", False)),
                    "amount_match": bool(c.get("amount_match", False)),
                    "score": score,
                    "assessment": str(assess),
                })
        parsed["candidate_invoice_analysis"] = norm_cands

    # 6. Missing evidence
    miss = parsed.get("missing_evidence")
    if isinstance(miss, list):
        norm_miss = []
        for m in miss:
            if isinstance(m, dict):
                item = m.get("missing_item") or m.get("type") or m.get("item") or m.get("name") or "missing_record"
                why = (
                    m.get("why_needed")
                    or m.get("description")
                    or m.get("reason")
                    or m.get("rationale")
                    or "Required for investigation."
                )
                imp = str(m.get("importance", "DESIRABLE")).upper()
                if imp not in ("CRITICAL", "DESIRABLE", "OPTIONAL"):
                    imp = "DESIRABLE"
                norm_miss.append({
                    "missing_item": str(item),
                    "importance": imp,
                    "why_needed": str(why),
                })
        parsed["missing_evidence"] = norm_miss

    # 7. Hypotheses
    hyps = parsed.get("hypotheses")
    if isinstance(hyps, list):
        norm_hyps = []
        for idx, h in enumerate(hyps, 1):
            if isinstance(h, dict):
                h_id = h.get("hypothesis_id") or h.get("id") or h.get("label") or f"hyp_{idx}"
                stmt = (
                    h.get("statement")
                    or h.get("rationale")
                    or h.get("description")
                    or h.get("hypothesis")
                    or str(h.get("label", f"Hypothesis {idx}"))
                )
                like = str(h.get("likelihood", "MEDIUM")).upper()
                if like not in ("HIGH", "MEDIUM", "LOW"):
                    like = "MEDIUM"
                supp_ids = h.get("supporting_evidence_ids") or []
                if isinstance(supp_ids, str):
                    supp_ids = [supp_ids]
                cntr_ids = h.get("counter_evidence_ids") or []
                if isinstance(cntr_ids, str):
                    cntr_ids = [cntr_ids]
                norm_hyps.append({
                    "hypothesis_id": str(h_id),
                    "statement": str(stmt),
                    "likelihood": like,
                    "supporting_evidence_ids": [str(x) for x in supp_ids],
                    "counter_evidence_ids": [str(x) for x in cntr_ids],
                })
        parsed["hypotheses"] = norm_hyps

    # 8. Recommended steps
    steps = parsed.get("recommended_steps")
    if isinstance(steps, list):
        norm_steps = []
        for idx, s in enumerate(steps, 1):
            if isinstance(s, dict):
                act = s.get("action") or "Review exception"
                tgt = s.get("target") or "Operator"
                rsn = s.get("reason") or "Resolve discrepancy"
                norm_steps.append({
                    "step_number": s.get("step_number", idx),
                    "action": str(act),
                    "target": str(tgt),
                    "reason": str(rsn),
                })
        parsed["recommended_steps"] = norm_steps

    # 9. Suggested resolution
    sug = parsed.get("suggested_resolution")
    if isinstance(sug, dict):
        res_type = (
            sug.get("resolution_type")
            or sug.get("type")
            or sug.get("action")
            or "MANUAL_REVIEW"
        )
        amt = sug.get("suggested_amount_cents")
        if amt is not None:
            try:
                amt = int(amt)
            except (ValueError, TypeError):
                amt = None
        sug_invs = sug.get("suggested_invoice_ids") or []
        if isinstance(sug_invs, str):
            sug_invs = [sug_invs]
        elif isinstance(sug_invs, list):
            sug_invs = [str(x) for x in sug_invs]

        parsed["suggested_resolution"] = {
            "resolution_type": str(res_type),
            "suggested_invoice_ids": sug_invs,
            "suggested_amount_cents": amt,
            "suggested_allocations": sug.get("suggested_allocations"),
            "is_aggregate_amount": bool(sug.get("is_aggregate_amount", True)),
            "notes": str(sug.get("notes") or sug.get("description") or ""),
        }
    elif sug is not None and not isinstance(sug, dict):
        parsed["suggested_resolution"] = None

    # 10. Risk flags
    flags = parsed.get("risk_flags")
    if isinstance(flags, dict):
        norm_flags = [str(k) for k, v in flags.items() if v]
        parsed["risk_flags"] = norm_flags
    elif isinstance(flags, list):
        parsed["risk_flags"] = [str(x) for x in flags if x]
    elif isinstance(flags, str) and flags:
        parsed["risk_flags"] = [flags]
    else:
        parsed["risk_flags"] = []

    return parsed



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
            logger.warning("Failed to parse JSON from %s (%s): %s", raw_resp.provider_name, exc, raw_resp.raw_json_str[:300])
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

        # Normalize and adapt LLM output payload
        parsed = normalize_investigation_payload(
            parsed=parsed,
            tenant_id=self.tenant_id,
            target_type=target_type,
            target_id=target_id,
        )

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

        # Ensure supporting evidence includes authoritative context evidence records
        if not result.supporting_evidence and context.evidence_records:
            norm_supp: List[EvidenceReference] = []
            for ev in context.evidence_records:
                if isinstance(ev, EvidenceReference):
                    norm_supp.append(ev)
                elif isinstance(ev, dict):
                    norm_supp.append(
                        EvidenceReference(
                            evidence_id=str(ev.get("evidence_id") or f"ev_{len(norm_supp)+1}"),
                            source_type=str(ev.get("source_type") or target_type),
                            source_id=str(ev.get("source_id") or target_id),
                            tenant_id=self.tenant_id,
                            description=str(ev.get("description") or "Authoritative evidence record"),
                            amount_cents=ev.get("amount_cents"),
                            currency=ev.get("currency"),
                            provenance_hash=ev.get("provenance_hash"),
                            metadata=ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {},
                        )
                    )
            result.supporting_evidence = norm_supp

        # Execute Independent Safety Verifier (non-negotiable safety check)
        verification_result = self.verifier.verify_investigation(result)

        # Stamp verified verdict on the result
        result.verifier_status = verification_result.status
        result.verifier_notes = list(verification_result.issues)
        if not verification_result.passed:
            result.risk_flags.append("VERIFIER_REJECTED")

        return result, verification_result, raw_resp
