import React, { useState } from 'react';
import { Layout, NavigationTab } from './components/Layout';
import { OverviewPage } from './pages/OverviewPage';
import { ReconciliationPage } from './pages/ReconciliationPage';
import { TransactionsPage } from './pages/TransactionsPage';
import { InvoicesPage } from './pages/InvoicesPage';
import { ProposalsPage } from './pages/ProposalsPage';
import { InvestigationsPage } from './pages/InvestigationsPage';
import { InvestigationDetailPage } from './pages/InvestigationDetailPage';
import { AuditTrailPage } from './pages/AuditTrailPage';
import { api } from './api/client';

export function App() {
  const [currentTab, setCurrentTab] = useState<NavigationTab>('overview');
  const [tenantId, setTenantId] = useState<string>('demo_merchant');
  const [selectedInvestigationId, setSelectedInvestigationId] = useState<string | null>(null);
  const [initialInvestigationTargetId, setInitialInvestigationTargetId] = useState<string | undefined>(undefined);
  const [isSeeding, setIsSeeding] = useState<boolean>(false);
  const [notification, setNotification] = useState<{ message: string; type: 'success' | 'error' } | null>(null);

  const showNotification = (message: string, type: 'success' | 'error' = 'success') => {
    setNotification({ message, type });
    setTimeout(() => {
      setNotification(null);
    }, 4000);
  };

  const handleTenantChange = (newTenant: string) => {
    setTenantId(newTenant);
    api.setTenantId(newTenant);
    setSelectedInvestigationId(null);
    setInitialInvestigationTargetId(undefined);
  };

  const handleSelectTab = (tab: NavigationTab) => {
    setCurrentTab(tab);
    if (tab !== 'investigations') {
      setSelectedInvestigationId(null);
      setInitialInvestigationTargetId(undefined);
    }
  };

  const handleStartInvestigation = (targetId: string) => {
    setInitialInvestigationTargetId(targetId);
    setSelectedInvestigationId(null);
    setCurrentTab('investigations');
  };

  const handleInspectInvestigation = (invId: string) => {
    setSelectedInvestigationId(invId);
    setCurrentTab('investigations');
  };

  const handleSeedDemo = async () => {
    setIsSeeding(true);
    try {
      const res = await api.seedDemoData(true, tenantId);
      showNotification(
        `Demo data reseeded: ${res.seeded.payments} payments, ${res.seeded.invoices} invoices, ${res.seeded.investigations} investigations.`,
        'success'
      );
      // Trigger a light refresh by re-setting currentTab
      setCurrentTab((prev) => prev);
    } catch (err: any) {
      console.error('Failed to seed demo data:', err);
      showNotification(`Failed to seed demo data: ${err.message || 'Server error'}`, 'error');
    } finally {
      setIsSeeding(false);
    }
  };

  return (
    <Layout
      currentTab={currentTab}
      onSelectTab={handleSelectTab}
      tenantId={tenantId}
      onTenantChange={handleTenantChange}
      onSeedDemo={handleSeedDemo}
      isSeeding={isSeeding}
    >
      {/* Toast Notification */}
      {notification && (
        <div
          className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-xl border text-sm font-medium transition-all ${
            notification.type === 'success'
              ? 'bg-emerald-950 border-emerald-700 text-emerald-200 shadow-emerald-950/40'
              : 'bg-rose-950 border-rose-700 text-rose-200 shadow-rose-950/40'
          }`}
        >
          {notification.message}
        </div>
      )}

      {/* Tab Renderers */}
      {currentTab === 'overview' && (
        <OverviewPage
          tenantId={tenantId}
          onNavigate={handleSelectTab}
          onStartInvestigation={handleStartInvestigation}
        />
      )}

      {currentTab === 'reconciliation' && (
        <ReconciliationPage tenantId={tenantId} />
      )}

      {currentTab === 'transactions' && (
        <TransactionsPage
          tenantId={tenantId}
          onStartInvestigation={handleStartInvestigation}
          onInspectInvestigation={handleInspectInvestigation}
        />
      )}

      {currentTab === 'invoices' && (
        <InvoicesPage tenantId={tenantId} />
      )}

      {currentTab === 'proposals' && (
        <ProposalsPage tenantId={tenantId} />
      )}

      {currentTab === 'investigations' && (
        selectedInvestigationId ? (
          <InvestigationDetailPage
            investigationId={selectedInvestigationId}
            tenantId={tenantId}
            onBack={() => setSelectedInvestigationId(null)}
          />
        ) : (
          <InvestigationsPage
            tenantId={tenantId}
            onSelectInvestigation={handleInspectInvestigation}
            initialTargetId={initialInvestigationTargetId}
          />
        )
      )}

      {currentTab === 'audit' && (
        <AuditTrailPage tenantId={tenantId} />
      )}
    </Layout>
  );
}

export default App;
