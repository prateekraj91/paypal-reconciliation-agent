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
