"""FastAPI application for the PayPal Reconciliation Agent.

Provides:
- Public health and readiness probes
- Verified PayPal webhook ingestion endpoint
- Authenticated operator admin endpoints (sync, runs, jobs)
"""

from __future__ import annotations

from datetime import datetime, timezone, date
import json
import logging
import os
import uuid
from typing import Any, Optional, List, Dict
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text, select, or_, and_, func, desc
from sqlalchemy.orm import Session

from reconcile.paypal import PayPalAPIError, PayPalClient, PayPalTimeoutError
from reconcile_store.repositories.tenant_repo import TenantRepository, ConnectionRepository
from reconcile_store.repositories.webhook_repo import (
    WebhookInboxRepository,
    InvalidWebhookStateTransitionError,
)
from reconcile_store.repositories.job_repo import JobRepository
from reconcile_store.repositories.reconciliation_repo import (
    ReconciliationRunRepository,
    ProposalRepository,
    VerificationRepository,
    DecisionRepository,
)
from reconcile_store.repositories.payment_repo import PaymentRepository, IneligiblePaymentError
from reconcile_store.repositories.invoice_repo import InvoiceRepository, IneligibleInvoiceError
from reconcile_store.repositories.audit_repo import AuditRepository
from reconcile_store.repositories.allocation_repo import AllocationRepository
from reconcile_store.repositories.settlement_repo import ProviderSettlementRepository
from reconcile_store.repositories.refund_repo import RefundRepository
from reconcile_store.repositories.investigation_repo import InvestigationRepository
from reconcile_store.schema import (
    PaymentRecord,
    InvoiceRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    RefundRecord,
    VerificationRecord,
    DecisionRecord,
    AuditEventRecord,
    InvestigationRecord,
)
from reconcile_store.audit import GENESIS_HASH, AuditIntegrityError
from reconcile_store.approval import ApprovalService, VerificationFailedError
from reconcile_store.workflow import StorageWorkflow
from reconcile_store.repositories.base import ConflictError, StaleVersionError, EntityNotFoundError
from reconcile_api.demo_data import seed_demo_data

from contextlib import asynccontextmanager
from reconcile_store.bootstrap import bootstrap_database

