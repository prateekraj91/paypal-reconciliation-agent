/**
 * Typed HTTP Client for the Fintech Operator Dashboard.
 */

import {
  DashboardSummary,
  TransactionItem,
  TransactionDetail,
  InvoiceItem,
  InvoiceDetail,
  ProposalItem,
  ProposalDetail,
  ReconciliationRunItem,
  InvestigationDetail,
  AuditEventItem,
  AuditVerifyResponse,
} from '../types/api';

const API_BASE_URL = import.meta.env.VITE_API_URL || '';

class ApiClient {
  private tenantId: string = 'demo_merchant';
  private apiKey: string = '';
  private onUnauthorizedCallback?: () => void;
  private inFlightRequests: Map<string, Promise<any>> = new Map();

  public setTenantId(tenantId: string) {
    this.tenantId = tenantId;
  }

  public getTenantId(): string {
    return this.tenantId;
  }

  public setApiKey(key: string) {
    this.apiKey = key.trim();
  }

  public clearApiKey() {
    this.apiKey = '';
    this.inFlightRequests.clear();
  }

  public hasApiKey(): boolean {
    return Boolean(this.apiKey && this.apiKey.length > 0);
  }

  public onUnauthorized(callback: () => void) {
    this.onUnauthorizedCallback = callback;
  }

  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    if (!this.hasApiKey()) {
      throw new Error(
        'Authentication required: No operator admin credential configured. Please sign in.'
      );
    }

    const method = (options.method || 'GET').toUpperCase();
    const isGet = method === 'GET';
    const cacheKey = isGet ? `${method}:${endpoint}:${this.apiKey}` : null;

    if (cacheKey && this.inFlightRequests.has(cacheKey)) {
      return this.inFlightRequests.get(cacheKey) as Promise<T>;
    }

    const execPromise = (async () => {
      const url = `${API_BASE_URL}${endpoint}`;
      const headers = new Headers(options.headers || {});
      headers.set('Authorization', `Bearer ${this.apiKey}`);
      if (!headers.has('Content-Type') && options.body) {
        headers.set('Content-Type', 'application/json');
      }

      const response = await fetch(url, {
        ...options,
        headers,
      });

      if (response.status === 401) {
        if (this.onUnauthorizedCallback) {
          this.onUnauthorizedCallback();
        }
        throw new Error(
          'Authentication failed (401 Unauthorized): Invalid or expired admin bearer credential.'
        );
      }

      if (!response.ok) {
        let errorMsg = `HTTP Error ${response.status} ${response.statusText}`;
        try {
          const errJson = await response.json();
          if (errJson && errJson.detail) {
            errorMsg = typeof errJson.detail === 'string' ? errJson.detail : JSON.stringify(errJson.detail);
          }
        } catch {
          // fallback to status text
        }
        throw new Error(errorMsg);
      }

      return response.json() as Promise<T>;
    })();

    if (cacheKey) {
      this.inFlightRequests.set(cacheKey, execPromise);
      execPromise.finally(() => {
        this.inFlightRequests.delete(cacheKey);
      });
    }

