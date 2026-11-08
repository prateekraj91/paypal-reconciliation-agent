"""Deterministic demo dataset generator for operator dashboard demonstration.

Delegates to comprehensive local demo dataset generator covering all 10 core fintech reconciliation scenarios:
1. Exact payment match (1:1 full reference & amount match)
2. Unmatched payment (unreferenced deposit with zero matching invoice)
3. Competing invoices (candidate ambiguity with identical amount and customer)
4. Partial payment (deposit less than invoice total)
5. Split payment (one payment covering multiple invoices)
6. Overpayment (payment greater than invoice total, leaving unapplied cash)
7. Refund (post-settlement partial refund record)
8. Reversal (chargeback / bank reversal dispute record)
9. Currency mismatch (USD payment targeting EUR invoice)
10. AI investigation requiring human review (unlinked remitter with verified grounded evidence)

All records explicitly labelled '(DEMO)'.
"""

from __future__ import annotations

from typing import Any, Dict
from sqlalchemy.orm import Session

from reconcile_api.local_demo_dataset import seed_local_demo_dataset


def seed_demo_data(
    session: Session,
    tenant_id: str = "demo_merchant",
    force: bool = False,
) -> Dict[str, Any]:
    """Idempotently seed rich, realistic demo data for operator dashboard review.

    DEMO SAFETY INVARIANT:
    Seeding synthetic demo records is STRICTLY RESTRICTED to the 'demo_merchant' tenant.
    Never run against real merchant tenants or live databases.
    """
    return seed_local_demo_dataset(session, tenant_id=tenant_id, force=force)