from .config import APIConfig
from .dependencies import get_db_engine, get_db_session, get_paypal_client
from .security import require_admin_auth

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Idempotently bootstrap default tenant and provider connection on startup."""
    try:
        engine = get_db_engine()
        with engine.session() as session:
            bootstrap_database(session)
    except Exception as exc:
        logger.warning("Database bootstrap during startup skipped or deferred: %s", exc)
    yield


app = FastAPI(
    title="PayPal Reconciliation Agent API",
    version="0.2.0",
    description="Fintech Reconciliation Agent API & Operator Dashboard Backend",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REQUIRED_PAYPAL_HEADERS = (
    "PAYPAL-TRANSMISSION-ID",
    "PAYPAL-TRANSMISSION-TIME",
    "PAYPAL-CERT-URL",
    "PAYPAL-AUTH-ALGO",
    "PAYPAL-TRANSMISSION-SIG",
)


# --------------------------------------------------------------------------
# Request / Response Models
# --------------------------------------------------------------------------


class SyncRequest(BaseModel):
    tenant_id: Optional[str] = Field(default=None, description="Existing tenant ID")
    start_date: Optional[str] = Field(default=None, description="ISO 8601 start timestamp")
    end_date: Optional[str] = Field(default=None, description="ISO 8601 end timestamp")
    environment: Optional[str] = Field(default="sandbox", description="Provider environment ('sandbox' or 'live')")
    page_size: Optional[int] = Field(default=None, ge=1, le=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            clean = v.strip()
            if clean.lower() == "string" or not clean:
                raise ValueError("tenant_id must be a valid tenant identifier, not placeholder 'string'")
            return clean
        return None

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: Optional[str]) -> str:
        if v is None:
            return "sandbox"
        clean = v.strip().lower()
        if clean not in ("sandbox", "live"):
            raise ValueError(f"environment must be 'sandbox' or 'live', got '{v}'")
        return clean

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_iso_date(cls, v: Optional[str], info) -> Optional[str]:
        if v is None:
            return None
        clean = v.strip()
        if clean.lower() == "string" or not clean:
            raise ValueError(f"{info.field_name} cannot be placeholder '{v}'")
        try:
            dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception as exc:
            raise ValueError(
                f"Invalid ISO 8601 date format for {info.field_name}: '{v}'. "
                "Expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"
            ) from exc

    @model_validator(mode="after")
    def validate_date_range(self) -> SyncRequest:
        if self.start_date and self.end_date:
            dt_start = datetime.fromisoformat(self.start_date)
            dt_end = datetime.fromisoformat(self.end_date)
            if dt_start > dt_end:
                raise ValueError(
                    f"start_date ({self.start_date}) cannot be after end_date ({self.end_date})"
                )
        return self


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=64, description="Target tenant ID")
    name: Optional[str] = Field(default=None, max_length=255, description="Human-readable tenant name")

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        clean = v.strip()
        if clean.lower() == "string" or not clean:
            raise ValueError("tenant_id must be a valid tenant identifier, not placeholder 'string'")
        return clean


class ConnectionProvisionRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=64, description="Target tenant ID")
    provider: str = Field(default="paypal", max_length=32, description="Provider identifier ('paypal')")
    environment: str = Field(default="sandbox", max_length=32, description="Environment ('sandbox' or 'live')")
    credential_ref: str = Field(default="env:PAYPAL_SANDBOX", max_length=128, description="Indirect credential reference")
    status: str = Field(default="ACTIVE", max_length=32, description="Connection status ('ACTIVE', 'INACTIVE', 'DISABLED')")

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        clean = v.strip()
        if clean.lower() == "string" or not clean:
            raise ValueError("tenant_id must be a valid tenant identifier, not placeholder 'string'")
        return clean

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        clean = v.strip().lower()
        if clean != "paypal":
            raise ValueError(f"Unsupported provider '{v}'. Only 'paypal' is currently supported.")
        return clean

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        clean = v.strip().lower()
        if clean not in ("sandbox", "live"):
            raise ValueError(f"environment must be 'sandbox' or 'live', got '{v}'")
        return clean

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        clean = v.strip().upper()
        if clean not in ("ACTIVE", "INACTIVE", "DISABLED"):
            raise ValueError(f"status must be one of 'ACTIVE', 'INACTIVE', 'DISABLED', got '{v}'")
        return clean

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, v: str) -> str:
        return ConnectionRepository.validate_credential_reference(v)


class InvestigationCreateRequest(BaseModel):
    tenant_id: Optional[str] = Field(default=None, description="Existing tenant ID")
    target_type: str = Field(..., description="Target type: 'payment', 'proposal', 'invoice'")
    target_id: str = Field(..., description="Target identifier (e.g. payment ID or invoice number)")
    sync_mode: Optional[bool] = Field(default=False, description="Whether to execute synchronously")

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        clean = v.strip().lower()
        if clean not in ("payment", "proposal", "invoice", "refund", "reversal"):
            raise ValueError(f"Invalid target_type '{v}'. Must be one of: payment, proposal, invoice, refund, reversal")
        return clean


class ProposalApproveRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=255, description="Name or identifier of operator approving proposal")
    notes: Optional[str] = Field(default=None, max_length=1000, description="Optional notes or audit context")
    tenant_id: Optional[str] = Field(default=None, description="Target tenant ID")


class ProposalRejectRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=255, description="Name or identifier of operator rejecting proposal")
    reason: str = Field(..., min_length=1, max_length=1000, description="Mandatory reason for proposal rejection")
    tenant_id: Optional[str] = Field(default=None, description="Target tenant ID")


class InvestigationDecisionRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=255, description="Name or identifier of operator making decision")
    notes: Optional[str] = Field(default=None, max_length=1000, description="Optional notes or explanation")
    tenant_id: Optional[str] = Field(default=None, description="Target tenant ID")


class RunTriggerRequest(BaseModel):
    tenant_id: Optional[str] = Field(default=None, description="Target tenant ID")
    matcher_name: Optional[str] = Field(default="deterministic_v2", max_length=64)
    matcher_version: Optional[str] = Field(default="2.0.0", max_length=32)


class DemoSeedRequest(BaseModel):
    tenant_id: Optional[str] = Field(default="demo_merchant", max_length=64)
    force: Optional[bool] = Field(default=False, description="Whether to reseed even if data already exists")





# --------------------------------------------------------------------------
# Health and Readiness Endpoints
# --------------------------------------------------------------------------


@app.get("/health", status_code=status.HTTP_200_OK)
def health() -> dict[str, str]:
    """Lightweight liveness probe.
    
    Must never expose credentials, database connection strings or secrets.
    """
    return {"status": "ok", "service": "paypal-reconciliation-agent"}


@app.get("/ready", status_code=status.HTTP_200_OK)
def readiness(response: Response) -> dict[str, str]:
    """Readiness probe checking essential connectivity (e.g. database).
    
    Returns 503 if database is unreachable, without leaking internal infrastructure details.
    """
    engine = get_db_engine()
    try:
        with engine.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception as exc:
        logger.error("Readiness check failed: database ping error", exc_info=False)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "disconnected"}


# --------------------------------------------------------------------------
# Webhook Endpoint (POST /webhooks/paypal)
# --------------------------------------------------------------------------


@app.post("/webhooks/paypal", status_code=status.HTTP_202_ACCEPTED)
async def paypal_webhook(
    request: Request,
    response: Response,
    session: Session = Depends(get_db_session),
    paypal_client: PayPalClient = Depends(get_paypal_client),
) -> dict[str, Any]:
    """Central D4 Webhook Ingestion Endpoint.

    1. Enforces body size limits.
    2. Validates mandatory PayPal transmission headers.
    3. Safely parses JSON without logging credentials.
    4. Records durable webhook receipt (idempotently).
    5. Verifies authenticity against PayPal's official signature verification API.
    6. Enqueues background job ONLY if authenticity is verified.
    """
    config = APIConfig.from_env()

    # 1. Enforce body size limit
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > config.max_request_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Payload exceeds maximum allowed size of {config.max_request_body_bytes} bytes",
        )

    body_bytes = await request.body()
    if len(body_bytes) > config.max_request_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Payload exceeds maximum allowed size of {config.max_request_body_bytes} bytes",
        )

    # 2. Extract and validate required transmission headers
    headers_map = {k.upper(): v for k, v in request.headers.items()}
    missing_headers = [h for h in REQUIRED_PAYPAL_HEADERS if h not in headers_map]
    if missing_headers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required PayPal transmission header(s): {', '.join(missing_headers)}",
        )

    transmission_id = headers_map["PAYPAL-TRANSMISSION-ID"]
    transmission_time = headers_map["PAYPAL-TRANSMISSION-TIME"]
    cert_url = headers_map["PAYPAL-CERT-URL"]
    auth_algo = headers_map["PAYPAL-AUTH-ALGO"]
    transmission_sig = headers_map["PAYPAL-TRANSMISSION-SIG"]

    # 3. Parse JSON safely
    try:
        raw_body_text = body_bytes.decode("utf-8")
        event = json.loads(raw_body_text)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON in request body",
        ) from exc

    if not isinstance(event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expected JSON object payload",
        )

    event_id = event.get("id")
    event_type = event.get("event_type")
    if not event_id or not event_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required event fields: 'id' and 'event_type'",
        )

    resource = event.get("resource") or {}
    resource_type = event.get("resource_type")
    resource_id = resource.get("id") if isinstance(resource, dict) else None

    # Resolve tenant safely (do NOT trust public JSON body for tenant isolation)
    tenant_id = config.default_tenant_id
    tenant_repo = TenantRepository(session)
    if not tenant_repo.get_tenant(tenant_id):
        tenant_repo.create_tenant(tenant_id, f"Default Tenant ({tenant_id})")

    # Safe subset of headers for metadata storage
    safe_headers = {
        h: headers_map.get(h)
        for h in (
            "PAYPAL-TRANSMISSION-ID",
            "PAYPAL-TRANSMISSION-TIME",
            "PAYPAL-CERT-URL",
            "PAYPAL-AUTH-ALGO",
            "USER-AGENT",
        )
        if h in headers_map
    }

    inbox_repo = WebhookInboxRepository(session)
    job_repo = JobRepository(session)
    environment = config.paypal_environment
    webhook_id = config.paypal_webhook_id

    # 4. Check for duplicate receipt
    existing = inbox_repo.get_by_event_id(
        tenant_id=tenant_id,
        provider="paypal",
        environment=environment,
        event_id=event_id,
    )

    if existing:
        if existing.verification_status == "VERIFIED":
            response.status_code = status.HTTP_200_OK
            return {
                "status": "already_received",
                "message": "Webhook event was previously verified and accepted",
                "event_id": event_id,
                "job_id": existing.job_id,
            }
        elif existing.verification_status == "REJECTED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Webhook event was previously rejected: {existing.failure_reason or 'Invalid signature'}",
            )

    # Record new receipt in VERIFICATION_PENDING state
    receipt = inbox_repo.record_receipt(
        tenant_id=tenant_id,
        provider="paypal",
        environment=environment,
        event_id=event_id,
        event_type=event_type,
        raw_payload=raw_body_text,
        headers=safe_headers,
        webhook_id=webhook_id,
        resource_type=resource_type,
        resource_id=resource_id,
        verification_status="VERIFICATION_PENDING",
    )
    session.flush()

    # 5. Verify authenticity with PayPal
    try:
        is_verified = paypal_client.verify_webhook_signature(
            transmission_id=transmission_id,
            transmission_time=transmission_time,
            cert_url=cert_url,
            auth_algo=auth_algo,
            transmission_sig=transmission_sig,
            webhook_id=webhook_id,
            webhook_event=event,
        )
    except (PayPalTimeoutError, PayPalAPIError) as exc:
        status_code = getattr(exc, "status_code", 0)
        logger.warning(
            "PayPal verification endpoint unavailable or timed out: %s",
            type(exc).__name__,
        )
        # Retain pending receipt so event is not lost and return 503 for PayPal retry
        receipt.failure_reason = f"Verification service unavailable: {type(exc).__name__}"
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PayPal verification service temporarily unavailable; retry requested",
        ) from exc
    except Exception as exc:
        logger.error("Unexpected error during webhook verification: %s", exc)
        receipt.failure_reason = f"Verification error: {type(exc).__name__}"
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Error verifying webhook signature",
        ) from exc

    if not is_verified:
        inbox_repo.update_verification_status(
            receipt.id,
            tenant_id,
            "REJECTED",
            failure_reason="PayPal signature verification returned FAILURE",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook signature verification failed",
        )

    # 6. Authenticity verified -> Enqueue background job
    inbox_repo.update_verification_status(receipt.id, tenant_id, "VERIFIED")

    job = job_repo.enqueue_job(
        tenant_id=tenant_id,
        job_type="WEBHOOK_INGEST",
        idempotency_key=f"webhook:{environment}:{event_id}",
        payload={
            "receipt_id": receipt.id,
            "event_id": event_id,
            "event_type": event_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "environment": environment,
        },
    )
    inbox_repo.link_job(receipt.id, tenant_id, job.id, processing_status="QUEUED")
    session.commit()

    return {
        "status": "accepted",
        "event_id": event_id,
        "job_id": job.id,
    }


# --------------------------------------------------------------------------
# Administrative Endpoints
# --------------------------------------------------------------------------


@app.post("/admin/tenants", status_code=status.HTTP_200_OK)
def create_or_get_tenant(
    request: TenantCreateRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Register or retrieve a tenant."""
    tenant_repo = TenantRepository(session)
    tenant = tenant_repo.get_or_create_tenant(
        request.tenant_id,
        request.name or f"Tenant ({request.tenant_id})",
    )
    session.commit()
    return {
        "status": "ready",
        "tenant_id": tenant.id,
        "name": tenant.name,
        "created_at": tenant.created_at.isoformat(),
    }


