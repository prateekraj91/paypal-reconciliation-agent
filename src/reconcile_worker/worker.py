"""Durable background worker for verified PayPal webhooks and synchronisation jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Optional
import uuid

from sqlalchemy.orm import Session

from reconcile.models import Payment, PaymentReversal, PaymentStatus, Refund
from reconcile.money import parse_money_to_minor_units
from reconcile.paypal import (
    PayPalAPIError,
    PayPalClient,
    PayPalConfigError,
    PayPalNotFoundError,
    PayPalTimeoutError,
    TRANSACTION_APPEARANCE_DELAY_HOURS,
    DEFAULT_TRANSACTION_OVERLAP,
)
from reconcile.sync import run_sync
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories.job_repo import JobRepository
from reconcile_store.repositories.webhook_repo import WebhookInboxRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.tenant_repo import ConnectionRepository
from reconcile_store.workflow import StorageWorkflow
from reconcile_store.schema import JobRecord, ConnectionRecord, _utcnow

logger = logging.getLogger(__name__)


class WorkerError(Exception):
    """Base exception for worker processing errors."""


class PermanentJobError(WorkerError):
    """Raised when a job fails with a non-recoverable error and should not be retried."""


class RetryableJobError(WorkerError):
    """Raised when a job fails with a transient error and should be retried with backoff."""


class InvoiceNotYetAvailableError(RetryableJobError):
    """Raised when a payment arrives before its referenced invoice is available.
    
    The payment is retained in the store, and the job is deferred to retry later.
    """


class Worker:
    """Separately runnable background worker claiming and executing durable jobs."""

    def __init__(
        self,
        db_engine: DatabaseEngine,
        worker_id: Optional[str] = None,
        poll_interval: float = 1.0,
        lease_duration: int = 60,
        paypal_client: Optional[PayPalClient] = None,
    ) -> None:
        self.db_engine = db_engine
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.lease_duration = lease_duration
        self._paypal_client = paypal_client
        self._stop_requested = False

    def request_stop(self) -> None:
        """Signal the worker loop to terminate cleanly after the current iteration."""
        self._stop_requested = True

    def get_paypal_client(self) -> PayPalClient:
        """Get or lazily construct a PayPalClient instance."""
        if self._paypal_client is None:
            self._paypal_client = PayPalClient()
        return self._paypal_client

    def resolve_paypal_client_for_connection(self, conn: ConnectionRecord) -> PayPalClient:
        """Resolve PayPalClient securely from a ConnectionRecord's indirect credential reference.
        
        Adheres strictly to the credential-reference architecture:
        - Never stores raw secrets in the database.
        - Never logs or prints secret values.
        - Resolves referenced environment variables.
        - Keeps sandbox and live configurations strictly distinct.
        """
        if self._paypal_client is not None:
            return self._paypal_client

        if not hasattr(self, "_clients_by_ref"):
            self._clients_by_ref = {}

        ref = conn.credential_ref
        if ref in self._clients_by_ref:
            return self._clients_by_ref[ref]

        from reconcile.paypal import _load_dotenv
        env_dict = _load_dotenv()

        if ref == "env" or ref.startswith("env:"):
            prefix = "PAYPAL"
            if ":" in ref:
                suffix = ref.split(":", 1)[1].strip()
                if suffix.upper() in ("PAYPAL_SANDBOX", "SANDBOX"):
                    prefix = "PAYPAL"
                elif suffix.upper() in ("PAYPAL_LIVE", "LIVE"):
                    prefix = "PAYPAL_LIVE"
                else:
                    prefix = suffix

            # Look up environment variables strictly by prefix with fallback
            if prefix == "PAYPAL":
                client_id = (
                    os.environ.get("PAYPAL_CLIENT_ID")
                    or env_dict.get("PAYPAL_CLIENT_ID")
                    or os.environ.get("PAYPAL_SANDBOX_US_CLIENT_ID")
                    or env_dict.get("PAYPAL_SANDBOX_US_CLIENT_ID")
                )
                client_secret = (
                    os.environ.get("PAYPAL_CLIENT_SECRET")
                    or env_dict.get("PAYPAL_CLIENT_SECRET")
                    or os.environ.get("PAYPAL_SANDBOX_US_CLIENT_SECRET")
                    or env_dict.get("PAYPAL_SANDBOX_US_CLIENT_SECRET")
                )
                base_url = (
                    os.environ.get("PAYPAL_BASE_URL")
                    or env_dict.get("PAYPAL_BASE_URL")
                    or os.environ.get("PAYPAL_SANDBOX_US_BASE_URL")
                    or env_dict.get("PAYPAL_SANDBOX_US_BASE_URL")
                )
            else:
                client_id = os.environ.get(f"{prefix}_CLIENT_ID") or env_dict.get(f"{prefix}_CLIENT_ID")
                client_secret = os.environ.get(f"{prefix}_CLIENT_SECRET") or env_dict.get(f"{prefix}_CLIENT_SECRET")
                base_url = os.environ.get(f"{prefix}_BASE_URL") or env_dict.get(f"{prefix}_BASE_URL")
                # Fallback to standard PAYPAL_CLIENT_ID if prefix-specific not found and environment is sandbox
                if not client_id and ("SANDBOX" in prefix.upper() or conn.environment == "sandbox"):
                    client_id = os.environ.get("PAYPAL_CLIENT_ID") or env_dict.get("PAYPAL_CLIENT_ID")
                    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET") or env_dict.get("PAYPAL_CLIENT_SECRET")
                    base_url = base_url or os.environ.get("PAYPAL_BASE_URL") or env_dict.get("PAYPAL_BASE_URL")

            if not client_id or not client_secret:
                raise PermanentJobError(
                    f"Missing PayPal credentials for reference '{ref}': client ID and secret must be configured in environment."
                )

            # Keep Sandbox and Live environments separate
            if conn.environment == "sandbox":
                base_url = base_url or "https://api-m.sandbox.paypal.com"
            elif conn.environment == "live":
                base_url = base_url or "https://api-m.paypal.com"
                if "sandbox" in base_url.lower():
                    raise PermanentJobError(
                        f"Refusing to execute LIVE connection against Sandbox URL: '{base_url}'"
                    )

            try:
                client = PayPalClient(
                    client_id=client_id,
                    client_secret=client_secret,
                    base_url=base_url,
                )
                self._clients_by_ref[ref] = client
                return client
            except PayPalConfigError as exc:
                raise PermanentJobError(f"PayPal configuration error: {exc}") from exc
        else:
            raise PermanentJobError(
                f"Unsupported credential reference scheme '{ref}'. Only 'env:' references are currently supported."
            )


    def run_once(self, tenant_id: Optional[str] = None) -> Optional[JobRecord]:
        """Claim and execute a single job from the durable database queue.
        
        Returns the completed JobRecord or None if no eligible job was available.
        """
        with self.db_engine.session() as session:
            job_repo = JobRepository(session)
            inbox_repo = WebhookInboxRepository(session)

            job = job_repo.claim_next_job(
                worker_id=self.worker_id,
                lease_seconds=self.lease_duration,
                tenant_id=tenant_id,
            )
            if job is None:
                return None

            job_id = job.id
            job_type = job.job_type
            job_tenant = job.tenant_id
            attempt = job.attempt_count
            receipt_id = job.payload.get("receipt_id")

            logger.info("Claimed job %s (type=%s, attempt=%d)", job_id, job_type, attempt)

            try:
                if job_type == "WEBHOOK_INGEST":
                    self._handle_webhook_ingest(session, job)
                elif job_type == "MANUAL_SYNC":
                    self._handle_manual_sync(session, job)
                else:
                    raise PermanentJobError(f"Unsupported job type: {job_type}")

                # Success: mark completed
                job_repo.complete_job(job_id, self.worker_id)
                if receipt_id:
                    inbox_repo.update_processing_status(
                        record_id=receipt_id,
                        tenant_id=job_tenant,
                        processing_status="PROCESSED",
                    )
                session.commit()
                logger.info("Successfully completed job %s", job_id)
                return job

            except PermanentJobError as exc:
                err_msg = str(exc)
                logger.warning("Job %s permanently failed: %s", job_id, err_msg)
                job_repo.fail_job(job_id, self.worker_id, err_msg)
                if receipt_id:
                    inbox_repo.update_processing_status(
                        record_id=receipt_id,
                        tenant_id=job_tenant,
                        processing_status="FAILED",
                        failure_reason=err_msg,
                    )
                session.commit()
                return job

            except (RetryableJobError, PayPalTimeoutError, PayPalAPIError, Exception) as exc:
                err_msg = str(exc)
                logger.warning("Job %s encountered retryable failure: %s", job_id, err_msg)

                # Exponential backoff: min(5 * 2^(attempt-1), 300) seconds
                backoff_seconds = min(5 * (2 ** max(0, attempt - 1)), 300)
                if isinstance(exc, InvoiceNotYetAvailableError):
                    # Payment before invoice: retry sooner for invoice arrival
                    backoff_seconds = 10

                job_repo.retry_job(
                    job_id=job_id,
                    worker_id=self.worker_id,
                    error_message=err_msg,
                    delay_seconds=backoff_seconds,
                )
                if receipt_id:
                    inbox_repo.update_processing_status(
                        record_id=receipt_id,
                        tenant_id=job_tenant,
                        processing_status="RETRY",
                        failure_reason=err_msg,
                    )
                session.commit()
                return job

    def run(self, max_iterations: Optional[int] = None, tenant_id: Optional[str] = None) -> None:
        """Continuously process queued jobs until stopped."""
        logger.info("Worker %s started", self.worker_id)
        iterations = 0

        while not self._stop_requested:
            if max_iterations is not None and iterations >= max_iterations:
                break

            job = self.run_once(tenant_id=tenant_id)
            iterations += 1

            if job is not None:
                # Immediate next attempt if work was found
                continue

            # No work found; back off for poll_interval
            time.sleep(self.poll_interval)

        logger.info("Worker %s stopped", self.worker_id)

    # ----------------------------------------------------------------------
    # Job Type Handlers
    # ----------------------------------------------------------------------

    def _handle_webhook_ingest(self, session: Session, job: JobRecord) -> None:
        """Process a verified PayPal webhook event.
        
        Fetches authoritative provider records, ingests them, handles payment-before-invoice,
        and executes reconciliation.
        """
        tenant_id = job.tenant_id
        payload = job.payload
        receipt_id = payload.get("receipt_id")
        event_type = payload.get("event_type", "")
        resource_id = payload.get("resource_id")

        inbox_repo = WebhookInboxRepository(session)
        receipt = inbox_repo.get_by_id(tenant_id, receipt_id) if receipt_id else None
        if not receipt:
            raise PermanentJobError(f"Webhook receipt '{receipt_id}' not found for tenant '{tenant_id}'")

        try:
            event_data = json.loads(receipt.raw_payload)
        except Exception as exc:
            raise PermanentJobError("Malformed raw webhook payload stored in receipt") from exc

        conn_repo = ConnectionRepository(session)
        environment = payload.get("environment") or getattr(receipt, "environment", "sandbox")
        conn = conn_repo.get_connection(tenant_id, provider="paypal", environment=environment)
        client = self.resolve_paypal_client_for_connection(conn) if conn else self.get_paypal_client()
        workflow = StorageWorkflow(session)
        resource = event_data.get("resource") or {}

        # 1. Invoicing Webhooks
        if event_type.startswith("INVOICING.INVOICE."):
            invoice_id = resource.get("id") or (resource.get("invoice") or {}).get("id")
            if not invoice_id:
                raise PermanentJobError(f"No invoice ID found in invoice webhook event '{event_type}'")

            # Fetch authoritative invoice details
            try:
                raw_invoice = client.get_raw_invoice(invoice_id)
            except PayPalNotFoundError:
                # If cannot fetch from API, use resource if complete
                raw_invoice = resource
            except Exception as exc:
                raise RetryableJobError(f"Failed to fetch invoice '{invoice_id}' from PayPal: {exc}") from exc

            domain_invoice = client.map_invoice(raw_invoice)
            linked_allocs = client.extract_linked_allocations(raw_invoice)

            # Ingest invoice & settlements
            workflow.ingest_invoices(
                tenant_id=tenant_id,
                invoices=[domain_invoice],
                raw_payloads={domain_invoice.invoice_id: raw_invoice},
            )
            if linked_allocs:
                embedded_ev = {}
                payments_dict = raw_invoice.get("payments") or {}
                transactions = payments_dict.get("transactions") or []
                for txn in transactions:
                    txn_id = txn.get("payment_id") or txn.get("transaction_id") or txn.get("id")
                    if txn_id:
                        embedded_ev[str(txn_id)] = txn

                workflow.ingest_provider_settlements(
                    tenant_id=tenant_id,
                    allocations=linked_allocs,
                    raw_payloads=embedded_ev,
                    provider="paypal",
                    environment=conn.environment if conn else "sandbox",
                )

            # Trigger reconciliation
            workflow.run_reconciliation(tenant_id=tenant_id)
            return

        # 2. Payment Webhooks (e.g. PAYMENT.CAPTURE.COMPLETED, PAYMENT.SALE.COMPLETED)
        if event_type in (
            "PAYMENT.CAPTURE.COMPLETED",
            "PAYMENT.SALE.COMPLETED",
            "CHECKOUT.ORDER.APPROVED",
        ):
            capture_id = resource.get("id")
            if not capture_id:
                raise PermanentJobError(f"No resource ID found in payment event '{event_type}'")

            # Extract amount, currency and referenced invoice
            amount_dict = resource.get("amount") or {}
            value_str = amount_dict.get("value", "0.00")
            currency = amount_dict.get("currency_code", "USD")
            amount_cents = parse_money_to_minor_units(value_str, currency)

            # Inspect references linking to an invoice
            custom_id = resource.get("custom_id") or resource.get("invoice_id")
            note = resource.get("note") or resource.get("description") or ""
            invoice_ref = custom_id

            # Canonical Payment representation
            pay_time = resource.get("create_time") or _utcnow().isoformat()
            payment = Payment(
                payment_id=capture_id,
                amount_cents=amount_cents,
                currency=currency,
                status=PaymentStatus.COMPLETED,
                payer_name="PayPal Customer",
                payer_email=None,
                reference=invoice_ref,
                note=note,
                received_at=datetime.fromisoformat(pay_time.replace("Z", "+00:00")),
            )

            # Payment arrives before invoice check:
            # If the payment has an explicit invoice reference:
            if invoice_ref:
                inv_repo = InvoiceRepository(session)
                existing_inv = inv_repo.get_by_provider_id(tenant_id, "paypal", invoice_ref)
                if existing_inv is None:
                    # Check by invoice_number
                    existing_inv = inv_repo.get_by_number(tenant_id, "paypal", invoice_ref)

                if existing_inv is None:
                    # Attempt authoritative fetch from PayPal Invoicing
                    try:
                        raw_inv = client.get_raw_invoice(invoice_ref)
                        domain_inv = client.map_invoice(raw_inv)
                        workflow.ingest_invoices(
                            tenant_id=tenant_id,
                            invoices=[domain_inv],
                            raw_payloads={domain_inv.invoice_id: raw_inv},
                        )
                    except PayPalNotFoundError:
                        # Invoice does not exist yet on PayPal!
                        # Ingest the open payment safely so it is retained
                        workflow.ingest_payments(
                            tenant_id=tenant_id,
                            payments=[payment],
                            raw_payloads={payment.payment_id: resource},
                        )
                        raise InvoiceNotYetAvailableError(
                            f"Payment '{capture_id}' arrived for invoice '{invoice_ref}' which is not yet available."
                        )
                    except Exception as exc:
                        # Temporary network error fetching invoice
                        workflow.ingest_payments(
                            tenant_id=tenant_id,
                            payments=[payment],
                            raw_payloads={payment.payment_id: resource},
                        )
                        raise RetryableJobError(
                            f"Failed to check referenced invoice '{invoice_ref}': {exc}"
                        ) from exc

            # Ingest payment and execute reconciliation
            workflow.ingest_payments(
                tenant_id=tenant_id,
                payments=[payment],
                raw_payloads={payment.payment_id: resource},
            )
            workflow.run_reconciliation(tenant_id=tenant_id)
            return

        # 3. Involuntary Processor Reversals (PAYMENT.CAPTURE.REVERSED)
        if event_type == "PAYMENT.CAPTURE.REVERSED":
            capture_id = resource.get("id")
            if not capture_id:
                raise PermanentJobError(f"No resource ID found in event '{event_type}'")

            amount_dict = resource.get("amount") or {}
            value_str = amount_dict.get("value", "0.00")
            currency = amount_dict.get("currency_code", "USD")
            amount_cents = parse_money_to_minor_units(value_str, currency)

            custom_id = resource.get("custom_id") or resource.get("invoice_id")
            rev_time = resource.get("create_time") or resource.get("update_time") or _utcnow().isoformat()
            rev_id = (
                f"rev_{capture_id}_{resource.get('update_time', '')}"
                if resource.get("update_time")
                else f"rev_{capture_id}"
            )

            reversal = PaymentReversal(
                reversal_id=rev_id,
                parent_payment_id=capture_id,
                amount_cents=amount_cents,
                currency=currency,
                reversed_at=datetime.fromisoformat(rev_time.replace("Z", "+00:00")),
                reason=resource.get("reason_code") or "Involuntary processor reversal / chargeback",
                dispute_id=resource.get("dispute_id"),
                invoice_id=custom_id,
            )
            workflow.ingest_reversals(
                tenant_id=tenant_id,
                reversals=[reversal],
                raw_payloads={reversal.reversal_id: resource},
            )
            workflow.run_reconciliation(tenant_id=tenant_id)
            return

        # 4. Dispute Resolution (CUSTOMER.DISPUTE.RESOLVED)
        if event_type == "CUSTOMER.DISPUTE.RESOLVED":
            dispute_id = resource.get("dispute_id") or resource.get("id")
            if not dispute_id:
                raise PermanentJobError(f"No dispute ID found in event '{event_type}'")

            outcome = resource.get("dispute_outcome") or {}
            outcome_code = (outcome.get("outcome_code") or "").upper()

            # Confirm a dispute resolution changes financial balances only when authoritative outcome data supports that change
            if outcome_code in ("RESOLVED_BUYER_FAVOUR", "RESOLVED_WITH_PAYOUT"):
                amt_dict = outcome.get("amount") or resource.get("dispute_amount") or {}
                val_str = amt_dict.get("value", "0.00")
                curr = amt_dict.get("currency_code", "USD")
                reversal_cents = parse_money_to_minor_units(val_str, curr)

                disputed_txns = resource.get("disputed_transactions") or []
                parent_txn_id = ""
                if disputed_txns:
                    parent_txn_id = (
                        disputed_txns[0].get("seller_transaction_id")
                        or disputed_txns[0].get("buyer_transaction_id")
                        or ""
                    )

                if reversal_cents > 0 and parent_txn_id:
                    rev_time = resource.get("update_time") or resource.get("create_time") or _utcnow().isoformat()
                    reversal = PaymentReversal(
                        reversal_id=f"rev_dispute_{dispute_id}",
                        parent_payment_id=parent_txn_id,
                        amount_cents=reversal_cents,
                        currency=curr,
                        reversed_at=datetime.fromisoformat(rev_time.replace("Z", "+00:00")),
                        reason=f"Dispute resolved in buyer favour: {outcome_code}",
                        dispute_id=dispute_id,
                    )
                    workflow.ingest_reversals(
                        tenant_id=tenant_id,
                        reversals=[reversal],
                        raw_payloads={reversal.reversal_id: resource},
                    )
                    workflow.run_reconciliation(tenant_id=tenant_id)
                    return
                else:
                    logger.warning(
                        "Dispute %s resolved with outcome '%s' but lacked authoritative amount or transaction ID; financial balances unchanged.",
                        dispute_id,
                        outcome_code,
                    )
            else:
                logger.info(
                    "Dispute %s resolved with outcome code '%s'; financial balances unchanged.",
                    dispute_id,
                    outcome_code,
                )
            return

        # 5. Merchant Refunds (PAYMENT.CAPTURE.REFUNDED)
        if event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.SALE.REFUNDED"):
            refund_id = resource.get("id")
            if not refund_id:
                raise PermanentJobError(f"No resource ID found in event '{event_type}'")

            capture_id = resource.get("capture_id") or resource.get("parent_payment")
            if not capture_id:
                raise PermanentJobError(f"No parent capture ID found in refund event '{event_type}'")

            amount_dict = resource.get("amount") or {}
            value_str = amount_dict.get("value", "0.00")
            currency = amount_dict.get("currency_code", "USD")
            amount_cents = parse_money_to_minor_units(value_str, currency)

            custom_id = resource.get("custom_id") or resource.get("invoice_id")
            ref_time = resource.get("create_time") or resource.get("update_time") or _utcnow().isoformat()

            refund = Refund(
                refund_id=refund_id,
                parent_payment_id=capture_id,
                amount_cents=amount_cents,
                currency=currency,
                refunded_at=datetime.fromisoformat(ref_time.replace("Z", "+00:00")),
                reason=resource.get("note_to_payer") or "Merchant-initiated refund",
                invoice_id=custom_id,
            )
            workflow.ingest_refunds(
                tenant_id=tenant_id,
                refunds=[refund],
                raw_payloads={refund.refund_id: resource},
            )
            workflow.run_reconciliation(tenant_id=tenant_id)
            return

        # 6. Safely handle other known non-reconciliation PayPal events
        logger.info("Safely acknowledged non-reconciliation event type: %s", event_type)
        return

    def _handle_manual_sync(self, session: Session, job: JobRecord) -> None:
        """Execute a full manual synchronization pass using existing D2 sync logic."""
        tenant_id = job.tenant_id
        payload = job.payload
        environment = payload.get("environment") or "sandbox"
        page_size = payload.get("page_size")

        # 1. Resolve the tenant's actual PayPal connection
        conn_repo = ConnectionRepository(session)
        conn = conn_repo.get_connection(tenant_id, provider="paypal", environment=environment)
        if conn is None:
            raise PermanentJobError(
                f"No active PayPal connection found for tenant '{tenant_id}' (provider='paypal', environment='{environment}')"
            )
        if conn.status != "ACTIVE":
            raise PermanentJobError(
                f"PayPal connection for tenant '{tenant_id}' is '{conn.status}' (expected 'ACTIVE')"
            )

        # 2. Resolve client securely
        client = self.resolve_paypal_client_for_connection(conn)

        # 3. Calculate date windows with sensible defaults
        now = datetime.now(timezone.utc)
        try:
            if payload.get("start_date") and payload.get("end_date"):
                window_start = datetime.fromisoformat(payload["start_date"].replace("Z", "+00:00"))
                window_end = datetime.fromisoformat(payload["end_date"].replace("Z", "+00:00"))
            elif payload.get("start_date"):
                window_start = datetime.fromisoformat(payload["start_date"].replace("Z", "+00:00"))
                window_end = min(
                    window_start + timedelta(days=31),
                    now - timedelta(hours=TRANSACTION_APPEARANCE_DELAY_HOURS),
                )
                if window_start > window_end:
                    window_end = window_start + timedelta(hours=1)
            elif payload.get("end_date"):
                window_end = datetime.fromisoformat(payload["end_date"].replace("Z", "+00:00"))
                window_start = window_end - timedelta(days=7)
            else:
                window_end = now - timedelta(hours=TRANSACTION_APPEARANCE_DELAY_HOURS)
                window_start = window_end - timedelta(days=7)
        except (ValueError, TypeError) as exc:
            raise PermanentJobError(f"Invalid date in job payload: {exc}") from exc

        if window_start > window_end:
            raise PermanentJobError(f"Sync start date ({window_start}) must be before or equal to end date ({window_end})")

        # 4. Fetch invoices & transactions via D2 sync
        try:
            sync_result = run_sync(
                connector=client,
                start=window_start,
                end=window_end,
                page_size=page_size,
            )
        except PayPalAPIError as exc:
            if exc.status_code in (401, 403):
                raise PermanentJobError(f"PayPal OAuth/API authorization failed (HTTP {exc.status_code}): {exc}") from exc
            if 400 <= exc.status_code < 500 and exc.status_code not in (429,):
                raise PermanentJobError(f"PayPal API client error (HTTP {exc.status_code}): {exc}") from exc
            raise RetryableJobError(f"PayPal API temporary error (HTTP {exc.status_code}): {exc}") from exc
        except PayPalTimeoutError as exc:
            raise RetryableJobError(f"PayPal API timeout during sync: {exc}") from exc
        except Exception as exc:
            err_str = str(exc).lower()
            if "oauth" in err_str or "unauthorized" in err_str or "invalid client" in err_str or "credentials" in err_str:
                raise PermanentJobError(f"PayPal authentication failed: {exc}") from exc
            raise RetryableJobError(f"Unexpected error during PayPal sync: {exc}") from exc

        # 5. Persist canonical records, invoices and payments
        workflow = StorageWorkflow(session)
        environment = conn.environment if conn else "sandbox"

        if sync_result.invoices:
            workflow.ingest_invoices(
                tenant_id=tenant_id,
                invoices=sync_result.invoices,
                raw_payloads=dict(sync_result.invoice_evidence),
            )

        if sync_result.ledger_allocations:
            workflow.ingest_provider_settlements(
                tenant_id=tenant_id,
                allocations=sync_result.ledger_allocations,
                raw_payloads=dict(sync_result.embedded_payment_evidence),
                provider="paypal",
                environment=environment,
            )

        if sync_result.payments:
            combined_payment_evidence = {
                **dict(sync_result.embedded_payment_evidence),
                **dict(sync_result.transaction_evidence),
            }
            workflow.ingest_payments(
                tenant_id=tenant_id,
                payments=sync_result.payments,
                raw_payloads=combined_payment_evidence,
            )

        # 6. Run reconciliation and independent verification (no auto-approvals, no write-back)
        workflow.run_reconciliation(
            tenant_id=tenant_id,
            config_snapshot={
                "sync_window_start": window_start.isoformat(),
                "sync_window_end": window_end.isoformat(),
                "transactions_seen": sync_result.report.transactions_ingested,
            },
        )

