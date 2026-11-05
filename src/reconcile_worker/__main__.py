"""CLI entry point for running the background reconciliation worker.

Usage:
    python -m reconcile_worker [--once] [--poll-interval 1.0] [--tenant-id ID]
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import Any

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from .worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reconcile_worker")


def main() -> None:
    parser = argparse.ArgumentParser(description="PayPal Reconciliation Background Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one eligible job and exit immediately",
    )
    default_poll = float(os.environ.get("WORKER_POLL_INTERVAL", "2.0"))
    default_lease = int(os.environ.get("WORKER_LEASE_DURATION", "60"))
    default_tenant = os.environ.get("DEFAULT_TENANT_ID") or None

    parser.add_argument(
        "--poll-interval",
        type=float,
        default=default_poll,
        help=f"Seconds to sleep when no jobs are available (default: {default_poll})",
    )
    parser.add_argument(
        "--lease-duration",
        type=int,
        default=default_lease,
        help=f"Worker lease duration in seconds (default: {default_lease})",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=default_tenant,
        help="Optional tenant ID to restrict processing to",
    )
    args = parser.parse_args()

    db_engine = DatabaseEngine(DatabaseConfig.for_worker())
    worker = Worker(
        db_engine=db_engine,
        poll_interval=args.poll_interval,
        lease_duration=args.lease_duration,
    )

    # Register graceful shutdown signals
    def _signal_handler(sig: int, _frame: Any) -> None:
        logger.info("Signal %s received; requesting worker shutdown...", sig)
        worker.request_stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.once:
        logger.info("Running single worker iteration (--once)")
        job = worker.run_once(tenant_id=args.tenant_id)
        if job:
            logger.info("Processed job %s (status=%s)", job.id, job.status)
        else:
            logger.info("No eligible jobs found")
        sys.exit(0)

    worker.run(tenant_id=args.tenant_id)


if __name__ == "__main__":
    main()
