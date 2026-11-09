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
import { ProposalApprovalModal, ProposalRejectModal, EvidenceInspectModal } from '../components/Modals';
import { getProposalEmptyLabels, getMatcherExplanation } from '../utils/formatters';

export function ProposalsPage({ tenantId }: { tenantId: string }) {
  const [proposals, setProposals] = useState<ProposalItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<'PENDING' | 'APPROVED' | 'REJECTED' | 'ALL'>('PENDING');

  // Modals
  const [selectedForApproval, setSelectedForApproval] = useState<ProposalItem | null>(null);
  const [selectedForRejection, setSelectedForRejection] = useState<ProposalItem | null>(null);
  const [selectedForInspect, setSelectedForInspect] = useState<ProposalItem | null>(null);
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

  const actionableCount = proposals.filter((p) => p.decision_status === 'PENDING' && p.outcome === 'PROPOSED').length;
  const unresolvedCount = proposals.filter((p) => p.decision_status === 'PENDING').length;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold tracking-tight text-white">Human Review Queue</h2>
            {statusFilter === 'PENDING' && (
              <span className="badge badge-info text-xs font-mono">
                {actionableCount} Actionable ({unresolvedCount} Unresolved)
              </span>
            )}
          </div>
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
              {tab === 'PENDING' ? `Pending Review (${actionableCount} actionable)` : tab}
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
              title={getProposalEmptyLabels(statusFilter).title}
              description={getProposalEmptyLabels(statusFilter).description}
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
                  <th>Match Reason & Evidence</th>
                  <th>Safety Verifier</th>
                  <th>Decision Status</th>
                  <th className="col-sticky-right text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {proposals.map((p) => {
                  const isPending = p.decision_status === 'PENDING';
                  const isEligible = p.approval_eligible !== false && p.verifier_status !== 'INVALID';
                  const expl = getMatcherExplanation(
                    p.outcome,
                    p.evidence,
                    p.excess_amount_minor,
                    p.ambiguity_certificate,
                    p.allocations
                  );

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
                          <span className="text-slate-500 text-xs italic">No allocation</span>
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
                      <td className="max-w-[280px]">
                        <div className="space-y-1">
                          {p.verifier_status === 'INVALID' || expl.badgeLabel === 'PROMPT_INJECTION_SUSPECT' ? (
                            <span className="badge badge-danger text-[11px] font-mono">
                              PROMPT_INJECTION_SUSPECT
                            </span>
                          ) : p.verifier_status === 'NEEDS_REVIEW' && !isEligible ? (
                            <span className="badge badge-warning text-[11px] font-mono">
                              REMITTER MISMATCH
                            </span>
                          ) : (
                            <span className="badge badge-info text-[11px] font-mono">
                              {expl.badgeLabel}
                            </span>
                          )}
                          <p className="text-[11px] text-slate-400 leading-tight line-clamp-2">
                            {p.approval_prohibited_reason || expl.summary}
                          </p>
                        </div>
                      </td>
                      <td>
                        <VerifierStatusBadge status={p.verifier_status} />
                      </td>
                      <td>
                        <StatusBadge status={p.decision_status} />
                      </td>
                      <td className="col-sticky-right text-right" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            onClick={() => setSelectedForInspect(p)}
                            className="btn btn-secondary py-1 px-2 text-[11px] text-slate-300 hover:text-white"
                            title="Inspect matching signals, candidate invoices, and verifier checks"
                          >
                            Evidence
                          </button>
                          {isPending && p.outcome === 'PROPOSED' ? (
                            <>
                              {isEligible ? (
                                <button
                                  onClick={() => setSelectedForApproval(p)}
                                  className="btn btn-success py-1 px-2.5 text-xs font-semibold"
                                >
                                  Approve
                                </button>
                              ) : (
                                <button
                                  disabled
                                  className="btn btn-secondary py-1 px-2.5 text-xs font-semibold opacity-50 cursor-not-allowed text-slate-400"
                                  title={p.approval_prohibited_reason || "Approval prohibited by safety verifier"}
                                >
                                  Approve
                                </button>
                              )}
                              <button
                                onClick={() => setSelectedForRejection(p)}
                                className="btn btn-danger py-1 px-2 text-xs"
                                title="Reject proposed reconciliation"
                              >
                                Reject
                              </button>
                            </>
                          ) : (
                            <span className="text-[11px] text-slate-500 italic pr-1">No action</span>
                          )}
                        </div>
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
      <EvidenceInspectModal
        isOpen={Boolean(selectedForInspect)}
        onClose={() => setSelectedForInspect(null)}
        proposal={selectedForInspect}
      />

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
