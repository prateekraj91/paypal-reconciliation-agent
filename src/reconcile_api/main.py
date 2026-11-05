"""FastAPI application for the PayPal Reconciliation Agent.

Provides:
- Public health and readiness probes
- Verified PayPal webhook ingestion endpoint
- Authenticated operator admin endpoints (sync, runs, jobs)
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Optional
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from reconcile.paypal import PayPalAPIError, PayPalClient, PayPalTimeoutError
from reconcile_store.repositories.tenant_repo import TenantRepository, ConnectionRepository
from reconcile_store.repositories.webhook_repo import (
    WebhookInboxRepository,
    InvalidWebhookStateTransitionError,
)
from reconcile_store.repositories.job_repo import JobRepository
from reconcile_store.repositories.reconciliation_repo import ReconciliationRunRepository

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
    version="0.1.0",
    description="D4 Verified Webhook Ingestion, Background Processing and Reconciliation API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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
