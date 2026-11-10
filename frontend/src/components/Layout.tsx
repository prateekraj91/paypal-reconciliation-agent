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
  LogOut,
  Menu,
  X,
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
  onLogout: () => void;
  children: React.ReactNode;
}

export const Layout: React.FC<LayoutProps> = ({
  currentTab,
  onSelectTab,
  tenantId,
  onTenantChange,
  onSeedDemo,
  isSeeding,
  onLogout,
  children,
}) => {
  const [healthStatus, setHealthStatus] = React.useState<'ok' | 'checking' | 'error'>('checking');
  const [mobileMenuOpen, setMobileMenuOpen] = React.useState<boolean>(false);

  React.useEffect(() => {
    api
      .checkHealth()
      .then(() => setHealthStatus('ok'))
      .catch(() => setHealthStatus('error'));
  }, []);

  React.useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && mobileMenuOpen) {
        setMobileMenuOpen(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [mobileMenuOpen]);

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
    <div className="flex min-h-screen bg-[#0a0a0a] text-neutral-100 overflow-x-hidden">
      {/* Mobile Drawer Backdrop */}
      {mobileMenuOpen && (
        <div
          className="fixed inset-0 bg-black/75 backdrop-blur-sm z-40 lg:hidden transition-opacity"
          onClick={() => setMobileMenuOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar (Desktop pinned, Mobile off-canvas drawer) */}
      <aside
        className={`w-64 border-r border-neutral-800/80 bg-[#121212] flex flex-col fixed inset-y-0 z-50 lg:z-30 transition-transform duration-200 ease-in-out ${
          mobileMenuOpen ? 'translate-x-0 shadow-2xl' : '-translate-x-full lg:translate-x-0'
        }`}
        aria-label="Navigation Sidebar"
      >
        {/* Brand Header */}
        <div className="p-5 border-b border-neutral-800/80 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-neutral-700/20 border border-neutral-400/40 flex items-center justify-center text-neutral-200 font-bold text-sm shadow-[0_0_12px_rgba(123,123,123,0.25)]">
              PR
            </div>
            <div>
              <h1 className="text-sm font-semibold tracking-tight text-white flex items-center gap-1.5">
                Reconciliation
                <span className="text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-neutral-900/60 text-neutral-200 border border-neutral-500/50">
                  D7
                </span>
              </h1>
              <p className="text-[11px] text-neutral-400 font-mono tracking-tight">You can prove.</p>
            </div>
          </div>
          <button
            onClick={() => setMobileMenuOpen(false)}
            className="p-1 rounded-md text-neutral-400 hover:text-white hover:bg-neutral-800 lg:hidden"
            aria-label="Close navigation menu"
          >
            <X size={18} />
          </button>
        </div>

        {/* Tenant Selector */}
        <div className="px-4 py-3.5 border-b border-neutral-800/60 bg-black/40">
          <label className="text-[10px] uppercase font-bold tracking-wider text-neutral-400 block mb-1.5 flex items-center gap-1">
            <Building2 size={11} />
            Active Tenant
          </label>
          <select
            value={tenantId}
            onChange={(e) => onTenantChange(e.target.value)}
            className="select-input text-xs py-1.5 bg-neutral-900 border-neutral-700 text-neutral-200 rounded font-medium"
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
                onClick={() => {
                  onSelectTab(item.id);
                  setMobileMenuOpen(false);
                }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-md text-xs font-medium transition-all ${
                  active
                    ? 'bg-neutral-700/15 text-neutral-200 border border-neutral-400/30 shadow-sm'
                    : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/50 border border-transparent'
                }`}
              >
                <Icon size={16} className={active ? 'text-neutral-200' : 'text-neutral-400'} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        {/* Quick Demo Seeder Footer */}
        <div className="p-4 border-t border-neutral-800/80 bg-black/60 space-y-3">
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-neutral-400 flex items-center gap-1.5">
              <Server size={12} />
              Backend:
            </span>
            <span
              className={`inline-flex items-center gap-1 font-mono text-[10px] ${
                healthStatus === 'ok' ? 'text-neutral-200' : healthStatus === 'checking' ? 'text-neutral-200' : 'text-neutral-200'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  healthStatus === 'ok' ? 'bg-neutral-300' : healthStatus === 'checking' ? 'bg-neutral-300 animate-pulse' : 'bg-neutral-300'
                }`}
              />
              {healthStatus === 'ok' ? 'ONLINE' : healthStatus === 'checking' ? 'CONNECTING' : 'OFFLINE'}
            </span>
          </div>

          <button
            onClick={onSeedDemo}
            disabled={isSeeding}
            className="w-full btn btn-secondary text-xs py-2 border-neutral-700/80 bg-neutral-900 hover:bg-neutral-800 text-neutral-200 flex items-center justify-center gap-2"
          >
            <Zap size={13} className={isSeeding ? 'animate-spin text-neutral-200' : 'text-neutral-200'} />
            <span>{isSeeding ? 'Seeding Scenarios...' : 'Reseed Demo Data'}</span>
          </button>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 w-full lg:ml-64 min-w-0 flex flex-col min-h-screen">
        {/* Top Header */}
        <header className="h-16 border-b border-neutral-800/80 bg-[#0d0d0d]/90 backdrop-blur-md px-4 sm:px-6 lg:px-8 flex items-center justify-between sticky top-0 z-20">
          <div className="flex items-center gap-2.5 sm:gap-3 min-w-0">
            <button
              onClick={() => setMobileMenuOpen(true)}
              className="p-1.5 -ml-1 rounded-md text-neutral-400 hover:text-white hover:bg-neutral-800 lg:hidden"
              aria-label="Open navigation menu"
            >
              <Menu size={20} />
            </button>
            <span className="text-xs font-semibold text-neutral-400 uppercase tracking-wider truncate">
              {currentTab}
            </span>
            <span className="text-neutral-400 hidden sm:inline">/</span>
            <span className="text-xs font-mono px-2 py-0.5 rounded bg-neutral-800 text-neutral-300 border border-neutral-700 truncate max-w-[130px] sm:max-w-none">
              tenant: {tenantId}
            </span>
          </div>

          <div className="flex items-center gap-2 sm:gap-4 shrink-0">
            <span className="hidden md:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-neutral-700/10 text-neutral-200 border border-neutral-400/20 text-xs font-medium font-mono">
              <ShieldCheck size={13} />
              Zero-Mutations Verified
            </span>
            <span className="hidden xl:inline text-xs text-neutral-400 font-mono">
              Human-in-the-Loop Mode
            </span>
            <button
              onClick={onLogout}
              title="Log out and purge credential from memory"
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium text-neutral-400 hover:text-white bg-neutral-800/80 hover:bg-neutral-700/80 border border-neutral-700 transition-colors"
            >
              <LogOut size={13} />
              <span className="hidden sm:inline">Log Out</span>
            </button>
          </div>
        </header>

        {/* Page Content */}
        <main className="flex-1 p-4 sm:p-6 lg:p-8 overflow-y-auto min-w-0">
          {children}
        </main>
      </div>
    </div>
  );
};
