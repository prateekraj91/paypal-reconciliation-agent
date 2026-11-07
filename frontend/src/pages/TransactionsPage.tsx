import React, { useEffect, useState } from 'react';
import {
  Search,
  Filter,
  ArrowUpDown,
  RefreshCw,
  Sparkles,
  ExternalLink,
  ShieldCheck,
  X,
  FileText,
  Clock,
  ArrowRight,
} from 'lucide-react';
import { api } from '../api/client';
import { TransactionItem, TransactionDetail } from '../types/api';
import { MoneyDisplay, StatusBadge, VerifierStatusBadge, HashBadge, LoadingSpinner, EmptyState } from '../components/Common';

export function TransactionsPage({
  tenantId,
  onStartInvestigation,
  onInspectInvestigation,
}: {
  tenantId: string;
  onStartInvestigation: (paymentId: string) => void;
  onInspectInvestigation: (invId: string) => void;
}) {
  const [transactions, setTransactions] = useState<TransactionItem[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('ALL');
  const [currencyFilter, setCurrencyFilter] = useState('ALL');

  // Drawer / Detail state
  const [selectedTxId, setSelectedTxId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TransactionDetail | null>(null);
  const [isDetailLoading, setIsDetailLoading] = useState(false);

  const fetchTransactions = async () => {
    setIsLoading(true);
    try {
      const data = await api.listTransactions({
        tenantId,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        currency: currencyFilter === 'ALL' ? undefined : currencyFilter,
        search: search.trim() || undefined,
        limit: 100,
      });
      setTransactions(data.transactions);
      setTotalCount(data.total_count);
    } catch (err) {
      console.error('Failed to load transactions', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchTransactions();
  }, [tenantId, statusFilter, currencyFilter]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchTransactions();
  };

  const openDetail = async (txId: string) => {
    setSelectedTxId(txId);
    setIsDetailLoading(true);
    setDetail(null);
    try {
      const tx = await api.getTransaction(txId, tenantId);
      setDetail(tx);
    } catch (err) {
      console.error('Failed to load tx detail', err);
    } finally {
      setIsDetailLoading(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in relative">
      {/* Header & Filter Controls */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white">Financial Transactions</h2>
          <p className="text-xs text-slate-400 mt-1">
            Authoritative cash receipts, ledger allocations, and unapplied cash tracking.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchTransactions}
            disabled={isLoading}
            className="btn btn-secondary text-xs flex items-center gap-1.5"
          >
            <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Search and Filters Bar */}
      <div className="p-4 rounded-lg bg-[#0e1526] border border-slate-800 flex flex-wrap items-center gap-3">
        <form onSubmit={handleSearchSubmit} className="flex-1 min-w-[220px] relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by Payment ID, Payer, Email, or Reference..."
            className="input-text text-xs pl-8 bg-slate-900 border-slate-700"
          />
        </form>

        <div className="flex items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="select-input text-xs w-auto py-2 bg-slate-900 border-slate-700"
          >
            <option value="ALL">All Reconciliation Statuses</option>
            <option value="RECONCILED">Reconciled (Zero Unapplied)</option>
            <option value="UNMATCHED">Unmatched (100% Unapplied)</option>
            <option value="PARTIALLY_RECONCILED">Partially Reconciled</option>
          </select>

          <select
            value={currencyFilter}
            onChange={(e) => setCurrencyFilter(e.target.value)}
            className="select-input text-xs w-auto py-2 bg-slate-900 border-slate-700"
          >
            <option value="ALL">All Currencies</option>
            <option value="USD">USD ($)</option>
            <option value="EUR">EUR (€)</option>
            <option value="GBP">GBP (£)</option>
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="fintech-card p-0 overflow-hidden">
        {isLoading ? (
          <LoadingSpinner text="Querying Transaction Ledger..." />
        ) : transactions.length === 0 ? (
          <div className="p-8">
            <EmptyState
              title="No transactions found"
              description="No financial transactions match the selected filters for this tenant."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="fintech-table">
              <thead>
                <tr>
                  <th>Payment ID</th>
                  <th>Payer</th>
                  <th>Received Date</th>
                  <th>Total Amount</th>
                  <th>Unapplied Cash</th>
                  <th>Reconciliation State</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {transactions.map((tx) => (
                  <tr
                    key={tx.id}
                    onClick={() => openDetail(tx.provider_payment_id)}
                    className="cursor-pointer"
                  >
                    <td>
                      <span className="font-mono text-xs text-blue-400 font-medium">
                        {tx.provider_payment_id}
                      </span>
                    </td>
                    <td>
                      <div className="text-xs font-medium text-slate-200">
                        {tx.payer_name || 'Anonymous Remitter'}
                      </div>
                      {tx.payer_email && (
                        <div className="text-[11px] text-slate-500 font-mono">{tx.payer_email}</div>
                      )}
                    </td>
                    <td className="text-xs text-slate-400 font-mono">
                      {tx.received_at ? new Date(tx.received_at).toLocaleDateString() : '-'}
                    </td>
                    <td>
                      <MoneyDisplay amountMinor={tx.amount_minor} currency={tx.currency} />
                    </td>
                    <td>
                      {tx.unapplied_cash_minor > 0 ? (
                        <MoneyDisplay
                          amountMinor={tx.unapplied_cash_minor}
                          currency={tx.currency}
                          className="text-amber-400 font-semibold"
                        />
                      ) : (
                        <span className="text-slate-500 text-xs font-mono">$0.00</span>
                      )}
                    </td>
                    <td>
                      <StatusBadge status={tx.reconciliation_status} />
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {tx.unapplied_cash_minor > 0 && (
                        <button
                          onClick={() => onStartInvestigation(tx.provider_payment_id)}
                          className="btn btn-secondary py-1 px-2.5 text-[11px] text-violet-300 border-violet-800/40 hover:bg-violet-950/40 flex items-center gap-1.5"
                        >
                          <Sparkles size={11} />
                          Investigate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Transaction Slide-Over Detail Drawer */}
      {selectedTxId && (
        <div
          className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex justify-end animate-fade-in"
          onClick={() => setSelectedTxId(null)}
        >
          <div
            className="w-full max-w-2xl bg-[#0c1222] border-l border-slate-800 h-full overflow-y-auto p-6 space-y-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Drawer Header */}
            <div className="flex items-start justify-between pb-4 border-b border-slate-800">
              <div>
                <span className="text-[10px] uppercase font-bold text-slate-500 tracking-wider">
                  Transaction Detail
                </span>
                <h3 className="text-lg font-bold font-mono text-white mt-0.5">
                  {selectedTxId}
                </h3>
              </div>
              <button
                onClick={() => setSelectedTxId(null)}
                className="p-1 rounded text-slate-400 hover:text-white hover:bg-slate-800"
              >
                <X size={18} />
              </button>
            </div>

            {isDetailLoading || !detail ? (
              <LoadingSpinner text="Retrieving complete ledger history..." />
            ) : (
              <div className="space-y-6">
                {/* Status & Unapplied Cash Banner */}
                <div className="grid grid-cols-2 gap-3 p-4 rounded-lg bg-slate-900 border border-slate-800">
                  <div>
                    <span className="text-[11px] text-slate-400 block mb-1">Reconciliation Status</span>
                    <StatusBadge status={detail.reconciliation_status} />
                  </div>
                  <div>
                    <span className="text-[11px] text-slate-400 block mb-1">Unapplied Cash</span>
                    <MoneyDisplay
                      amountMinor={detail.unapplied_cash_minor}
                      currency={detail.currency}
                      className={detail.unapplied_cash_minor > 0 ? 'text-amber-400 text-sm' : 'text-slate-400 text-sm'}
                    />
                  </div>
                </div>

                {/* Core Attributes */}
                <div className="space-y-2 text-xs">
                  <h4 className="font-semibold text-slate-300 uppercase tracking-wider text-[11px]">
                    Payment Details
                  </h4>
                  <div className="p-3.5 rounded-lg bg-slate-900/60 border border-slate-800/80 space-y-2">
                    <div className="flex justify-between">
                      <span className="text-slate-500">Payer Name:</span>
                      <span className="text-slate-200 font-medium">{detail.payer_name || '-'}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-500">Payer Email:</span>
                      <span className="text-slate-200 font-mono">{detail.payer_email || '-'}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-500">Reference:</span>
                      <span className="text-slate-200 font-mono">{detail.reference || '-'}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-500">Received At:</span>
                      <span className="text-slate-200 font-mono">{detail.received_at || '-'}</span>
                    </div>
                    {detail.note && (
                      <div className="pt-2 border-t border-slate-800 text-slate-400">
                        <span className="text-[10px] uppercase font-bold text-slate-500 block mb-1">Memo / Note:</span>
                        <p className="p-2 rounded bg-slate-950 font-mono text-[11px] text-slate-300 break-words">
                          {detail.note}
                        </p>
                      </div>
                    )}
                  </div>
                </div>

                {/* Approved Allocations */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <h4 className="font-semibold text-slate-300 uppercase tracking-wider text-[11px]">
                      Approved Application Allocations ({detail.allocations.length})
                    </h4>
                  </div>
                  {detail.allocations.length === 0 ? (
                    <p className="text-xs text-slate-500 italic p-3 bg-slate-900/40 rounded border border-slate-800/60">
                      Zero application allocations approved yet.
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {detail.allocations.map((alloc) => (
                        <div
                          key={alloc.id}
                          className="p-3 rounded-lg bg-slate-900/90 border border-slate-800 flex justify-between items-center text-xs"
                        >
                          <div>
                            <span className="font-mono text-blue-400 font-medium">{alloc.invoice_number}</span>
                            <span className="text-[11px] text-slate-500 block">
                              Approved by {alloc.actor} on {alloc.allocated_at ? new Date(alloc.allocated_at).toLocaleDateString() : '-'}
                            </span>
                          </div>
                          <MoneyDisplay amountMinor={alloc.amount_minor} currency={alloc.currency} className="text-emerald-400 font-semibold" />
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Linked AI Investigations */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <h4 className="font-semibold text-slate-300 uppercase tracking-wider text-[11px] flex items-center gap-1.5">
                      <Sparkles size={12} className="text-violet-400" />
                      Linked AI Investigations ({detail.investigations.length})
                    </h4>
                    {detail.unapplied_cash_minor > 0 && (
                      <button
                        onClick={() => onStartInvestigation(detail.provider_payment_id)}
                        className="btn btn-secondary py-0.5 px-2 text-[10px] text-violet-300 border-violet-700/40 hover:bg-violet-900/20"
                      >
                        + New Investigation
                      </button>
                    )}
                  </div>

                  {detail.investigations.length === 0 ? (
                    <p className="text-xs text-slate-500 italic p-3 bg-slate-900/40 rounded border border-slate-800/60">
                      No AI investigations launched for this transaction.
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {detail.investigations.map((inv) => (
                        <div
                          key={inv.id}
                          onClick={() => onInspectInvestigation(inv.id)}
                          className="p-3 rounded-lg bg-slate-900/90 border border-slate-800 hover:border-violet-500/50 cursor-pointer transition-all space-y-1.5"
                        >
                          <div className="flex justify-between items-center text-xs">
                            <span className="font-mono font-bold text-violet-300">{inv.classification}</span>
                            <VerifierStatusBadge status={inv.verifier_status} />
                          </div>
                          <p className="text-xs text-slate-400 line-clamp-2">{inv.summary}</p>
                          <span className="text-[10px] text-blue-400 flex items-center gap-1 mt-1">
                            Inspect forensic findings <ArrowRight size={10} />
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Cryptographic Audit Events */}
                <div className="space-y-2 pt-2 border-t border-slate-800">
                  <h4 className="font-semibold text-slate-300 uppercase tracking-wider text-[11px] flex items-center gap-1.5">
                    <ShieldCheck size={12} className="text-slate-400" />
                    Transaction Audit Events
                  </h4>
                  {detail.audit_events.length === 0 ? (
                    <p className="text-xs text-slate-500 italic">No specific audit events.</p>
                  ) : (
                    <div className="space-y-1.5">
                      {detail.audit_events.map((ev, idx) => (
                        <div key={idx} className="p-2.5 rounded bg-slate-950 border border-slate-800/80 text-xs flex justify-between items-center">
                          <div>
                            <span className="font-mono text-emerald-400 font-semibold">{ev.event_type}</span>
                            <span className="text-[11px] text-slate-500 block">by {ev.actor}</span>
                          </div>
                          <HashBadge hash={ev.event_hash} label="Event Hash" />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
