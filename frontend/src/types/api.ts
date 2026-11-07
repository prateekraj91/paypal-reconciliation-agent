/**
 * TypeScript API models for PayPal Reconciliation Agent Frontend.
 */

export type ReconciliationStatus = 'RECONCILED' | 'UNMATCHED' | 'PARTIALLY_RECONCILED';
export type DecisionStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'ESCALATED';
export type VerifierStatus = 'NOT_VERIFIED' | 'VERIFIED' | 'NEEDS_REVIEW' | 'INVALID' | 'UNVERIFIED';

export interface DashboardSummary {
  tenant_id: string;
  counts: {
    total_payments: number;
    reconciled_payments: number;
    unmatched_payments: number;
    partially_reconciled_payments: number;
    open_exceptions: number;
    total_invoices: number;
    total_proposals: number;
    pending_proposals: number;
    approved_proposals: number;
    rejected_proposals: number;
    total_investigations: number;
    investigations_needs_review: number;
    investigations_completed: number;
    investigations_invalid: number;
  };
  financial_totals: Record<
    string,
    {
      currency: string;
      payments_amount_minor: number;
      invoices_amount_minor: number;
      reconciled_amount_minor: number;
      unapplied_cash_minor: number;
    }
  >;
  recent_runs: Array<{
    id: string;
    matcher_name: string;
    matcher_version: string;
    summary_metrics: Record<string, any>;
    run_at: string | null;
  }>;
  recent_exceptions: Array<{
    id: string;
    provider_payment_id: string;
    amount_minor: number;
    currency: string;
    payer_name: string;
    unapplied_cash_minor: number;
    received_at: string | null;
    status: string;
  }>;
  recent_investigations: Array<{
    id: string;
    target_type: string;
    target_id: string;
    status: string;
    classification: string;
    verifier_status: VerifierStatus;
    created_at: string | null;
  }>;
}

export interface TransactionItem {
  id: string;
  provider: string;
  provider_payment_id: string;
  amount_minor: number;
  currency: string;
  status: string;
  payer_name: string;
  payer_email: string | null;
  reference: string | null;
  note: string;
  received_at: string | null;
  unapplied_cash_minor: number;
  allocated_minor: number;
  reconciliation_status: ReconciliationStatus;
}

export interface TransactionDetail extends TransactionItem {
  allocations: Array<{
    id: string;
    invoice_id: string;
    invoice_number: string;
    customer_name: string;
    amount_minor: number;
    currency: string;
    actor: string;
    allocated_at: string | null;
  }>;
  settlements: Array<{
    id: string;
    provider_settlement_id: string;
    invoice_id: string;
    invoice_number: string;
    amount_minor: number;
    currency: string;
    settled_at: string | null;
  }>;
  refunds: Array<{
    id: string;
    provider_refund_id: string;
    amount_minor: number;
    currency: string;
    status: string;
    record_type: string;
    refunded_at: string | null;
  }>;
  proposals: Array<{
    id: string;
    run_id: string;
    outcome: string;
    verifier_status: string;
    decision_status: DecisionStatus;
    allocations: Array<{
      invoice_id: string;
      invoice_number: string;
      amount_minor: number;
      currency: string;
    }>;
    excess_amount_minor: number;
    created_at: string | null;
  }>;
  investigations: Array<{
    id: string;
    status: string;
    classification: string;
    summary: string;
    verifier_status: VerifierStatus;
    created_at: string | null;
  }>;
  audit_events: Array<{
    sequence_number: number;
    event_type: string;
    actor: string;
    timestamp: string | null;
    event_hash: string;
  }>;
}

export interface InvoiceItem {
  id: string;
  provider: string;
  provider_invoice_id: string;
  invoice_number: string;
  total_minor: number;
  currency: string;
  provider_due_minor: number | null;
  status: string;
  customer_name: string;
  customer_email: string | null;
  issued_on: string | null;
  due_on: string | null;
  memo: string;
  allocated_minor: number;
  settled_minor: number;
  outstanding_balance_minor: number;
  reconciliation_status: 'FULLY_RECONCILED' | 'PARTIAL' | 'OPEN';
}

export interface InvoiceDetail extends InvoiceItem {
  allocations: Array<{
    id: string;
    payment_id: string;
    provider_payment_id: string;
    payer_name: string;
    amount_minor: number;
    currency: string;
    actor: string;
    allocated_at: string | null;
  }>;
  settlements: Array<{
    id: string;
    provider_settlement_id: string;
    amount_minor: number;
    currency: string;
    settled_at: string | null;
  }>;
}

