import React, { useEffect, useState } from 'react';
import {
  Sparkles,
  RefreshCw,
  Plus,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  ArrowRight,
  Filter,
} from 'lucide-react';
import { api } from '../api/client';
import { InvestigationDetail } from '../types/api';
import { LoadErrorBanner, StatusBadge, VerifierStatusBadge, LoadingSpinner, EmptyState } from '../components/Common';
import { NewInvestigationModal } from '../components/Modals';
import { getInvestigationEmptyLabels } from '../utils/formatters';

export function InvestigationsPage({
  tenantId,
  onSelectInvestigation,
  initialTargetId,
}: {
  tenantId: string;
  onSelectInvestigation: (invId: string) => void;
  initialTargetId?: string;
}) {
  const [loadError, setLoadError] = useState<string | null>(null);
  const [investigations, setInvestigations] = useState<InvestigationDetail[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<'ALL' | 'NEEDS_REVIEW' | 'COMPLETED' | 'INVALID'>('ALL');
  const [isNewModalOpen, setIsNewModalOpen] = useState(false);
  const [isSubmittingNew, setIsSubmittingNew] = useState(false);

  const fetchInvestigations = async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await api.listInvestigations({
        tenantId,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        limit: 100,
      });
      setInvestigations(data.investigations);
    } catch (err) {
      console.error('Failed to load investigations', err);
      setLoadError(err instanceof Error ? err.message : 'Request failed');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchInvestigations();
  }, [tenantId, statusFilter]);

  useEffect(() => {
    if (initialTargetId) {
      setIsNewModalOpen(true);
    }
  }, [initialTargetId]);

  const handleLaunchInvestigation = async (type: string, id: string, syncMode: boolean) => {
    setIsSubmittingNew(true);
    try {
      const res = await api.submitInvestigation(type, id, syncMode, tenantId);
      setIsNewModalOpen(false);
      await fetchInvestigations();
      if (res && res.investigation_id) {
        onSelectInvestigation(res.investigation_id);
      }
    } catch (err: any) {
      alert(`Investigation failed: ${err.message}`);
    } finally {
      setIsSubmittingNew(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <LoadErrorBanner message={loadError} />
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            <Sparkles size={20} className="text-violet-400" />
            AI Exception Investigations
          </h2>
          <p className="text-xs text-slate-400 mt-1">
            Grounded investigation of financial discrepancies using 12 read-only tools and independent verification.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={fetchInvestigations}
            disabled={isLoading}
            className="btn btn-secondary text-xs flex items-center gap-1.5"
          >
            <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
            <span>Refresh</span>
          </button>
          <button
            onClick={() => setIsNewModalOpen(true)}
            className="btn btn-primary text-xs flex items-center gap-2 bg-gradient-to-r from-blue-600 to-indigo-600"
          >
            <Plus size={14} />
            <span>New Investigation</span>
          </button>
        </div>
      </div>

      {/* Status Filter Tabs */}
      <div className="flex items-center gap-2 border-b border-slate-800 pb-2">
        {(['ALL', 'NEEDS_REVIEW', 'COMPLETED', 'INVALID'] as const).map((tab) => {
          const active = statusFilter === tab;
          return (
            <button
              key={tab}
              onClick={() => setStatusFilter(tab)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                active
                  ? 'bg-violet-600/20 text-violet-300 border border-violet-500/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/40'
              }`}
            >
              {tab === 'NEEDS_REVIEW'
                ? 'Needs Human Review'
                : tab === 'INVALID'
                ? 'Flagged Invalid (Fail-Closed)'
                : tab}
            </button>
          );
        })}
      </div>

      {/* Investigations Table */}
      <div className="fintech-card p-0 overflow-hidden">
        {isLoading ? (
          <LoadingSpinner text="Querying Exception Cases..." />
        ) : investigations.length === 0 ? (
          <div className="p-12 text-center">
            <EmptyState
              title={getInvestigationEmptyLabels(statusFilter).title}
              description={getInvestigationEmptyLabels(statusFilter).description}
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="fintech-table">
              <thead>
                <tr>
                  <th>Case ID</th>
                  <th>Target Exception</th>
                  <th>Classification</th>
                  <th>Model & Provider</th>
                  <th>Safety Verifier</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {investigations.map((inv) => (
                  <tr
                    key={inv.id}
                    onClick={() => onSelectInvestigation(inv.id)}
                    className="cursor-pointer"
                  >
                    <td>
                      <span className="font-mono text-xs font-semibold text-slate-300">{inv.id}</span>
                      <span className="text-[10px] text-slate-500 block">
                        {inv.created_at ? new Date(inv.created_at).toLocaleString() : '-'}
                      </span>
                    </td>
                    <td>
                      <span className="font-mono text-xs font-medium text-blue-400">{inv.target_id}</span>
                      <span className="text-[10px] uppercase font-bold text-slate-500 block">{inv.target_type}</span>
                    </td>
                    <td>
                      <span className="font-mono text-[11px] font-semibold text-violet-300 px-2 py-0.5 rounded bg-violet-950/50 border border-violet-800/40">
                        {inv.classification}
                      </span>
                    </td>
                    <td>
                      <div className="text-xs text-slate-300 font-medium">{inv.model_name || 'qwen3:8b'}</div>
                      <div className="text-[10px] text-slate-500 uppercase">{inv.model_provider || 'ollama'}</div>
                    </td>
                    <td>
                      <VerifierStatusBadge status={inv.verifier_status} />
                    </td>
                    <td>
                      <StatusBadge status={inv.status} />
                    </td>
                    <td>
                      <span className="text-xs text-blue-400 hover:text-blue-300 flex items-center gap-1 font-medium">
                        Inspect Case
                        <ArrowRight size={12} />
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* New Investigation Modal */}
      <NewInvestigationModal
        isOpen={isNewModalOpen}
        onClose={() => setIsNewModalOpen(false)}
        targetId={initialTargetId}
        onConfirm={handleLaunchInvestigation}
        isLoading={isSubmittingNew}
      />
    </div>
  );
}
