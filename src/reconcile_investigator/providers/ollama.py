"""Local Ollama model provider for the AI Exception Investigator."""

from __future__ import annotations

import json
import logging
import time
from typing import Optional
import httpx

from .base import InvestigationContext, InvestigationModelProvider, InvestigationRawResponse

logger = logging.getLogger(__name__)


def extract_clean_json(text: str) -> str:
    """Robustly extract valid JSON from LLM output, discarding thoughts, fences, and trailing commas."""
    import re
    content = text.strip()

    # 1. Strip thinking blocks if present
    if "</think>" in content:
        content = content.split("</think>", 1)[1].strip()
    if "...done thinking." in content:
        content = content.split("...done thinking.", 1)[1].strip()

    # 2. Extract from markdown code fence if present
    if "熟json" in content or "```json" in content:
        if "```json" in content:
            after_fence = content.split("```json", 1)[1]
            if "```" in after_fence:
                content = after_fence.split("```", 1)[0].strip()
            else:
                content = after_fence.strip()
    elif "```" in content:
        parts = content.split("```")
        if len(parts) >= 3:
            content = parts[1].strip()
        elif len(parts) == 2:
            content = parts[1].strip()

    # 3. Find outer braces
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        content = content[first_brace : last_brace + 1]

    # 4. Direct validation check
    try:
        json.loads(content)
        return content
    except Exception:
        pass

    # 5. Remove trailing commas before closing braces/brackets (common LLM flaw)
    sanitized = re.sub(r",\s*([\]}])", r"\1", content)
    try:
        json.loads(sanitized)
        return sanitized
    except Exception:
        pass

    # 6. Repair missing commas between array elements or object properties
    sanitized = re.sub(r"\}\s*\n*\s*\{", "}, {", sanitized)
    sanitized = re.sub(r'("|\d|true|false|null|\]|\})\s*\n\s*(")', r"\1,\n\2", sanitized)
    try:
        json.loads(sanitized)
        return sanitized
    except Exception:
        pass

    # 7. Iterative error-position-guided repair for LLM-generated JSON
    curr = sanitized
    for _ in range(20):
        try:
            json.loads(curr)
            return curr
        except json.JSONDecodeError as exc:
            msg = str(exc).lower()
            pos = exc.pos
            idx = pos - 1
            while idx >= 0 and curr[idx].isspace():
                idx -= 1
            if "trailing comma" in msg or "expecting property name" in msg:
                if idx >= 0 and curr[idx] == ",":
                    curr = curr[:idx] + curr[idx + 1 :]
                    continue
                elif pos < len(curr) and curr[pos] == ",":
                    curr = curr[:pos] + curr[pos + 1 :]
                    continue
            if "expecting ',' delimiter" in msg or "expecting ':' delimiter" in msg:
                if idx >= 0 and curr[idx] in ('"', "}", "]", "e", "d", "l", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    curr = curr[: idx + 1] + "," + curr[idx + 1 :]
                    continue
            break

    # 8. Truncated output repair (balances unclosed braces if output hit token limit)
    try:
        json.loads(curr)
        return curr
    except Exception:
        clean = curr.rstrip()
        while clean:
            open_braces = clean.count("{") - clean.count("}")
            open_brackets = clean.count("[") - clean.count("]")
            attempt = clean + ("]" * max(0, open_brackets)) + ("}" * max(0, open_braces))
            try:
                json.loads(attempt)
                return attempt
            except Exception:
                pass
            last_delim = max(clean.rfind(","), clean.rfind("\n"))
            if last_delim == -1:
                break
            clean = clean[:last_delim].rstrip()

    return curr


class OllamaProviderError(Exception):
    """Exception raised when local Ollama inference fails."""


class OllamaProvider(InvestigationModelProvider):
    """Provider connecting to local Ollama instance (e.g. qwen3:8b)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:8b",
        timeout_seconds: float = 300.0,
        max_retries: int = 0,
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
            "think": False,
            "options": {
                "temperature": 0.0,
                "num_ctx": 4096,
                "num_predict": 2048,
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
                    clean_content = extract_clean_json(raw_content)

                    latency_ms = (time.perf_counter() - start_time) * 1000.0
                    prompt_tokens = data.get("prompt_eval_count")
                    completion_tokens = data.get("eval_count")

                    return InvestigationRawResponse(
                        raw_json_str=clean_content,
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
