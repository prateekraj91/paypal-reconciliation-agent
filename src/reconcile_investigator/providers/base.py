"""Base abstractions for investigation LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class InvestigationContext:
    """Pre-assembled tenant-scoped financial context provided to the model."""

    investigation_id: str
    tenant_id: str
    target_type: str
    target_id: str
    target_data: Dict[str, Any] = field(default_factory=dict)
    evidence_records: List[Dict[str, Any]] = field(default_factory=list)
    candidate_invoices: List[Dict[str, Any]] = field(default_factory=list)
    related_events: Dict[str, Any] = field(default_factory=dict)
    missing_evidence: List[Dict[str, Any]] = field(default_factory=list)
    suspected_prompt_injection: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvestigationRawResponse:
    """Raw structured output returned by an investigation model provider."""

    raw_json_str: str
    provider_name: str
    model_name: str
    prompt_version: str
    latency_ms: float
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    error: Optional[str] = None


class InvestigationModelProvider(ABC):
    """Abstract interface for local or synthetic LLM providers."""

    @abstractmethod
    def generate_investigation(
        self,
        context: InvestigationContext,
        system_prompt: str,
        user_prompt: str,
    ) -> InvestigationRawResponse:
        """Execute model inference and return raw JSON output."""
        raise NotImplementedError
