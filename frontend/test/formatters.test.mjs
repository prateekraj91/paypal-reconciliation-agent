import test from 'node:test';
import assert from 'node:assert';
import {
  formatReconciliationRate,
  getInvestigationEmptyLabels,
  getProposalEmptyLabels,
  getTransactionEmptyLabels,
  getInvoiceEmptyLabels,
  validateHeaderValue,
  getMatcherExplanation,
  isApprovalEligible,
} from '../src/utils/formatters.ts';

test('formatReconciliationRate handles zero denominator without showing 100%', () => {
  const zeroTotal = formatReconciliationRate(0, 0);
  assert.strictEqual(zeroTotal.displayText, '—');
  assert.strictEqual(zeroTotal.rate, null);
  assert.strictEqual(zeroTotal.progressPercent, 0);

  const negativeTotal = formatReconciliationRate(0, -1);
  assert.strictEqual(negativeTotal.displayText, '—');
  assert.strictEqual(negativeTotal.rate, null);
  assert.strictEqual(negativeTotal.progressPercent, 0);
});

test('formatReconciliationRate calculates accurately when transactions exist', () => {
  const half = formatReconciliationRate(5, 10);
  assert.strictEqual(half.displayText, '50%');
  assert.strictEqual(half.rate, 50);
  assert.strictEqual(half.progressPercent, 50);

  const full = formatReconciliationRate(12, 12);
  assert.strictEqual(full.displayText, '100%');
  assert.strictEqual(full.rate, 100);
  assert.strictEqual(full.progressPercent, 100);

  const third = formatReconciliationRate(1, 3);
  assert.strictEqual(third.displayText, '33%');
  assert.strictEqual(third.rate, 33);
  assert.strictEqual(third.progressPercent, 33);
});

test('getInvestigationEmptyLabels provides human-readable labels instead of raw enum strings', () => {
  const review = getInvestigationEmptyLabels('NEEDS_REVIEW');
  assert.strictEqual(review.title, 'No investigations need human review.');
  assert.ok(!review.title.includes('needs_review'));

  const invalid = getInvestigationEmptyLabels('INVALID');
  assert.strictEqual(invalid.title, 'No invalid investigations.');
  assert.strictEqual(invalid.title, 'No invalid investigations.');

  const all = getInvestigationEmptyLabels('ALL');
  assert.strictEqual(all.title, 'No investigations found.');
  assert.ok(!all.title.toLowerCase().includes('zero all'));
});

test('getProposalEmptyLabels provides human-readable review queue labels', () => {
  const pending = getProposalEmptyLabels('PENDING');
  assert.strictEqual(pending.title, 'No proposals pending review.');

  const approved = getProposalEmptyLabels('APPROVED');
  assert.strictEqual(approved.title, 'No approved proposals.');

  const rejected = getProposalEmptyLabels('REJECTED');
  assert.strictEqual(rejected.title, 'No rejected proposals.');

  const all = getProposalEmptyLabels('ALL');
  assert.strictEqual(all.title, 'No proposals found.');
});

test('getTransactionEmptyLabels provides human-readable status labels', () => {
  assert.strictEqual(getTransactionEmptyLabels('RECONCILED').title, 'No reconciled transactions.');
  assert.strictEqual(getTransactionEmptyLabels('UNMATCHED').title, 'No unmatched transactions.');
  assert.strictEqual(getTransactionEmptyLabels('PARTIALLY_RECONCILED').title, 'No partially reconciled transactions.');
  assert.strictEqual(getTransactionEmptyLabels('ALL').title, 'No transactions found.');
});

test('getInvoiceEmptyLabels provides human-readable invoice ledger labels', () => {
  assert.strictEqual(getInvoiceEmptyLabels('FULLY_RECONCILED').title, 'No fully reconciled invoices.');
  assert.strictEqual(getInvoiceEmptyLabels('OPEN').title, 'No open invoices.');
  assert.strictEqual(getInvoiceEmptyLabels('PARTIAL').title, 'No partially settled invoices.');
  assert.strictEqual(getInvoiceEmptyLabels('ALL').title, 'No invoices found.');
});

