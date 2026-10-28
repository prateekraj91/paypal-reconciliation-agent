"""Tiny hand-built fixtures for unit tests (the dataset generator covers scale)."""

from datetime import date, datetime, timedelta, timezone

from reconcile.models import Invoice, InvoiceStatus, Payment, PaymentStatus

T0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def inv(n, amount, *, customer="Acme Studio", status=InvoiceStatus.SENT, currency="USD", number=None):
    return Invoice(
        invoice_id=f"inv_{n}",
        number=number or f"INV-{1000 + n}",
        customer_name=customer,
        amount_cents=amount,
        currency=currency,
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        status=status,
    )


def pay(n, amount, *, reference=None, payer="Acme Studio", status=PaymentStatus.COMPLETED,
        minutes=0, currency="USD", note=""):
    return Payment(
        payment_id=f"pay_{n}",
        amount_cents=amount,
        currency=currency,
        received_at=T0 + timedelta(minutes=minutes),
        payer_name=payer,
        reference=reference,
        note=note,
        status=status,
    )
