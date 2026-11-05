"""Configuration settings for the FastAPI application."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class APIConfig:
    """Runtime configuration for API endpoints, webhook verification, and database connectivity."""

    admin_api_key: str = ""
    paypal_webhook_id: str = ""
    paypal_environment: str = "sandbox"
    paypal_client_id: Optional[str] = None
    paypal_client_secret: Optional[str] = None
    paypal_base_url: Optional[str] = None
    default_tenant_id: str = "default"
    max_request_body_bytes: int = 1_048_576  # 1MB
    database_url: Optional[str] = None

    @classmethod
    def from_env(cls) -> "APIConfig":
        """Load API configuration from environment variables."""
        from reconcile.paypal import load_dotenv
        load_dotenv()
        return cls(
            admin_api_key=os.getenv("ADMIN_API_KEY") or os.getenv("ADMIN_TOKEN", ""),
            paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
            paypal_environment=os.getenv("PAYPAL_ENVIRONMENT", "sandbox").lower(),
            paypal_client_id=(
                os.getenv("PAYPAL_CLIENT_ID")
                or os.getenv("PAYPAL_SANDBOX_US_CLIENT_ID")
                or os.getenv("PAYPAL_SANDBOX_CLIENT_ID")
            ),
            paypal_client_secret=(
                os.getenv("PAYPAL_CLIENT_SECRET")
                or os.getenv("PAYPAL_SANDBOX_US_CLIENT_SECRET")
                or os.getenv("PAYPAL_SANDBOX_CLIENT_SECRET")
            ),
            paypal_base_url=(
                os.getenv("PAYPAL_BASE_URL")
                or os.getenv("PAYPAL_SANDBOX_US_BASE_URL")
                or os.getenv("PAYPAL_SANDBOX_BASE_URL")
            ),
            default_tenant_id=os.getenv("DEFAULT_TENANT_ID", "default"),
            max_request_body_bytes=int(os.getenv("MAX_REQUEST_BODY_BYTES", "1048576")),
            database_url=os.getenv("RECONCILE_DB_URL") or os.getenv("DATABASE_URL"),
        )

