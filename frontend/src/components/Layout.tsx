import React from 'react';
import {
  LayoutDashboard,
  ArrowLeftRight,
  FileText,
  GitMerge,
  Search,
  ShieldCheck,
  CheckCircle,
  Sparkles,
  RefreshCw,
  Building2,
  Server,
  Zap,
} from 'lucide-react';
import { api } from '../api/client';

export type NavigationTab =
  | 'overview'
  | 'reconciliation'
  | 'transactions'
  | 'invoices'
  | 'proposals'
  | 'investigations'
  | 'audit';

interface LayoutProps {
  currentTab: NavigationTab;
  onSelectTab: (tab: NavigationTab) => void;
  tenantId: string;
  onTenantChange: (tenant: string) => void;
  onSeedDemo: () => Promise<void>;
  isSeeding: boolean;
  children: React.ReactNode;
}

export const Layout: React.FC<LayoutProps> = ({
  currentTab,
  onSelectTab,
  tenantId,
  onTenantChange,
  onSeedDemo,
  isSeeding,
  children,
}) => {
  const [healthStatus, setHealthStatus] = React.useState<'ok' | 'checking' | 'error'>('checking');

  React.useEffect(() => {
    api
      .checkHealth()
      .then(() => setHealthStatus('ok'))
      .catch(() => setHealthStatus('error'));
  }, []);

  const navItems = [
    { id: 'overview' as NavigationTab, label: 'Overview', icon: LayoutDashboard },
    { id: 'reconciliation' as NavigationTab, label: 'Reconciliation Runs', icon: GitMerge },
    { id: 'transactions' as NavigationTab, label: 'Transactions', icon: ArrowLeftRight },
    { id: 'invoices' as NavigationTab, label: 'Invoices & Ledger', icon: FileText },
    { id: 'proposals' as NavigationTab, label: 'Review Queue', icon: CheckCircle },
    { id: 'investigations' as NavigationTab, label: 'AI Investigations', icon: Sparkles },
    { id: 'audit' as NavigationTab, label: 'Audit Trail', icon: ShieldCheck },
  ];

  return (
    <div className="flex min-h-screen bg-[#070a12] text-slate-100">
      {/* Sidebar */}
      <aside className="w-64 border-r border-slate-800/80 bg-[#0c1222] flex flex-col fixed inset-y-0 z-30">
        {/* Brand Header */}
        <div className="p-5 border-b border-slate-800/80">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-blue-600/20 border border-blue-500/40 flex items-center justify-center text-blue-400 font-bold text-sm shadow-[0_0_12px_rgba(59,130,246,0.25)]">
              PR
            </div>
            <div>
              <h1 className="text-sm font-semibold tracking-tight text-white flex items-center gap-1.5">
                Reconciliation
                <span className="text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-blue-900/60 text-blue-300 border border-blue-700/50">
                  D7
                </span>
              </h1>
              <p className="text-[11px] text-slate-400 font-mono tracking-tight">You can prove.</p>
            </div>
          </div>
        </div>

        {/* Tenant Selector */}
        <div className="px-4 py-3.5 border-b border-slate-800/60 bg-slate-950/40">
          <label className="text-[10px] uppercase font-bold tracking-wider text-slate-400 block mb-1.5 flex items-center gap-1">
            <Building2 size={11} />
            Active Tenant
          </label>
          <select
            value={tenantId}
            onChange={(e) => onTenantChange(e.target.value)}
            className="select-input text-xs py-1.5 bg-slate-900 border-slate-700 text-slate-200 rounded font-medium"
          >
            <option value="demo_merchant">Demo Merchant Corp (Global)</option>
            <option value="default_tenant">Default Tenant</option>
            <option value="test_tenant">Test Tenant</option>
          </select>
        </div>

        {/* Navigation Items */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = currentTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onSelectTab(item.id)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-md text-xs font-medium transition-all ${
                  active
                    ? 'bg-blue-600/15 text-blue-400 border border-blue-500/30 shadow-sm'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 border border-transparent'
                }`}
              >
                <Icon size={16} className={active ? 'text-blue-400' : 'text-slate-400'} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        {/* Quick Demo Seeder Footer */}
        <div className="p-4 border-t border-slate-800/80 bg-slate-950/60 space-y-3">
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-slate-400 flex items-center gap-1.5">
              <Server size={12} />
              Backend:
            </span>
            <span
              className={`inline-flex items-center gap-1 font-mono text-[10px] ${
                healthStatus === 'ok' ? 'text-emerald-400' : healthStatus === 'checking' ? 'text-amber-400' : 'text-red-400'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  healthStatus === 'ok' ? 'bg-emerald-400' : healthStatus === 'checking' ? 'bg-amber-400 animate-pulse' : 'bg-red-400'
                }`}
              />
              {healthStatus === 'ok' ? 'ONLINE' : healthStatus === 'checking' ? 'CONNECTING' : 'OFFLINE'}
            </span>
          </div>

          <button
            onClick={onSeedDemo}
            disabled={isSeeding}
            className="w-full btn btn-secondary text-xs py-2 border-slate-700/80 bg-slate-900 hover:bg-slate-800 text-slate-200 flex items-center justify-center gap-2"
          >
            <Zap size={13} className={isSeeding ? 'animate-spin text-amber-400' : 'text-amber-400'} />
            <span>{isSeeding ? 'Seeding Scenarios...' : 'Reseed Demo Data'}</span>
          </button>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 ml-64 flex flex-col min-h-screen">
        {/* Top Header */}
        <header className="h-16 border-b border-slate-800/80 bg-[#090d18]/90 backdrop-blur-md px-8 flex items-center justify-between sticky top-0 z-20">
          <div className="flex items-center gap-3">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              {currentTab}
            </span>
            <span className="text-slate-600">/</span>
            <span className="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
              tenant: {tenantId}
            </span>
          </div>

          <div className="flex items-center gap-4">
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-xs font-medium font-mono">
              <ShieldCheck size={13} />
              Zero-Mutations Verified
            </span>
            <span className="text-xs text-slate-400 font-mono">
              Human-in-the-Loop Mode
            </span>
          </div>
        </header>

        {/* Page Content */}
        <main className="flex-1 p-8 overflow-y-auto">
          {children}
        </main>
      </div>
    </div>
  );
};