export interface ProposalItem {
  id: string;
  run_id: string;
  payment_id: string;
  provider_payment_id: string;
  payer_name: string;
  payment_amount_minor: number;
  payment_currency: string;
  outcome: string;
  verifier_status: string;
  decision_status: DecisionStatus;
  allocations: Array<{
    invoice_id: string;
    invoice_number: string;
    amount_minor: number;
    currency: string;
  }>;
  excess_amount_minor: number;
  created_at: string | null;
}

export interface ProposalDetail extends ProposalItem {
  tenant_id: string;
  allocations: Array<{
    id: string;
    invoice_id: string;
    invoice_number: string;
    customer_name: string;
    amount_minor: number;
    currency: string;
  }>;
  verifications: Array<{
    id: string;
    check_name: string;
    passed: boolean;
    failure_reason: string | null;
    verified_at: string | null;
  }>;
  decisions: Array<{
    id: string;
    decision_type: string;
    actor: string;
    reason: string;
    decided_at: string | null;
  }>;
}

export interface ReconciliationRunItem {
  id: string;
  tenant_id: string;
  matcher_name: string;
  matcher_version: string;
  status: string;
  metrics: Record<string, any>;
  run_at: string | null;
  proposals?: ProposalItem[];
  total_proposals?: number;
}

export interface VerifiedFact {
  statement: string;
  evidence_ids: string[];
  amount_cents?: number | null;
  currency?: string | null;
}

export interface EvidenceReference {
  evidence_id: string;
  source_type: string;
  source_id: string;
  tenant_id: string;
  description: string;
  amount_cents?: number | null;
  currency?: string | null;
  metadata?: Record<string, any>;
}

export interface CandidateAnalysis {
  invoice_id: string;
  invoice_number: string;
  customer_match: boolean;
  amount_match: boolean;
  score?: number | null;
  assessment: string;
}

export interface Hypothesis {
  hypothesis_id: string;
  statement: string;
  likelihood: 'HIGH' | 'MEDIUM' | 'LOW';
  supporting_evidence_ids: string[];
  counter_evidence_ids?: string[];
}

export interface MissingEvidenceItem {
  missing_item: string;
  importance: 'CRITICAL' | 'DESIRABLE' | 'OPTIONAL';
  why_needed: string;
}

export interface RecommendedStep {
  step_number: number;
  action: string;
  target: string;
  reason: string;
}

export interface SuggestedResolution {
  resolution_type: string;
  suggested_invoice_ids: string[];
  suggested_amount_cents?: number | null;
  suggested_allocations?: Record<string, number> | null;
  notes: string;
}

export interface InvestigationDetail {
  id: string;
  tenant_id: string;
  target_type: string;
  target_id: string;
  status: 'QUEUED' | 'RUNNING' | 'NEEDS_REVIEW' | 'COMPLETED' | 'FAILED' | 'INVALID' | 'REJECTED' | 'APPROVED';
  classification: string;
  summary: string;
  verifier_status: VerifierStatus;
  verifier_details?: {
    status?: string;
    passed?: boolean;
    notes?: string[];
    issues?: string[];
    verified_evidence_ids?: string[];
    checked_invariants?: Record<string, boolean>;
    summary?: string;
  } | null;
  evidence_references?: EvidenceReference[];
  result?: {
    investigation_id?: string;
    classification?: string;
    executive_summary?: string;
    verified_facts?: VerifiedFact[];
    supporting_evidence?: EvidenceReference[];
    candidate_invoice_analysis?: CandidateAnalysis[];
    conflicting_evidence?: Array<{ issue: string; evidence_ids: string[] }>;
    hypotheses?: Hypothesis[];
    missing_evidence?: MissingEvidenceItem[];
    recommended_steps?: RecommendedStep[];
    suggested_resolution?: SuggestedResolution | null;
    risk_flags?: string[];
    requires_human_review?: boolean;
    verifier_status?: VerifierStatus;
    verifier_notes?: string[];
    model_info?: Record<string, any>;
  } | null;
  model_provider?: string | null;
  model_name?: string | null;
  error_message?: string | null;
  created_at: string | null;
  completed_at: string | null;
}

export interface AuditEventItem {
  id: string;
  sequence_number: number;
  event_id: string;
  event_type: string;
  actor: string;
  subject_type: string;
  subject_id: string;
  payload: Record<string, any>;
  prev_hash: string;
  event_hash: string;
  timestamp: string | null;
}

export interface AuditVerifyResponse {
  verified: boolean;
  tenant_id: string;
  total_events: number;
  algorithm?: string;
  genesis_hash?: string;
  latest_hash?: string;
  message?: string;
  error?: string;
  details?: string[];
}