test('validateHeaderValue allows valid ASCII bearer tokens without error', () => {
  assert.doesNotThrow(() => {
    validateHeaderValue('Authorization', 'valid-ascii-token-12345_!@#$%^&*()');
  });
});

test('validateHeaderValue rejects non-ISO-8859-1 / Unicode characters with clear authentication error', () => {
  // Test em-dash (U+2014)
  assert.throws(
    () => {
      validateHeaderValue('Authorization', 'token—with—emdash');
    },
    (err) => {
      assert.ok(err instanceof Error);
      assert.ok(err.message.includes('non-ISO-8859-1'));
      assert.ok(err.message.includes('U+2014'));
      return true;
    }
  );

  // Test zero-width space (U+200B)
  assert.throws(
    () => {
      validateHeaderValue('Authorization', 'token\u200Bwith\u200Bzero\u200Bwidth');
    },
    (err) => {
      assert.ok(err instanceof Error);
      assert.ok(err.message.includes('U+200B'));
      return true;
    }
  );

  // Test curly quotes (U+201C)
  assert.throws(
    () => {
      validateHeaderValue('Authorization', '“quoted-token”');
    },
    (err) => {
      assert.ok(err instanceof Error);
      assert.ok(err.message.includes('non-ISO-8859-1'));
      return true;
    }
  );
});

test('getMatcherExplanation distinguishes reason categories instead of generic no invoice candidates', () => {
  // 1. Competing candidates / Ambiguity
  const ambiguous = getMatcherExplanation('AMBIGUOUS', ['Candidate 1', 'Candidate 2'], 0, {
    candidate_invoices: ['INV-1', 'INV-2'],
  });
  assert.strictEqual(ambiguous.category, 'ambiguous');
  assert.strictEqual(ambiguous.badgeLabel, 'Multiple Competing Candidates');
  assert.ok(ambiguous.summary.includes('Multiple competing candidates'));
  assert.ok(ambiguous.summary.includes('2 candidates'));

  // 2. Currency mismatch
  const currMismatch = getMatcherExplanation('CURRENCY_MISMATCH', ['Payment currency USD vs Invoice EUR']);
  assert.strictEqual(currMismatch.category, 'currency_mismatch');
  assert.strictEqual(currMismatch.badgeLabel, 'Currency Mismatch');
  assert.ok(currMismatch.summary.includes('Currency mismatch'));

  const detailedMismatch = getMatcherExplanation('NO_MATCH', [
    'CURRENCY_MISMATCH',
    'Currency Mismatch: Remittance in USD cannot be matched to EUR invoice INV-DEMO-EUR-009. Strict currency isolation prevents cross-currency allocations.',
  ]);
  assert.strictEqual(detailedMismatch.category, 'currency_mismatch');
  assert.strictEqual(detailedMismatch.badgeLabel, 'Currency Mismatch');
  assert.ok(detailedMismatch.summary.includes('INV-DEMO-EUR-009'));

  // 3. Amount mismatch (overpayment)
  const overpay = getMatcherExplanation('AMOUNT_OVERPAYMENT', ['Payment 15000 exceeds invoice 10000'], 5000);
  assert.strictEqual(overpay.category, 'overpayment');
  assert.strictEqual(overpay.badgeLabel, 'Amount Mismatch (Overpayment)');
  assert.ok(overpay.summary.includes('exceeds invoice balance by $50.00'));

  // 4. Amount mismatch (partial / underpayment)
  const underpay = getMatcherExplanation('AMOUNT_UNDERPAYMENT', ['Payment 30000 partially satisfies 60000']);
  assert.strictEqual(underpay.category, 'partial');
  assert.strictEqual(underpay.badgeLabel, 'Partial Payment');

  const partialPay = getMatcherExplanation('PROPOSED', ['REFERENCE_MATCH', 'AMOUNT_PARTIAL']);
  assert.strictEqual(partialPay.category, 'partial');
  assert.strictEqual(partialPay.badgeLabel, 'Partial Payment');
  assert.ok(partialPay.summary.includes('Partial payment'));

  // 5. Insufficient corroboration
  const unverified = getMatcherExplanation('UNVERIFIED', ['Candidate found but verifier rejected single-signal match']);
  assert.strictEqual(unverified.category, 'insufficient_corroboration');
  assert.strictEqual(unverified.badgeLabel, 'Insufficient Corroboration');
  assert.ok(unverified.summary.includes('Insufficient corroboration'));

  // 6. Generic no candidate invoices found
  const noMatch = getMatcherExplanation('NO_MATCH', []);
  assert.strictEqual(noMatch.category, 'no_candidates');
  assert.strictEqual(noMatch.badgeLabel, 'No Candidate Invoices');
  assert.ok(noMatch.summary.includes('No candidate invoices found'));

  // 7. Exact match
  const exact = getMatcherExplanation('EXACT_MATCH', ['Corroborated by reference']);
  assert.strictEqual(exact.category, 'exact');
  assert.strictEqual(exact.badgeLabel, 'Exact Match');
});

