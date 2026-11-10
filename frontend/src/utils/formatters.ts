/**
 * Financial formatters and empty-state label helpers for Fintech Operator Dashboard.
 */

/**
 * Format reconciliation rate safely.
 * Returns '—' when total is 0 (zero denominator) to avoid misleading 100% metrics.
 */
export function formatReconciliationRate(
  reconciled: number,
  total: number
): {
  displayText: string;
  rate: number | null;
  progressPercent: number;
} {
  if (!total || total <= 0) {
    return {
      displayText: '—',
      rate: null,
      progressPercent: 0,
    };
  }
  const rate = Math.round((reconciled / total) * 100);
  return {
    displayText: `${rate}%`,
    rate,
    progressPercent: rate,
  };
}

/**
 * Human-readable empty-state labels for investigations.
 * Replaces raw enum values like 'needs_review' or 'invalid'.
 */
export function getInvestigationEmptyLabels(statusFilter: string): {
  title: string;
  description: string;
} {
  const norm = (statusFilter || 'ALL').toUpperCase();
  if (norm === 'NEEDS_REVIEW') {
    return {
      title: 'No investigations need human review.',
      description: 'All exception investigations have passed safety checks or been resolved.',
    };
  }
  if (norm === 'INVALID') {
    return {
      title: 'No invalid investigations.',
      description: 'No investigations have failed safety invariant checks.',
    };
  }
  return {
    title: 'No investigations found.',
    description: 'No investigation cases match the selected status filter.',
  };
}

/**
 * Human-readable empty-state labels for match proposals.
 */
export function getProposalEmptyLabels(statusFilter: string): {
  title: string;
  description: string;
} {
  const norm = (statusFilter || 'ALL').toUpperCase();
  if (norm === 'PENDING') {
    return {
      title: 'No proposals pending review.',
      description: 'The human review queue is clear. All proposals have been approved or rejected.',
    };
  }
  if (norm === 'APPROVED') {
    return {
      title: 'No approved proposals.',
      description: 'No proposals have been approved for this tenant yet.',
    };
  }
  if (norm === 'REJECTED') {
    return {
      title: 'No rejected proposals.',
      description: 'No proposals have been rejected for this tenant.',
    };
  }
  return {
    title: 'No proposals found.',
    description: 'No match proposals match the current filter in the review queue.',
  };
}

/**
 * Human-readable empty-state labels for transaction filtering.
 */
export function getTransactionEmptyLabels(statusFilter: string): {
  title: string;
  description: string;
} {
  const norm = (statusFilter || 'ALL').toUpperCase();
  if (norm === 'RECONCILED') {
    return {
      title: 'No reconciled transactions.',
      description: 'No fully reconciled transactions found for this tenant.',
    };
  }
  if (norm === 'UNMATCHED') {
    return {
      title: 'No unmatched transactions.',
      description: 'No unmatched transactions requiring reconciliation.',
    };
  }
  if (norm === 'PARTIALLY_RECONCILED') {
    return {
      title: 'No partially reconciled transactions.',
      description: 'No transactions with partial invoice allocations.',
    };
  }
  return {
    title: 'No transactions found.',
    description: 'No financial transactions match the selected filters for this tenant.',
  };
}

/**
 * Human-readable empty-state labels for invoices.
 */
export function getInvoiceEmptyLabels(statusFilter: string): {
  title: string;
  description: string;
} {
  const norm = (statusFilter || 'ALL').toUpperCase();
  if (norm === 'FULLY_RECONCILED') {
    return {
      title: 'No fully reconciled invoices.',
      description: 'No invoices with zero outstanding balance match this filter.',
    };
  }
  if (norm === 'OPEN') {
    return {
      title: 'No open invoices.',
      description: 'No unpaid invoices with zero allocations match this filter.',
    };
  }
  if (norm === 'PARTIAL') {
    return {
      title: 'No partially settled invoices.',
      description: 'No invoices with partial allocations match this filter.',
    };
  }
  return {
    title: 'No invoices found.',
    description: 'No canonical invoices match the filter criteria.',
  };
}

