import React, { useState } from 'react';
import { ShieldCheck, ShieldAlert, AlertTriangle, CheckCircle, XCircle, Sparkles, Play } from 'lucide-react';
import { Modal, MoneyDisplay } from './Common';
import { ProposalItem, InvestigationDetail } from '../types/api';

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
        <div className="p-3.5 rounded-lg bg-emerald-950/30 border border-emerald-500/30 flex items-start gap-3">
          <ShieldCheck size={18} className="text-emerald-400 mt-0.5 shrink-0" />
          <div className="text-xs space-y-1">
            <span className="font-semibold text-emerald-300 block">
              Independent Verifier Status: {proposal.verifier_status}
            </span>
            <p className="text-emerald-400/90 leading-relaxed">
              Ledger capacities, currencies, and mathematical invariants verified. Proceeding will execute the 10-step atomic financial transaction.
            </p>
          </div>
        </div>

        {/* Financial Allocation Summary */}
        <div className="bg-slate-900/60 p-4 rounded-lg border border-slate-800 space-y-2.5">
          <div className="flex justify-between items-center text-xs">
            <span className="text-slate-400">Payment:</span>
            <span className="font-mono text-slate-200 font-medium">
              {proposal.provider_payment_id} ({proposal.payer_name || 'Direct Remittance'})
            </span>
          </div>
          <div className="flex justify-between items-center text-xs">
            <span className="text-slate-400">Payment Total:</span>
            <MoneyDisplay amountMinor={proposal.payment_amount_minor} currency={proposal.payment_currency} />
          </div>

          <div className="pt-2 border-t border-slate-800">
            <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider block mb-1.5">
              Target Invoice Allocations:
            </span>
            {proposal.allocations.map((alloc, idx) => (
              <div key={idx} className="flex justify-between items-center text-xs py-1">
                <span className="font-mono text-blue-400">{alloc.invoice_number}</span>
                <MoneyDisplay amountMinor={alloc.amount_minor} currency={alloc.currency} />
              </div>
            ))}
          </div>

          {proposal.excess_amount_minor > 0 && (
            <div className="flex justify-between items-center text-xs pt-2 border-t border-slate-800 text-amber-400">
              <span>Unapplied Cash Remainder:</span>
              <MoneyDisplay amountMinor={proposal.excess_amount_minor} currency={proposal.payment_currency} />
            </div>
          )}
        </div>

        {/* Operator Accountability */}
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-1">
              Approving Operator Name / Email <span className="text-red-400">*</span>
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
            <label className="block text-xs font-medium text-slate-300 mb-1">
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
        <div className="flex justify-end gap-3 pt-4 border-t border-slate-800/80">
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
            disabled={isLoading || !reviewer.trim()}
            className="btn btn-success text-xs font-semibold px-4"
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
        <div className="p-3 rounded-lg bg-red-950/20 border border-red-500/20 text-xs text-red-300">
          Rejection will record a human decision record and prevent this proposal from creating ledger allocations. An audit event will be appended to the hash chain.
        </div>

        <div>
          <label className="block text-xs font-medium text-slate-300 mb-1">
            Rejecting Operator <span className="text-red-400">*</span>
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
          <label className="block text-xs font-medium text-slate-300 mb-1">
            Mandatory Rejection Reason <span className="text-red-400">*</span>
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

        <div className="flex justify-end gap-3 pt-3 border-t border-slate-800">
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
          <label className="block text-xs font-medium text-slate-300 mb-1.5">
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
          <p className="text-[11px] text-slate-400 mt-1.5">
            Invariant: Match runs generate PROPOSED outcomes only. No allocations will be approved automatically.
          </p>
        </div>

        <div className="flex justify-end gap-3 pt-4 border-t border-slate-800">
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
          <label className="block text-xs font-medium text-slate-300 mb-1">
            Exception Target Type
          </label>
          <select value={type} onChange={(e) => setType(e.target.value)} className="select-input text-xs">
            <option value="payment">Unmatched / Partial Payment</option>
            <option value="invoice">Unpaid / Discrepant Invoice</option>
            <option value="proposal">Ambiguous Proposal</option>
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-slate-300 mb-1">
            Target Identifier (Payment ID or Invoice Number) <span className="text-red-400">*</span>
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

        <div className="flex items-center gap-2 p-3 rounded bg-slate-900 border border-slate-800 text-xs text-slate-300">
          <input
            type="checkbox"
            id="syncMode"
            checked={syncMode}
            onChange={(e) => setSyncMode(e.target.checked)}
            className="rounded border-slate-700 bg-slate-800 text-blue-600 focus:ring-0"
          />
          <label htmlFor="syncMode" className="cursor-pointer">
            Run investigation synchronously (results available immediately)
          </label>
        </div>

        <div className="flex justify-end gap-3 pt-4 border-t border-slate-800">
          <button type="button" onClick={onClose} disabled={isLoading} className="btn btn-secondary text-xs">
            Cancel
          </button>
          <button
            type="submit"
            disabled={isLoading || !id.trim()}
            className="btn btn-primary text-xs font-semibold flex items-center gap-2 bg-gradient-to-r from-blue-600 to-indigo-600"
          >
            <Sparkles size={13} />
            {isLoading ? 'Running AI Investigation...' : 'Start Investigation'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
