import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../api/client';
import { AuditEventItem, AuditVerifyResponse } from '../types/api';
import { HashBadge, LoadingSpinner, EmptyState, LoadErrorBanner } from '../components/Common';
import {
  ShieldCheck,
  ShieldAlert,
  Search,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  Link2,
  Lock,
  Layers,
  CheckCircle2,
  AlertTriangle,
} from 'lucide-react';

interface AuditTrailPageProps {
  tenantId: string;
}

export const AuditTrailPage: React.FC<AuditTrailPageProps> = ({ tenantId }) => {
  const [events, setEvents] = useState<AuditEventItem[]>([]);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(true);
  const [verifying, setVerifying] = useState<boolean>(false);
  const [verifyResult, setVerifyResult] = useState<AuditVerifyResponse | null>(null);
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});

  const [loadError, setLoadError] = useState<string | null>(null);

  // Filters
  const [subjectTypeFilter, setSubjectTypeFilter] = useState<string>('');
  const [subjectIdFilter, setSubjectIdFilter] = useState<string>('');

  const loadData = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await api.listAuditEvents({
        tenantId,
        subjectType: subjectTypeFilter || undefined,
        subjectId: subjectIdFilter || undefined,
        limit: 100,
      });
      setEvents(data.audit_events || []);
      setTotalCount(data.total_count || 0);
    } catch (err) {
      console.error('Failed to load audit events:', err);
      setLoadError(err instanceof Error ? err.message : 'Request failed');
    } finally {
      setLoading(false);
    }
  }, [tenantId, subjectTypeFilter, subjectIdFilter]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleVerify = async () => {
    setVerifying(true);
    try {
      const res = await api.verifyAuditTrail(tenantId);
      setVerifyResult(res);
    } catch (err: any) {
      console.error('Audit verification failed:', err);
      setVerifyResult({
        verified: false,
        tenant_id: tenantId,
        total_events: 0,
        error: err.message || 'Audit trail verification failed',
      });
    } finally {
      setVerifying(false);
    }
  };

  const toggleExpand = (id: string) => {
    setExpandedRows(prev => ({
      ...prev,
      [id]: !prev[id],
    }));
  };

  const getEventTypeColor = (type: string) => {
    if (type.includes('APPROVED')) return 'bg-emerald-950 text-emerald-400 border-emerald-800';
    if (type.includes('REJECTED')) return 'bg-rose-950 text-rose-400 border-rose-800';
    if (type.includes('INVESTIGATION')) return 'bg-cyan-950 text-cyan-400 border-cyan-800';
    if (type.includes('PROPOSAL')) return 'bg-blue-950 text-blue-400 border-blue-800';
    if (type.includes('ALLOCATION')) return 'bg-purple-950 text-purple-400 border-purple-800';
    return 'bg-slate-900 text-slate-300 border-slate-800';
  };

  return (
    <div className="space-y-6">
      <LoadErrorBanner message={loadError} />
      {/* Header & Verification Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-white flex items-center gap-2">
            <Lock className="w-5 h-5 text-emerald-400" />
            Cryptographic Audit Trail
          </h1>
          <p className="text-sm text-slate-400">
            Immutable, SHA-256 hash-chained proof of all financial allocations and operator decisions.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={loadData}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-900 text-slate-300 hover:text-white hover:bg-slate-800 text-sm transition-colors"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>

          <button
            onClick={handleVerify}
            disabled={verifying}
            className="flex items-center gap-2 px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-sm font-medium transition-colors shadow-lg shadow-emerald-900/20"
          >
            {verifying ? (
              <>
                <RefreshCw className="w-4 h-4 animate-spin" />
                Verifying Chain...
              </>
            ) : (
              <>
                <ShieldCheck className="w-4 h-4" />
                Verify Chain Integrity
              </>
            )}
          </button>
        </div>
      </div>

      {/* Verification Result Banner */}
      {verifyResult && (
        <div
          className={`p-4 rounded-xl border ${
            verifyResult.verified
              ? 'bg-emerald-950/40 border-emerald-800/60 text-emerald-300'
              : 'bg-rose-950/40 border-rose-800/60 text-rose-300'
          }`}
        >
          <div className="flex items-start gap-3">
            {verifyResult.verified ? (
              <CheckCircle2 className="w-6 h-6 text-emerald-400 flex-shrink-0 mt-0.5" />
            ) : (
              <ShieldAlert className="w-6 h-6 text-rose-400 flex-shrink-0 mt-0.5" />
            )}
            <div className="flex-1 space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-white">
                  {verifyResult.verified
                    ? 'Cryptographic Audit Chain Intact & Verified'
                    : 'Audit Trail Tamper Verification Failed'}
                </h3>
                <span className="text-xs px-2.5 py-0.5 rounded-full border border-current font-mono">
                  {verifyResult.algorithm || 'SHA-256'}
                </span>
              </div>
              <p className="text-sm opacity-90">
                {verifyResult.verified
                  ? `Successfully validated ${verifyResult.total_events} consecutive audit event hashes without tampering or link breakage.`
                  : verifyResult.error || verifyResult.message || 'The chain verification detected hash mismatch.'}
              </p>
              {verifyResult.verified && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2 pt-2 text-xs font-mono text-slate-300 border-t border-emerald-800/40">
                  <div>
                    <span className="text-slate-400">Genesis Hash: </span>
                    <span className="text-emerald-400">
                      {verifyResult.genesis_hash ? verifyResult.genesis_hash.slice(0, 24) + '...' : '0'.repeat(64)}
                    </span>
                  </div>
                  <div>
                    <span className="text-slate-400">Latest Hash: </span>
                    <span className="text-emerald-400">
                      {verifyResult.latest_hash ? verifyResult.latest_hash.slice(0, 24) + '...' : 'N/A'}
                    </span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Chain Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wider text-slate-400">Total Audit Events</div>
            <div className="text-2xl font-bold text-white mt-1">{totalCount}</div>
          </div>
          <Layers className="w-8 h-8 text-blue-500/40" />
        </div>
        <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wider text-slate-400">Hash Algorithm</div>
            <div className="text-xl font-bold font-mono text-emerald-400 mt-1">SHA-256 Chain</div>
          </div>
          <Link2 className="w-8 h-8 text-emerald-500/40" />
        </div>
        <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wider text-slate-400">Tenant Namespace</div>
            <div className="text-lg font-bold font-mono text-purple-400 mt-1 truncate max-w-[200px]">
              {tenantId}
            </div>
          </div>
          <Lock className="w-8 h-8 text-purple-500/40" />
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 p-3 bg-slate-900/80 rounded-xl border border-slate-800">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="w-4 h-4 absolute left-3 top-2.5 text-slate-500" />
          <input
            type="text"
            placeholder="Filter by Subject ID..."
            value={subjectIdFilter}
            onChange={(e) => setSubjectIdFilter(e.target.value)}
            className="w-full pl-9 pr-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-sm text-slate-200 focus:outline-none focus:border-blue-500 placeholder-slate-600"
          />
        </div>

        <select
          value={subjectTypeFilter}
          onChange={(e) => setSubjectTypeFilter(e.target.value)}
          className="bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-sm text-slate-300 focus:outline-none focus:border-blue-500"
        >
          <option value="">All Subject Types</option>
          <option value="proposal">proposal</option>
          <option value="payment">payment</option>
          <option value="invoice">invoice</option>
          <option value="investigation">investigation</option>
          <option value="allocation">allocation</option>
        </select>
      </div>

      {/* Events Table / Timeline */}
      {loading ? (
        <div className="p-12 bg-slate-900/40 rounded-xl border border-slate-800">
          <LoadingSpinner text="Retrieving cryptographic audit events..." />
        </div>
      ) : events.length === 0 ? (
        <EmptyState
          title="No Audit Events Recorded"
          description={
            subjectTypeFilter || subjectIdFilter
              ? 'No audit events match your filter criteria.'
              : 'Audit events are generated when reconciliation runs, approvals, or investigations occur.'
          }
        />
      ) : (
        <div className="space-y-3">
          {events.map((evt) => {
            const isExpanded = !!expandedRows[evt.id];
            return (
              <div
                key={evt.id}
                className="bg-slate-900/70 border border-slate-800 hover:border-slate-700 rounded-xl overflow-hidden transition-all"
              >
                {/* Row Header */}
                <div
                  onClick={() => toggleExpand(evt.id)}
                  className="p-4 flex flex-col md:flex-row md:items-center justify-between gap-3 cursor-pointer hover:bg-slate-800/40 transition-colors"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <button className="text-slate-500 hover:text-white">
                      {isExpanded ? (
                        <ChevronDown className="w-4 h-4 text-slate-300" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-slate-500" />
                      )}
                    </button>

                    <div className="w-8 h-8 rounded-lg bg-slate-800 flex items-center justify-center font-mono font-bold text-xs text-slate-300 border border-slate-700">
                      #{evt.sequence_number}
                    </div>

                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span
                          className={`text-xs px-2.5 py-0.5 rounded font-mono font-medium border ${getEventTypeColor(
                            evt.event_type
                          )}`}
                        >
                          {evt.event_type}
                        </span>
                        <span className="text-xs text-slate-400">
                          by <strong className="text-slate-200">{evt.actor}</strong>
                        </span>
                      </div>
                      <div className="text-xs text-slate-500 mt-1 flex items-center gap-2">
                        <span>Subject:</span>
                        <span className="font-mono text-slate-300">
                          {evt.subject_type}:{evt.subject_id}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex flex-col md:items-end gap-1 flex-shrink-0">
                    <div className="text-xs text-slate-400">
                      {evt.timestamp ? new Date(evt.timestamp).toLocaleString() : 'N/A'}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] text-slate-500">Hash:</span>
                      <HashBadge hash={evt.event_hash} />
                    </div>
                  </div>
                </div>

                {/* Expanded Details */}
                {isExpanded && (
                  <div className="px-4 pb-4 pt-2 border-t border-slate-800/60 bg-slate-950/40 space-y-4">
                    {/* Hash Linkage */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-3 rounded-lg bg-slate-900 border border-slate-800 text-xs font-mono">
                      <div>
                        <div className="text-slate-500 flex items-center gap-1.5 mb-1">
                          <Link2 className="w-3.5 h-3.5 text-blue-400" />
                          Previous Event Hash (Chain Link)
                        </div>
                        <div className="text-slate-300 break-all bg-slate-950 p-2 rounded border border-slate-800">
                          {evt.prev_hash || '0'.repeat(64) + ' (Genesis)'}
                        </div>
                      </div>
                      <div>
                        <div className="text-slate-500 flex items-center gap-1.5 mb-1">
                          <Lock className="w-3.5 h-3.5 text-emerald-400" />
                          Canonical Event Hash (SHA-256)
                        </div>
                        <div className="text-emerald-400 break-all bg-slate-950 p-2 rounded border border-slate-800">
                          {evt.event_hash}
                        </div>
                      </div>
                    </div>

                    {/* Payload JSON */}
                    <div>
                      <div className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-1.5">
                        Event Payload Details
                      </div>
                      <pre className="p-3 rounded-lg bg-slate-950 border border-slate-800 text-xs font-mono text-slate-300 overflow-x-auto">
                        {JSON.stringify(evt.payload, null, 2)}
                      </pre>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
