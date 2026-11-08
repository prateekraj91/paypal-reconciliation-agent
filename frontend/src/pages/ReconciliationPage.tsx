import React, { useEffect, useState } from 'react';
import {
  GitMerge,
  Play,
  RefreshCw,
  Clock,
  ShieldCheck,
  CheckCircle,
  AlertTriangle,
  ArrowRight,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import { ReconciliationRunItem, ProposalItem } from '../types/api';
import { MoneyDisplay, StatusBadge, VerifierStatusBadge, LoadingSpinner, EmptyState } from '../components/Common';
import { RunTriggerModal, ProposalApprovalModal, ProposalRejectModal } from '../components/Modals';
import { getMatcherExplanation } from '../utils/formatters';

export function ReconciliationPage({ tenantId }: { tenantId: string }) {
  const [runs, setRuns] = useState<ReconciliationRunItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isTriggerModalOpen, setIsTriggerModalOpen] = useState(false);
  const [isTriggering, setIsTriggering] = useState(false);

  // Selected Run Detail
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<ReconciliationRunItem | null>(null);
  const [isRunDetailLoading, setIsRunDetailLoading] = useState(false);

  // Approval / Rejection from Run Detail
  const [selectedProposalForApproval, setSelectedProposalForApproval] = useState<ProposalItem | null>(null);
  const [selectedProposalForRejection, setSelectedProposalForRejection] = useState<ProposalItem | null>(null);
  const [isActionLoading, setIsActionLoading] = useState(false);

  const fetchRuns = async () => {
    setIsLoading(true);
    try {
      const data = await api.listRuns(tenantId);
      setRuns(data.runs);
      if (data.runs.length > 0 && !selectedRunId) {
        openRunDetail(data.runs[0].id);
      }
    } catch (err) {
      console.error('Failed to load runs', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchRuns();
  }, [tenantId]);

  const handleTriggerRun = async (matcherName: string) => {
    setIsTriggering(true);
    try {
      await api.triggerRun(matcherName, tenantId);
      setIsTriggerModalOpen(false);
      await fetchRuns();
    } catch (err: any) {
      alert(`Run execution failed: ${err.message}`);
    } finally {
      setIsTriggering(false);
    }
  };

  const openRunDetail = async (runId: string) => {
    setSelectedRunId(runId);
    setIsRunDetailLoading(true);
    try {
      const detail = await api.getRun(runId, tenantId);
      setRunDetail(detail);
    } catch (err) {
      console.error('Failed to load run detail', err);
    } finally {
      setIsRunDetailLoading(false);
    }
  };

  const handleApproveProposal = async (reviewer: string, notes?: string) => {
    if (!selectedProposalForApproval) return;
    setIsActionLoading(true);
    try {
      await api.approveProposal(selectedProposalForApproval.id, reviewer, notes, tenantId);
      setSelectedProposalForApproval(null);
      if (selectedRunId) openRunDetail(selectedRunId);
    } catch (err: any) {
      alert(`Approval failed: ${err.message}`);
    } finally {
      setIsActionLoading(false);
    }
  };

  const handleRejectProposal = async (reviewer: string, reason: string) => {
    if (!selectedProposalForRejection) return;
    setIsActionLoading(true);
    try {
      await api.rejectProposal(selectedProposalForRejection.id, reviewer, reason, tenantId);
      setSelectedProposalForRejection(null);
      if (selectedRunId) openRunDetail(selectedRunId);
    } catch (err: any) {
      alert(`Rejection failed: ${err.message}`);
    } finally {
      setIsActionLoading(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Page Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white">Reconciliation Runs</h2>
          <p className="text-xs text-slate-400 mt-1">
            Deterministic matching passes producing proposed allocations with independent verifications.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={fetchRuns}
            disabled={isLoading}
            className="btn btn-secondary text-xs flex items-center gap-1.5"
          >
            <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
            <span>Refresh</span>
          </button>
          <button
            onClick={() => setIsTriggerModalOpen(true)}
            className="btn btn-primary text-xs flex items-center gap-2"
          >
            <Play size={13} />
            <span>Trigger Run</span>
          </button>
        </div>
      </div>

      {/* Grid: Left = Run History, Right = Selected Run Proposals */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Runs List */}
        <div className="lg:col-span-1 space-y-3">
          <h3 className="text-xs font-semibold uppercase text-slate-400 tracking-wider">
            Run History ({runs.length})
          </h3>

          {isLoading ? (
            <LoadingSpinner text="Fetching Runs..." />
          ) : runs.length === 0 ? (
            <div className="fintech-card p-6 text-center">
              <EmptyState
                title="No reconciliation runs"
                description="Trigger a deterministic match pass to process unallocated transactions."
              />
            </div>
          ) : (
            <div className="space-y-2 max-h-[70vh] overflow-y-auto pr-1">
              {runs.map((r) => {
                const isSelected = selectedRunId === r.id;
                return (
                  <div
                    key={r.id}
                    onClick={() => openRunDetail(r.id)}
                    className={`p-3.5 rounded-lg border cursor-pointer transition-all ${
                      isSelected
                        ? 'bg-blue-950/40 border-blue-500/60 shadow-[0_0_15px_rgba(59,130,246,0.15)]'
                        : 'bg-slate-900/80 border-slate-800 hover:border-slate-700'
                    }`}
                  >
                    <div className="flex justify-between items-start">
                      <span className="font-mono text-xs font-semibold text-blue-400">{r.id}</span>
                      <span className="badge badge-reconciled text-[10px] py-0.5">COMPLETED</span>
                    </div>

                    <div className="mt-2 space-y-1 text-xs text-slate-400">
                      <div className="flex justify-between">
                        <span>Matcher:</span>
                        <span className="font-mono text-slate-300 font-medium">{r.matcher_name}</span>
                      </div>
                      <div className="flex justify-between">
                        <span>Executed:</span>
                        <span className="font-mono text-slate-400 text-[11px]">
                          {(() => {
                            const execTime = r.run_at || r.executed_at || r.created_at;
                            return execTime ? new Date(execTime).toLocaleString() : 'Completed';
                          })()}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Selected Run Details & Generated Proposals */}
        <div className="lg:col-span-2 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase text-slate-400 tracking-wider">
              {selectedRunId ? `Run Findings: ${selectedRunId}` : 'Run Proposals'}
            </h3>
            {runDetail?.total_proposals !== undefined && (
              <span className="text-xs text-slate-400 font-mono">
                {runDetail.total_proposals} Proposals Generated
              </span>
            )}
          </div>

          <div className="fintech-card p-0 overflow-hidden min-h-[400px]">
            {isRunDetailLoading ? (
              <LoadingSpinner text="Loading run proposals and verifier results..." />
            ) : !runDetail || !runDetail.proposals || runDetail.proposals.length === 0 ? (
              <div className="p-12 text-center">
                <EmptyState
                  title="No match proposals found."
                  description="This run completed with no eligible unapplied match proposals."
                />
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="fintech-table">
                  <thead>
                    <tr>
                      <th>Payment</th>
                      <th>Target Invoice(s)</th>
                      <th>Match Reason & Evidence</th>
                      <th>Verifier</th>
                      <th>Decision</th>
                      <th className="col-sticky-right text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runDetail.proposals.map((p) => {
                      const isPending = p.decision_status === 'PENDING';
                      const expl = getMatcherExplanation(
                        p.outcome,
                        p.evidence,
                        p.excess_amount_minor,
                        p.ambiguity_certificate
                      );

                      return (
                        <tr key={p.id}>
                          <td>
                            <div className="font-mono text-xs text-blue-400 font-semibold">
                              {p.provider_payment_id}
                            </div>
                            <div className="text-[11px] text-slate-400">{p.payer_name || 'Direct Remitter'}</div>
                            <MoneyDisplay amountMinor={p.payment_amount_minor} currency={p.payment_currency} className="text-xs text-slate-300" />
                          </td>
                          <td>
                            {p.allocations.length === 0 ? (
                              <span className="text-slate-500 text-xs italic">No invoice allocated</span>
                            ) : (
                              <div className="space-y-1">
                                {p.allocations.map((a, idx) => (
                                  <div key={idx} className="text-xs flex items-center justify-between gap-2">
                                    <span className="font-mono text-emerald-400 font-medium">{a.invoice_number}</span>
                                    <MoneyDisplay amountMinor={a.amount_minor} currency={a.currency} className="text-slate-300 font-medium" />
                                  </div>
                                ))}
                              </div>
                            )}
                          </td>
                          <td className="max-w-[260px]">
                            <div className="space-y-1">
                              <span className="badge badge-info text-[10px] font-mono">
                                {expl.badgeLabel}
                              </span>
                              <p className="text-[11px] text-slate-400 leading-tight">
                                {expl.summary}
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
                            {isPending && p.outcome === 'PROPOSED' ? (
                              <div className="flex items-center justify-end gap-1.5">
                                <button
                                  onClick={() => setSelectedProposalForApproval(p)}
                                  className="btn btn-success py-1 px-2.5 text-[11px]"
                                >
                                  Approve
                                </button>
                                <button
                                  onClick={() => setSelectedProposalForRejection(p)}
                                  className="btn btn-danger py-1 px-2 text-[11px]"
                                >
                                  Reject
                                </button>
                              </div>
                            ) : (
                              <span className="text-[11px] text-slate-500 italic">Resolved</span>
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
        </div>
      </div>

      {/* Modals */}
      <RunTriggerModal
        isOpen={isTriggerModalOpen}
        onClose={() => setIsTriggerModalOpen(false)}
        onConfirm={handleTriggerRun}
        isLoading={isTriggering}
      />

      <ProposalApprovalModal
        isOpen={Boolean(selectedProposalForApproval)}
        onClose={() => setSelectedProposalForApproval(null)}
        proposal={selectedProposalForApproval}
        onConfirm={handleApproveProposal}
        isLoading={isActionLoading}
      />

      <ProposalRejectModal
        isOpen={Boolean(selectedProposalForRejection)}
        onClose={() => setSelectedProposalForRejection(null)}
        proposal={selectedProposalForRejection}
        onConfirm={handleRejectProposal}
        isLoading={isActionLoading}
      />
    </div>
  );
}