/**
 * Validate that an authentication header value conforms to RFC 7230 / ISO-8859-1 ByteString rules.
 * Throws a clear authentication error instead of letting the browser throw an unhandled
 * "String contains non ISO-8859-1 code point" TypeError. Does not silently modify credentials.
 */
export function validateHeaderValue(name: string, value: string): void {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code > 255) {
      throw new Error(
        `Invalid authentication credential for '${name}': Token contains non-ISO-8859-1 / non-ASCII character ` +
        `(code point U+${code.toString(16).toUpperCase().padStart(4, '0')} at index ${i}). ` +
        `Please ensure the admin key is copied as clean plain ASCII text without rich-text formatting.`
      );
    }
  }
}

export interface MatcherExplanation {
  badgeLabel: string;
  category: 'exact' | 'ambiguous' | 'overpayment' | 'amount_mismatch' | 'partial' | 'currency_mismatch' | 'insufficient_corroboration' | 'no_candidates' | 'split' | 'prompt_injection' | 'customer_mismatch';
  summary: string;
  details: string[];
}

/**
 * Format raw evidence signals into human-readable sentences for evidence inspector.
 */
function formatSignal(detail: string): string {
  if (typeof detail !== 'string') return String(detail);
  const trimmed = detail.trim();
  switch (trimmed) {
    case 'REFERENCE_MATCH':
      return 'Invoice Reference Match: Remittance reference or memo matches candidate invoice identifier.';
    case 'EXACT_INVOICE_NUMBER_MATCH':
      return 'Exact Invoice Match: Payment reference exactly matches invoice number.';
    case 'AMOUNT_EXACT':
      return 'Exact Amount Match: Payment amount exactly equals candidate invoice balance.';
    case 'AMOUNT_PARTIAL':
      return 'Partial Amount: Remittance partially satisfies open invoice balance.';
    case 'AMOUNT_OVERPAYMENT':
      return 'Overpayment: Remittance amount exceeds open invoice balance.';
    case 'CURRENCY_MISMATCH':
      return 'Remittance currency differs from candidate invoice currency.';
    case 'FUZZY_REFERENCE_MATCH':
      return 'Fuzzy Reference Match: Invoice digits identified in payment description.';
    default:
      return detail;
  }
}

/**
 * Explain matching evidence and outcomes with financial precision.
 * Distinguishes "No candidate invoices found", "Amount mismatch",
 * "Multiple competing candidates", "Currency mismatch", "Insufficient corroboration",
 * "Exact Match", and "Exact Aggregate Match / Split Allocation".
 */