@app.get("/admin/tenants", status_code=status.HTTP_200_OK)
def list_tenants(
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List registered tenants."""
    tenant_repo = TenantRepository(session)
    tenants = tenant_repo.list_tenants()
    return {
        "tenants": [
            {
                "id": t.id,
                "name": t.name,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t in tenants
        ]
    }


@app.post("/admin/connections", status_code=status.HTTP_200_OK)
def provision_connection(
    request: ConnectionProvisionRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Provision or update a provider connection for an existing tenant.
    
    Adheres strictly to the credential-reference architecture: raw credentials,
    bearer tokens or secrets are rejected and never stored.
    """
    tenant_repo = TenantRepository(session)
    if not tenant_repo.get_tenant(request.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{request.tenant_id}' not found. Tenants must be registered prior to provisioning connections.",
        )

    conn_repo = ConnectionRepository(session)
    try:
        conn = conn_repo.upsert_connection(
            tenant_id=request.tenant_id,
            provider=request.provider,
            environment=request.environment,
            credential_reference=request.credential_ref,
            status=request.status,
        )
        session.commit()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return {
        "status": "provisioned",
        "id": conn.id,
        "tenant_id": conn.tenant_id,
        "provider": conn.provider,
        "environment": conn.environment,
        "credential_ref": conn.credential_ref,
        "connection_status": conn.status,
        "updated_at": conn.updated_at.isoformat(),
    }


@app.get("/admin/connections", status_code=status.HTTP_200_OK)
def list_connections(
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List registered connections."""
    conn_repo = ConnectionRepository(session)
    conns = conn_repo.list_connections(tenant_id)
    return {
        "connections": [
            {
                "id": c.id,
                "tenant_id": c.tenant_id,
                "provider": c.provider,
                "environment": c.environment,
                "credential_ref": c.credential_ref,
                "status": c.status,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in conns
        ]
    }


@app.post("/admin/sync", status_code=status.HTTP_202_ACCEPTED)
def trigger_sync(
    request: SyncRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Trigger a manual background synchronisation pass against PayPal Sandbox.
    
    Enforces that:
    1. Tenant must exist.
    2. Connection must exist and be ACTIVE for the target environment.
    3. Dates are valid ISO timestamps with start_date <= end_date.
    Enqueues a durable MANUAL_SYNC job and returns immediately.
    """
    config = APIConfig.from_env()
    tenant_id = request.tenant_id or config.default_tenant_id

    tenant_repo = TenantRepository(session)
    tenant = tenant_repo.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found. Tenants must be registered prior to synchronization.",
        )

    environment = request.environment or config.paypal_environment or "sandbox"
    conn_repo = ConnectionRepository(session)
    conn = conn_repo.get_connection(tenant_id, provider="paypal", environment=environment)
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No PayPal connection found for tenant '{tenant_id}' in environment '{environment}'. "
                "Please provision a connection via POST /admin/connections before synchronizing."
            ),
        )
    if conn.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PayPal connection for tenant '{tenant_id}' is '{conn.status}'. Synchronization requires an ACTIVE connection.",
        )

    job_repo = JobRepository(session)
    now_utc = datetime.now(timezone.utc)
    # Default idempotency key buckets by hour to avoid rapid duplicate sync bursts
    hour_bucket = now_utc.strftime("%Y%m%d%H")
    idempotency_key = request.idempotency_key or f"manual-sync:{tenant_id}:{environment}:{hour_bucket}"

    job = job_repo.enqueue_job(
        tenant_id=tenant_id,
        job_type="MANUAL_SYNC",
        idempotency_key=idempotency_key,
        payload={
            "tenant_id": tenant_id,
            "provider": "paypal",
            "environment": environment,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "page_size": request.page_size,
            "triggered_at": now_utc.isoformat(),
        },
    )
    session.commit()

    return {
        "status": "queued",
        "job_id": job.id,
        "idempotency_key": job.idempotency_key,
    }



