import React, { useEffect, useState } from 'react';
import {
  DollarSign,
  CheckCircle2,
  AlertTriangle,
  FileText,
  Clock,
  Sparkles,
  ArrowRight,
  RefreshCw,
  TrendingUp,
} from 'lucide-react';
import { api } from '../api/client';
import { DashboardSummary } from '../types/api';
import { MoneyDisplay, StatusBadge, VerifierStatusBadge, LoadingSpinner } from '../components/Common';
import { NavigationTab } from '../components/Layout';
import { formatReconciliationRate } from '../utils/formatters';

export function OverviewPage({
  tenantId,
  onNavigate,
  onStartInvestigation,
}: {
  tenantId: string;
  onNavigate: (tab: NavigationTab) => void;
  onStartInvestigation: (targetId: string) => void;
}) {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSummary = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.getDashboardSummary(tenantId);
      setSummary(data);
    } catch (err: any) {
      setError(err.message || 'Failed to load dashboard summary');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchSummary();
  }, [tenantId]);

  if (isLoading && !summary) {
    return <LoadingSpinner text="Aggregating Business Reconciliation Health..." />;
  }

  if (error || !summary) {
    return (
      <div className="p-6 rounded-lg bg-neutral-900/20 border border-neutral-400/30 text-neutral-200">
        <h3 className="font-semibold text-sm">Failed to load overview</h3>
        <p className="text-xs mt-1 text-neutral-200">{error}</p>
        <button onClick={fetchSummary} className="btn btn-secondary text-xs mt-3">
          Retry
        </button>
      </div>
    );
  }

  const { counts, financial_totals, recent_runs, recent_exceptions, recent_investigations } = summary;
  const rateInfo = formatReconciliationRate(counts.reconciled_payments, counts.total_payments);

  return (
    <div className="space-y-8 animate-fade-in min-w-0">
      {/* Top Welcome & Actions */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white">Financial Reconciliation Health</h2>
          <p className="text-xs text-neutral-400 mt-1">
            Real-time ledger overview, unapplied cash monitoring, and AI-investigated exceptions.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={fetchSummary}
            disabled={isLoading}
            className="btn btn-secondary text-xs flex items-center gap-1.5"
          >
            <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
            <span>Refresh</span>
          </button>
          <button
            onClick={() => onNavigate('reconciliation')}
            className="btn btn-primary text-xs flex items-center gap-1.5"
          >
            <span>Run Reconciliation</span>
            <ArrowRight size={13} />
          </button>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Reconciled Rate */}
        <div className="fintech-card relative overflow-hidden">
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-neutral-400">Reconciliation Rate</span>
            <div className={`p-1.5 rounded-md ${rateInfo.rate !== null ? 'bg-neutral-700/10 text-neutral-200' : 'bg-neutral-800 text-neutral-400'}`}>
              <CheckCircle2 size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-white">{rateInfo.displayText}</span>
            <span className="text-xs text-neutral-400 font-medium">
              ({counts.reconciled_payments} of {counts.total_payments})
            </span>
          </div>
          <div className="mt-3 w-full bg-black border border-neutral-600 rounded-full h-2 overflow-hidden" aria-hidden="true">
            <div
              className={`h-full rounded-full transition-all duration-500 ${
                rateInfo.rate !== null ? 'bg-white' : 'bg-transparent'
              }`}
              style={{ width: `${rateInfo.progressPercent}%` }}
            />
          </div>
        </div>

        {/* Open Exceptions */}
        <div
          onClick={() => onNavigate('transactions')}
          className="fintech-card cursor-pointer hover:border-neutral-400/40"
        >
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-neutral-400">Open Exceptions</span>
            <div className="p-1.5 rounded-md bg-neutral-700/10 text-neutral-200">
              <AlertTriangle size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-neutral-200">{counts.open_exceptions}</span>
            <span className="text-xs text-neutral-400 font-medium">unresolved payments</span>
          </div>
          <p className="text-[11px] text-neutral-400 mt-3 flex items-center gap-1">
            <span>Requires matching or investigation</span>
            <ArrowRight size={11} className="text-neutral-400" />
          </p>
        </div>

        {/* Pending Proposals Queue */}
        <div
          onClick={() => onNavigate('proposals')}
          className="fintech-card cursor-pointer hover:border-neutral-400/40"
        >
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-neutral-400">Review Queue</span>
            <div className="p-1.5 rounded-md bg-neutral-700/10 text-neutral-200">
              <Clock size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-neutral-200">
              {counts.actionable_proposals !== undefined ? counts.actionable_proposals : counts.pending_proposals}
            </span>
            <span className="text-xs text-neutral-400 font-medium">
              actionable ({counts.unresolved_proposals ?? counts.pending_proposals} unresolved)
            </span>
          </div>
          <p className="text-[11px] text-neutral-400 mt-3 flex items-center gap-1">
            <span>Human-in-the-loop decisions</span>
            <ArrowRight size={11} className="text-neutral-400" />
          </p>
        </div>

        {/* AI Investigations */}
        <div
          onClick={() => onNavigate('investigations')}
          className="fintech-card cursor-pointer hover:border-neutral-400/40 bg-gradient-to-b from-[#1c1c1c] to-[#1d1d1d]"
        >
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-neutral-300">AI Investigations</span>
            <div className="p-1.5 rounded-md bg-neutral-700/20 text-neutral-200 border border-neutral-400/30">
              <Sparkles size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-neutral-200">
              {counts.investigations_needs_review}
            </span>
            <span className="text-xs text-neutral-400 font-medium">ready for controller review</span>
          </div>
          <p className="text-[11px] text-neutral-200/80 mt-3 flex items-center gap-1">
            <span>Grounded in verified DB facts</span>
            <ArrowRight size={11} className="text-neutral-200" />
          </p>
        </div>
      </div>

      {/* Currency Partitioned Balances */}
      <div className="fintech-card">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h3 className="text-sm font-semibold text-white">Currency Ledger Balances</h3>
            <p className="text-xs text-neutral-400 mt-0.5">
              Partitioned strict accounting: amounts in integer minor units without cross-currency conflation.
            </p>
          </div>
        </div>

        {Object.keys(financial_totals).length === 0 ? (
          <div className="py-8 px-4 text-center rounded-lg border border-dashed border-neutral-800 bg-neutral-900/30">
            <p className="text-xs text-neutral-400">No currency balances available yet.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {Object.entries(financial_totals).map(([curr, bucket]) => (
              <div key={curr} className="p-4 rounded-lg bg-neutral-900/80 border border-neutral-800 space-y-3">
                <div className="flex items-center justify-between pb-2 border-b border-neutral-800">
                  <span className="font-mono font-bold text-sm text-neutral-200 px-2 py-0.5 rounded bg-neutral-900/60 border border-neutral-700/40">
                    {curr}
                  </span>
                  <div className="flex items-center gap-3 text-xs text-neutral-400">
                    <span>Payments: <strong className="text-neutral-200 font-mono">{bucket.payments_count ?? 0}</strong></span>
                    <span>Invoices: <strong className="text-neutral-200 font-mono">{bucket.invoices_count ?? 0}</strong></span>
                  </div>
                </div>
                <div className="space-y-1.5 text-xs">
                  <div className="flex justify-between text-neutral-300">
                    <span className="text-neutral-400">Payments Ingested:</span>
                    <MoneyDisplay amountMinor={bucket.payments_amount_minor} currency={curr} />
                  </div>
                  <div className="flex justify-between text-neutral-300">
                    <span className="text-neutral-400">Reconciled to Invoices:</span>
                    <MoneyDisplay amountMinor={bucket.reconciled_amount_minor} currency={curr} className="text-neutral-200" />
                  </div>
                  <div className="flex justify-between text-neutral-300">
                    <span className="text-neutral-400">Invoice Outstanding:</span>
                    <MoneyDisplay amountMinor={bucket.invoices_outstanding_minor ?? 0} currency={curr} className="text-neutral-300" />
                  </div>
                  <div className="flex justify-between text-neutral-300 pt-1.5 border-t border-neutral-800">
                    <span className="text-neutral-200 font-medium">Unapplied Cash:</span>
                    <MoneyDisplay amountMinor={bucket.unapplied_amount_minor ?? bucket.unapplied_cash_minor} currency={curr} className="text-neutral-200" />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Two Column Layout: Recent Exceptions & Investigations */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Unmatched Exceptions */}
        <div className="fintech-card flex flex-col">
          <div className="flex items-center justify-between pb-3 border-b border-neutral-800 mb-3">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <AlertTriangle size={15} className="text-neutral-200" />
                Open Payment Exceptions
              </h3>
              <p className="text-[11px] text-neutral-400">Unmatched or partially allocated payments</p>
            </div>
            <button
              onClick={() => onNavigate('transactions')}
              className="text-xs text-neutral-200 hover:text-neutral-200 flex items-center gap-1"
            >
              View All ({counts.open_exceptions})
              <ArrowRight size={12} />
            </button>
          </div>

          <div className="flex-1 overflow-x-auto">
            {recent_exceptions.length === 0 ? (
              <p className="text-xs text-neutral-400 py-6 text-center">Zero open payment exceptions. All cash reconciled.</p>
            ) : (
              <table className="fintech-table">
                <thead>
                  <tr>
                    <th>Payment ID</th>
                    <th>Payer</th>
                    <th>Unapplied</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {recent_exceptions.map((ex) => (
                    <tr key={ex.id}>
                      <td className="font-mono text-xs text-neutral-200">{ex.provider_payment_id}</td>
                      <td className="text-xs text-neutral-300">{ex.payer_name || 'Anonymous'}</td>
                      <td>
                        <MoneyDisplay amountMinor={ex.unapplied_cash_minor} currency={ex.currency} className="text-neutral-200" />
                      </td>
                      <td>
                        <button
                          onClick={() => onStartInvestigation(ex.provider_payment_id)}
                          className="btn btn-secondary py-1 px-2.5 text-[11px] text-neutral-200 border-neutral-700/40 hover:bg-neutral-900/40 flex items-center gap-1.5"
                        >
                          <Sparkles size={11} />
                          Investigate
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* Recent AI Investigations */}
        <div className="fintech-card flex flex-col">
          <div className="flex items-center justify-between pb-3 border-b border-neutral-800 mb-3">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <Sparkles size={15} className="text-neutral-200" />
                Recent AI Exception Cases
              </h3>
              <p className="text-[11px] text-neutral-400">Grounded investigations & safety verdicts</p>
            </div>
            <button
              onClick={() => onNavigate('investigations')}
              className="text-xs text-neutral-200 hover:text-neutral-200 flex items-center gap-1"
            >
              View Cases ({counts.total_investigations})
              <ArrowRight size={12} />
            </button>
          </div>

          <div className="flex-1 overflow-x-auto">
            {recent_investigations.length === 0 ? (
              <p className="text-xs text-neutral-400 py-6 text-center">No AI investigations recorded yet.</p>
            ) : (
              <table className="fintech-table">
                <thead>
                  <tr>
                    <th>Target</th>
                    <th>Classification</th>
                    <th>Verifier</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {recent_investigations.map((inv) => (
                    <tr
                      key={inv.id}
                      onClick={() => onNavigate('investigations')}
                      className="cursor-pointer"
                    >
                      <td className="font-mono text-xs text-neutral-200">{inv.target_id}</td>
                      <td className="text-xs text-neutral-300 font-mono text-[11px]">{inv.classification}</td>
                      <td>
                        <VerifierStatusBadge status={inv.verifier_status} />
                      </td>
                      <td>
                        <StatusBadge status={inv.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