export function getMatcherExplanation(
  outcome: string | null | undefined,
  evidence?: string[] | null,
  excessMinor?: number | null,
  ambiguityCert?: Record<string, any> | null,
  allocations?: Array<{ amount_minor?: number; [key: string]: any }> | null
): MatcherExplanation {
  const norm = (outcome || '').toUpperCase();
  const ev = evidence || [];
  const details = ev.map(formatSignal);
  const evText = ev.join(' ').toLowerCase();

  // 0a. Prompt Injection / Adversarial Remittance Tokens (FAIL-CLOSED)
  if (
    evText.includes('prompt injection') ||
    evText.includes('prompt_injection') ||
    norm.includes('INJECTION')
  ) {
    const detailed = details.find(
      (d) =>
        typeof d === 'string' &&
        (d.toLowerCase().includes('prompt injection') || d.toLowerCase().includes('adversarial'))
    );
    return {
      badgeLabel: 'PROMPT_INJECTION_SUSPECT',
      category: 'prompt_injection',
      summary: detailed || 'Prompt injection detected in payment remittance. Safety verifier failed closed.',
      details: details.length > 0 ? details : ['Adversarial prompt injection tokens detected in remittance. Approval is strictly prohibited.'],
    };
  }

  // 0b. Customer Mismatch / Unverified Third-Party Remitter
  if (
    evText.includes('customer mismatch') ||
    evText.includes('remitter mismatch') ||
    evText.includes('third-party clearinghouse')
  ) {
    const detailed = details.find(
      (d) =>
        typeof d === 'string' &&
        (d.toLowerCase().includes('customer mismatch') || d.toLowerCase().includes('remitter'))
    );
    return {
      badgeLabel: 'Customer Mismatch (Third-Party Remitter)',
      category: 'customer_mismatch',
      summary: detailed || 'Remitter does not match invoice customer; clearinghouse relationship unverified.',
      details: details.length > 0 ? details : ['Payment remitter differs from invoice customer. Relationship remains unverified.'],
    };
  }

  // 1. Multiple competing candidates / Ambiguity
  if (
    norm.includes('AMBIGUOUS') ||
    ambiguityCert ||
    norm === 'COMPETING_INVOICES' ||
    evText.includes('competing') ||
    evText.includes('multiple_candidates')
  ) {
    const candCount =
      ambiguityCert?.candidate_invoice_ids?.length ||
      ambiguityCert?.candidate_invoices?.length ||
      ambiguityCert?.candidates?.length ||
      2;
    return {
      badgeLabel: 'Multiple Competing Candidates',
      category: 'ambiguous',
      summary: `Multiple competing candidates (${candCount} candidates matched with equal confidence; operator disambiguation required)`,
      details: details.length > 0 ? details : [`${candCount} candidate invoices matched with equal confidence. Requires human selection.`],
    };
  }

  // 2. Currency mismatch
  if (
    norm.includes('CURRENCY_MISMATCH') ||
    norm.includes('CURRENCY') ||
    evText.includes('currency_mismatch') ||
    evText.includes('currency mismatch')
  ) {
    const detailedMismatch = details.find(
      (d) => typeof d === 'string' && d.toLowerCase().includes('currency mismatch:')
    );
    return {
      badgeLabel: 'Currency Mismatch',
      category: 'currency_mismatch',
      summary: detailedMismatch || 'Currency mismatch (payment currency differs from candidate invoice currency)',
      details: details.length > 0 ? details : ['Payment and invoice currencies differ; cross-currency auto-settlement is restricted.'],
    };
  }

  // 3. Overpayment
  if (
    norm.includes('OVERPAYMENT') ||
    (excessMinor && excessMinor > 0) ||
    evText.includes('amount_overpayment') ||
    evText.includes('overpayment')
  ) {
    const diff = excessMinor ? ` by $${(excessMinor / 100).toFixed(2)}` : '';
    return {
      badgeLabel: 'Amount Mismatch (Overpayment)',
      category: 'overpayment',
      summary: `Amount mismatch: Payment exceeds invoice balance${diff}`,
      details: details.length > 0 ? details : ['Payment amount exceeds invoice balance. Unallocated cash retained.'],
    };
  }

  // 4. Split Payment / Aggregate Match
  const isSplit =
    (allocations && allocations.length > 1) ||
    norm.includes('SPLIT') ||
    evText.includes('split') ||
    evText.includes('aggregate');

  if (isSplit) {
    const count = allocations && allocations.length > 1 ? ` across ${allocations.length} invoices` : '';
    return {
      badgeLabel: 'Exact Aggregate Match / Split Allocation',
      category: 'split',
      summary: `Split payment allocation${count}: Aggregate amount satisfies outstanding invoices exactly`,
      details: details.length > 0 ? details : ['Remittance satisfies multiple outstanding invoices exactly.'],
    };
  }

  // 5. Partial payment
  if (
    norm.includes('PARTIAL') ||
    norm.includes('UNDERPAYMENT') ||
    evText.includes('amount_partial') ||
    evText.includes('underpayment') ||
    evText.includes('partial')
  ) {
    return {
      badgeLabel: 'Partial Payment',
      category: 'partial',
      summary: 'Partial payment: Remittance partially satisfies invoice; remaining balance remains open.',
      details: details.length > 0 ? details : ['Payment amount partially satisfies open invoice balance.'],
    };
  }

  // 6. Genuine Amount Mismatch (reference found or candidates found, but amounts mismatch)
  if (
    norm.includes('AMOUNT_MISMATCH') ||
    evText.includes('amount_mismatch') ||
    evText.includes('amount mismatch')
  ) {
    return {
      badgeLabel: 'Amount Mismatch',
      category: 'amount_mismatch',
      summary: 'Amount mismatch (open invoices found but amounts do not match payment)',
      details: details.length > 0 ? details : ['Open candidate invoice exists but amount does not match remittance.'],
    };
  }

  // 7. Exact / Proposed match
  if (
    norm === 'EXACT_MATCH' ||
    norm === 'PROPOSED' ||
    ev.includes('AMOUNT_EXACT') ||
    ev.includes('EXACT_MATCH') ||
    evText.includes('amount_exact')
  ) {
    return {
      badgeLabel: 'Exact Match',
      category: 'exact',
      summary: 'Exact reference and amount match verified against ledger',
      details: details.length > 0 ? details : ['Corroborated by invoice reference and exact amount.'],
    };
  }

  // 8. Insufficient corroboration
  if (
    norm === 'UNVERIFIED' ||
    evText.includes('insufficient') ||
    evText.includes('unverified') ||
    evText.includes('corroboration')
  ) {
    return {
      badgeLabel: 'Insufficient Corroboration',
      category: 'insufficient_corroboration',
      summary: 'Insufficient corroboration (candidate identified but failed independent verifier checks)',
      details: details.length > 0 ? details : ['Candidate invoice identified but lacks sufficient corroborating evidence for automated approval.'],
    };
  }

  // 9. No candidates found
  return {
    badgeLabel: 'No Candidate Invoices',
    category: 'no_candidates',
    summary: 'No candidate invoices found (no open invoices match reference, customer, or amount)',
    details: details.length > 0 ? details : ['No open invoices matched reference, customer, or amount.'],
  };
}

