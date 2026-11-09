import React from 'react';
import { CheckCircle2, AlertTriangle, XCircle, ShieldCheck, ShieldAlert, Copy, Check } from 'lucide-react';
import { VerifierStatus, ReconciliationStatus, DecisionStatus } from '../types/api';

/**
 * Format integer minor units (e.g. cents) to currency string.
 */
export function formatMoney(amountMinor: number, currency: string = 'USD'): string {
  const isNegative = amountMinor < 0;
  const absMinor = Math.abs(amountMinor);
  const major = absMinor / 100;
  const symbol = currency.toUpperCase() === 'EUR' ? '€' : currency.toUpperCase() === 'GBP' ? '£' : '$';
  const prefix = isNegative ? '-' : '';
  return `${prefix}${symbol}${major.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency.toUpperCase()}`;
}

export function MoneyDisplay({
  amountMinor,
  currency = 'USD',
  className = '',
}: {
  amountMinor: number;
  currency?: string;
  className?: string;
}) {
  const isNegative = amountMinor < 0;
  return (
    <span className={`font-mono font-medium tracking-tight ${isNegative ? 'text-rose-400' : ''} ${className}`}>
      {formatMoney(amountMinor, currency)}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const s = (status || '').toUpperCase();

  if (s === 'RECONCILED' || s === 'VERIFIED' || s === 'APPROVED' || s === 'COMPLETED' || s === 'SUCCESS' || s === 'FULLY_RECONCILED') {
    return (
      <span className="badge badge-reconciled">
        <CheckCircle2 size={12} />
        {s === 'FULLY_RECONCILED' ? 'RECONCILED' : s}
      </span>
    );
  }

  if (s === 'AMBIGUOUS') {
    return (
      <span className="badge badge-ambiguous">
        <AlertTriangle size={12} className="text-amber-400" />
        AMBIGUOUS
      </span>
    );
  }

  if (s === 'UNMATCHED' || s === 'INVALID' || s === 'REJECTED' || s === 'FAILED' || s === 'VOID' || s === 'CANCELLED') {
    return (
      <span className="badge badge-unmatched">
        <XCircle size={12} />
        {s}
      </span>
    );
  }

  if (s === 'PARTIALLY_RECONCILED' || s === 'PARTIAL' || s === 'NEEDS_REVIEW' || s === 'PENDING' || s === 'OPEN') {
    return (
      <span className="badge badge-partial">
        <AlertTriangle size={12} />
        {s === 'PARTIALLY_RECONCILED' ? 'PARTIAL' : s}
      </span>
    );
  }

  if (s === 'QUEUED' || s === 'RUNNING' || s === 'INVESTIGATING') {
    return (
      <span className="badge badge-info">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
        {s}
      </span>
    );
  }

  return <span className="badge badge-neutral">{s || '—'}</span>;
}

export function RecordTypeBadge({ recordType }: { recordType: string }) {
  const rt = (recordType || 'PAYMENT').toUpperCase();
  if (rt === 'REFUND') {
    return (
      <span className="badge badge-refund font-mono">
        REFUND
      </span>
    );
  }
  if (rt === 'REVERSAL') {
    return (
      <span className="badge badge-reversal font-mono">
        REVERSAL
      </span>
    );
  }
  if (rt === 'SETTLEMENT') {
    return (
      <span className="badge badge-settlement font-mono">
        SETTLEMENT
      </span>
    );
  }
  return (
    <span className="badge badge-payment font-mono">
      PAYMENT
    </span>
  );
}

export function VerifierStatusBadge({ status }: { status: VerifierStatus | string }) {
  const s = (status || '').toUpperCase();
  if (s === 'VERIFIED') {
    return (
      <span className="badge badge-verified font-mono">
        <ShieldCheck size={13} className="text-emerald-400" />
        VERIFIED
      </span>
    );
  }
  if (s === 'INVALID') {
    return (
      <span className="badge badge-invalid font-mono">
        <ShieldAlert size={13} className="text-red-400" />
        INVALID (FAIL-CLOSED)
      </span>
    );
  }
  if (s === 'NEEDS_REVIEW') {
    return (
      <span className="badge badge-needs-review font-mono">
        <AlertTriangle size={13} className="text-amber-400" />
        NEEDS REVIEW
      </span>
    );
  }
  return <span className="badge badge-neutral font-mono">{s}</span>;
}

export function HashBadge({ hash, label = '' }: { hash: string; label?: string }) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(hash);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (!hash) return <span className="text-slate-500 font-mono text-xs">-</span>;

  const truncated = hash.length > 16 ? `${hash.substring(0, 8)}...${hash.substring(hash.length - 8)}` : hash;

  return (
    <span
      onClick={handleCopy}
      title={`${label ? label + ': ' : ''}${hash} (Click to copy)`}
      className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded bg-slate-900 border border-slate-700/80 font-mono text-xs text-slate-300 hover:text-white hover:border-slate-500 cursor-pointer transition-colors"
    >
      <span>{truncated}</span>
      {copied ? <Check size={12} className="text-emerald-400" /> : <Copy size={11} className="text-slate-400 opacity-60" />}
    </span>
  );
}

export function Modal({
  isOpen,
  onClose,
  title,
  subtitle,
  children,
  maxWidth = 'max-w-xl',
}: {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  maxWidth?: string;
}) {
  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className={`modal-dialog ${maxWidth} p-6 relative`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between pb-4 border-b border-slate-700/60 mb-5">
          <div>
            <h3 className="text-lg font-semibold text-slate-50">{title}</h3>
            {subtitle && <p className="text-xs text-slate-400 mt-0.5">{subtitle}</p>}
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition-colors"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function LoadingSpinner({ text = 'Loading...' }: { text?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 gap-3 text-slate-400">
      <div className="w-8 h-8 border-2 border-slate-700 border-t-blue-500 rounded-full animate-spin" />
      <span className="text-xs font-medium tracking-wide uppercase">{text}</span>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="text-center py-12 px-4 rounded-lg border border-dashed border-slate-800 bg-slate-900/30">
      <div className="w-10 h-10 mx-auto rounded-full bg-slate-800 flex items-center justify-center text-slate-400 mb-3">
        ∅
      </div>
      <h4 className="text-sm font-semibold text-slate-200">{title}</h4>
      <p className="text-xs text-slate-400 mt-1 max-w-sm mx-auto">{description}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function LoadErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="flex items-center gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
      <AlertTriangle size={14} className="shrink-0" />
      <span>Could not load this data, so the view below may be incomplete: {message}</span>
    </div>
  );
}
