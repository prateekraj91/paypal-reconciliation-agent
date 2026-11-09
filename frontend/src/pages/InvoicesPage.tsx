import React, { useEffect, useState } from 'react';
import {
  Search,
  Filter,
  FileText,
  RefreshCw,
  ExternalLink,
  X,
  CheckCircle2,
  AlertTriangle,
  Clock,
} from 'lucide-react';
import { api } from '../api/client';
import { InvoiceItem, InvoiceDetail } from '../types/api';
import { LoadErrorBanner, MoneyDisplay, StatusBadge, LoadingSpinner, EmptyState } from '../components/Common';
import { getInvoiceEmptyLabels } from '../utils/formatters';

export function InvoicesPage({ tenantId }: { tenantId: string }) {
  const [loadError, setLoadError] = useState<string | null>(null);
  const [invoices, setInvoices] = useState<InvoiceItem[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('ALL');
  const [currencyFilter, setCurrencyFilter] = useState('ALL');

  // Detail drawer
  const [selectedInvId, setSelectedInvId] = useState<string | null>(null);
  const [detail, setDetail] = useState<InvoiceDetail | null>(null);
  const [isDetailLoading, setIsDetailLoading] = useState(false);

  const fetchInvoices = async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await api.listInvoices({
        tenantId,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        currency: currencyFilter === 'ALL' ? undefined : currencyFilter,
        search: search.trim() || undefined,
        limit: 100,
      });
      setInvoices(data.invoices);
      setTotalCount(data.total_count);
    } catch (err) {
      console.error('Failed to load invoices', err);
      setLoadError(err instanceof Error ? err.message : 'Request failed');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchInvoices();
  }, [tenantId, statusFilter, currencyFilter]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchInvoices();
  };

  const openDetail = async (invId: string) => {
    setSelectedInvId(invId);
    setIsDetailLoading(true);
    setDetail(null);
    try {
      const inv = await api.getInvoice(invId, tenantId);
      setDetail(inv);
    } catch (err) {
      console.error('Failed to load invoice detail', err);
    } finally {
      setIsDetailLoading(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in relative">
      <LoadErrorBanner message={loadError} />
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white">Invoices & Financial Ledger</h2>
          <p className="text-xs text-slate-400 mt-1">
            Authoritative invoice obligations, approved allocations, external settlements, and true outstanding balances.
          </p>
        </div>
        <button
          onClick={fetchInvoices}
          disabled={isLoading}
          className="btn btn-secondary text-xs flex items-center gap-1.5"
        >
          <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Filter Bar */}
      <div className="p-4 rounded-lg bg-[#0e1526] border border-slate-800 flex flex-wrap items-center gap-3">
        <form onSubmit={handleSearchSubmit} className="flex-1 min-w-[220px] relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by Invoice Number, Customer Name, or Email..."
            className="input-text text-xs pl-8 bg-slate-900 border-slate-700"
          />
        </form>

        <div className="flex items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="select-input text-xs w-auto py-2 bg-slate-900 border-slate-700"
          >
            <option value="ALL">All Ledger Statuses</option>
            <option value="FULLY_RECONCILED">Fully Reconciled (Balance 0)</option>
            <option value="OPEN">Open (Zero Allocations)</option>
            <option value="PARTIAL">Partially Settled</option>
          </select>

          <select
            value={currencyFilter}
            onChange={(e) => setCurrencyFilter(e.target.value)}
            className="select-input text-xs w-auto py-2 bg-slate-900 border-slate-700"
          >
            <option value="ALL">All Currencies</option>
            <option value="USD">USD ($)</option>
            <option value="EUR">EUR (€)</option>
          </select>
        </div>
      </div>

      {/* Invoices Table */}
      <div className="fintech-card p-0 overflow-hidden">
        {isLoading ? (
          <LoadingSpinner text="Computing Application & Provider Balances..." />
        ) : invoices.length === 0 ? (
          <div className="p-8">
            <EmptyState
              title={getInvoiceEmptyLabels(statusFilter).title}
              description={getInvoiceEmptyLabels(statusFilter).description}
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="fintech-table">
              <thead>
                <tr>
                  <th>Invoice Number</th>
                  <th>Customer</th>
                  <th>Due Date</th>
                  <th>Total Amount</th>
                  <th>Allocated / Settled</th>
                  <th>Ledger Outstanding</th>
                  <th className="col-sticky-right text-right">Status</th>
                </tr>
              </thead>
              <tbody>
                {invoices.map((inv) => (
                  <tr
                    key={inv.id}
                    onClick={() => openDetail(inv.invoice_number)}
                    className="cursor-pointer"
                  >
                    <td>
                      <span className="font-mono text-xs font-semibold text-blue-400">
                        {inv.invoice_number}
                      </span>
                    </td>
                    <td>
                      <div className="text-xs font-medium text-slate-200">
                        {inv.customer_name || 'Generic Customer'}
                      </div>
                      {inv.customer_email && (
                        <div className="text-[11px] text-slate-500 font-mono">{inv.customer_email}</div>
                      )}
                    </td>
                    <td className="text-xs text-slate-400 font-mono">
                      {inv.due_on ? new Date(inv.due_on).toLocaleDateString() : '-'}
                    </td>
                    <td>
                      <MoneyDisplay amountMinor={inv.total_minor} currency={inv.currency} />
                    </td>
                    <td>
                      <MoneyDisplay
                        amountMinor={inv.allocated_minor + inv.settled_minor}
                        currency={inv.currency}
                        className="text-slate-400"
                      />
                    </td>
                    <td>
                      {inv.outstanding_balance_minor > 0 ? (
                        <MoneyDisplay
                          amountMinor={inv.outstanding_balance_minor}
                          currency={inv.currency}
                          className="text-amber-400 font-semibold"
                        />
                      ) : (
                        <span className="text-emerald-400 text-xs font-mono font-medium">PAID ($0.00)</span>
                      )}
                    </td>
                    <td className="col-sticky-right text-right">
                      <StatusBadge status={inv.reconciliation_status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Invoice Detail Drawer */}
      {selectedInvId && (
        <div
          className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex justify-end animate-fade-in"
          onClick={() => setSelectedInvId(null)}
        >
          <div
            className="w-full max-w-xl bg-[#0c1222] border-l border-slate-800 h-full overflow-y-auto p-6 space-y-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between pb-4 border-b border-slate-800">
              <div>
                <span className="text-[10px] uppercase font-bold text-slate-500 tracking-wider">
                  Invoice Ledger Detail
                </span>
                <h3 className="text-lg font-bold font-mono text-white mt-0.5">
                  {selectedInvId}
                </h3>
              </div>
              <button
                onClick={() => setSelectedInvId(null)}
                className="p-1 rounded text-slate-400 hover:text-white hover:bg-slate-800"
              >
                <X size={18} />
              </button>
            </div>

            {isDetailLoading || !detail ? (
              <LoadingSpinner text="Computing ledger records..." />
            ) : (
              <div className="space-y-6 text-xs">
                {/* Outstanding Balance Banner */}
                <div className="grid grid-cols-2 gap-3 p-4 rounded-lg bg-slate-900 border border-slate-800">
                  <div>
                    <span className="text-[11px] text-slate-400 block mb-1">Outstanding Balance</span>
                    <MoneyDisplay
                      amountMinor={detail.outstanding_balance_minor}
                      currency={detail.currency}
                      className={detail.outstanding_balance_minor > 0 ? 'text-amber-400 text-base font-bold' : 'text-emerald-400 text-base font-bold'}
                    />
                  </div>
                  <div>
                    <span className="text-[11px] text-slate-400 block mb-1">Total Due</span>
                    <MoneyDisplay amountMinor={detail.total_minor} currency={detail.currency} className="text-white text-base font-bold" />
                  </div>
                </div>

                {/* Customer Details */}
                <div className="p-3.5 rounded-lg bg-slate-900/60 border border-slate-800 space-y-2">
                  <div className="flex justify-between">
                    <span className="text-slate-500">Customer Name:</span>
                    <span className="text-slate-200 font-medium">{detail.customer_name}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Customer Email:</span>
                    <span className="text-slate-200 font-mono">{detail.customer_email || '-'}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Issued On:</span>
                    <span className="text-slate-200 font-mono">{detail.issued_on || '-'}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Due Date:</span>
                    <span className="text-slate-200 font-mono">{detail.due_on || '-'}</span>
                  </div>
                  {detail.memo && (
                    <div className="pt-2 border-t border-slate-800 text-slate-400">
                      <span className="text-[10px] uppercase font-bold text-slate-500 block mb-1">Invoice Memo:</span>
                      <p className="p-2 rounded bg-slate-950 font-mono text-[11px] text-slate-300">
                        {detail.memo}
                      </p>
                    </div>
                  )}
                </div>

                {/* Approved Allocations Against This Invoice */}
                <div className="space-y-2">
                  <h4 className="font-semibold text-slate-300 uppercase tracking-wider text-[11px]">
                    Approved Cash Allocations ({detail.allocations.length})
                  </h4>
                  {detail.allocations.length === 0 ? (
                    <p className="text-xs text-slate-500 italic p-3 bg-slate-900/40 rounded border border-slate-800/60">
                      No application cash allocated to this invoice yet.
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {detail.allocations.map((alloc) => (
                        <div
                          key={alloc.id}
                          className="p-3 rounded-lg bg-slate-900 border border-slate-800 flex justify-between items-center"
                        >
                          <div>
                            <span className="font-mono text-blue-400 font-medium">{alloc.provider_payment_id}</span>
                            <span className="text-[11px] text-slate-500 block">
                              From: {alloc.payer_name} • Approved by {alloc.actor}
                            </span>
                          </div>
                          <MoneyDisplay amountMinor={alloc.amount_minor} currency={alloc.currency} className="text-emerald-400 font-semibold" />
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Settlements */}
                {detail.settlements.length > 0 && (
                  <div className="space-y-2">
                    <h4 className="font-semibold text-slate-300 uppercase tracking-wider text-[11px]">
                      Provider External Settlements ({detail.settlements.length})
                    </h4>
                    <div className="space-y-2">
                      {detail.settlements.map((sett) => (
                        <div
                          key={sett.id}
                          className="p-3 rounded-lg bg-slate-900 border border-slate-800 flex justify-between items-center"
                        >
                          <div>
                            <span className="font-mono text-slate-300 font-medium">{sett.provider_settlement_id}</span>
                            <span className="text-[11px] text-slate-500 block">Reported by PayPal</span>
                          </div>
                          <MoneyDisplay amountMinor={sett.amount_minor} currency={sett.currency} className="text-blue-400 font-semibold" />
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