@app.get("/admin/runs", status_code=status.HTTP_200_OK)
def list_runs(
    tenant_id: Optional[str] = None,
    limit: int = 50,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List persisted reconciliation runs and high-level status."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    run_repo = ReconciliationRunRepository(session)
    runs = list(run_repo.list_runs(effective_tenant))[:limit]

    return {
        "runs": [
            {
                "id": r.id,
                "tenant_id": r.tenant_id,
                "matcher_name": r.matcher_name,
                "matcher_version": r.matcher_version,
                "summary_metrics": getattr(r, "metrics", None) if getattr(r, "metrics", None) is not None else (json.loads(r.metrics_json) if r.metrics_json else {}),
                "created_at": r.run_at.isoformat() if r.run_at else None,
            }
            for r in runs
        ]
    }



@app.get("/admin/jobs", status_code=status.HTTP_200_OK)
def list_jobs(
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Operational monitoring of queued, running, completed and failed background jobs."""
    job_repo = JobRepository(session)
    jobs = job_repo.list_jobs(tenant_id=tenant_id, status=status, limit=limit)

    return {
        "jobs": [
            {
                "id": j.id,
                "tenant_id": j.tenant_id,
                "job_type": j.job_type,
                "idempotency_key": j.idempotency_key,
                "status": j.status,
                "attempt_count": j.attempt_count,
                "max_attempts": j.max_attempts,
                "available_at": j.available_at.isoformat() if j.available_at else None,
                "lease_expires_at": j.lease_expires_at.isoformat() if j.lease_expires_at else None,
                "worker_id": j.worker_id,
                "last_error": j.last_error,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            }
            for j in jobs
        ]
    }


# --------------------------------------------------------------------------
# AI Exception Investigation Endpoints
# --------------------------------------------------------------------------


@app.post("/admin/investigations", status_code=status.HTTP_202_ACCEPTED)
def submit_investigation(
    request: InvestigationCreateRequest,
    response: Response,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Submit an investigation request for an unresolved financial discrepancy."""
    config = APIConfig.from_env()
    tenant_id = request.tenant_id or config.default_tenant_id

    tenant_repo = TenantRepository(session)
    if not tenant_repo.get_tenant(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found.",
        )

    from reconcile_investigator.service import InvestigationService
    service = InvestigationService(session)

    if request.sync_mode:
        inv_rec, result, ver_res = service.run_investigation(
            tenant_id=tenant_id,
            target_type=request.target_type,
            target_id=request.target_id,
        )
        response.status_code = status.HTTP_200_OK
        return {
            "status": inv_rec.status,
            "investigation_id": inv_rec.id,
            "classification": inv_rec.classification,
            "summary": inv_rec.summary,
            "verifier_status": inv_rec.verifier_status,
            "requires_human_review": result.requires_human_review,
            "result": inv_rec.result,
        }
    else:
        inv_rec, job = service.enqueue_investigation(
            tenant_id=tenant_id,
            target_type=request.target_type,
            target_id=request.target_id,
        )
        return {
            "status": "queued",
            "investigation_id": inv_rec.id,
            "job_id": job.id,
            "target_type": request.target_type,
            "target_id": request.target_id,
        }


@app.get("/admin/investigations", status_code=status.HTTP_200_OK)
def list_investigations(
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    target_type: Optional[str] = None,
    limit: int = 50,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List investigation cases for the authorized tenant."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    from reconcile_store.repositories.investigation_repo import InvestigationRepository
    repo = InvestigationRepository(session)
    investigations = repo.list_investigations(
        tenant_id=effective_tenant,
        status=status,
        target_type=target_type,
        limit=limit,
    )

    return {
        "investigations": [
            {
                "id": inv.id,
                "tenant_id": inv.tenant_id,
                "target_type": inv.target_type,
                "target_id": inv.target_id,
                "status": inv.status,
                "classification": inv.classification,
                "summary": inv.summary,
                "verifier_status": inv.verifier_status,
                "model_provider": inv.model_provider,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
            }
            for inv in investigations
        ]
    }


@app.get("/admin/investigations/{investigation_id}", status_code=status.HTTP_200_OK)
def get_investigation(
    investigation_id: str,
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve full status, findings, and verifier report for an investigation."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    from reconcile_store.repositories.investigation_repo import InvestigationRepository
    repo = InvestigationRepository(session)
    inv = repo.get_investigation(effective_tenant, investigation_id)
    if not inv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation '{investigation_id}' not found for tenant '{effective_tenant}'.",
        )

    return {
        "id": inv.id,
        "tenant_id": inv.tenant_id,
        "target_type": inv.target_type,
        "target_id": inv.target_id,
        "status": inv.status,
        "classification": inv.classification,
        "summary": inv.summary,
        "result": inv.result,
        "evidence_references": inv.evidence_references,
        "verifier_status": inv.verifier_status,
        "verifier_details": inv.verifier_details,
        "model_provider": inv.model_provider,
        "model_name": inv.model_name,
        "prompt_version": inv.prompt_version,
        "error_message": inv.error_message,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
    }


# --------------------------------------------------------------------------
# D7 Fintech Operator Dashboard & Workflow Endpoints
# --------------------------------------------------------------------------


@app.get("/admin/dashboard/summary", status_code=status.HTTP_200_OK)
def get_dashboard_summary(
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Provide overall business reconciliation health and operational summary metrics."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    pay_repo = PaymentRepository(session)
    inv_repo = InvoiceRepository(session)
    prop_repo = ProposalRepository(session)
    invg_repo = InvestigationRepository(session)
    run_repo = ReconciliationRunRepository(session)

    payments = list(pay_repo.list_payments(effective_tenant))
    invoices = list(inv_repo.list_invoices(effective_tenant))
    runs = list(run_repo.list_runs(effective_tenant))
    investigations = list(invg_repo.list_investigations(effective_tenant, limit=100))

    financial_totals: dict[str, dict[str, Any]] = {}

    def get_currency_bucket(curr: str) -> dict[str, Any]:
        c = (curr or "USD").upper()
        if c not in financial_totals:
            financial_totals[c] = {
                "currency": c,
                "payments_amount_minor": 0,
                "invoices_amount_minor": 0,
                "reconciled_amount_minor": 0,
                "unapplied_cash_minor": 0,
            }
        return financial_totals[c]

    open_exceptions_list = []
    unmatched_count = 0
    reconciled_count = 0
    partially_reconciled_count = 0

    for p in payments:
        bucket = get_currency_bucket(p.currency)
        bucket["payments_amount_minor"] += p.amount_minor
        unapplied = pay_repo.get_unapplied_cash_minor(effective_tenant, p.id)
        bucket["unapplied_cash_minor"] += unapplied
        allocated = p.amount_minor - unapplied
        bucket["reconciled_amount_minor"] += allocated

        if unapplied > 0:
            open_exceptions_list.append({
                "id": p.id,
                "provider_payment_id": p.provider_payment_id,
                "amount_minor": p.amount_minor,
                "currency": p.currency,
                "payer_name": p.payer_name,
                "unapplied_cash_minor": unapplied,
                "received_at": p.received_at.isoformat() if p.received_at else None,
                "status": "UNMATCHED" if unapplied == p.amount_minor else "PARTIAL",
            })
            if unapplied == p.amount_minor:
                unmatched_count += 1
            else:
                partially_reconciled_count += 1
        else:
            reconciled_count += 1

    for inv in invoices:
        bucket = get_currency_bucket(inv.currency)
        bucket["invoices_amount_minor"] += inv.total_minor

    stmt_props = select(ProposalRecord).where(ProposalRecord.tenant_id == effective_tenant)
    proposals = session.scalars(stmt_props).all()
    pending_proposals = 0
    approved_proposals = 0
    rejected_proposals = 0

    for prop in proposals:
        decs = prop.decisions
        dec_status = decs[-1].decision_type if decs else "PENDING"
        if dec_status == "APPROVED":
            approved_proposals += 1
        elif dec_status == "REJECTED":
            rejected_proposals += 1
        else:
            pending_proposals += 1

    invg_needs_review = sum(1 for i in investigations if i.status == "NEEDS_REVIEW")
    invg_completed = sum(1 for i in investigations if i.status == "COMPLETED")
    invg_invalid = sum(1 for i in investigations if i.status == "INVALID")

    return {
        "tenant_id": effective_tenant,
        "counts": {
            "total_payments": len(payments),
            "reconciled_payments": reconciled_count,
            "unmatched_payments": unmatched_count,
            "partially_reconciled_payments": partially_reconciled_count,
            "open_exceptions": len(open_exceptions_list),
            "total_invoices": len(invoices),
            "total_proposals": len(proposals),
            "pending_proposals": pending_proposals,
            "approved_proposals": approved_proposals,
            "rejected_proposals": rejected_proposals,
            "total_investigations": len(investigations),
            "investigations_needs_review": invg_needs_review,
            "investigations_completed": invg_completed,
            "investigations_invalid": invg_invalid,
        },
        "financial_totals": financial_totals,
        "recent_runs": [
            {
                "id": r.id,
                "matcher_name": r.matcher_name,
                "matcher_version": r.matcher_version,
                "summary_metrics": getattr(r, "metrics", None) or (json.loads(r.metrics_json) if r.metrics_json else {}),
                "run_at": r.run_at.isoformat() if r.run_at else None,
            }
            for r in runs[:5]
        ],
        "recent_exceptions": open_exceptions_list[:10],
        "recent_investigations": [
            {
                "id": i.id,
                "target_type": i.target_type,
                "target_id": i.target_id,
                "status": i.status,
                "classification": i.classification,
                "verifier_status": i.verifier_status,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in investigations[:5]
        ],
    }


@app.get("/admin/transactions", status_code=status.HTTP_200_OK)
def list_transactions(
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    currency: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List financial transactions with search, currency and reconciliation status filters."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id
    pay_repo = PaymentRepository(session)

    stmt = select(PaymentRecord).where(PaymentRecord.tenant_id == effective_tenant)
    if currency:
        stmt = stmt.where(PaymentRecord.currency == currency.strip().upper())
    if search:
        s = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                PaymentRecord.provider_payment_id.ilike(s),
                PaymentRecord.payer_name.ilike(s),
                PaymentRecord.payer_email.ilike(s),
                PaymentRecord.reference.ilike(s),
            )
        )
    stmt = stmt.order_by(PaymentRecord.received_at.desc())
    all_payments = session.scalars(stmt).all()

    filtered_transactions = []
    for p in all_payments:
        unapplied = pay_repo.get_unapplied_cash_minor(effective_tenant, p.id)
        if unapplied == 0:
            reconciliation_status = "RECONCILED"
        elif unapplied == p.amount_minor:
            reconciliation_status = "UNMATCHED"
        else:
            reconciliation_status = "PARTIALLY_RECONCILED"

        if status:
            norm_s = status.strip().upper()
            if norm_s in ("RECONCILED", "UNMATCHED", "PARTIALLY_RECONCILED"):
                if reconciliation_status != norm_s:
                    continue
            else:
                if (p.status or "").upper() != norm_s:
                    continue

        filtered_transactions.append({
            "id": p.id,
            "provider": p.provider,
            "provider_payment_id": p.provider_payment_id,
            "amount_minor": p.amount_minor,
            "currency": p.currency,
            "status": p.status,
            "payer_name": p.payer_name,
            "payer_email": p.payer_email,
            "reference": p.reference,
            "note": p.note,
            "received_at": p.received_at.isoformat() if p.received_at else None,
            "unapplied_cash_minor": unapplied,
            "allocated_minor": p.amount_minor - unapplied,
            "reconciliation_status": reconciliation_status,
        })

    paginated = filtered_transactions[offset : offset + limit]
    return {
        "transactions": paginated,
        "total_count": len(filtered_transactions),
        "limit": limit,
        "offset": offset,
    }


@app.get("/admin/transactions/{payment_id}", status_code=status.HTTP_200_OK)
def get_transaction(
    payment_id: str,
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve complete transaction details, linked allocations, proposals, investigations and audit trail."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id
    pay_repo = PaymentRepository(session)

    stmt = select(PaymentRecord).where(
        PaymentRecord.tenant_id == effective_tenant,
        or_(
            PaymentRecord.id == payment_id,
            PaymentRecord.provider_payment_id == payment_id,
        ),
    )
    p = session.scalars(stmt).first()
    if not p:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction '{payment_id}' not found for tenant '{effective_tenant}'.",
        )

    unapplied = pay_repo.get_unapplied_cash_minor(effective_tenant, p.id)

    # Allocations
    stmt_alloc = select(AllocationRecord).where(
        AllocationRecord.tenant_id == effective_tenant,
        AllocationRecord.payment_id == p.id,
    )
    allocs = session.scalars(stmt_alloc).all()
    alloc_list = []
    for a in allocs:
        inv = session.get(InvoiceRecord, a.invoice_id)
        alloc_list.append({
            "id": a.id,
            "invoice_id": a.invoice_id,
            "invoice_number": inv.invoice_number if inv else a.invoice_id,
            "customer_name": inv.customer_name if inv else "",
            "amount_minor": a.amount_minor,
            "currency": a.currency,
            "actor": a.actor,
            "allocated_at": a.allocated_at.isoformat() if a.allocated_at else None,
        })

    # Settlements
    stmt_sett = select(ProviderSettlementRecord).where(
        ProviderSettlementRecord.tenant_id == effective_tenant,
        or_(
            ProviderSettlementRecord.payment_id == p.id,
            ProviderSettlementRecord.provider_settlement_id == p.provider_payment_id,
        ),
    )
    settlements = session.scalars(stmt_sett).all()
    sett_list = []
    for s in settlements:
        inv = session.get(InvoiceRecord, s.invoice_id)
        sett_list.append({
            "id": s.id,
            "provider_settlement_id": s.provider_settlement_id,
            "invoice_id": s.invoice_id,
            "invoice_number": inv.invoice_number if inv else s.invoice_id,
            "amount_minor": s.amount_minor,
            "currency": s.currency,
            "settled_at": s.settled_at.isoformat() if s.settled_at else None,
        })

    # Refunds & Reversals
    stmt_ref = select(RefundRecord).where(
        RefundRecord.tenant_id == effective_tenant,
        RefundRecord.parent_payment_id == p.id,
    )
    refunds = session.scalars(stmt_ref).all()
    ref_list = [
        {
            "id": r.id,
            "provider_refund_id": r.provider_refund_id,
            "amount_minor": r.amount_minor,
            "currency": r.currency,
            "status": r.status,
            "record_type": getattr(r, "record_type", "REFUND"),
            "refunded_at": r.refunded_at.isoformat() if r.refunded_at else None,
        }
        for r in refunds
    ]

    # Proposals
    prop_repo = ProposalRepository(session)
    proposals = prop_repo.list_by_payment(effective_tenant, p.id)
    prop_list = []
    for prop in proposals:
        decs = prop.decisions
        dec_status = decs[-1].decision_type if decs else "PENDING"
        pallocs = []
        for pa in prop.allocations:
            inv = session.get(InvoiceRecord, pa.invoice_id)
            pallocs.append({
                "invoice_id": pa.invoice_id,
                "invoice_number": inv.invoice_number if inv else pa.invoice_id,
                "amount_minor": pa.amount_minor,
                "currency": pa.currency,
            })
        prop_list.append({
            "id": prop.id,
            "run_id": prop.run_id,
            "outcome": prop.outcome,
            "verifier_status": prop.verifier_status,
            "decision_status": dec_status,
            "allocations": pallocs,
            "excess_amount_minor": prop.excess_amount_minor,
            "created_at": prop.created_at.isoformat() if prop.created_at else None,
        })

    # Investigations
    stmt_invg = select(InvestigationRecord).where(
        InvestigationRecord.tenant_id == effective_tenant,
        or_(
            InvestigationRecord.target_id == p.provider_payment_id,
            InvestigationRecord.target_id == p.id,
        ),
    )
    invgs = session.scalars(stmt_invg).all()
    invg_list = [
        {
            "id": i.id,
            "status": i.status,
            "classification": i.classification,
            "summary": i.summary,
            "verifier_status": i.verifier_status,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in invgs
    ]

    # Audit Events
    stmt_aud = select(AuditEventRecord).where(
        AuditEventRecord.tenant_id == effective_tenant,
        or_(
            AuditEventRecord.subject_id == p.id,
            AuditEventRecord.subject_id == p.provider_payment_id,
        ),
    ).order_by(AuditEventRecord.sequence_number.desc())
    audit_events = session.scalars(stmt_aud).all()
    aud_list = [
        {
            "sequence_number": a.sequence_number,
            "event_type": a.event_type,
            "actor": a.actor,
            "timestamp": a.timestamp.isoformat() if a.timestamp else None,
            "event_hash": a.event_hash,
        }
        for a in audit_events
    ]

    return {
        "id": p.id,
        "provider": p.provider,
        "provider_payment_id": p.provider_payment_id,
        "amount_minor": p.amount_minor,
        "currency": p.currency,
        "status": p.status,
        "payer_name": p.payer_name,
        "payer_email": p.payer_email,
        "reference": p.reference,
        "note": p.note,
        "received_at": p.received_at.isoformat() if p.received_at else None,
        "unapplied_cash_minor": unapplied,
        "allocated_minor": p.amount_minor - unapplied,
        "allocations": alloc_list,
        "settlements": sett_list,
        "refunds": ref_list,
        "proposals": prop_list,
        "investigations": invg_list,
        "audit_events": aud_list,
    }


@app.get("/admin/invoices", status_code=status.HTTP_200_OK)
def list_invoices(
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    currency: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List invoices with outstanding balances calculated from application and provider records."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id
    inv_repo = InvoiceRepository(session)

    stmt = select(InvoiceRecord).where(InvoiceRecord.tenant_id == effective_tenant)
    if currency:
        stmt = stmt.where(InvoiceRecord.currency == currency.strip().upper())
    if search:
        s = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                InvoiceRecord.provider_invoice_id.ilike(s),
                InvoiceRecord.invoice_number.ilike(s),
                InvoiceRecord.customer_name.ilike(s),
                InvoiceRecord.customer_email.ilike(s),
            )
        )
    stmt = stmt.order_by(InvoiceRecord.issued_on.desc())
    all_invoices = session.scalars(stmt).all()

    invoice_list = []
    for inv in all_invoices:
        allocated = inv_repo.get_total_allocated_minor(effective_tenant, inv.id)
        settled = inv_repo.get_total_settled_minor(effective_tenant, inv.id)
        outstanding = max(0, inv.total_minor - (allocated + settled))

        reconciliation_status = "FULLY_RECONCILED" if outstanding == 0 else ("PARTIAL" if (allocated + settled) > 0 else "OPEN")
        if status:
            norm_s = status.strip().upper()
            if norm_s in ("FULLY_RECONCILED", "OPEN", "PARTIAL"):
                if reconciliation_status != norm_s:
                    continue
            else:
                if (inv.status or "").upper() != norm_s:
                    continue

        invoice_list.append({
            "id": inv.id,
            "provider": inv.provider,
            "provider_invoice_id": inv.provider_invoice_id,
            "invoice_number": inv.invoice_number,
            "total_minor": inv.total_minor,
            "currency": inv.currency,
            "provider_due_minor": inv.provider_due_minor,
            "status": inv.status,
            "customer_name": inv.customer_name,
            "customer_email": inv.customer_email,
            "issued_on": inv.issued_on.isoformat() if inv.issued_on else None,
            "due_on": inv.due_on.isoformat() if inv.due_on else None,
            "memo": inv.memo,
            "allocated_minor": allocated,
            "settled_minor": settled,
            "outstanding_balance_minor": outstanding,
            "reconciliation_status": reconciliation_status,
        })

    paginated = invoice_list[offset : offset + limit]
    return {
        "invoices": paginated,
        "total_count": len(invoice_list),
        "limit": limit,
        "offset": offset,
    }


@app.get("/admin/invoices/{invoice_id}", status_code=status.HTTP_200_OK)
def get_invoice(
    invoice_id: str,
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve invoice details, ledger allocations, provider settlements and outstanding balance."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id
    inv_repo = InvoiceRepository(session)

    stmt = select(InvoiceRecord).where(
        InvoiceRecord.tenant_id == effective_tenant,
        or_(
            InvoiceRecord.id == invoice_id,
            InvoiceRecord.provider_invoice_id == invoice_id,
            InvoiceRecord.invoice_number == invoice_id,
        ),
    )
    inv = session.scalars(stmt).first()
    if not inv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice '{invoice_id}' not found for tenant '{effective_tenant}'.",
        )

    allocated = inv_repo.get_total_allocated_minor(effective_tenant, inv.id)
    settled = inv_repo.get_total_settled_minor(effective_tenant, inv.id)
    outstanding = max(0, inv.total_minor - (allocated + settled))

    # Allocations against this invoice
    stmt_alloc = select(AllocationRecord).where(
        AllocationRecord.tenant_id == effective_tenant,
        AllocationRecord.invoice_id == inv.id,
    )
    allocs = session.scalars(stmt_alloc).all()
    alloc_list = []
    for a in allocs:
        p = session.get(PaymentRecord, a.payment_id)
        alloc_list.append({
            "id": a.id,
            "payment_id": a.payment_id,
            "provider_payment_id": p.provider_payment_id if p else a.payment_id,
            "payer_name": p.payer_name if p else "",
            "amount_minor": a.amount_minor,
            "currency": a.currency,
            "actor": a.actor,
            "allocated_at": a.allocated_at.isoformat() if a.allocated_at else None,
        })

    # Settlements against this invoice
    stmt_sett = select(ProviderSettlementRecord).where(
        ProviderSettlementRecord.tenant_id == effective_tenant,
        ProviderSettlementRecord.invoice_id == inv.id,
    )
    settlements = session.scalars(stmt_sett).all()
    sett_list = [
        {
            "id": s.id,
            "provider_settlement_id": s.provider_settlement_id,
            "amount_minor": s.amount_minor,
            "currency": s.currency,
            "settled_at": s.settled_at.isoformat() if s.settled_at else None,
        }
        for s in settlements
    ]

    return {
        "id": inv.id,
        "provider": inv.provider,
        "provider_invoice_id": inv.provider_invoice_id,
        "invoice_number": inv.invoice_number,
        "total_minor": inv.total_minor,
        "currency": inv.currency,
        "provider_due_minor": inv.provider_due_minor,
        "status": inv.status,
        "customer_name": inv.customer_name,
        "customer_email": inv.customer_email,
        "issued_on": inv.issued_on.isoformat() if inv.issued_on else None,
        "due_on": inv.due_on.isoformat() if inv.due_on else None,
        "memo": inv.memo,
        "allocated_minor": allocated,
        "settled_minor": settled,
        "outstanding_balance_minor": outstanding,
        "allocations": alloc_list,
        "settlements": sett_list,
    }


@app.post("/admin/runs", status_code=status.HTTP_200_OK)
def trigger_reconciliation_run(
    request: RunTriggerRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Execute deterministic reconciliation run and persist proposed match outcomes."""
    config = APIConfig.from_env()
    effective_tenant = request.tenant_id or config.default_tenant_id

    tenant_repo = TenantRepository(session)
    if not tenant_repo.get_tenant(effective_tenant):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{effective_tenant}' not found.",
        )

    workflow = StorageWorkflow(session)
    run_rec, run_result = workflow.run_reconciliation(
        tenant_id=effective_tenant,
        matcher_name=request.matcher_name or "deterministic_v2",
        matcher_version=request.matcher_version or "2.0.0",
    )
    session.commit()

    return {
        "status": "completed",
        "run_id": run_rec.id,
        "tenant_id": effective_tenant,
        "matcher_name": run_rec.matcher_name,
        "matcher_version": run_rec.matcher_version,
        "proposals_generated": len(run_result.proposals),
        "metrics": run_rec.metrics,
        "run_at": run_rec.run_at.isoformat() if run_rec.run_at else None,
    }


@app.get("/admin/runs/{run_id}", status_code=status.HTTP_200_OK)
def get_run(
    run_id: str,
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve full details of a reconciliation run and its generated proposals."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    run_repo = ReconciliationRunRepository(session)
    prop_repo = ProposalRepository(session)
    run = run_repo.get_run(effective_tenant, run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reconciliation run '{run_id}' not found for tenant '{effective_tenant}'.",
        )

    proposals = prop_repo.list_by_run(effective_tenant, run_id)
    proposals_list = []
    for p in proposals:
        decs = p.decisions
        dec_status = decs[-1].decision_type if decs else "PENDING"
        pay = session.get(PaymentRecord, p.payment_id)
        allocs = []
        for pa in p.allocations:
            inv = session.get(InvoiceRecord, pa.invoice_id)
            allocs.append({
                "invoice_id": pa.invoice_id,
                "invoice_number": inv.invoice_number if inv else pa.invoice_id,
                "amount_minor": pa.amount_minor,
                "currency": pa.currency,
            })
        proposals_list.append({
            "id": p.id,
            "payment_id": p.payment_id,
            "provider_payment_id": pay.provider_payment_id if pay else p.payment_id,
            "payer_name": pay.payer_name if pay else "",
            "payment_amount_minor": pay.amount_minor if pay else 0,
            "payment_currency": pay.currency if pay else "USD",
            "outcome": p.outcome,
            "verifier_status": p.verifier_status,
            "decision_status": dec_status,
            "allocations": allocs,
            "excess_amount_minor": p.excess_amount_minor,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })

    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "matcher_name": run.matcher_name,
        "matcher_version": run.matcher_version,
        "status": run.status,
        "metrics": run.metrics,
        "run_at": run.run_at.isoformat() if run.run_at else None,
        "proposals": proposals_list,
        "total_proposals": len(proposals_list),
    }


@app.get("/admin/proposals", status_code=status.HTTP_200_OK)
def list_proposals(
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    run_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List match proposals pending or resolved by operators."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    prop_repo = ProposalRepository(session)
    proposals = prop_repo.list_proposals(
        tenant_id=effective_tenant,
        status=status,
        run_id=run_id,
        payment_id=payment_id,
        limit=limit,
        offset=offset,
    )

    items = []
    for p in proposals:
        decs = p.decisions
        dec_status = decs[-1].decision_type if decs else "PENDING"
        pay = session.get(PaymentRecord, p.payment_id)
        allocs = []
        for pa in p.allocations:
            inv = session.get(InvoiceRecord, pa.invoice_id)
            allocs.append({
                "invoice_id": pa.invoice_id,
                "invoice_number": inv.invoice_number if inv else pa.invoice_id,
                "amount_minor": pa.amount_minor,
                "currency": pa.currency,
            })
        items.append({
            "id": p.id,
            "run_id": p.run_id,
            "payment_id": p.payment_id,
            "provider_payment_id": pay.provider_payment_id if pay else p.payment_id,
            "payer_name": pay.payer_name if pay else "",
            "payment_amount_minor": pay.amount_minor if pay else 0,
            "payment_currency": pay.currency if pay else "USD",
            "outcome": p.outcome,
            "verifier_status": p.verifier_status,
            "decision_status": dec_status,
            "allocations": allocs,
            "excess_amount_minor": p.excess_amount_minor,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })

    return {
        "proposals": items,
        "total_count": len(items),
        "limit": limit,
        "offset": offset,
    }


@app.get("/admin/proposals/{proposal_id}", status_code=status.HTTP_200_OK)
def get_proposal(
    proposal_id: str,
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve full proposal record, independent verification checks, and decision history."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    prop_repo = ProposalRepository(session)
    p = prop_repo.get_proposal(effective_tenant, proposal_id)
    if not p:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Proposal '{proposal_id}' not found for tenant '{effective_tenant}'.",
        )

    decs = p.decisions
    dec_status = decs[-1].decision_type if decs else "PENDING"
    pay = session.get(PaymentRecord, p.payment_id)

    allocs = []
    for pa in p.allocations:
        inv = session.get(InvoiceRecord, pa.invoice_id)
        allocs.append({
            "id": pa.id,
            "invoice_id": pa.invoice_id,
            "invoice_number": inv.invoice_number if inv else pa.invoice_id,
            "customer_name": inv.customer_name if inv else "",
            "amount_minor": pa.amount_minor,
            "currency": pa.currency,
        })

    ver_repo = VerificationRepository(session)
    verifications = ver_repo.list_by_proposal(effective_tenant, proposal_id)
    ver_list = [
        {
            "id": v.id,
            "check_name": v.check_name,
            "passed": v.passed,
            "failure_reason": v.failure_reason,
            "verified_at": v.verified_at.isoformat() if v.verified_at else None,
        }
        for v in verifications
    ]

    dec_list = [
        {
            "id": d.id,
            "decision_type": d.decision_type,
            "actor": d.actor,
            "reason": d.reason,
            "decided_at": d.decided_at.isoformat() if d.decided_at else None,
        }
        for d in decs
    ]

    return {
        "id": p.id,
        "tenant_id": p.tenant_id,
        "run_id": p.run_id,
        "payment_id": p.payment_id,
        "provider_payment_id": pay.provider_payment_id if pay else p.payment_id,
        "payer_name": pay.payer_name if pay else "",
        "payment_amount_minor": pay.amount_minor if pay else 0,
        "payment_currency": pay.currency if pay else "USD",
        "outcome": p.outcome,
        "verifier_status": p.verifier_status,
        "decision_status": dec_status,
        "allocations": allocs,
        "verifications": ver_list,
        "decisions": dec_list,
        "excess_amount_minor": p.excess_amount_minor,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@app.post("/admin/proposals/{proposal_id}/approve", status_code=status.HTTP_200_OK)
def approve_proposal(
    proposal_id: str,
    request: ProposalApproveRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Approve a reconciliation proposal via the 10-step atomic financial transaction."""
    config = APIConfig.from_env()
    effective_tenant = request.tenant_id or config.default_tenant_id

    approval_service = ApprovalService(session)
    idempotency_key = f"approval:{effective_tenant}:{proposal_id}:{request.reviewer}"

    try:
        res = approval_service.approve_proposal(
            tenant_id=effective_tenant,
            proposal_id=proposal_id,
            actor=request.reviewer,
            idempotency_key=idempotency_key,
            reason=request.notes or "Manual operator approval",
        )
    except VerificationFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Proposal approval rejected by independent safety verifier: {exc}",
        ) from exc
    except (IneligiblePaymentError, IneligibleInvoiceError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except (ConflictError, StaleVersionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return {
        "status": "APPROVED",
        "decision_id": res.decision_id,
        "proposal_id": res.proposal_id,
        "allocation_ids": list(res.allocation_ids),
        "audit_event_id": res.audit_event_id,
        "is_idempotent_replay": res.is_idempotent_replay,
    }


@app.post("/admin/proposals/{proposal_id}/reject", status_code=status.HTTP_200_OK)
def reject_proposal(
    proposal_id: str,
    request: ProposalRejectRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Record operator rejection of a proposed reconciliation."""
    config = APIConfig.from_env()
    effective_tenant = request.tenant_id or config.default_tenant_id

    approval_service = ApprovalService(session)
    idempotency_key = f"rejection:{effective_tenant}:{proposal_id}:{request.reviewer}"

    try:
        dec = approval_service.reject_proposal(
            tenant_id=effective_tenant,
            proposal_id=proposal_id,
            actor=request.reviewer,
            idempotency_key=idempotency_key,
            reason=request.reason,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return {
        "status": "REJECTED",
        "decision_id": dec.id,
        "proposal_id": proposal_id,
        "reason": dec.reason,
    }


@app.post("/admin/investigations/{investigation_id}/approve", status_code=status.HTTP_200_OK)
def approve_investigation(
    investigation_id: str,
    request: InvestigationDecisionRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Approve an investigation recommendation.

    NON-NEGOTIABLE FINANCIAL SAFETY INVARIANTS:
    1. If verifier_status == "INVALID", approval is STRICTLY BLOCKED (fail-closed).
    2. Any financial allocation mutation MUST proceed through ApprovalService with full verification.
    """
    config = APIConfig.from_env()
    effective_tenant = request.tenant_id or config.default_tenant_id

    invg_repo = InvestigationRepository(session)
    inv = invg_repo.get_investigation(effective_tenant, investigation_id)
    if not inv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation '{investigation_id}' not found for tenant '{effective_tenant}'.",
        )

    # Invariant 1: Fail-closed on INVALID verifier status
    if inv.verifier_status == "INVALID":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Cannot approve investigation: Independent safety verifier status is 'INVALID'. "
                "Financial mutation is strictly blocked."
            ),
        )

    result_dict = inv.result or {}
    suggested_res = result_dict.get("suggested_resolution")

    allocation_ids: list[str] = []
    if suggested_res and suggested_res.get("suggested_invoice_ids"):
        prop_repo = ProposalRepository(session)
        approval_service = ApprovalService(session)

        stmt_pay = select(PaymentRecord).where(
            PaymentRecord.tenant_id == effective_tenant,
            or_(
                PaymentRecord.id == inv.target_id,
                PaymentRecord.provider_payment_id == inv.target_id,
            ),
        )
        pay = session.scalars(stmt_pay).first()
        if not pay:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Investigation target payment '{inv.target_id}' could not be resolved in ledger.",
            )

        existing_props = prop_repo.list_by_payment(effective_tenant, pay.id)
        target_prop = None
        for p in existing_props:
            if p.outcome == "PROPOSED":
                decs = p.decisions
                if not decs or decs[-1].decision_type == "PENDING":
                    target_prop = p
                    break

        if not target_prop:
            target_inv_id = suggested_res["suggested_invoice_ids"][0]
            stmt_inv = select(InvoiceRecord).where(
                InvoiceRecord.tenant_id == effective_tenant,
                or_(
                    InvoiceRecord.id == target_inv_id,
                    InvoiceRecord.provider_invoice_id == target_inv_id,
                    InvoiceRecord.invoice_number == target_inv_id,
                ),
            )
            inv_rec = session.scalars(stmt_inv).first()
            if not inv_rec:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Suggested invoice '{target_inv_id}' could not be resolved in ledger.",
                )

            run_repo = ReconciliationRunRepository(session)
            runs = list(run_repo.list_runs(effective_tenant))
            run_id = runs[0].id if runs else "run_manual_operator"

            alloc_amount = suggested_res.get("suggested_amount_cents") or pay.amount_minor
            target_prop = prop_repo.create_proposal(
                tenant_id=effective_tenant,
                run_id=run_id,
                payment_id=pay.id,
                outcome="PROPOSED",
                evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
                allocations=[
                    {
                        "invoice_id": inv_rec.id,
                        "amount_minor": alloc_amount,
                        "currency": pay.currency,
                        "evidence": ["REFERENCE_MATCH", "AMOUNT_EXACT"],
                    }
                ],
            )

        idempotency_key = f"invg-approval:{effective_tenant}:{investigation_id}:{request.reviewer}"
        try:
            approval_res = approval_service.approve_proposal(
                tenant_id=effective_tenant,
                proposal_id=target_prop.id,
                actor=request.reviewer,
                idempotency_key=idempotency_key,
                reason=request.notes or f"Approved via Investigation {investigation_id}",
            )
            allocation_ids = list(approval_res.allocation_ids)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to execute approved allocation: {exc}",
            ) from exc

    invg_repo.update_status(effective_tenant, investigation_id, "COMPLETED")

    audit_repo = AuditRepository(session)
    audit_event = audit_repo.append_event(
        tenant_id=effective_tenant,
        event_type="INVESTIGATION_APPROVED",
        actor=request.reviewer,
        subject_type="investigation",
        subject_id=investigation_id,
        details={
            "investigation_id": investigation_id,
            "target_type": inv.target_type,
            "target_id": inv.target_id,
            "notes": request.notes or "",
            "allocation_ids": allocation_ids,
        },
    )
    session.commit()

    return {
        "status": "APPROVED",
        "investigation_id": investigation_id,
        "allocation_ids": allocation_ids,
        "audit_event_id": audit_event.event_id,
    }


@app.post("/admin/investigations/{investigation_id}/reject", status_code=status.HTTP_200_OK)
def reject_investigation(
    investigation_id: str,
    request: InvestigationDecisionRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Record operator rejection of an investigation recommendation."""
    config = APIConfig.from_env()
    effective_tenant = request.tenant_id or config.default_tenant_id

    invg_repo = InvestigationRepository(session)
    inv = invg_repo.get_investigation(effective_tenant, investigation_id)
    if not inv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation '{investigation_id}' not found for tenant '{effective_tenant}'.",
        )

    invg_repo.update_status(effective_tenant, investigation_id, "REJECTED")

    audit_repo = AuditRepository(session)
    audit_event = audit_repo.append_event(
        tenant_id=effective_tenant,
        event_type="INVESTIGATION_REJECTED",
        actor=request.reviewer,
        subject_type="investigation",
        subject_id=investigation_id,
        details={
            "investigation_id": investigation_id,
            "target_type": inv.target_type,
            "target_id": inv.target_id,
            "notes": request.notes or "",
        },
    )
    session.commit()

    return {
        "status": "REJECTED",
        "investigation_id": investigation_id,
        "audit_event_id": audit_event.event_id,
    }


@app.get("/admin/audit-events", status_code=status.HTTP_200_OK)
def list_audit_events(
    tenant_id: Optional[str] = None,
    subject_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve immutable, tamper-evident hash-chained audit events."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    stmt = select(AuditEventRecord).where(AuditEventRecord.tenant_id == effective_tenant)
    if subject_type:
        stmt = stmt.where(AuditEventRecord.subject_type == subject_type)
    if subject_id:
        stmt = stmt.where(AuditEventRecord.subject_id == subject_id)
    stmt = stmt.order_by(AuditEventRecord.sequence_number.desc())

    all_events = session.scalars(stmt).all()
    paginated = all_events[offset : offset + limit]

    return {
        "audit_events": [
            {
                "id": a.id,
                "sequence_number": a.sequence_number,
                "event_id": a.event_id,
                "event_type": a.event_type,
                "actor": a.actor,
                "subject_type": a.subject_type,
                "subject_id": a.subject_id,
                "payload": json.loads(a.payload_json) if a.payload_json else {},
                "prev_hash": a.prev_hash,
                "event_hash": a.event_hash,
                "timestamp": a.timestamp.isoformat() if a.timestamp else None,
            }
            for a in paginated
        ],
        "total_count": len(all_events),
        "limit": limit,
        "offset": offset,
    }


@app.get("/admin/audit-events/verify", status_code=status.HTTP_200_OK)
def verify_audit_trail(
    tenant_id: Optional[str] = None,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Cryptographically verify the tenant's SHA-256 hash-chained audit trail."""
    config = APIConfig.from_env()
    effective_tenant = tenant_id or config.default_tenant_id

    audit_repo = AuditRepository(session)
    events = list(audit_repo.list_events(effective_tenant))

    try:
        audit_repo.verify_tenant_chain(effective_tenant)
        return {
            "verified": True,
            "tenant_id": effective_tenant,
            "total_events": len(events),
            "algorithm": "SHA-256",
            "genesis_hash": GENESIS_HASH,
            "latest_hash": events[-1].event_hash if events else GENESIS_HASH,
            "message": "Cryptographic audit chain verified: zero tampering detected.",
        }
    except AuditIntegrityError as exc:
        return {
            "verified": False,
            "tenant_id": effective_tenant,
            "total_events": len(events),
            "error": str(exc),
            "details": list(exc.details),
        }


@app.post("/admin/demo/seed", status_code=status.HTTP_200_OK)
def seed_demo_endpoint(
    request: DemoSeedRequest,
    _auth: str = Depends(require_admin_auth),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Idempotently seed rich, realistic demo data for the operator dashboard."""
    tenant_id = request.tenant_id or "demo_merchant"
    result = seed_demo_data(session, tenant_id=tenant_id, force=request.force or False)
    return result


# Mount pre-built frontend distribution if present
_FRONTEND_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "frontend",
    "dist",
)
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
