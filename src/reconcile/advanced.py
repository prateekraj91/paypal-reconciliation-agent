"""Advanced Reconciliation Matcher (T1 - T6).

Combines deterministic reference matching (T1, T3) with strong multi-signal
contextual matching (T4), composite/split/partial/overpayment resolution (T5),
and exception-aware ambiguity detection with evidence certificates (T6).

Architectural Invariants:
1. Integer minor units only (no floats for money).
2. Never turn an amount coincidence into an automatic allocation.
   Contextual matching strictly requires multiple independent corroborating signals.
3. Overpayments allocate up to invoice capacity and explicitly record excess amount.
4. Ambiguous candidates produce a deterministic AmbiguityCertificate and require human review.
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .ambiguity import build_ambiguity_certificate, create_ambiguous_proposal
from .baseline import BaselineConfig, BaselineMatcher
from .matching import MatchRun
from .models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
)
from .normalize import canonical_reference, extract_invoice_references

_INVOICE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:INV|INVOICE)[^A-Za-z0-9]*[0-9]+(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_DIGITS_PATTERN = re.compile(r"\b\d{3,8}\b")


def _normalize_ref(val: str) -> str:
    c = canonical_reference(val)
    if c.startswith("INVOICE"):
        c = "INV" + c[7:]
    return c


def _normalize_text(val: Optional[str]) -> str:
    if not val:
        return ""
    return re.sub(r"[^a-z0-9]", "", val.lower())


def _token_set(val: Optional[str]) -> Set[str]:
    if not val:
        return set()
    return set(re.findall(r"[a-z0-9]+", val.lower()))


def _name_similarity(name1: Optional[str], name2: Optional[str]) -> float:
    if not name1 or not name2:
        return 0.0
    n1 = name1.strip().lower()
    n2 = name2.strip().lower()
    if n1 == n2:
        return 1.0
    t1 = _token_set(n1)
    t2 = _token_set(n2)
    if t1 and t2:
        overlap = len(t1 & t2) / max(len(t1), len(t2))
        if overlap >= 0.75:
            return overlap
    return difflib.SequenceMatcher(None, _normalize_text(n1), _normalize_text(n2)).ratio()


def _email_similarity(email1: Optional[str], email2: Optional[str]) -> float:
    if not email1 or not email2:
        return 0.0
    e1 = email1.strip().lower()
    e2 = email2.strip().lower()
    if e1 == e2:
        return 1.0
    u1, _, d1 = e1.partition("@")
    u2, _, d2 = e2.partition("@")
    if u1 and u1 == u2:
        return 0.8
    if d1 and d1 == d2 and d1 not in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}:
        return 0.4
    return 0.0


def _date_diff_days(p_time: datetime, inv_date: date) -> int:
    return abs((p_time.date() - inv_date).days)


class AdvancedMatcher(BaselineMatcher):
    """Advanced financial reconciliation matcher covering T1 through T6.

    Tier 1: Exact reference match.
    Tier 2/4: Strong multi-signal contextual matching when reference is absent or free-text.
    Tier 3/5: Composite, split, partial, and overpayment resolution.
    Tier 6: Exception-aware ambiguity certificates and safe conflict detection.
    """

    def __init__(
        self,
        name: str = "advanced-matcher-d5",
        contextual_threshold: float = 65.0,
        ambiguity_margin: float = 12.0,
    ) -> None:
        super().__init__(
            BaselineConfig(allow_amount_only=False),
            name=name,
        )
        self.contextual_threshold = contextual_threshold
        self.ambiguity_margin = ambiguity_margin

    def _match_one(self, p: Payment, run: MatchRun) -> MatchProposal:
        # 1. Screen deliveries (duplicates, contradictions, uncompleted, provider settled)
        blocker = self.screen_delivery(p, run)
        if blocker is not None:
            if blocker.code is EvidenceCode.PROVIDER_SETTLED:
                return MatchProposal(
                    payment_id=p.payment_id,
                    outcome=MatchOutcome.PROVIDER_SETTLED,
                    candidates=(),
                    reasons=(blocker,),
                    matcher=self.name,
                )
            return self.no_match(p, blocker)

        invoice_numbers = [invoice.number for invoice in run.invoices]
        known_canonicals = {_normalize_ref(num): num for num in invoice_numbers}

        ref_hits: list[str] = []
        note_hits: list[str] = []
        unknown_ref_found = False

        # 2a. Inspect structured reference field
        if p.reference and canonical_reference(p.reference):
            segments = [
                s.strip()
                for s in re.split(r"[,;/]|\band\b|&", p.reference)
                if s.strip()
            ]
            seen_in_ref: set[str] = set()
            for seg in segments:
                extracted = extract_invoice_references([seg], invoice_numbers)
                if not extracted:
                    unknown_ref_found = True
                    break
                for num in extracted:
                    canon = _normalize_ref(num)
                    if canon in seen_in_ref:
                        return self.no_match(
                            p,
                            Evidence(
                                EvidenceCode.MULTIPLE_CANDIDATES,
                                f"duplicate invoice reference {num!r} found in payment reference",
                            ),
                        )
                    seen_in_ref.add(canon)
                    ref_hits.append(num)

        if unknown_ref_found:
            return self.no_match(
                p,
                Evidence(
                    EvidenceCode.REFERENCE_UNKNOWN,
                    f"could not identify a known invoice in reference {p.reference!r}",
                ),
            )

        # 2b. Inspect payment note
        if p.note and canonical_reference(p.note):
            pattern_matches = list(_INVOICE_PATTERN.finditer(p.note))
            for match in pattern_matches:
                canon = _normalize_ref(match.group(0))
                if canon not in known_canonicals:
                    return self.no_match(
                        p,
                        Evidence(
                            EvidenceCode.REFERENCE_UNKNOWN,
                            f"could not identify a known invoice for {match.group(0)!r} in payment note",
                        ),
                    )

            extracted_note = extract_invoice_references([p.note], invoice_numbers)
            if pattern_matches:
                match_canons = [_normalize_ref(m.group(0)) for m in pattern_matches]
                if len(match_canons) != len(set(match_canons)):
                    return self.no_match(
                        p,
                        Evidence(
                            EvidenceCode.MULTIPLE_CANDIDATES,
                            "duplicate invoice reference found in payment note",
                        ),
                    )

            note_hits.extend(extracted_note)

        # Combine distinct invoice references
        all_hits: list[str] = []
        seen_all: set[str] = set()
        for num in ref_hits + note_hits:
            canon = _normalize_ref(num)
            if canon not in seen_all:
                seen_all.add(canon)
                all_hits.append(num)

        # ------------------------------------------------------------------
        # PATH A: Explicit reference hits found (T1, T3, T5)
        # ------------------------------------------------------------------
        if all_hits:
            return self._match_referenced_invoices(p, all_hits, ref_hits, run)

        # ------------------------------------------------------------------
        # PATH B: No explicit reference hits -> Multi-Signal Contextual (T4, T5, T6)
        # ------------------------------------------------------------------
        return self._match_contextual(p, run)

    def _match_referenced_invoices(
        self,
        p: Payment,
        all_hits: list[str],
        ref_hits: list[str],
        run: MatchRun,
    ) -> MatchProposal:
        """Handle payments with explicit references, including partial and overpayments."""
        matched_invoices: list[Invoice] = []
        for ref in all_hits:
            hits = run.by_ref.get(canonical_reference(ref), [])
            if not hits:
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.REFERENCE_UNKNOWN, f"no invoice found for {ref!r}"),
                )
            if len(hits) > 1:
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.MULTIPLE_CANDIDATES, f"invoice reference {ref!r} is not unique"),
                )
            matched_invoices.append(hits[0])

        if len({inv.invoice_id for inv in matched_invoices}) != len(matched_invoices):
            return self.no_match(
                p,
                Evidence(EvidenceCode.MULTIPLE_CANDIDATES, "duplicate invoice references found in payment"),
            )

        # Single invoice referenced
        if len(matched_invoices) == 1:
            inv = matched_invoices[0]
            if inv.currency.upper() != p.currency.upper():
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.CURRENCY_MISMATCH, f"{p.currency} payment vs {inv.currency} invoice"),
                )
            if not inv.is_open:
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.INVOICE_NOT_OPEN, f"invoice {inv.number} is {inv.status.value}"),
                )
            if inv.invoice_id in run.claimed:
                return self.no_match(
                    p,
                    Evidence(
                        EvidenceCode.INVOICE_ALREADY_CLAIMED,
                        f"invoice {inv.number} already claimed by proposal {run.claimed[inv.invoice_id]}",
                    ),
                )

            outstanding = run.outstanding[inv.invoice_id]
            if outstanding <= 0:
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.AMOUNT_MISMATCH, f"invoice {inv.number} has no outstanding balance"),
                )

            source = "reference field" if inv.number in ref_hits else "payment note"
            ref_ev = Evidence(EvidenceCode.REFERENCE_MATCH, f"invoice {inv.number} extracted from {source}")

            # Case 1: Exact amount
            if p.amount_cents == outstanding:
                run.claimed[inv.invoice_id] = p.payment_id
                amount_ev = Evidence(
                    EvidenceCode.AMOUNT_EXACT,
                    f"{p.amount_cents} cents equals outstanding balance on {inv.number}",
                )
                cand = Candidate(
                    allocations=(
                        PaymentAllocation(
                            payment_id=p.payment_id,
                            invoice_id=inv.invoice_id,
                            amount_cents=p.amount_cents,
                            currency=inv.currency,
                        ),
                    ),
                    evidence=(ref_ev, amount_ev),
                )
                return MatchProposal(
                    payment_id=p.payment_id,
                    outcome=MatchOutcome.PROPOSED,
                    candidates=(cand,),
                    matcher=self.name,
                )

            # Case 2: Partial payment
            elif p.amount_cents < outstanding:
                run.claimed[inv.invoice_id] = p.payment_id
                amount_ev = Evidence(
                    EvidenceCode.AMOUNT_PARTIAL,
                    f"{p.amount_cents} cents is a partial payment against {outstanding} cents outstanding on {inv.number}",
                )
                cand = Candidate(
                    allocations=(
                        PaymentAllocation(
                            payment_id=p.payment_id,
                            invoice_id=inv.invoice_id,
                            amount_cents=p.amount_cents,
                            currency=inv.currency,
                        ),
                    ),
                    evidence=(ref_ev, amount_ev),
                )
                return MatchProposal(
                    payment_id=p.payment_id,
                    outcome=MatchOutcome.PROPOSED,
                    candidates=(cand,),
                    matcher=self.name,
                )

            # Case 3: Overpayment
            else:
                excess_cents = p.amount_cents - outstanding
                run.claimed[inv.invoice_id] = p.payment_id
                over_ev = Evidence(
                    EvidenceCode.AMOUNT_OVERPAYMENT,
                    f"payment of {p.amount_cents} cents exceeds outstanding balance {outstanding} cents "
                    f"on {inv.number} by {excess_cents} cents excess",
                )
                cand = Candidate(
                    allocations=(
                        PaymentAllocation(
                            payment_id=p.payment_id,
                            invoice_id=inv.invoice_id,
                            amount_cents=outstanding,
                            currency=inv.currency,
                        ),
                    ),
                    evidence=(ref_ev, over_ev),
                )
                return MatchProposal(
                    payment_id=p.payment_id,
                    outcome=MatchOutcome.PROPOSED,
                    candidates=(cand,),
                    reasons=(over_ev,),
                    matcher=self.name,
                    excess_amount_cents=excess_cents,
                )

        # Multi-invoice grouped payment
        for inv in matched_invoices:
            if inv.currency.upper() != p.currency.upper():
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.CURRENCY_MISMATCH, f"{p.currency} payment vs {inv.currency} invoice {inv.number}"),
                )
            if not inv.is_open:
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.INVOICE_NOT_OPEN, f"invoice {inv.number} is {inv.status.value}"),
                )
            if inv.invoice_id in run.claimed:
                return self.no_match(
                    p,
                    Evidence(
                        EvidenceCode.INVOICE_ALREADY_CLAIMED,
                        f"invoice {inv.number} already claimed by proposal {run.claimed[inv.invoice_id]}",
                    ),
                )
            if run.outstanding[inv.invoice_id] <= 0:
                return self.no_match(
                    p,
                    Evidence(EvidenceCode.AMOUNT_MISMATCH, f"invoice {inv.number} has no outstanding balance"),
                )

        total_outstanding = sum(run.outstanding[inv.invoice_id] for inv in matched_invoices)
        if p.amount_cents != total_outstanding:
            return self.no_match(
                p,
                Evidence(
                    EvidenceCode.AMOUNT_MISMATCH,
                    f"payment {p.amount_cents} cents does not match total outstanding "
                    f"{total_outstanding} cents across referenced invoices",
                ),
            )

        for inv in matched_invoices:
            run.claimed[inv.invoice_id] = p.payment_id

        allocations = tuple(
            PaymentAllocation(
                payment_id=p.payment_id,
                invoice_id=inv.invoice_id,
                amount_cents=run.outstanding[inv.invoice_id],
                currency=inv.currency,
            )
            for inv in matched_invoices
        )
        inv_names = ", ".join(inv.number for inv in matched_invoices)
        evidence = (
            Evidence(EvidenceCode.REFERENCE_MATCH, f"invoices {inv_names} extracted from payment reference/note"),
            Evidence(EvidenceCode.AMOUNT_EXACT, f"{p.amount_cents} cents exactly matches total outstanding across referenced invoices"),
        )
        candidate = Candidate(allocations, evidence)
        return MatchProposal(
            payment_id=p.payment_id,
            outcome=MatchOutcome.PROPOSED,
            candidates=(candidate,),
            matcher=self.name,
        )

    def _match_contextual(self, p: Payment, run: MatchRun) -> MatchProposal:
        """Perform strong multi-signal contextual matching when explicit reference is missing."""
        eligible_invoices: list[Invoice] = [
            inv
            for inv in run.invoices
            if inv.is_open
            and inv.currency.upper() == p.currency.upper()
            and inv.invoice_id not in run.claimed
            and run.outstanding[inv.invoice_id] > 0
        ]

        if not eligible_invoices:
            return self.no_match(
                p,
                Evidence(EvidenceCode.NO_CANDIDATE, "no open invoice matching currency and available balance"),
            )

        scored_candidates: list[Tuple[Invoice, float, List[Evidence], List[str]]] = []

        for inv in eligible_invoices:
            outstanding = run.outstanding[inv.invoice_id]
            score = 0.0
            evidences: list[Evidence] = []
            non_amount_signals: list[str] = []

            # 1. Amount signals
            if p.amount_cents == outstanding:
                score += 30.0
                evidences.append(Evidence(EvidenceCode.AMOUNT_EXACT, f"{p.amount_cents} cents matches outstanding balance"))
            elif p.amount_cents < outstanding:
                score += 15.0
                evidences.append(Evidence(EvidenceCode.AMOUNT_PARTIAL, f"{p.amount_cents} cents fits within {outstanding} cents outstanding"))
            else:
                score += 10.0
                evidences.append(Evidence(EvidenceCode.AMOUNT_OVERPAYMENT, f"{p.amount_cents} cents exceeds {outstanding} cents outstanding"))

            # 2. Customer Name matching
            name_sim = _name_similarity(p.payer_name, inv.customer_name)
            if name_sim >= 0.95:
                score += 35.0
                evidences.append(Evidence(EvidenceCode.CUSTOMER_NAME_MATCH, f"exact/high-confidence customer name match: {inv.customer_name!r}"))
                non_amount_signals.append("CUSTOMER_NAME_EXACT")
            elif name_sim >= 0.75:
                score += 25.0
                evidences.append(Evidence(EvidenceCode.CUSTOMER_NAME_MATCH, f"fuzzy customer name match ({name_sim:.2f}): {inv.customer_name!r}"))
                non_amount_signals.append("CUSTOMER_NAME_FUZZY")

            # 3. Customer Email matching
            email_sim = _email_similarity(p.payer_email, inv.customer_email)
            if email_sim >= 0.95:
                score += 35.0
                evidences.append(Evidence(EvidenceCode.CUSTOMER_EMAIL_MATCH, f"exact customer email match: {inv.customer_email!r}"))
                non_amount_signals.append("CUSTOMER_EMAIL_EXACT")
            elif email_sim >= 0.60:
                score += 20.0
                evidences.append(Evidence(EvidenceCode.CUSTOMER_EMAIL_MATCH, f"customer email prefix/domain match: {inv.customer_email!r}"))
                non_amount_signals.append("CUSTOMER_EMAIL_PARTIAL")

            # 4. Fuzzy / Embedded invoice reference in free text
            inv_digits = "".join(re.findall(r"\d+", inv.number))
            if inv_digits and len(inv_digits) >= 3:
                full_text = f"{p.reference or ''} {p.note or ''}".lower()
                if inv_digits in full_text:
                    score += 30.0
                    evidences.append(Evidence(EvidenceCode.FUZZY_REFERENCE_MATCH, f"invoice digits {inv_digits} identified in payment text"))
                    non_amount_signals.append("FUZZY_REFERENCE")

            # 5. Temporal proximity
            issue_days = _date_diff_days(p.received_at, inv.issued_on)
            due_days = _date_diff_days(p.received_at, inv.due_on)
            min_days = min(issue_days, due_days)
            if min_days <= 7:
                score += 20.0
                evidences.append(Evidence(EvidenceCode.TEMPORAL_PROXIMITY_MATCH, f"payment received within {min_days} days of invoice date"))
                non_amount_signals.append("TEMPORAL_PROXIMITY_7D")
            elif min_days <= 14:
                score += 15.0
                evidences.append(Evidence(EvidenceCode.TEMPORAL_PROXIMITY_MATCH, f"payment received within {min_days} days of invoice date"))
                non_amount_signals.append("TEMPORAL_PROXIMITY_14D")
            elif min_days <= 30:
                score += 5.0
                evidences.append(Evidence(EvidenceCode.TEMPORAL_PROXIMITY_MATCH, f"payment received within {min_days} days of invoice date"))
                non_amount_signals.append("TEMPORAL_PROXIMITY_30D")

            scored_candidates.append((inv, score, evidences, non_amount_signals))

        # Sort candidates descending by score
        scored_candidates.sort(key=lambda item: (-item[1], item[0].number))

        # Filter qualifying candidates: must meet score threshold AND have at least 2 independent non-amount signals
        qualifying = [
            (inv, sc, evs, sigs)
            for inv, sc, evs, sigs in scored_candidates
            if sc >= self.contextual_threshold and len(sigs) >= 2
        ]

        # Case 1: Exact amount ties with no qualifying contextual winner (Ambiguity)
        exact_amount_invoices = [
            inv for inv in eligible_invoices if run.outstanding[inv.invoice_id] == p.amount_cents
        ]
        if len(exact_amount_invoices) >= 2 and not qualifying:
            # Emit structured ambiguity certificate for exact amount collision
            return create_ambiguous_proposal(
                payment=p,
                competing_invoices=exact_amount_invoices,
                matcher_name=self.name,
                scores={inv.invoice_id: 50.0 for inv in exact_amount_invoices},
                rejection_reasons=(
                    f"{len(exact_amount_invoices)} open invoices share identical outstanding balance of "
                    f"{p.amount_cents} cents with no differentiating reference or customer signals.",
                    "Amount-only matching is unsafe and disabled.",
                ),
                missing_evidence=(
                    "Specific invoice reference in reference or note",
                    "Differentiating customer identifier or payment remittance",
                ),
            )

        if not qualifying:
            return self.no_match(
                p,
                Evidence(
                    EvidenceCode.REFERENCE_ABSENT,
                    "no explicit invoice reference was found and contextual signals were insufficient "
                    f"(top candidate scored {scored_candidates[0][1]:.1f} vs threshold {self.contextual_threshold})",
                ),
            )

        # Check for ambiguity among qualifying candidates
        top_inv, top_score, top_evs, top_sigs = qualifying[0]
        competing = [
            inv for inv, sc, _, _ in qualifying
            if top_score - sc <= self.ambiguity_margin
        ]

        if len(competing) >= 2:
            scores_map = {inv.invoice_id: sc for inv, sc, _, _ in qualifying if inv in competing}
            ev_map = {inv.invoice_id: evs for inv, _, evs, _ in qualifying if inv in competing}
            return create_ambiguous_proposal(
                payment=p,
                competing_invoices=competing,
                matcher_name=self.name,
                scores=scores_map,
                candidate_evidence=ev_map,
                rejection_reasons=(
                    f"Multiple candidate invoices scored within ambiguity margin of {self.ambiguity_margin} points "
                    f"(top candidate scored {top_score:.1f}).",
                ),
            )

        # Confident single contextual match
        inv = top_inv
        outstanding = run.outstanding[inv.invoice_id]
        run.claimed[inv.invoice_id] = p.payment_id

        context_evidence = tuple(
            [Evidence(EvidenceCode.CONTEXTUAL_MATCH, f"strong multi-signal contextual match (score {top_score:.1f})")]
            + top_evs
        )

        if p.amount_cents == outstanding:
            cand = Candidate(
                allocations=(
                    PaymentAllocation(
                        payment_id=p.payment_id,
                        invoice_id=inv.invoice_id,
                        amount_cents=p.amount_cents,
                        currency=inv.currency,
                    ),
                ),
                evidence=context_evidence,
            )
            return MatchProposal(
                payment_id=p.payment_id,
                outcome=MatchOutcome.PROPOSED,
                candidates=(cand,),
                matcher=self.name,
            )
        elif p.amount_cents < outstanding:
            cand = Candidate(
                allocations=(
                    PaymentAllocation(
                        payment_id=p.payment_id,
                        invoice_id=inv.invoice_id,
                        amount_cents=p.amount_cents,
                        currency=inv.currency,
                    ),
                ),
                evidence=context_evidence,
            )
            return MatchProposal(
                payment_id=p.payment_id,
                outcome=MatchOutcome.PROPOSED,
                candidates=(cand,),
                matcher=self.name,
            )
        else:
            # Overpayment with strong contextual match
            excess_cents = p.amount_cents - outstanding
            over_ev = Evidence(
                EvidenceCode.AMOUNT_OVERPAYMENT,
                f"payment {p.amount_cents} cents exceeds outstanding balance {outstanding} cents "
                f"on contextual match {inv.number} by {excess_cents} cents excess",
            )
            cand = Candidate(
                allocations=(
                    PaymentAllocation(
                        payment_id=p.payment_id,
                        invoice_id=inv.invoice_id,
                        amount_cents=outstanding,
                        currency=inv.currency,
                    ),
                ),
                evidence=context_evidence + (over_ev,),
            )
            return MatchProposal(
                payment_id=p.payment_id,
                outcome=MatchOutcome.PROPOSED,
                candidates=(cand,),
                reasons=(over_ev,),
                matcher=self.name,
                excess_amount_cents=excess_cents,
            )
