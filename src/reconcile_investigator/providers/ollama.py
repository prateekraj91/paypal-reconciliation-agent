"""Local Ollama model provider for the AI Exception Investigator."""

from __future__ import annotations

import json
import logging
import time
from typing import Optional
import httpx

from .base import InvestigationContext, InvestigationModelProvider, InvestigationRawResponse

logger = logging.getLogger(__name__)


class OllamaProviderError(Exception):
    """Exception raised when local Ollama inference fails."""


class OllamaProvider(InvestigationModelProvider):
    """Provider connecting to local Ollama instance (e.g. qwen3:8b)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:8b",
        timeout_seconds: float = 300.0,
        max_retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def check_availability(self) -> bool:
        """Check whether local Ollama is running and responsive."""
        try:
            with httpx.Client(timeout=3.0) as client:
                res = client.get(f"{self.base_url}/api/tags")
                return res.status_code == 200
        except Exception:
            return False

    def generate_investigation(
        self,
        context: InvestigationContext,
        system_prompt: str,
        user_prompt: str,
    ) -> InvestigationRawResponse:
        """Invoke local Ollama with JSON mode enabled."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 8192,
            },
        }

        start_time = time.perf_counter()
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    resp = client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                    msg = data.get("message") or {}
                    raw_content = msg.get("content", "").strip()

                    # Defensively strip markdown fences if returned
                    if raw_content.startswith("```json"):
                        raw_content = raw_content[7:]
                    elif raw_content.startswith("```"):
                        raw_content = raw_content[3:]
                    if raw_content.endswith("```"):
                        raw_content = raw_content[:-3]
                    raw_content = raw_content.strip()

                    latency_ms = (time.perf_counter() - start_time) * 1000.0
                    prompt_tokens = data.get("prompt_eval_count")
                    completion_tokens = data.get("eval_count")

                    return InvestigationRawResponse(
                        raw_json_str=raw_content,
                        provider_name="ollama",
                        model_name=self.model,
                        prompt_version="1.0.0",
                        latency_ms=latency_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Ollama inference attempt %d failed: %s",
                    attempt + 1,
                    str(exc),
                )
                if attempt < self.max_retries:
                    time.sleep(1.0 * (attempt + 1))

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        raise OllamaProviderError(f"Ollama inference failed after {self.max_retries + 1} attempts: {last_exc}")
