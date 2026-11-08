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
  category: 'exact' | 'ambiguous' | 'overpayment' | 'amount_mismatch' | 'partial' | 'currency_mismatch' | 'insufficient_corroboration' | 'no_candidates' | 'split';
  summary: string;
  details: string[];
}

/**
 * Explain matching evidence and outcomes with financial precision.
 * Distinguishes "No candidate invoices found", "Amount mismatch",
 * "Multiple competing candidates", "Currency mismatch", "Insufficient corroboration".
 */
export function getMatcherExplanation(
  outcome: string | null | undefined,
  evidence?: string[] | null,
  excessMinor?: number | null,
  ambiguityCert?: Record<string, any> | null
): MatcherExplanation {
  const norm = (outcome || '').toUpperCase();
  const ev = evidence || [];
  const details = [...ev];
  const evText = ev.join(' ').toLowerCase();

  // 1. Multiple competing candidates / Ambiguity
  if (
    norm.includes('AMBIGUOUS') ||
    ambiguityCert ||
    norm === 'COMPETING_INVOICES' ||
    evText.includes('competing') ||
    evText.includes('multiple_candidates')
  ) {
    const candCount = ambiguityCert?.candidate_invoice_ids?.length || ambiguityCert?.candidate_invoices?.length || ambiguityCert?.candidates?.length || 2;
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
    evText.includes('currency mismatch') ||
    evText.includes('currency')
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

  // 3. Overpayment / Underpayment / Partial / Split
  if (norm.includes('OVERPAYMENT') || (excessMinor && excessMinor > 0) || evText.includes('excess') || evText.includes('overpayment')) {
    const diff = excessMinor ? ` by $${(excessMinor / 100).toFixed(2)}` : '';
    return {
      badgeLabel: 'Amount Mismatch (Overpayment)',
      category: 'overpayment',
      summary: `Amount mismatch: Payment exceeds invoice balance${diff}`,
      details: details.length > 0 ? details : ['Payment amount exceeds invoice balance. Unallocated cash retained.'],
    };
  }

  if (
    norm.includes('PARTIAL') ||
    evText.includes('amount_partial') ||
    evText.includes('partial')
  ) {
    return {
      badgeLabel: 'Partial Payment',
      category: 'partial',
      summary: 'Partial payment: Remittance partially satisfies invoice; remaining balance remains open.',
      details: details.length > 0 ? details : ['Payment amount partially satisfies open invoice balance.'],
    };
  }

  if (norm.includes('UNDERPAYMENT') || evText.includes('underpayment')) {
    return {
      badgeLabel: 'Amount Mismatch (Partial)',
      category: 'amount_mismatch',
      summary: 'Amount mismatch: Payment partially satisfies invoice',
      details: details.length > 0 ? details : ['Payment amount is less than invoice total.'],
    };
  }

  if (evText.includes('amount mismatch') || evText.includes('amount')) {
    return {
      badgeLabel: 'Amount Mismatch',
      category: 'amount_mismatch',
      summary: 'Amount mismatch (open invoices found but amounts do not match payment)',
      details: details.length > 0 ? details : ['Open candidate invoice exists but amount does not match remittance.'],
    };
  }

  if (norm.includes('SPLIT')) {
    return {
      badgeLabel: 'Split Payment',
      category: 'split',
      summary: 'Split payment allocation across multiple invoices',
      details: details.length > 0 ? details : ['Remittance satisfies multiple outstanding invoices.'],
    };
  }

  // 4. Exact / Proposed match
  if (norm === 'EXACT_MATCH' || norm === 'PROPOSED') {
    return {
      badgeLabel: 'Exact Match',
      category: 'exact',
      summary: 'Authoritative candidate match verified against ledger',
      details: details.length > 0 ? details : ['Corroborated by invoice reference and amount.'],
    };
  }

  // 5. Insufficient corroboration
  if (norm === 'UNVERIFIED' || evText.includes('insufficient') || evText.includes('unverified') || evText.includes('corroboration')) {
    return {
      badgeLabel: 'Insufficient Corroboration',
      category: 'insufficient_corroboration',
      summary: 'Insufficient corroboration (candidate identified but failed independent verifier checks)',
      details: details.length > 0 ? details : ['Candidate invoice identified but lacks sufficient corroborating evidence for automated approval.'],
    };
  }

  // 6. No candidates found
  return {
    badgeLabel: 'No Candidate Invoices',
    category: 'no_candidates',
    summary: 'No candidate invoices found (no open invoices match reference, customer, or amount)',
    details: details.length > 0 ? details : ['No open invoices matched reference, customer, or amount.'],
  };
}
