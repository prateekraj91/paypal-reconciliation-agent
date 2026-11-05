"""Authentication and security utilities for API endpoints."""

from __future__ import annotations

import secrets
from typing import Optional
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import APIConfig

_bearer_security = HTTPBearer(auto_error=False)


def require_admin_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_security),
) -> str:
    """Validate admin bearer token with constant-time comparison.
    
    Rejects missing or invalid tokens with HTTP 401.
    If the server has no configured admin token, fails closed.
    """
    config = APIConfig.from_env()
    expected_token = config.admin_api_key

    if not expected_token:
        # Fail closed if admin key is not configured
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin API key is not configured on the server",
        )

    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header with Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided_token = credentials.credentials
    # Constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return provided_token
