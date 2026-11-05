"""Vercel Serverless Function entry point for PayPal Reconciliation Agent API.

Exposes the FastAPI application to Vercel's Python runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure 'src' directory is in Python module search path
_src_dir = Path(__file__).resolve().parent.parent / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from reconcile_api.main import app  # noqa: F401, E402
