import React, { useState, useEffect } from 'react';
import { Layout, NavigationTab } from './components/Layout';
import { AuthGate } from './components/AuthGate';
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
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(() => api.hasApiKey());
  const [currentTab, setCurrentTab] = useState<NavigationTab>('overview');
  const [tenantId, setTenantId] = useState<string>('demo_merchant');
  const [selectedInvestigationId, setSelectedInvestigationId] = useState<string | null>(null);
  const [initialInvestigationTargetId, setInitialInvestigationTargetId] = useState<string | undefined>(undefined);
  const [isSeeding, setIsSeeding] = useState<boolean>(false);
  const [notification, setNotification] = useState<{ message: string; type: 'success' | 'error' } | null>(null);

  useEffect(() => {
    // Listen for 401 unauthorized errors across all background requests
    api.onUnauthorized(() => {
      setIsAuthenticated(false);
      showNotification('Session expired or unauthorized (401). Please re-authenticate.', 'error');
    });
  }, []);

  const showNotification = (message: string, type: 'success' | 'error' = 'success') => {
    setNotification({ message, type });
    setTimeout(() => {
      setNotification(null);
    }, 4500);
  };

  const handleLogout = () => {
    api.clearApiKey();
    setIsAuthenticated(false);
    setSelectedInvestigationId(null);
    setInitialInvestigationTargetId(undefined);
    showNotification('Logged out: Bearer credential cleared from volatile memory.', 'success');
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
        `Demo data reseeded: ${res.payments_count || res.invoices_count || 6} records refreshed.`,
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

  if (!isAuthenticated) {
    return (
      <>
        {notification && (
          <div
            className={`fixed top-6 right-6 z-50 px-4 py-3 rounded-xl shadow-xl border text-sm font-medium transition-all ${
              notification.type === 'success'
                ? 'bg-neutral-900 border-neutral-500 text-neutral-100 shadow-neutral-700/40'
                : 'bg-neutral-900 border-neutral-500 text-neutral-100 shadow-neutral-700/40'
            }`}
          >
            {notification.message}
          </div>
        )}
        <AuthGate
          onAuthenticated={() => setIsAuthenticated(true)}
          tenantId={tenantId}
        />
      </>
    );
  }

  return (
    <Layout
      currentTab={currentTab}
      onSelectTab={handleSelectTab}
      tenantId={tenantId}
      onTenantChange={handleTenantChange}
      onSeedDemo={handleSeedDemo}
      isSeeding={isSeeding}
      onLogout={handleLogout}
    >
      {/* Toast Notification */}
      {notification && (
        <div
          className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-xl border text-sm font-medium transition-all ${
            notification.type === 'success'
              ? 'bg-neutral-900 border-neutral-500 text-neutral-100 shadow-neutral-700/40'
              : 'bg-neutral-900 border-neutral-500 text-neutral-100 shadow-neutral-700/40'
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
