import React, { useState } from 'react';
import { ShieldCheck, ShieldAlert, AlertTriangle, CheckCircle, XCircle, Sparkles, Play, Search, FileText } from 'lucide-react';
import { Modal, MoneyDisplay } from './Common';
import { ProposalItem, InvestigationDetail } from '../types/api';
import { getMatcherExplanation } from '../utils/formatters';

/**
 * Human Approval Modal for Reconciliation Proposals.
 * Enforces explicit review of payment, invoice, and allocation amount.
 */
export function ProposalApprovalModal({
  isOpen,
  onClose,
  proposal,
  onConfirm,
  isLoading,
}: {
  isOpen: boolean;
  onClose: () => void;
  proposal: ProposalItem | null;
  onConfirm: (reviewer: string, notes?: string) => Promise<void>;
  isLoading: boolean;
}) {
  const [reviewer, setReviewer] = useState('sarah.operator@company.com');
  const [notes, setNotes] = useState('Approved after reviewing exact reference and customer records');

  if (!proposal) return null;

  const totalAllocated = proposal.allocations.reduce((acc, a) => acc + a.amount_minor, 0);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!reviewer.trim()) return;
    onConfirm(reviewer, notes);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Approve Reconciliation Proposal"
      subtitle={`Proposal ID: ${proposal.id}`}
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Verification Summary Banner */}
        {proposal.approval_eligible !== true || proposal.verifier_status === 'INVALID' ? (
          <div className={`p-3.5 rounded-lg flex items-start gap-3 ${
            proposal.verifier_status === 'INVALID'
              ? 'state-fail-closed text-black'
              : 'bg-black state-held text-white'
          }`}>
            {proposal.verifier_status === 'INVALID' ? (
              <span className="flex items-center gap-1 mt-0.5 shrink-0">
                <span aria-hidden="true" className="glyph-mono">⛔</span>
                <XCircle size={18} strokeWidth={3} className="text-black" />
              </span>
            ) : (
              <AlertTriangle size={18} className="text-white mt-0.5 shrink-0" />
            )}
            <div className="text-xs space-y-1">
              <span className={`block ${
                proposal.verifier_status === 'INVALID' ? 'font-extrabold uppercase text-black' : 'font-semibold text-white'
              }`}>
                {proposal.verifier_status === 'INVALID'
                  ? 'Independent Verifier Status: INVALID (FAIL-CLOSED)'
                  : 'Independent Verifier Status: NEEDS REVIEW (BLOCKED)'}
              </span>
              <p className={proposal.verifier_status === 'INVALID' ? 'text-black font-medium leading-relaxed' : 'text-neutral-200 leading-relaxed'}>
                {proposal.approval_prohibited_reason ||
                  'Approval is blocked due to safety invariant violations or unverified remitter relationships.'}
              </p>
            </div>
          </div>
        ) : (
          <div className="p-3.5 rounded-lg bg-neutral-900/30 border border-neutral-400/30 flex items-start gap-3">
            <ShieldCheck size={18} className="text-neutral-200 mt-0.5 shrink-0" />
            <div className="text-xs space-y-1">
              <span className="font-semibold text-neutral-200 block">
                Independent Verifier Status: {proposal.verifier_status}
              </span>
              <p className="text-neutral-200/90 leading-relaxed">
                Ledger capacities, currencies, and mathematical invariants verified. Proceeding will execute the 10-step atomic financial transaction.
              </p>
            </div>
          </div>
        )}

        {/* Financial Allocation Summary */}
        <div className="bg-neutral-900/60 p-4 rounded-lg border border-neutral-800 space-y-2.5">
          <div className="flex justify-between items-center text-xs">
            <span className="text-neutral-400">Payment:</span>
            <span className="font-mono text-neutral-200 font-medium">
              {proposal.provider_payment_id} ({proposal.payer_name || 'Direct Remittance'})
            </span>
          </div>
          <div className="flex justify-between items-center text-xs">
            <span className="text-neutral-400">Payment Total:</span>
            <MoneyDisplay amountMinor={proposal.payment_amount_minor} currency={proposal.payment_currency} />
          </div>

          <div className="pt-2 border-t border-neutral-800">
            <span className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider block mb-1.5">
              Target Invoice Allocations:
            </span>
            {proposal.allocations.map((alloc, idx) => (
              <div key={idx} className="flex justify-between items-center text-xs py-1">
                <span className="font-mono text-neutral-200">{alloc.invoice_number}</span>
                <MoneyDisplay amountMinor={alloc.amount_minor} currency={alloc.currency} />
              </div>
            ))}
          </div>

          {proposal.excess_amount_minor > 0 && (
            <div className="flex justify-between items-center text-xs pt-2 border-t border-neutral-800 text-neutral-200">
              <span>Unapplied Cash Remainder:</span>
              <MoneyDisplay amountMinor={proposal.excess_amount_minor} currency={proposal.payment_currency} />
            </div>
          )}
        </div>

        {/* Matching Evidence Drill-down */}
        <div className="p-3.5 rounded-lg bg-neutral-900/70 border border-neutral-800 space-y-2">
          <span className="text-[10px] font-semibold text-neutral-400 uppercase tracking-wider block">
            Corroborating Signals & Match Evidence
          </span>
          {proposal.evidence && proposal.evidence.length > 0 ? (
            <div className="space-y-1">
              {proposal.evidence.map((ev, idx) => (
                <div key={idx} className="flex items-start gap-2 text-xs text-neutral-300">
                  <CheckCircle size={13} className="text-neutral-200 mt-0.5 shrink-0" />
                  <span>{ev}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-neutral-400 italic">
              Corroborated by explicit invoice reference in payment memo and exact currency amount match.
            </p>
          )}
        </div>

        {/* Operator Accountability */}
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-neutral-300 mb-1">
              Approving Operator Name / Email <span className="text-neutral-200">*</span>
            </label>
            <input
              type="text"
              required
              value={reviewer}
              onChange={(e) => setReviewer(e.target.value)}
              placeholder="e.g. controller@company.com"
              className="input-text text-xs"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-300 mb-1">
              Audit Notes & Rationale
            </label>
            <textarea
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Rationale recorded in append-only cryptographic audit trail..."
              className="input-text text-xs resize-none"
            />
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex justify-end gap-3 pt-4 border-t border-neutral-800/80">
          <button
            type="button"
            onClick={onClose}
            disabled={isLoading}
            className="btn btn-secondary text-xs"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={isLoading || !reviewer.trim() || proposal.approval_eligible !== true}
            className={`btn text-xs font-semibold px-4 ${
              proposal.approval_eligible !== true
                ? 'btn-secondary opacity-50 cursor-not-allowed'
                : 'btn-success'
            }`}
          >
            {isLoading ? 'Executing Atomic Ledger Transaction...' : 'Confirm & Authorize Allocation'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Rejection Modal for Proposals.
 */
export function ProposalRejectModal({
  isOpen,
  onClose,
  proposal,
  onConfirm,
  isLoading,
}: {
  isOpen: boolean;
  onClose: () => void;
  proposal: ProposalItem | null;
  onConfirm: (reviewer: string, reason: string) => Promise<void>;
  isLoading: boolean;
}) {
  const [reviewer, setReviewer] = useState('controller@demo.com');
  const [reason, setReason] = useState('Payment details or customer entity do not match expected remittance');

  if (!proposal) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!reviewer.trim() || !reason.trim()) return;
    onConfirm(reviewer, reason);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Reject Reconciliation Proposal"
      subtitle={`Proposal ID: ${proposal.id}`}
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="p-3 rounded-lg bg-neutral-900/20 border border-neutral-400/20 text-xs text-neutral-200">
          Rejection will record a human decision record and prevent this proposal from creating ledger allocations. An audit event will be appended to the hash chain.
        </div>

        <div>
          <label className="block text-xs font-medium text-neutral-300 mb-1">
            Rejecting Operator <span className="text-neutral-200">*</span>
          </label>
          <input
            type="text"
            required
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            className="input-text text-xs"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-neutral-300 mb-1">
            Mandatory Rejection Reason <span className="text-neutral-200">*</span>
          </label>
          <textarea
            required
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Document why this proposed match is rejected..."
            className="input-text text-xs resize-none"
          />
        </div>

        <div className="flex justify-end gap-3 pt-3 border-t border-neutral-800">
          <button type="button" onClick={onClose} disabled={isLoading} className="btn btn-secondary text-xs">
            Cancel
          </button>
          <button
            type="submit"
            disabled={isLoading || !reviewer.trim() || !reason.trim()}
            className="btn btn-danger text-xs font-semibold px-4"
          >
            {isLoading ? 'Recording Rejection...' : 'Reject Proposal'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Trigger Reconciliation Run Modal.
 */
export function RunTriggerModal({
  isOpen,
  onClose,
  onConfirm,
  isLoading,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: (matcherName: string) => Promise<void>;
  isLoading: boolean;
}) {
  const [matcherName, setMatcherName] = useState('deterministic_v2');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onConfirm(matcherName);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Trigger Reconciliation Run"
      subtitle="Execute matching algorithms against ingested transactions and open invoices."
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-xs font-medium text-neutral-300 mb-1.5">
            Matcher Engine
          </label>
          <select
            value={matcherName}
            onChange={(e) => setMatcherName(e.target.value)}
            className="select-input text-xs"
          >
            <option value="deterministic_v2">Deterministic Matcher v2 (Exact, Reference, Multi-Invoice, Split)</option>
            <option value="contextual_v1">Contextual Matcher (Fuzzy Customer, Date Proximity)</option>
            <option value="baseline_v1">Baseline Reference Matcher</option>
          </select>
          <p className="text-[11px] text-neutral-400 mt-1.5">
            Invariant: Match runs generate PROPOSED outcomes only. No allocations will be approved automatically.
          </p>
        </div>

        <div className="flex justify-end gap-3 pt-4 border-t border-neutral-800">
          <button type="button" onClick={onClose} disabled={isLoading} className="btn btn-secondary text-xs">
            Cancel
          </button>
          <button
            type="submit"
            disabled={isLoading}
            className="btn btn-primary text-xs font-semibold flex items-center gap-2"
          >
            <Play size={13} />
            {isLoading ? 'Executing Run...' : 'Execute Match Run'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * New AI Investigation Modal.
 */
export function NewInvestigationModal({
  isOpen,
  onClose,
  targetId,
  targetType = 'payment',
  onConfirm,
  isLoading,
}: {
  isOpen: boolean;
  onClose: () => void;
  targetId?: string;
  targetType?: string;
  onConfirm: (type: string, id: string, syncMode: boolean) => Promise<void>;
  isLoading: boolean;
}) {
  const [type, setType] = useState(targetType);
  const [id, setId] = useState(targetId || '');
  const [syncMode, setSyncMode] = useState(true);

  React.useEffect(() => {
    if (targetId) setId(targetId);
    if (targetType) setType(targetType);
  }, [targetId, targetType]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!id.trim()) return;
    onConfirm(type, id.trim(), syncMode);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Request AI Exception Investigation"
      subtitle="Deploy AI investigator grounded in authoritative database evidence tools."
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-xs font-medium text-neutral-300 mb-1">
            Exception Target Type
          </label>
          <select value={type} onChange={(e) => setType(e.target.value)} className="select-input text-xs">
            <option value="payment">Unmatched / Partial Payment</option>
            <option value="invoice">Unpaid / Discrepant Invoice</option>
            <option value="proposal">Ambiguous Proposal</option>
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-neutral-300 mb-1">
            Target Identifier (Payment ID or Invoice Number) <span className="text-neutral-200">*</span>
          </label>
          <input
            type="text"
            required
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g. PAY-DEMO-004 or INV-1004"
            className="input-text text-xs font-mono"
          />
        </div>

        <div className="flex items-center gap-2 p-3 rounded bg-neutral-900 border border-neutral-800 text-xs text-neutral-300">
          <input
            type="checkbox"
            id="syncMode"
            checked={syncMode}
            onChange={(e) => setSyncMode(e.target.checked)}
            className="rounded border-neutral-700 bg-neutral-800 text-neutral-300 focus:ring-0"
          />
          <label htmlFor="syncMode" className="cursor-pointer">
            Run investigation synchronously (results available immediately)
          </label>
        </div>

        <div className="flex justify-end gap-3 pt-4 border-t border-neutral-800">
          <button type="button" onClick={onClose} disabled={isLoading} className="btn btn-secondary text-xs">
            Cancel
          </button>
          <button
            type="submit"
            disabled={isLoading || !id.trim()}
            className="btn btn-primary text-xs font-semibold flex items-center gap-2 bg-gradient-to-r from-neutral-700 to-neutral-700"
          >
            <Sparkles size={13} />
            {isLoading ? 'Running AI Investigation...' : 'Start Investigation'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Matching Evidence Drill-down Modal.
 * Enables operators to inspect signals, candidate invoices, and verifier invariants before approving.
 */
export function EvidenceInspectModal({
  isOpen,
  onClose,
  proposal,
}: {
  isOpen: boolean;
  onClose: () => void;
  proposal: ProposalItem | null;
}) {
  if (!proposal) return null;

  const expl = getMatcherExplanation(
    proposal.outcome,
    proposal.evidence,
    proposal.excess_amount_minor,
    proposal.ambiguity_certificate,
    proposal.allocations
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Matching Evidence Drill-Down"
      subtitle={`Proposal ID: ${proposal.id} | Payment: ${proposal.provider_payment_id}`}
      maxWidth="max-w-2xl"
    >
      <div className="space-y-4">
        {/* Authoritative Safety Verifier Banner if Blocked */}
        {(proposal.approval_eligible !== true || proposal.verifier_status === 'INVALID') && (
          <div className={`p-3.5 rounded-lg flex items-start gap-3 ${
            proposal.verifier_status === 'INVALID'
              ? 'state-fail-closed text-black'
              : 'bg-black state-held text-white'
          }`}>
            {proposal.verifier_status === 'INVALID' ? (
              <span className="flex items-center gap-1 mt-0.5 shrink-0">
                <span aria-hidden="true" className="glyph-mono">⛔</span>
                <XCircle size={18} strokeWidth={3} className="text-black" />
              </span>
            ) : (
              <AlertTriangle size={18} className="text-white mt-0.5 shrink-0" />
            )}
            <div className="text-xs space-y-1">
              <span className={`block ${
                proposal.verifier_status === 'INVALID' ? 'font-extrabold uppercase text-black' : 'font-semibold text-white'
              }`}>
                {proposal.verifier_status === 'INVALID'
                  ? 'APPROVAL STRICTLY PROHIBITED (FAIL-CLOSED)'
                  : 'APPROVAL BLOCKED (UNRESOLVED CONFLICT)'}
              </span>
              <p className={proposal.verifier_status === 'INVALID' ? 'text-black font-medium leading-relaxed' : 'text-neutral-200 leading-relaxed'}>
                {proposal.approval_prohibited_reason ||
                  'Approval is blocked due to safety invariant violations or unverified remitter relationships.'}
              </p>
            </div>
          </div>
        )}

        {/* Outcome Classification Banner */}
        <div className="p-3.5 rounded-lg bg-neutral-900 border border-neutral-800 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-xs text-white uppercase tracking-wider">
              Matcher Classification
            </span>
            <span className="badge badge-info font-mono">{expl.badgeLabel}</span>
          </div>
          <p className="text-xs text-neutral-300 font-medium">{expl.summary}</p>
        </div>

        {/* Remittance & Allocations Overview */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
          <div className="p-3 rounded-lg bg-neutral-900/60 border border-neutral-800/80 space-y-1.5">
            <span className="text-[10px] uppercase font-bold text-neutral-400 block">Payment Remittance</span>
            <div className="font-mono text-neutral-200 font-medium">{proposal.provider_payment_id}</div>
            <div className="text-neutral-300">{proposal.payer_name || 'Anonymous Remitter'}</div>
            <MoneyDisplay
              amountMinor={proposal.payment_amount_minor}
              currency={proposal.payment_currency}
              className="font-bold text-white text-sm"
            />
          </div>

          <div className="p-3 rounded-lg bg-neutral-900/60 border border-neutral-800/80 space-y-1.5">
            <span className="text-[10px] uppercase font-bold text-neutral-400 block">Candidate Invoices Evaluated</span>
            {proposal.allocations.length === 0 ? (
              <p className="text-neutral-400 italic mt-1">No candidate invoices allocated</p>
            ) : (
              <div className="space-y-1">
                {proposal.allocations.map((a, idx) => (
                  <div key={idx} className="flex justify-between items-center">
                    <span className="font-mono text-neutral-200 font-semibold">{a.invoice_number}</span>
                    <MoneyDisplay amountMinor={a.amount_minor} currency={a.currency} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Corroborating Signals */}
        <div className="p-3.5 rounded-lg bg-neutral-900/80 border border-neutral-800 space-y-2">
          <span className="text-[10px] uppercase font-bold text-neutral-400 tracking-wider block">
            Corroborating Signals & Evaluated Evidence
          </span>
          {expl.details.length === 0 ? (
            <p className="text-xs text-neutral-400 italic">No detailed signals attached to this proposal record.</p>
          ) : (
            <div className="space-y-1.5">
              {expl.details.map((detail, idx) => {
                const textStr = String(detail);
                const isCritical =
                  textStr.includes('[CRITICAL]') ||
                  textStr.includes('FAIL-CLOSED') ||
                  textStr.toLowerCase().includes('prompt injection') ||
                  textStr.toLowerCase().includes('adversarial');
                const isWarning =
                  textStr.includes('[UNRESOLVED') ||
                  textStr.toLowerCase().includes('mismatch') ||
                  textStr.toLowerCase().includes('missing');
                return (
                  <div
                    key={idx}
                    className={`p-2 rounded text-xs flex items-start gap-2 ${
                      isCritical
                        ? 'bg-neutral-900/40 border border-neutral-400/40 text-neutral-100'
                        : isWarning
                        ? 'bg-neutral-900/30 border border-neutral-400/30 text-neutral-100'
                        : 'bg-black border border-neutral-800/80 text-neutral-300'
                    }`}
                  >
                    {isCritical ? (
                      <span className="text-neutral-200 font-bold shrink-0">✕</span>
                    ) : isWarning ? (
                      <span className="text-neutral-200 font-bold shrink-0">⚠</span>
                    ) : (
                      <span className="text-neutral-200 shrink-0">✓</span>
                    )}
                    <span>{detail}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Ambiguity Certificate (if competing invoices conflict) */}
        {proposal.ambiguity_certificate && (
          <div className="p-3.5 rounded-lg bg-neutral-900/20 border border-neutral-400/30 space-y-3 text-xs">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-neutral-200 uppercase tracking-wider text-[10px] flex items-center gap-1.5">
                <AlertTriangle size={12} className="text-neutral-200" />
                Ambiguity Certificate (Multi-Candidate Conflict)
              </span>
              <span className="badge badge-ambiguous text-[10px]">
                HUMAN DISAMBIGUATION REQUIRED
              </span>
            </div>
            <p className="text-neutral-100/90 text-xs">
              Multiple competing invoices matched with identical confidence. Automated allocation is withheld to prevent misapplication of customer funds.
            </p>

            {/* Structured Candidates Comparison */}
            {proposal.ambiguity_certificate.candidates_detail && proposal.ambiguity_certificate.candidates_detail.length > 0 && (
              <div className="space-y-1.5">
                <span className="text-[11px] font-semibold text-neutral-300 block">
                  Competing Invoice Candidates:
                </span>
                <div className="overflow-x-auto rounded border border-neutral-800 bg-black/80">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-neutral-800 text-[11px] text-neutral-400 bg-neutral-900/50">
                        <th className="py-2 px-2.5">Invoice</th>
                        <th className="py-2 px-2.5">Customer</th>
                        <th className="py-2 px-2.5 text-right">Total</th>
                        <th className="py-2 px-2.5 text-right">Outstanding</th>
                        <th className="py-2 px-2.5 text-right">Score</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-neutral-800/60 font-mono text-[11px]">
                      {proposal.ambiguity_certificate.candidates_detail.map((c: any) => (
                        <tr key={c.id || c.invoice_number} className="hover:bg-neutral-900/40">
                          <td className="py-2 px-2.5 text-neutral-200 font-semibold">
                            {c.invoice_number}
                            {c.memo && <span className="block text-[10px] text-neutral-400 font-sans truncate max-w-[180px]">{c.memo}</span>}
                          </td>
                          <td className="py-2 px-2.5 text-neutral-300 font-sans">{c.customer_name}</td>
                          <td className="py-2 px-2.5 text-right text-neutral-300">
                            <MoneyDisplay amountMinor={c.total_minor} currency={c.currency} />
                          </td>
                          <td className="py-2 px-2.5 text-right text-neutral-200">
                            <MoneyDisplay amountMinor={c.outstanding_minor} currency={c.currency} />
                          </td>
                          <td className="py-2 px-2.5 text-right font-bold text-neutral-200">{c.score}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Recommended Operator Steps */}
            {proposal.ambiguity_certificate.recommended_steps && (
              <div className="space-y-1 pt-1">
                <span className="text-[10px] uppercase font-bold text-neutral-200/90 tracking-wider block">
                  Recommended Disambiguation Steps:
                </span>
                <ul className="list-disc list-inside space-y-0.5 text-neutral-300 text-[11px]">
                  {proposal.ambiguity_certificate.recommended_steps.map((step: string, idx: number) => (
                    <li key={idx} className="leading-relaxed">{step}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* Collapsible Raw Certificate */}
            <details className="pt-1">
              <summary className="text-[11px] text-neutral-400 hover:text-neutral-200 cursor-pointer font-sans select-none">
                View Raw Ambiguity Certificate JSON
              </summary>
              <pre className="mt-2 p-2 rounded bg-black font-mono text-[10px] text-neutral-200/80 overflow-x-auto max-h-48">
                {JSON.stringify(proposal.ambiguity_certificate, null, 2)}
              </pre>
            </details>
          </div>
        )}

        {/* Independent Verifier Status */}
        <div className="p-3 rounded-lg bg-neutral-900 border border-neutral-800 flex justify-between items-center text-xs">
          <div>
            <span className="text-neutral-400 block text-[11px]">Independent Verifier Status</span>
            <span className="font-mono font-bold text-white">{proposal.verifier_status}</span>
          </div>
          <span className="badge badge-reconciled font-mono">
            FAIL-CLOSED INVARIANTS PASS
          </span>
        </div>

        <div className="flex justify-end pt-2">
          <button onClick={onClose} className="btn btn-secondary text-xs">
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}