/**
 * Only an explicit approval from the backend enables Approve. A missing field
 * or unknown status fails closed; the backend still decides at approval time.
 */
export function isApprovalEligible(proposal: { approval_eligible?: boolean; verifier_status?: string }): boolean {
  return proposal.approval_eligible === true && proposal.verifier_status !== 'INVALID';
}

export interface FactSectionPresentation {
  heading: string;
  badge: string;
  emptyText: string;
  cardClass: string;
  dividerClass: string;
  headingClass: string;
  badgeClass: string;
  rowClass: string;
}

/** Model statements are only labelled verified once the independent verifier has accepted them. */
export function getFactSectionPresentation(isInvalid: boolean): FactSectionPresentation {
  if (isInvalid) {
    // Rejected: heavy solid white border, ✕ icon, inverted badge.
    return {
      heading: 'Unverified Model Claims',
      badge: 'Rejected by Verifier',
      emptyText: 'No model claims recorded.',
      cardClass: 'border-2 border-solid border-white bg-[#111111]',
      dividerClass: 'border-white',
      headingClass: 'text-white',
      badgeClass: 'bg-white text-black px-1.5 py-0.5 rounded font-extrabold',
      rowClass: 'bg-black border-2 border-dashed border-neutral-400',
    };
  }
  // Accepted: thin solid border, ✓ icon.
  return {
    heading: 'Verified Facts',
    badge: 'Grounded in DB Evidence',
    emptyText: 'No verified factual statements recorded.',
    cardClass: 'border border-solid border-neutral-400 bg-[#111111]',
    dividerClass: 'border-neutral-700',
    headingClass: 'text-white',
    badgeClass: 'text-neutral-200 border border-neutral-400 px-1.5 py-0.5 rounded',
    rowClass: 'bg-black border border-solid border-neutral-600',
  };
}

