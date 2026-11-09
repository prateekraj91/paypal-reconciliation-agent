import React, { useEffect, useState } from 'react';
import {
  ArrowLeft,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Sparkles,
  Database,
  HelpCircle,
  FileText,
  Lightbulb,
  Check,
  X,
  Lock,
} from 'lucide-react';
import { api } from '../api/client';
import { InvestigationDetail } from '../types/api';
import {
  MoneyDisplay,
  StatusBadge,
  VerifierStatusBadge,
  HashBadge,
  LoadingSpinner,
  Modal,
} from '../components/Common';
import { getFactSectionPresentation } from '../utils/formatters';

export function InvestigationDetailPage({
  investigationId,
  tenantId,
  onBack,
}: {
  investigationId: string;
  tenantId: string;
  onBack: () => void;
}) {
  const [inv, setInv] = useState<InvestigationDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Decision Modals
  const [isApproveOpen, setIsApproveOpen] = useState(false);
  const [isRejectOpen, setIsRejectOpen] = useState(false);
  const [reviewer, setReviewer] = useState('controller.alex@demo.com');
  const [notes, setNotes] = useState('Approved based on verified project code and candidate analysis');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const fetchInvestigation = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.getInvestigation(investigationId, tenantId);
      setInv(data);
    } catch (err: any) {
      setError(err.message || 'Failed to load investigation details');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchInvestigation();
  }, [investigationId, tenantId]);

  if (isLoading) {
    return <LoadingSpinner text="Retrieving Forensic Investigation Record..." />;
  }

  if (error || !inv) {
    return (
      <div className="p-6 rounded-lg bg-red-950/20 border border-red-500/30 text-red-300 space-y-3">
        <h3 className="font-semibold text-sm">Failed to load investigation</h3>
        <p className="text-xs text-red-400">{error}</p>
        <button onClick={onBack} className="btn btn-secondary text-xs">
          ← Return to Queue
        </button>
      </div>
    );
  }

  const result = inv.result || {};
  const verifiedFacts = result.verified_facts || [];
  const hypotheses = result.hypotheses || [];
  const candidates = result.candidate_invoice_analysis || [];
  const missingEvidence = result.missing_evidence || [];
  const recommendedSteps = result.recommended_steps || [];
  const suggestedResolution = result.suggested_resolution;
  const riskFlags = result.risk_flags || [];
  const verifierNotes = result.verifier_notes || inv.verifier_details?.notes || [];

  const verifierDetails = inv.verifier_details || {};
  const checkedInvariants = (verifierDetails.checked_invariants || {}) as Record<string, boolean>;
  const hasVerifierIssues = Array.isArray(verifierDetails.issues) && verifierDetails.issues.length > 0;
  const hasFailedInvariants =
    Object.keys(checkedInvariants).length === 0 || Object.values(checkedInvariants).some((v) => v !== true);

  const BLOCKING_PATTERNS = ['INJECTION', 'CURRENCY_MISMATCH', 'CONTRADICT', 'REVERSAL', 'DISPUTE', 'REJECTED', 'MALFORMED'];
  const hasBlockingRiskFlag = (riskFlags || []).some((flag: string) =>
    BLOCKING_PATTERNS.some((pat) => String(flag).toUpperCase().includes(pat))
  );
  const hasConflictingEvidence = Array.isArray(result.conflicting_evidence) && result.conflicting_evidence.length > 0;

  const isEligibleVerifierStatus = inv.verifier_status === 'VERIFIED' || inv.verifier_status === 'NEEDS_REVIEW';
  const isVerifierPassed =
    isEligibleVerifierStatus &&
    verifierDetails.passed === true &&
    !hasVerifierIssues &&
    !hasFailedInvariants &&
    !hasBlockingRiskFlag &&
    !hasConflictingEvidence;

  const isInvalid = !isVerifierPassed;
  const isResolved = inv.status === 'APPROVED' || inv.status === 'REJECTED';
  const factSection = getFactSectionPresentation(isInvalid);
  const FactIcon = isInvalid ? XCircle : CheckCircle2;

  // Compute allocation distribution for display
  const allocDistribution: Array<{ invoiceId: string; amountMinor: number; currency: string }> = [];
  if (suggestedResolution) {
    const allocMap = suggestedResolution.suggested_allocations;
    const invIds = suggestedResolution.suggested_invoice_ids || [];
    const totalMinor = suggestedResolution.suggested_amount_cents || 0;
    const curr = inv.evidence_references?.[0]?.currency || 'USD';

    if (allocMap && Object.keys(allocMap).length > 0) {
      Object.entries(allocMap).forEach(([id, amt]) => {
        allocDistribution.push({ invoiceId: id, amountMinor: amt, currency: curr });
      });
    } else if (invIds.length > 0 && totalMinor > 0) {
      allocDistribution.push({ invoiceId: invIds[0], amountMinor: totalMinor, currency: curr });
    }
  }

  const handleApprove = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isInvalid) return;
    setIsSubmitting(true);
    try {
      await api.approveInvestigation(inv.id, reviewer, notes, tenantId);
      setIsApproveOpen(false);
      await fetchInvestigation();
    } catch (err: any) {
      alert(`Approval error: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleReject = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      await api.rejectInvestigation(inv.id, reviewer, notes, tenantId);
      setIsRejectOpen(false);
      await fetchInvestigation();
    } catch (err: any) {
      alert(`Rejection error: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in max-w-5xl mx-auto pb-16">
      {/* Back button and Meta Bar */}
      <div className="flex items-center justify-between pb-4 border-b border-slate-800">
        <button
          onClick={onBack}
          className="text-xs text-slate-400 hover:text-white flex items-center gap-1.5 transition-colors font-medium"
        >
          <ArrowLeft size={14} />
          Back to AI Investigations
        </button>
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-400 font-mono">Case ID: {inv.id}</span>
          <StatusBadge status={inv.status} />
        </div>
      </div>

      {/* Target & Classification Banner */}
      <div className="p-5 rounded-lg bg-slate-900 border border-slate-800 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <span className="text-[11px] font-bold uppercase text-slate-500 tracking-wider">
            Investigated Target
          </span>
          <div className="flex items-baseline gap-2.5 mt-1">
            <span className="text-xl font-bold font-mono text-white">{inv.target_id}</span>
            <span className="text-xs uppercase font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
              {inv.target_type}
            </span>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <span className="text-xs font-mono font-bold text-violet-300 px-2.5 py-0.5 rounded bg-violet-950/60 border border-violet-800/40">
              {inv.classification}
            </span>
            <span className="text-xs text-slate-400">
              Model: <strong className="text-slate-200">{inv.model_name || 'qwen3:8b'}</strong> ({inv.model_provider || 'ollama'})
            </span>
          </div>
        </div>

        {/* Verifier Badge in Header */}
        <div className="flex flex-col items-end gap-1.5">
          <span className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">
            Safety Verifier Status
          </span>
          <VerifierStatusBadge status={inv.verifier_status} />
        </div>
      </div>

      {/* CRITICAL FAIL-CLOSED SAFETY WARNING (if INVALID) */}
      {isInvalid && (
        <div className="p-4 rounded-lg bg-red-950/40 border-2 border-red-500/80 text-red-200 space-y-2 animate-fade-in shadow-[0_0_20px_rgba(239,68,68,0.2)]">
          <div className="flex items-center gap-2 text-sm font-bold text-red-300">
            <ShieldAlert size={18} className="text-red-400" />
            CRITICAL SAFETY ALERT: Independent Verifier Failed Closed (Verdict: INVALID)
          </div>
          <p className="text-xs text-red-300/90 leading-relaxed">
            The independent safety verifier rejected this investigation (e.g. prompt injection, capacity violation, or currency mismatch detected).
            Per financial safety invariants, single-click approval and financial ledger mutations are <strong>strictly disabled</strong>.
          </p>
          {riskFlags.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {riskFlags.map((flag, idx) => (
                <span key={idx} className="font-mono text-[10px] font-bold px-2 py-0.5 rounded bg-red-900/60 border border-red-700 text-red-200">
                  ⚠️ {flag}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Executive Summary */}
      <div className="fintech-card space-y-2">
        <h3 className="text-xs font-bold uppercase text-slate-400 tracking-wider flex items-center gap-2">
          <FileText size={14} className="text-blue-400" />
          Executive Summary
        </h3>
        <p className="text-xs text-slate-200 leading-relaxed font-sans bg-slate-900/60 p-3.5 rounded-lg border border-slate-800/80">
          {inv.summary || result.executive_summary || 'No summary available.'}
        </p>
      </div>

      {/* SECTION 1: MODEL FACTS (verified only when the independent verifier accepted them) */}
      <div className={`fintech-card space-y-3 ${factSection.cardClass}`}>
        <div className={`flex items-center justify-between pb-2 border-b ${factSection.dividerClass}`}>
          <h3 className={`text-xs font-bold uppercase tracking-wider flex items-center gap-2 ${factSection.headingClass}`}>
            <FactIcon size={14} className={factSection.headingClass} />
            {factSection.heading} ({verifiedFacts.length})
          </h3>
          <span className={`text-[10px] font-mono uppercase font-semibold ${factSection.badgeClass}`}>
            {factSection.badge}
          </span>
        </div>

        {verifiedFacts.length === 0 ? (
          <p className="text-xs text-slate-500 italic p-3">{factSection.emptyText}</p>
        ) : (
          <div className="space-y-2.5">
            {verifiedFacts.map((fact, idx) => (
              <div
                key={idx}
                className={`p-3 rounded-lg border space-y-1.5 ${factSection.rowClass}`}
              >
                <div className="flex items-start justify-between gap-3 text-xs">
                  <p className="text-slate-100 font-medium leading-relaxed">{fact.statement}</p>
                  {fact.amount_cents !== undefined && fact.amount_cents !== null && (
                    <MoneyDisplay
                      amountMinor={fact.amount_cents}
                      currency={fact.currency || 'USD'}
                      className={`font-semibold shrink-0 ${factSection.headingClass}`}
                    />
                  )}
                </div>
                {/* Cited Evidence IDs */}
                <div className="flex items-center gap-1.5 pt-1 text-[11px] text-slate-400">
                  <Database size={11} className="text-emerald-400" />
                  <span className={`text-[10px] uppercase font-bold ${factSection.badgeClass}`}>Cited Evidence:</span>
                  <div className="flex flex-wrap gap-1">
                    {fact.evidence_ids.map((evId, evIdx) => (
                      <span
                        key={evIdx}
                        className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-slate-900 border border-slate-700 text-emerald-300"
                      >
                        {evId}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* SECTION 2: HYPOTHESES (Unconfirmed Plausible Explanations) */}
      <div className="fintech-card space-y-3 border-violet-950/60 bg-[#121429]">
        <div className="flex items-center justify-between pb-2 border-b border-violet-900/40">
          <h3 className="text-xs font-bold uppercase text-violet-300 tracking-wider flex items-center gap-2">
            <Lightbulb size={14} className="text-violet-400" />
            Hypotheses ({hypotheses.length})
          </h3>
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-violet-950 text-violet-300 border border-violet-800/50 uppercase font-bold">
            Unconfirmed / Plausible
          </span>
        </div>

        {hypotheses.length === 0 ? (
          <p className="text-xs text-slate-500 italic p-3">No active hypotheses formulated.</p>
        ) : (
          <div className="space-y-2.5">
            {hypotheses.map((hyp) => (
              <div
                key={hyp.hypothesis_id}
                className="p-3 rounded-lg bg-violet-950/20 border border-violet-500/20 space-y-1.5"
              >
                <div className="flex items-start justify-between gap-3 text-xs">
                  <p className="text-slate-200 leading-relaxed">{hyp.statement}</p>
                  <span
                    className={`text-[10px] font-mono uppercase font-bold px-2 py-0.5 rounded shrink-0 ${
                      hyp.likelihood === 'HIGH'
                        ? 'bg-emerald-950 text-emerald-400 border border-emerald-800'
                        : hyp.likelihood === 'MEDIUM'
                        ? 'bg-amber-950 text-amber-400 border border-amber-800'
                        : 'bg-slate-900 text-slate-400 border border-slate-700'
                    }`}
                  >
                    Likelihood: {hyp.likelihood}
                  </span>
                </div>
                {hyp.supporting_evidence_ids.length > 0 && (
                  <div className="flex items-center gap-1.5 pt-1 text-[11px] text-slate-400">
                    <span className="text-[10px] uppercase font-bold text-violet-400">Supporting Evidence:</span>
                    <div className="flex flex-wrap gap-1">
                      {hyp.supporting_evidence_ids.map((id, i) => (
                        <span key={i} className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-slate-900 border border-slate-700 text-slate-300">
                          {id}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* SECTION 3: CANDIDATE INVOICE ANALYSIS */}
      {candidates.length > 0 && (
        <div className="fintech-card space-y-3">
          <h3 className="text-xs font-bold uppercase text-slate-400 tracking-wider">
            Candidate Invoices Evaluated ({candidates.length})
          </h3>
          <div className="space-y-2">
            {candidates.map((cand, idx) => (
              <div key={idx} className="p-3.5 rounded-lg bg-slate-900/80 border border-slate-800 space-y-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-mono font-bold text-blue-400">{cand.invoice_number}</span>
                  {cand.score !== undefined && cand.score !== null && (
                    <span className="font-mono text-xs font-semibold px-2 py-0.5 rounded bg-blue-950/60 text-blue-300 border border-blue-800/40">
                      Match Score: {cand.score}/100
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-4 text-[11px] text-slate-400">
                  <span>Customer Match: {cand.customer_match ? '✅ Yes' : '❌ No'}</span>
                  <span>Amount Match: {cand.amount_match ? '✅ Exact' : '❌ No'}</span>
                </div>
                <p className="text-slate-300 text-xs bg-slate-950 p-2.5 rounded border border-slate-850">
                  {cand.assessment}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* SECTION 4: MISSING EVIDENCE & RECOMMENDED OPERATOR STEPS */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Missing Evidence */}
        <div className="fintech-card space-y-2.5">
          <h3 className="text-xs font-bold uppercase text-slate-400 tracking-wider flex items-center gap-1.5">
            <HelpCircle size={14} className="text-amber-400" />
            Missing Information ({missingEvidence.length})
          </h3>
          {missingEvidence.length === 0 ? (
            <p className="text-xs text-slate-500 italic p-3">Zero missing items identified.</p>
          ) : (
            <div className="space-y-2">
              {missingEvidence.map((item, idx) => (
                <div key={idx} className="p-2.5 rounded bg-slate-900 border border-slate-800 text-xs space-y-1">
                  <div className="flex justify-between items-start">
                    <span className="font-medium text-slate-200">{item.missing_item}</span>
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-amber-950/80 text-amber-300 border border-amber-800/40 uppercase">
                      {item.importance}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-400">{item.why_needed}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Recommended Operator Steps */}
        <div className="fintech-card space-y-2.5">
          <h3 className="text-xs font-bold uppercase text-slate-400 tracking-wider flex items-center gap-1.5">
            <CheckCircle2 size={14} className="text-blue-400" />
            Recommended Investigation Steps ({recommendedSteps.length})
          </h3>
          {recommendedSteps.length === 0 ? (
            <p className="text-xs text-slate-500 italic p-3">No further steps required.</p>
          ) : (
            <div className="space-y-2">
              {recommendedSteps.map((step) => (
                <div key={step.step_number} className="p-2.5 rounded bg-slate-900 border border-slate-800 text-xs flex gap-2.5 items-start">
                  <span className="w-5 h-5 rounded-full bg-blue-900/60 text-blue-300 border border-blue-700/50 flex items-center justify-center font-mono text-[10px] font-bold shrink-0 mt-0.5">
                    {step.step_number}
                  </span>
                  <div>
                    <span className="font-semibold text-slate-200 block">{step.action}</span>
                    <span className="text-[11px] text-slate-400">Target: {step.target} • {step.reason}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* SECTION 5: SUGGESTED RESOLUTION & HUMAN DECISION ACTION BAR */}
      <div className="p-6 rounded-lg bg-gradient-to-r from-slate-900 to-[#12192e] border-2 border-blue-500/40 space-y-4 shadow-xl">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 pb-3 border-b border-slate-800">
          <div>
            <span className="text-[10px] uppercase font-bold text-blue-400 tracking-wider">
              AI Suggested Resolution
            </span>
            <h3 className="text-base font-bold text-white mt-0.5">
              {suggestedResolution ? suggestedResolution.resolution_type : 'No automatic resolution suggested'}
            </h3>
          </div>
          {suggestedResolution && suggestedResolution.suggested_amount_cents && (
            <div className="text-right">
              <span className="text-[10px] text-slate-400 block">Proposed Allocation Amount</span>
              <MoneyDisplay amountMinor={suggestedResolution.suggested_amount_cents} className="text-lg font-bold text-emerald-400" />
            </div>
          )}
        </div>

        {suggestedResolution?.notes && (
          <p className="text-xs text-slate-300 leading-relaxed bg-slate-950/70 p-3 rounded border border-slate-800">
            {suggestedResolution.notes}
          </p>
        )}

        {/* Action Buttons */}
        <div className="flex flex-col sm:flex-row items-center justify-between gap-3 pt-2">
          <span className="text-xs text-slate-400 font-mono">
            {isInvalid
              ? '⛔ Approval strictly blocked by safety verifier'
              : isResolved
              ? '✓ Investigation has been decided and resolved'
              : '⚡ Requires authorized operator review to execute'}
          </span>

          <div className="flex items-center gap-3">
            <button
              onClick={() => setIsRejectOpen(true)}
              disabled={isResolved}
              className="btn btn-secondary text-xs px-4"
            >
              Reject Case
            </button>

            <button
              onClick={() => setIsApproveOpen(true)}
              disabled={isInvalid || isResolved}
              title={isInvalid ? 'Cannot approve: Safety verifier status is INVALID' : 'Approve and execute resolution'}
              className="btn btn-success text-xs font-semibold px-5 flex items-center gap-2"
            >
              {isInvalid && <Lock size={12} />}
              <span>{isResolved ? 'Resolved' : 'Approve Proposed Resolution'}</span>
            </button>
          </div>
        </div>
      </div>

      {/* Approve Modal */}
      <Modal
        isOpen={isApproveOpen}
        onClose={() => setIsApproveOpen(false)}
        title="Authorize Investigation Resolution"
        subtitle={`Case ID: ${inv.id}`}
        maxWidth="max-w-2xl"
      >
        <form onSubmit={handleApprove} className="space-y-4">
          <div className="p-3 rounded-lg bg-emerald-950/30 border border-emerald-500/30 text-xs text-emerald-300 leading-relaxed">
            Approving this investigation links to the verified deterministic proposal and executes the 10-step atomic allocation via <strong>ApprovalService</strong>.
          </div>

          {/* Allocation Distribution Table */}
          <div className="p-3.5 rounded-xl bg-slate-900 border border-slate-800 space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wider text-slate-300 flex items-center justify-between">
              <span>Proposed Allocation Distribution</span>
              <span className="font-mono text-emerald-400">
                Target: {inv.target_type}:{inv.target_id}
              </span>
            </div>

            {allocDistribution.length > 0 ? (
              <table className="w-full text-xs font-mono text-left">
                <thead>
                  <tr className="border-b border-slate-800 text-slate-400">
                    <th className="py-1.5 font-medium">Target Invoice</th>
                    <th className="py-1.5 font-medium">Allocation Amount</th>
                    <th className="py-1.5 font-medium text-right">Currency</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                  {allocDistribution.map((a, idx) => (
                    <tr key={idx} className="text-slate-200">
                      <td className="py-2">{a.invoiceId}</td>
                      <td className="py-2 text-emerald-400">
                        <MoneyDisplay amountMinor={a.amountMinor} currency={a.currency} />
                      </td>
                      <td className="py-2 text-right text-slate-400">{a.currency}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="text-xs text-rose-400">
                Warning: No structured allocations detected in the recommendation.
              </div>
            )}
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-300 mb-1 flex items-center justify-between">
              <span>
                Approving Operator Identity <span className="text-red-400">*</span>
              </span>
              <span className="text-[10px] text-slate-500 font-normal">
                Self-reported for local demo audit
              </span>
            </label>
            <input
              type="text"
              required
              value={reviewer}
              onChange={(e) => setReviewer(e.target.value)}
              className="input-text text-xs"
              placeholder="e.g. controller.sarah@demo.com"
            />
            <p className="text-[11px] text-slate-500 mt-1">
              Local Operator Notice: Operator identity is recorded in the SHA-256 audit log for local accountability and does not represent authenticated multi-user RBAC.
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-300 mb-1">
              Audit Notes
            </label>
            <textarea
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="input-text text-xs resize-none"
              placeholder="Audit justification for operator decision..."
            />
          </div>

          <div className="flex justify-end gap-3 pt-4 border-t border-slate-800">
            <button type="button" onClick={() => setIsApproveOpen(false)} disabled={isSubmitting} className="btn btn-secondary text-xs">
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || !reviewer.trim() || isInvalid}
              className="btn btn-success text-xs font-semibold px-4"
            >
              {isSubmitting ? 'Executing Allocation Transaction...' : 'Confirm & Authorize'}
            </button>
          </div>
        </form>
      </Modal>

      {/* Reject Modal */}
      <Modal
        isOpen={isRejectOpen}
        onClose={() => setIsRejectOpen(false)}
        title="Reject Investigation Recommendation"
        subtitle={`Case ID: ${inv.id}`}
      >
        <form onSubmit={handleReject} className="space-y-4">
          <div className="p-3.5 rounded-lg bg-red-950/30 border border-red-500/30 text-xs text-red-300 leading-relaxed">
            Rejecting this recommendation records an immutable audit event and closes the investigation without financial mutation.
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-300 mb-1">
              Operator Name / Email <span className="text-red-400">*</span>
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
              Rejection Reason & Next Steps <span className="text-red-400">*</span>
            </label>
            <textarea
              required
              rows={3}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="input-text text-xs resize-none"
            />
          </div>

          <div className="flex justify-end gap-3 pt-4 border-t border-slate-800">
            <button type="button" onClick={() => setIsRejectOpen(false)} disabled={isSubmitting} className="btn btn-secondary text-xs">
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || !reviewer.trim() || !notes.trim()}
              className="btn btn-danger text-xs font-semibold px-4"
            >
              {isSubmitting ? 'Recording Rejection...' : 'Reject Investigation'}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