    return execPromise;
  }

  // Dashboard
  public async getDashboardSummary(tenantId: string = this.tenantId): Promise<DashboardSummary> {
    return this.request<DashboardSummary>(`/admin/dashboard/summary?tenant_id=${encodeURIComponent(tenantId)}`);
  }

  // Transactions
  public async listTransactions(params: {
    tenantId?: string;
    status?: string;
    currency?: string;
    search?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<{ transactions: TransactionItem[]; total_count: number }> {
    const t = params.tenantId || this.tenantId;
    const query = new URLSearchParams({ tenant_id: t });
    if (params.status) query.set('status', params.status);
    if (params.currency) query.set('currency', params.currency);
    if (params.search) query.set('search', params.search);
    if (params.limit) query.set('limit', params.limit.toString());
    if (params.offset) query.set('offset', params.offset.toString());
    return this.request(`/admin/transactions?${query.toString()}`);
  }

  public async getTransaction(paymentId: string, tenantId: string = this.tenantId): Promise<TransactionDetail> {
    return this.request<TransactionDetail>(
      `/admin/transactions/${encodeURIComponent(paymentId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
  }

  // Invoices
  public async listInvoices(params: {
    tenantId?: string;
    status?: string;
    currency?: string;
    search?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<{ invoices: InvoiceItem[]; total_count: number }> {
    const t = params.tenantId || this.tenantId;
    const query = new URLSearchParams({ tenant_id: t });
    if (params.status) query.set('status', params.status);
    if (params.currency) query.set('currency', params.currency);
    if (params.search) query.set('search', params.search);
    if (params.limit) query.set('limit', params.limit.toString());
    if (params.offset) query.set('offset', params.offset.toString());
    return this.request(`/admin/invoices?${query.toString()}`);
  }

  public async getInvoice(invoiceId: string, tenantId: string = this.tenantId): Promise<InvoiceDetail> {
    return this.request<InvoiceDetail>(
      `/admin/invoices/${encodeURIComponent(invoiceId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
  }

  // Proposals
  public async listProposals(params: {
    tenantId?: string;
    status?: string;
    runId?: string;
    paymentId?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<{ proposals: ProposalItem[]; total_count: number }> {
    const t = params.tenantId || this.tenantId;
    const query = new URLSearchParams({ tenant_id: t });
    if (params.status) query.set('status', params.status);
    if (params.runId) query.set('run_id', params.runId);
    if (params.paymentId) query.set('payment_id', params.paymentId);
    if (params.limit) query.set('limit', params.limit.toString());
    if (params.offset) query.set('offset', params.offset.toString());
    return this.request(`/admin/proposals?${query.toString()}`);
  }

  public async getProposal(proposalId: string, tenantId: string = this.tenantId): Promise<ProposalDetail> {
    return this.request<ProposalDetail>(
      `/admin/proposals/${encodeURIComponent(proposalId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
  }

  public async approveProposal(
    proposalId: string,
    reviewer: string,
    notes?: string,
    tenantId: string = this.tenantId
  ): Promise<{ status: string; decision_id: string; allocation_ids: string[]; audit_event_id: string }> {
    return this.request(`/admin/proposals/${encodeURIComponent(proposalId)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ reviewer, notes, tenant_id: tenantId }),
    });
  }

  public async rejectProposal(
    proposalId: string,
    reviewer: string,
    reason: string,
    tenantId: string = this.tenantId
  ): Promise<{ status: string; decision_id: string; proposal_id: string; reason: string }> {
    return this.request(`/admin/proposals/${encodeURIComponent(proposalId)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reviewer, reason, tenant_id: tenantId }),
    });
  }

  // Runs
  public async listRuns(tenantId: string = this.tenantId): Promise<{ runs: ReconciliationRunItem[] }> {
    return this.request(`/admin/runs?tenant_id=${encodeURIComponent(tenantId)}`);
  }

  public async triggerRun(
    matcherName: string = 'deterministic_v2',
    tenantId: string = this.tenantId
  ): Promise<ReconciliationRunItem> {
    return this.request(`/admin/runs`, {
      method: 'POST',
      body: JSON.stringify({ matcher_name: matcherName, tenant_id: tenantId }),
    });
  }

  public async getRun(runId: string, tenantId: string = this.tenantId): Promise<ReconciliationRunItem> {
    return this.request<ReconciliationRunItem>(
      `/admin/runs/${encodeURIComponent(runId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
  }

  // Investigations
  public async listInvestigations(params: {
    tenantId?: string;
    status?: string;
    targetType?: string;
    limit?: number;
  } = {}): Promise<{ investigations: InvestigationDetail[] }> {
    const t = params.tenantId || this.tenantId;
    const query = new URLSearchParams({ tenant_id: t });
    if (params.status) query.set('status', params.status);
    if (params.targetType) query.set('target_type', params.targetType);
    if (params.limit) query.set('limit', params.limit.toString());
    return this.request(`/admin/investigations?${query.toString()}`);
  }

  public async getInvestigation(investigationId: string, tenantId: string = this.tenantId): Promise<InvestigationDetail> {
    return this.request<InvestigationDetail>(
      `/admin/investigations/${encodeURIComponent(investigationId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
  }

  public async submitInvestigation(
    targetType: string,
    targetId: string,
    syncMode: boolean = false,
    tenantId: string = this.tenantId
  ): Promise<any> {
    return this.request(`/admin/investigations`, {
      method: 'POST',
      body: JSON.stringify({ target_type: targetType, target_id: targetId, sync_mode: syncMode, tenant_id: tenantId }),
    });
  }

  public async approveInvestigation(
    investigationId: string,
    reviewer: string,
    notes?: string,
    tenantId: string = this.tenantId
  ): Promise<{ status: string; investigation_id: string; allocation_ids: string[]; audit_event_id: string }> {
    return this.request(`/admin/investigations/${encodeURIComponent(investigationId)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ reviewer, notes, tenant_id: tenantId }),
    });
  }

  public async rejectInvestigation(
    investigationId: string,
    reviewer: string,
    notes?: string,
    tenantId: string = this.tenantId
  ): Promise<{ status: string; investigation_id: string; audit_event_id: string }> {
    return this.request(`/admin/investigations/${encodeURIComponent(investigationId)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reviewer, notes, tenant_id: tenantId }),
    });
  }

  // Audit Events
  public async listAuditEvents(params: {
    tenantId?: string;
    subjectType?: string;
    subjectId?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<{ audit_events: AuditEventItem[]; total_count: number }> {
    const t = params.tenantId || this.tenantId;
    const query = new URLSearchParams({ tenant_id: t });
    if (params.subjectType) query.set('subject_type', params.subjectType);
    if (params.subjectId) query.set('subject_id', params.subjectId);
    if (params.limit) query.set('limit', params.limit.toString());
    if (params.offset) query.set('offset', params.offset.toString());
    return this.request(`/admin/audit-events?${query.toString()}`);
  }

  public async verifyAuditTrail(tenantId: string = this.tenantId): Promise<AuditVerifyResponse> {
    return this.request<AuditVerifyResponse>(`/admin/audit-events/verify?tenant_id=${encodeURIComponent(tenantId)}`);
  }

  // Demo Seed
  public async seedDemoData(force: boolean = false, tenantId: string = this.tenantId): Promise<any> {
    return this.request(`/admin/demo/seed`, {
      method: 'POST',
      body: JSON.stringify({ force, tenant_id: tenantId }),
    });
  }

  // Health / Readiness (Public endpoint)
  public async checkHealth(): Promise<{ status: string; service: string }> {
    const cacheKey = 'HEALTH_CHECK';
    if (this.inFlightRequests.has(cacheKey)) {
      return this.inFlightRequests.get(cacheKey)!;
    }
    const healthPromise = (async () => {
      const url = `${API_BASE_URL}/health`;
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`Health check failed with status ${response.status}`);
      }
      return response.json();
    })();

    this.inFlightRequests.set(cacheKey, healthPromise);
    healthPromise.finally(() => {
      this.inFlightRequests.delete(cacheKey);
    });
    return healthPromise;
  }
}

export const api = new ApiClient();
