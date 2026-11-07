import React, { useEffect, useState } from 'react';
import {
  CheckCircle,
  XCircle,
  AlertTriangle,
  Clock,
  ShieldCheck,
  RefreshCw,
  ArrowRight,
  Filter,
} from 'lucide-react';
import { api } from '../api/client';
import { ProposalItem, ProposalDetail } from '../types/api';
import { MoneyDisplay, StatusBadge, VerifierStatusBadge, LoadingSpinner, EmptyState } from '../components/Common';
import { ProposalApprovalModal, ProposalRejectModal } from '../components/Modals';

export function ProposalsPage({ tenantId }: { tenantId: string }) {
  const [proposals, setProposals] = useState<ProposalItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<'PENDING' | 'APPROVED' | 'REJECTED' | 'ALL'>('PENDING');

  // Modals
  const [selectedForApproval, setSelectedForApproval] = useState<ProposalItem | null>(null);
  const [selectedForRejection, setSelectedForRejection] = useState<ProposalItem | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const fetchProposals = async () => {
    setIsLoading(true);
    try {
      const data = await api.listProposals({
        tenantId,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        limit: 100,
      });
      setProposals(data.proposals);
    } catch (err) {
      console.error('Failed to load proposals', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchProposals();
  }, [tenantId, statusFilter]);

  const handleApprove = async (reviewer: string, notes?: string) => {
    if (!selectedForApproval) return;
    setIsSubmitting(true);
    try {
      await api.approveProposal(selectedForApproval.id, reviewer, notes, tenantId);
      setSelectedForApproval(null);
      await fetchProposals();
    } catch (err: any) {
      alert(`Approval failed: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleReject = async (reviewer: string, reason: string) => {
    if (!selectedForRejection) return;
    setIsSubmitting(true);
    try {
      await api.rejectProposal(selectedForRejection.id, reviewer, reason, tenantId);
      setSelectedForRejection(null);
      await fetchProposals();
    } catch (err: any) {
      alert(`Rejection failed: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white">Human Review Queue</h2>
          <p className="text-xs text-slate-400 mt-1">
            Authoritative operator approval queue. Proposals require human sign-off before financial ledger mutation.
          </p>
        </div>
        <button
          onClick={fetchProposals}
          disabled={isLoading}
          className="btn btn-secondary text-xs flex items-center gap-1.5"
        >
          <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-2 border-b border-slate-800 pb-2">
        {(['PENDING', 'APPROVED', 'REJECTED', 'ALL'] as const).map((tab) => {
          const active = statusFilter === tab;
          return (
            <button
              key={tab}
              onClick={() => setStatusFilter(tab)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                active
                  ? 'bg-blue-600/20 text-blue-400 border border-blue-500/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/40'
              }`}
            >
              {tab === 'PENDING' ? 'Pending Human Review' : tab}
            </button>
          );
        })}
      </div>

      {/* Table / List */}
      <div className="fintech-card p-0 overflow-hidden">
        {isLoading ? (
          <LoadingSpinner text="Retrieving Proposal Queue..." />
        ) : proposals.length === 0 ? (
          <div className="p-12 text-center">
            <EmptyState
              title={`Zero ${statusFilter.toLowerCase()} proposals`}
              description="No proposals match the current filter in the review queue."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="fintech-table">
              <thead>
                <tr>
                  <th>Proposal ID</th>
                  <th>Payment</th>
                  <th>Allocated Invoices</th>
                  <th>Outcome</th>
                  <th>Safety Verifier</th>
                  <th>Decision Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {proposals.map((p) => {
                  const isPending = p.decision_status === 'PENDING';
                  return (
                    <tr key={p.id}>
                      <td>
                        <span className="font-mono text-xs text-slate-300 font-semibold">{p.id}</span>
                        <span className="text-[10px] text-slate-500 font-mono block">Run: {p.run_id}</span>
                      </td>
                      <td>
                        <div className="font-mono text-xs text-blue-400 font-medium">
                          {p.provider_payment_id}
                        </div>
                        <div className="text-[11px] text-slate-400">{p.payer_name || 'Direct Remitter'}</div>
                        <MoneyDisplay
                          amountMinor={p.payment_amount_minor}
                          currency={p.payment_currency}
                          className="text-xs text-slate-300 font-medium"
                        />
                      </td>
                      <td>
                        {p.allocations.length === 0 ? (
                          <span className="text-slate-500 text-xs italic">None</span>
                        ) : (
                          <div className="space-y-1">
                            {p.allocations.map((a, idx) => (
                              <div key={idx} className="text-xs flex items-center justify-between gap-2">
                                <span className="font-mono text-emerald-400 font-semibold">{a.invoice_number}</span>
                                <MoneyDisplay amountMinor={a.amount_minor} currency={a.currency} className="text-slate-300 font-mono" />
                              </div>
                            ))}
                          </div>
                        )}
                      </td>
                      <td>
                        <span className="font-mono text-[11px] px-2 py-0.5 rounded bg-slate-900 border border-slate-700 text-slate-300">
                          {p.outcome}
                        </span>
                      </td>
                      <td>
                        <VerifierStatusBadge status={p.verifier_status} />
                      </td>
                      <td>
                        <StatusBadge status={p.decision_status} />
                      </td>
                      <td>
                        {isPending && p.outcome === 'PROPOSED' ? (
                          <div className="flex items-center gap-2">
                            <button
                              onClick={() => setSelectedForApproval(p)}
                              className="btn btn-success py-1 px-3 text-xs"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => setSelectedForRejection(p)}
                              className="btn btn-danger py-1 px-2.5 text-xs"
                            >
                              Reject
                            </button>
                          </div>
                        ) : (
                          <span className="text-xs text-slate-500 italic">No action required</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Modals */}
      <ProposalApprovalModal
        isOpen={Boolean(selectedForApproval)}
        onClose={() => setSelectedForApproval(null)}
        proposal={selectedForApproval}
        onConfirm={handleApprove}
        isLoading={isSubmitting}
      />

      <ProposalRejectModal
        isOpen={Boolean(selectedForRejection)}
        onClose={() => setSelectedForRejection(null)}
        proposal={selectedForRejection}
        onConfirm={handleReject}
        isLoading={isSubmitting}
      />
    </div>
  );
}
