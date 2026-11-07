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
      <div className="p-6 rounded-lg bg-red-950/20 border border-red-500/30 text-red-300">
        <h3 className="font-semibold text-sm">Failed to load overview</h3>
        <p className="text-xs mt-1 text-red-400">{error}</p>
        <button onClick={fetchSummary} className="btn btn-secondary text-xs mt-3">
          Retry
        </button>
      </div>
    );
  }

  const { counts, financial_totals, recent_runs, recent_exceptions, recent_investigations } = summary;
  const reconciliationRate =
    counts.total_payments > 0
      ? Math.round((counts.reconciled_payments / counts.total_payments) * 100)
      : 100;

  return (
    <div className="space-y-8 animate-fade-in">
      {/* Top Welcome & Actions */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white">Financial Reconciliation Health</h2>
          <p className="text-xs text-slate-400 mt-1">
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
            <span className="text-xs font-medium text-slate-400">Reconciliation Rate</span>
            <div className="p-1.5 rounded-md bg-emerald-500/10 text-emerald-400">
              <CheckCircle2 size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-white">{reconciliationRate}%</span>
            <span className="text-xs text-slate-400 font-medium">
              ({counts.reconciled_payments} of {counts.total_payments})
            </span>
          </div>
          <div className="mt-3 w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-emerald-500 h-1.5 rounded-full transition-all duration-500"
              style={{ width: `${reconciliationRate}%` }}
            />
          </div>
        </div>

        {/* Open Exceptions */}
        <div
          onClick={() => onNavigate('transactions')}
          className="fintech-card cursor-pointer hover:border-amber-500/40"
        >
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-slate-400">Open Exceptions</span>
            <div className="p-1.5 rounded-md bg-amber-500/10 text-amber-400">
              <AlertTriangle size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-amber-300">{counts.open_exceptions}</span>
            <span className="text-xs text-slate-400 font-medium">unresolved payments</span>
          </div>
          <p className="text-[11px] text-slate-400 mt-3 flex items-center gap-1">
            <span>Requires matching or investigation</span>
            <ArrowRight size={11} className="text-slate-400" />
          </p>
        </div>

        {/* Pending Proposals Queue */}
        <div
          onClick={() => onNavigate('proposals')}
          className="fintech-card cursor-pointer hover:border-blue-500/40"
        >
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-slate-400">Review Queue</span>
            <div className="p-1.5 rounded-md bg-blue-500/10 text-blue-400">
              <Clock size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-blue-300">{counts.pending_proposals}</span>
            <span className="text-xs text-slate-400 font-medium">proposals pending approval</span>
          </div>
          <p className="text-[11px] text-slate-400 mt-3 flex items-center gap-1">
            <span>Human-in-the-loop decisions</span>
            <ArrowRight size={11} className="text-slate-400" />
          </p>
        </div>

        {/* AI Investigations */}
        <div
          onClick={() => onNavigate('investigations')}
          className="fintech-card cursor-pointer hover:border-violet-500/40 bg-gradient-to-b from-[#131d33] to-[#151c38]"
        >
          <div className="flex justify-between items-start">
            <span className="text-xs font-medium text-slate-300">AI Investigations</span>
            <div className="p-1.5 rounded-md bg-violet-500/20 text-violet-300 border border-violet-500/30">
              <Sparkles size={16} />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-violet-300">
              {counts.investigations_needs_review}
            </span>
            <span className="text-xs text-slate-400 font-medium">ready for controller review</span>
          </div>
          <p className="text-[11px] text-violet-300/80 mt-3 flex items-center gap-1">
            <span>Grounded in verified DB facts</span>
            <ArrowRight size={11} className="text-violet-400" />
          </p>
        </div>
      </div>

      {/* Currency Partitioned Balances */}
      <div className="fintech-card">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h3 className="text-sm font-semibold text-white">Currency Ledger Balances</h3>
            <p className="text-xs text-slate-400 mt-0.5">
              Partitioned strict accounting: amounts in integer minor units without cross-currency conflation.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {Object.entries(financial_totals).map(([curr, bucket]) => (
            <div key={curr} className="p-4 rounded-lg bg-slate-900/80 border border-slate-800 space-y-3">
              <div className="flex items-center justify-between pb-2 border-b border-slate-800">
                <span className="font-mono font-bold text-sm text-blue-400 px-2 py-0.5 rounded bg-blue-950/60 border border-blue-800/40">
                  {curr}
                </span>
                <span className="text-xs text-slate-400">Total Payments: {counts.total_payments}</span>
              </div>
              <div className="space-y-1.5 text-xs">
                <div className="flex justify-between text-slate-300">
                  <span className="text-slate-400">Payments Ingested:</span>
                  <MoneyDisplay amountMinor={bucket.payments_amount_minor} currency={curr} />
                </div>
                <div className="flex justify-between text-slate-300">
                  <span className="text-slate-400">Reconciled to Invoices:</span>
                  <MoneyDisplay amountMinor={bucket.reconciled_amount_minor} currency={curr} className="text-emerald-400" />
                </div>
                <div className="flex justify-between text-slate-300 pt-1.5 border-t border-slate-800">
                  <span className="text-amber-400 font-medium">Unapplied Cash:</span>
                  <MoneyDisplay amountMinor={bucket.unapplied_cash_minor} currency={curr} className="text-amber-400" />
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Two Column Layout: Recent Exceptions & Investigations */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Unmatched Exceptions */}
        <div className="fintech-card flex flex-col">
          <div className="flex items-center justify-between pb-3 border-b border-slate-800 mb-3">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <AlertTriangle size={15} className="text-amber-400" />
                Open Payment Exceptions
              </h3>
              <p className="text-[11px] text-slate-400">Unmatched or partially allocated payments</p>
            </div>
            <button
              onClick={() => onNavigate('transactions')}
              className="text-xs text-blue-400 hover:text-blue-300 flex items-center gap-1"
            >
              View All ({counts.open_exceptions})
              <ArrowRight size={12} />
            </button>
          </div>

          <div className="flex-1 overflow-x-auto">
            {recent_exceptions.length === 0 ? (
              <p className="text-xs text-slate-400 py-6 text-center">Zero open payment exceptions. All cash reconciled.</p>
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
                      <td className="font-mono text-xs text-blue-400">{ex.provider_payment_id}</td>
                      <td className="text-xs text-slate-300">{ex.payer_name || 'Anonymous'}</td>
                      <td>
                        <MoneyDisplay amountMinor={ex.unapplied_cash_minor} currency={ex.currency} className="text-amber-400" />
                      </td>
                      <td>
                        <button
                          onClick={() => onStartInvestigation(ex.provider_payment_id)}
                          className="btn btn-secondary py-1 px-2.5 text-[11px] text-violet-300 border-violet-800/40 hover:bg-violet-950/40 flex items-center gap-1.5"
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
          <div className="flex items-center justify-between pb-3 border-b border-slate-800 mb-3">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <Sparkles size={15} className="text-violet-400" />
                Recent AI Exception Cases
              </h3>
              <p className="text-[11px] text-slate-400">Grounded investigations & safety verdicts</p>
            </div>
            <button
              onClick={() => onNavigate('investigations')}
              className="text-xs text-blue-400 hover:text-blue-300 flex items-center gap-1"
            >
              View Cases ({counts.total_investigations})
              <ArrowRight size={12} />
            </button>
          </div>

          <div className="flex-1 overflow-x-auto">
            {recent_investigations.length === 0 ? (
              <p className="text-xs text-slate-400 py-6 text-center">No AI investigations recorded yet.</p>
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
                      <td className="font-mono text-xs text-slate-200">{inv.target_id}</td>
                      <td className="text-xs text-slate-300 font-mono text-[11px]">{inv.classification}</td>
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
