import test from 'node:test';
import assert from 'node:assert';
import {
  formatReconciliationRate,
  getInvestigationEmptyLabels,
  getProposalEmptyLabels,
  getTransactionEmptyLabels,
  getInvoiceEmptyLabels,
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
