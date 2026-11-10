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
import { getFactSectionPresentation, getInvestigationVerdict } from '../utils/formatters';

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
      <div className="p-6 rounded-lg bg-neutral-900/20 border border-neutral-400/30 text-neutral-200 space-y-3">
        <h3 className="font-semibold text-sm">Failed to load investigation</h3>
        <p className="text-xs text-neutral-200">{error}</p>
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

  // The fail-closed banner comes only from the deterministic verifier's verdict.
  // Model-raised flags are advisory: shown, and able to hold approval, but never the verdict.
  const verdict = getInvestigationVerdict(inv);
  const isInvalid = verdict.verifierRejected;
  const approvalBlocked = verdict.approvalBlocked;
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
    if (approvalBlocked) return;
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
      <div className="flex items-center justify-between pb-4 border-b border-neutral-800">
        <button
          onClick={onBack}
          className="text-xs text-neutral-400 hover:text-white flex items-center gap-1.5 transition-colors font-medium"
        >
          <ArrowLeft size={14} />
          Back to AI Investigations
        </button>
        <div className="flex items-center gap-2">
          <span className="text-xs text-neutral-400 font-mono">Case ID: {inv.id}</span>
          <StatusBadge status={inv.status} />
        </div>
      </div>

      {/* Target & Classification Banner */}
      <div className="p-5 rounded-lg bg-neutral-900 border border-neutral-800 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <span className="text-[11px] font-bold uppercase text-neutral-400 tracking-wider">
            Investigated Target
          </span>
          <div className="flex items-baseline gap-2.5 mt-1">
            <span className="text-xl font-bold font-mono text-white">{inv.target_id}</span>
            <span className="text-xs uppercase font-mono px-2 py-0.5 rounded bg-neutral-800 text-neutral-300 border border-neutral-700">
              {inv.target_type}
            </span>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <span className="text-xs font-mono font-bold text-neutral-200 px-2.5 py-0.5 rounded bg-neutral-900/60 border border-neutral-700/40">
              {inv.classification}
            </span>
            <span className="text-xs text-neutral-400">
              Model: <strong className="text-neutral-200">{inv.model_name || 'qwen3:8b'}</strong> ({inv.model_provider || 'ollama'})
            </span>
          </div>
        </div>

        {/* Verifier Badge in Header */}
        <div className="flex flex-col items-end gap-1.5">
          <span className="text-[10px] uppercase font-bold text-neutral-400 tracking-wider">
            Safety Verifier Status
          </span>
          <VerifierStatusBadge status={inv.verifier_status} />
        </div>
      </div>

      {/* CRITICAL FAIL-CLOSED SAFETY WARNING (if INVALID) */}
      {isInvalid && (
        <div
          data-testid="fail-closed-banner"
          role="alert"
          className="state-fail-closed p-5 rounded-lg space-y-2.5 animate-fade-in"
        >
          <div className="flex items-center gap-2.5 text-base font-extrabold uppercase tracking-wide text-black">
            <span aria-hidden="true" className="glyph-mono text-xl leading-none">⛔</span>
            <XCircle size={20} strokeWidth={3} className="text-black shrink-0" />
            CRITICAL SAFETY ALERT: Independent Verifier Failed Closed (Verdict: INVALID)
          </div>
          <p className="text-xs text-black leading-relaxed font-medium">
            The independent safety verifier rejected this investigation (e.g. prompt injection, capacity violation, or currency mismatch detected).
            Per financial safety invariants, single-click approval and financial ledger mutations are <strong>strictly disabled</strong>.
          </p>
          {riskFlags.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {riskFlags.map((flag, idx) => (
                <span key={idx} className="font-mono text-[10px] font-bold px-2 py-0.5 rounded bg-black border-2 border-black text-white">
                  ✕ {flag}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ADVISORY: flags the model raised on a run the verifier accepted */}
      {!isInvalid && verdict.modelRaisedFlags.length > 0 && (
        <div
          data-testid="model-raised-flags"
          className="state-advisory p-4 rounded-lg text-neutral-100 space-y-2"
        >
          <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-white">
            <span aria-hidden="true">⚠</span>
            <AlertTriangle size={14} className="text-white" />
            Model-raised flags (advisory)
          </div>
          <p className="text-xs text-neutral-100/90 leading-relaxed">
            The model raised these flags itself. They are not the verifier's verdict: the independent verifier
            accepted this investigation's facts and citations. A human should check each flag before deciding.
          </p>
          <div className="flex flex-wrap gap-1.5 pt-1">
            {verdict.modelRaisedFlags.map((flag, idx) => (
              <span
                key={idx}
                className="font-mono text-[10px] font-bold px-2 py-0.5 rounded bg-black border border-dashed border-neutral-300 text-white"
              >
                ⚠ {flag}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Executive Summary */}
      <div className="fintech-card space-y-2">
        <h3 className="text-xs font-bold uppercase text-neutral-400 tracking-wider flex items-center gap-2">
          <FileText size={14} className="text-neutral-200" />
          Executive Summary
        </h3>
        <p className="text-xs text-neutral-200 leading-relaxed font-sans bg-neutral-900/60 p-3.5 rounded-lg border border-neutral-800/80">
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
          <p className="text-xs text-neutral-400 italic p-3">{factSection.emptyText}</p>
        ) : (
          <div className="space-y-2.5">
            {verifiedFacts.map((fact, idx) => (
              <div
                key={idx}
                className={`p-3 rounded-lg border space-y-1.5 ${factSection.rowClass}`}
              >
                <div className="flex items-start justify-between gap-3 text-xs">
                  <p className="text-neutral-100 font-medium leading-relaxed">{fact.statement}</p>
                  {fact.amount_cents !== undefined && fact.amount_cents !== null && (
                    <MoneyDisplay
                      amountMinor={fact.amount_cents}
                      currency={fact.currency || 'USD'}
                      className={`font-semibold shrink-0 ${factSection.headingClass}`}
                    />
                  )}
                </div>
                {/* Cited Evidence IDs */}
                <div className="flex items-center gap-1.5 pt-1 text-[11px] text-neutral-400">
                  <Database size={11} className="text-neutral-200" />
                  <span className={`text-[10px] uppercase font-bold ${factSection.badgeClass}`}>Cited Evidence:</span>
                  <div className="flex flex-wrap gap-1">
                    {fact.evidence_ids.map((evId, evIdx) => (
                      <span
                        key={evIdx}
                        className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-200"
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
      <div className="fintech-card space-y-3 border-neutral-700/60 bg-[#151515]">
        <div className="flex items-center justify-between pb-2 border-b border-neutral-700/40">
          <h3 className="text-xs font-bold uppercase text-neutral-200 tracking-wider flex items-center gap-2">
            <Lightbulb size={14} className="text-neutral-200" />
            Hypotheses ({hypotheses.length})
          </h3>
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-neutral-900 text-neutral-200 border border-neutral-700/50 uppercase font-bold">
            Unconfirmed / Plausible
          </span>
        </div>

        {hypotheses.length === 0 ? (
          <p className="text-xs text-neutral-400 italic p-3">No active hypotheses formulated.</p>
        ) : (
          <div className="space-y-2.5">
            {hypotheses.map((hyp) => (
              <div
                key={hyp.hypothesis_id}
                className="p-3 rounded-lg bg-neutral-900/20 border border-neutral-400/20 space-y-1.5"
              >
                <div className="flex items-start justify-between gap-3 text-xs">
                  <p className="text-neutral-200 leading-relaxed">{hyp.statement}</p>
                  <span
                    className={`text-[10px] font-mono uppercase font-bold px-2 py-0.5 rounded shrink-0 ${
                      hyp.likelihood === 'HIGH'
                        ? 'bg-neutral-900 text-neutral-200 border border-neutral-700'
                        : hyp.likelihood === 'MEDIUM'
                        ? 'bg-neutral-900 text-neutral-200 border border-neutral-700'
                        : 'bg-neutral-900 text-neutral-400 border border-neutral-700'
                    }`}
                  >
                    Likelihood: {hyp.likelihood}
                  </span>
                </div>
                {hyp.supporting_evidence_ids.length > 0 && (
                  <div className="flex items-center gap-1.5 pt-1 text-[11px] text-neutral-400">
                    <span className="text-[10px] uppercase font-bold text-neutral-200">Supporting Evidence:</span>
                    <div className="flex flex-wrap gap-1">
                      {hyp.supporting_evidence_ids.map((id, i) => (
                        <span key={i} className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-300">
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
          <h3 className="text-xs font-bold uppercase text-neutral-400 tracking-wider">
            Candidate Invoices Evaluated ({candidates.length})
          </h3>
          <div className="space-y-2">
            {candidates.map((cand, idx) => (
              <div key={idx} className="p-3.5 rounded-lg bg-neutral-900/80 border border-neutral-800 space-y-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-mono font-bold text-neutral-200">{cand.invoice_number}</span>
                  {cand.score !== undefined && cand.score !== null && (
                    <span className="font-mono text-xs font-semibold px-2 py-0.5 rounded bg-neutral-900/60 text-neutral-200 border border-neutral-700/40">
                      Match Score: {cand.score}/100
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-4 text-[11px] text-neutral-400">
                  <span>Customer Match: {cand.customer_match ? '✓ Yes' : '✕ No'}</span>
                  <span>Amount Match: {cand.amount_match ? '✓ Exact' : '✕ No'}</span>
                </div>
                <p className="text-neutral-300 text-xs bg-black p-2.5 rounded border border-neutral-900">
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
          <h3 className="text-xs font-bold uppercase text-neutral-400 tracking-wider flex items-center gap-1.5">
            <HelpCircle size={14} className="text-neutral-200" />
            Missing Information ({missingEvidence.length})
          </h3>
          {missingEvidence.length === 0 ? (
            <p className="text-xs text-neutral-400 italic p-3">Zero missing items identified.</p>
          ) : (
            <div className="space-y-2">
              {missingEvidence.map((item, idx) => (
                <div key={idx} className="p-2.5 rounded bg-neutral-900 border border-neutral-800 text-xs space-y-1">
                  <div className="flex justify-between items-start">
                    <span className="font-medium text-neutral-200">{item.missing_item}</span>
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-neutral-900/80 text-neutral-200 border border-neutral-700/40 uppercase">
                      {item.importance}
                    </span>
                  </div>
                  <p className="text-[11px] text-neutral-400">{item.why_needed}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Recommended Operator Steps */}
        <div className="fintech-card space-y-2.5">
          <h3 className="text-xs font-bold uppercase text-neutral-400 tracking-wider flex items-center gap-1.5">
            <CheckCircle2 size={14} className="text-neutral-200" />
            Recommended Investigation Steps ({recommendedSteps.length})
          </h3>
          {recommendedSteps.length === 0 ? (
            <p className="text-xs text-neutral-400 italic p-3">No further steps required.</p>
          ) : (
            <div className="space-y-2">
              {recommendedSteps.map((step) => (
                <div key={step.step_number} className="p-2.5 rounded bg-neutral-900 border border-neutral-800 text-xs flex gap-2.5 items-start">
                  <span className="w-5 h-5 rounded-full bg-neutral-900/60 text-neutral-200 border border-neutral-500/50 flex items-center justify-center font-mono text-[10px] font-bold shrink-0 mt-0.5">
                    {step.step_number}
                  </span>
                  <div>
                    <span className="font-semibold text-neutral-200 block">{step.action}</span>
                    <span className="text-[11px] text-neutral-400">Target: {step.target} • {step.reason}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* SECTION 5: SUGGESTED RESOLUTION & HUMAN DECISION ACTION BAR */}
      <div className="p-6 rounded-lg bg-gradient-to-r from-neutral-900 to-[#191919] border-2 border-neutral-400/40 space-y-4 shadow-xl">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 pb-3 border-b border-neutral-800">
          <div>
            <span className="text-[10px] uppercase font-bold text-neutral-200 tracking-wider">
              AI Suggested Resolution
            </span>
            <h3 className="text-base font-bold text-white mt-0.5">
              {suggestedResolution ? suggestedResolution.resolution_type : 'No automatic resolution suggested'}
            </h3>
          </div>
          {suggestedResolution && suggestedResolution.suggested_amount_cents && (
            <div className="text-right">
              <span className="text-[10px] text-neutral-400 block">Proposed Allocation Amount</span>
              <MoneyDisplay amountMinor={suggestedResolution.suggested_amount_cents} className="text-lg font-bold text-neutral-200" />
            </div>
          )}
        </div>

        {suggestedResolution?.notes && (
          <p className="text-xs text-neutral-300 leading-relaxed bg-black/70 p-3 rounded border border-neutral-800">
            {suggestedResolution.notes}
          </p>
        )}

        {/* Action Buttons */}
        <div className="flex flex-col sm:flex-row items-center justify-between gap-3 pt-2">
          <span
            data-testid="approval-state"
            className={`glyph-mono text-xs font-mono ${
              isInvalid
                ? 'uppercase font-extrabold bg-white text-black border-2 border-white px-2 py-1 rounded'
                : approvalBlocked
                ? 'font-semibold text-white border-[3px] border-double border-neutral-300 px-2 py-1 rounded'
                : 'text-neutral-400'
            }`}
          >
            {isInvalid
              ? '⛔ Approval strictly blocked by safety verifier'
              : approvalBlocked
              ? '⏸ Approval held: model-raised flags need a human check'
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
              disabled={approvalBlocked || isResolved}
              title={verdict.approvalBlockedReason ?? 'Approve and execute resolution'}
              className="btn btn-success text-xs font-semibold px-5 flex items-center gap-2"
            >
              {approvalBlocked && <Lock size={12} />}
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
          <div className="p-3 rounded-lg bg-neutral-900/30 border border-neutral-400/30 text-xs text-neutral-200 leading-relaxed">
            Approving this investigation links to the verified deterministic proposal and executes the 10-step atomic allocation via <strong>ApprovalService</strong>.
          </div>

          {/* Allocation Distribution Table */}
          <div className="p-3.5 rounded-xl bg-neutral-900 border border-neutral-800 space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wider text-neutral-300 flex items-center justify-between">
              <span>Proposed Allocation Distribution</span>
              <span className="font-mono text-neutral-200">
                Target: {inv.target_type}:{inv.target_id}
              </span>
            </div>

            {allocDistribution.length > 0 ? (
              <table className="w-full text-xs font-mono text-left">
                <thead>
                  <tr className="border-b border-neutral-800 text-neutral-400">
                    <th className="py-1.5 font-medium">Target Invoice</th>
                    <th className="py-1.5 font-medium">Allocation Amount</th>
                    <th className="py-1.5 font-medium text-right">Currency</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-800/60">
                  {allocDistribution.map((a, idx) => (
                    <tr key={idx} className="text-neutral-200">
                      <td className="py-2">{a.invoiceId}</td>
                      <td className="py-2 text-neutral-200">
                        <MoneyDisplay amountMinor={a.amountMinor} currency={a.currency} />
                      </td>
                      <td className="py-2 text-right text-neutral-400">{a.currency}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="text-xs text-neutral-200">
                Warning: No structured allocations detected in the recommendation.
              </div>
            )}
          </div>

          <div>
            <label className="block text-xs font-medium text-neutral-300 mb-1 flex items-center justify-between">
              <span>
                Approving Operator Identity <span className="text-neutral-200">*</span>
              </span>
              <span className="text-[10px] text-neutral-400 font-normal">
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
            <p className="text-[11px] text-neutral-400 mt-1">
              Local Operator Notice: Operator identity is recorded in the SHA-256 audit log for local accountability and does not represent authenticated multi-user RBAC.
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-neutral-300 mb-1">
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

          <div className="flex justify-end gap-3 pt-4 border-t border-neutral-800">
            <button type="button" onClick={() => setIsApproveOpen(false)} disabled={isSubmitting} className="btn btn-secondary text-xs">
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || !reviewer.trim() || approvalBlocked}
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
          <div className="p-3.5 rounded-lg bg-neutral-900/30 border border-neutral-400/30 text-xs text-neutral-200 leading-relaxed">
            Rejecting this recommendation records an immutable audit event and closes the investigation without financial mutation.
          </div>

          <div>
            <label className="block text-xs font-medium text-neutral-300 mb-1">
              Operator Name / Email <span className="text-neutral-200">*</span>
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
              Rejection Reason & Next Steps <span className="text-neutral-200">*</span>
            </label>
            <textarea
              required
              rows={3}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="input-text text-xs resize-none"
            />
          </div>

          <div className="flex justify-end gap-3 pt-4 border-t border-neutral-800">
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