test('getMatcherExplanation correctly classifies EXACT-001, INJECTION-011, INVESTIGATE-010, and SPLIT-005 without false Amount Mismatch', () => {
  // Case 1: EXACT-001 ($1,250 payment against $1,250 invoice)
  const exact001 = getMatcherExplanation(
    'PROPOSED',
    ['REFERENCE_MATCH', 'AMOUNT_EXACT'],
    0,
    null,
    [{ invoice_id: 'inv_exact_001', amount_minor: 125000 }]
  );
  assert.strictEqual(exact001.category, 'exact');
  assert.strictEqual(exact001.badgeLabel, 'Exact Match');
  assert.notStrictEqual(exact001.badgeLabel, 'Amount Mismatch');
  assert.ok(exact001.summary.toLowerCase().includes('exact'));

  // Case 2: INJECTION-011 ($500 payment against $500 invoice)
  // Matching tier identifies exact match; safety verifier independently handles prompt injection
  const injection011 = getMatcherExplanation(
    'PROPOSED',
    ['REFERENCE_MATCH', 'AMOUNT_EXACT'],
    0,
    null,
    [{ invoice_id: 'inv_injection_011', amount_minor: 50000 }]
  );
  assert.strictEqual(injection011.category, 'exact');
  assert.strictEqual(injection011.badgeLabel, 'Exact Match');
  assert.notStrictEqual(injection011.badgeLabel, 'Amount Mismatch');

  // Case 3: INVESTIGATE-010 ($890 payment against $890 invoice)
  const investigate010 = getMatcherExplanation(
    'PROPOSED',
    ['REFERENCE_MATCH', 'AMOUNT_EXACT'],
    0,
    null,
    [{ invoice_id: 'inv_cyberdyne_010', amount_minor: 89000 }]
  );
  assert.strictEqual(investigate010.category, 'exact');
  assert.strictEqual(investigate010.badgeLabel, 'Exact Match');
  assert.notStrictEqual(investigate010.badgeLabel, 'Amount Mismatch');

  // Case 4: SPLIT-005 ($1,500 payment covering $900 + $600 invoices)
  const split005 = getMatcherExplanation(
    'PROPOSED',
    ['REFERENCE_MATCH', 'AMOUNT_EXACT'],
    0,
    null,
    [
      { invoice_id: 'inv_split_5a', amount_minor: 90000 },
      { invoice_id: 'inv_split_5b', amount_minor: 60000 },
    ]
  );
  assert.strictEqual(split005.category, 'split');
  assert.strictEqual(split005.badgeLabel, 'Exact Aggregate Match / Split Allocation');
  assert.notStrictEqual(split005.badgeLabel, 'Amount Mismatch');
  assert.ok(split005.summary.includes('Split payment allocation'));
  assert.ok(split005.summary.includes('2 invoices'));
});

test('isApprovalEligible fails closed unless the backend explicitly allows approval', () => {
  assert.strictEqual(isApprovalEligible({ approval_eligible: true, verifier_status: 'VERIFIED' }), true);
  assert.strictEqual(isApprovalEligible({ approval_eligible: false, verifier_status: 'VERIFIED' }), false);
  assert.strictEqual(isApprovalEligible({ verifier_status: 'VERIFIED' }), false);
  assert.strictEqual(isApprovalEligible({ approval_eligible: true, verifier_status: 'INVALID' }), false);
});