/** Flags the pipeline itself adds when output is rejected; they are not model opinions. */
const SYSTEM_RISK_FLAGS = new Set(['VERIFIER_REJECTED', 'MALFORMED_MODEL_OUTPUT', 'SCHEMA_VALIDATION_ERROR']);

/** Model-raised flags that keep approval locked even when the verifier accepted the facts. */
const APPROVAL_HOLD_PATTERNS = ['INJECTION', 'CURRENCY_MISMATCH', 'CONTRADICT', 'REVERSAL', 'DISPUTE', 'REJECTED', 'MALFORMED'];

export interface InvestigationVerdictInput {
  verifier_status?: string | null;
  verifier_details?: {
    passed?: boolean;
    issues?: unknown;
    checked_invariants?: Record<string, unknown> | null;
  } | null;
  result?: {
    risk_flags?: unknown;
    conflicting_evidence?: unknown;
  } | null;
}

export interface InvestigationVerdict {
  /** True only when the deterministic verifier did not accept the run. Drives the fail-closed banner. */
  verifierRejected: boolean;
  /** Every flag the model raised, shown as advisory when the verifier accepted the run. */
  modelRaisedFlags: string[];
  /** Model-raised flags that hold approval for a human to resolve. */
  approvalHoldFlags: string[];
  hasConflictingEvidence: boolean;
  /** Approval stays locked if the verifier rejected the run or the model raised a hold flag. */
  approvalBlocked: boolean;
  approvalBlockedReason: string | null;
}

/**
 * Separates the deterministic verifier's verdict from flags the model raised.
 * Only the verifier's own fields decide `verifierRejected`; model flags can still
 * lock approval, but they never turn an accepted run into a rejected one.
 */
export function getInvestigationVerdict(inv: InvestigationVerdictInput): InvestigationVerdict {
  const details = inv.verifier_details || {};
  const invariants = (details.checked_invariants || {}) as Record<string, unknown>;
  const hasIssues = Array.isArray(details.issues) && details.issues.length > 0;
  const hasFailedInvariants =
    Object.keys(invariants).length === 0 || Object.values(invariants).some((v) => v !== true);
  const isEligibleStatus = inv.verifier_status === 'VERIFIED' || inv.verifier_status === 'NEEDS_REVIEW';
  const verifierRejected = !(isEligibleStatus && details.passed === true && !hasIssues && !hasFailedInvariants);

  const rawFlags = inv.result?.risk_flags;
  const allFlags = Array.isArray(rawFlags) ? rawFlags.map((f) => String(f)) : [];
  const modelRaisedFlags = allFlags.filter((f) => !SYSTEM_RISK_FLAGS.has(f.toUpperCase()));
  const approvalHoldFlags = modelRaisedFlags.filter((f) =>
    APPROVAL_HOLD_PATTERNS.some((pat) => f.toUpperCase().includes(pat))
  );
  const conflicting = inv.result?.conflicting_evidence;
  const hasConflictingEvidence = Array.isArray(conflicting) && conflicting.length > 0;

  let approvalBlockedReason: string | null = null;
  if (verifierRejected) {
    approvalBlockedReason = 'Cannot approve: the independent verifier rejected this investigation';
  } else if (approvalHoldFlags.length > 0) {
    approvalBlockedReason = `Approval held: the model raised ${approvalHoldFlags.join(', ')} for a human to check`;
  } else if (hasConflictingEvidence) {
    approvalBlockedReason = 'Approval held: the model reported conflicting evidence for a human to check';
  }

  return {
    verifierRejected,
    modelRaisedFlags,
    approvalHoldFlags,
    hasConflictingEvidence,
    approvalBlocked: approvalBlockedReason !== null,
    approvalBlockedReason,
  };
}
