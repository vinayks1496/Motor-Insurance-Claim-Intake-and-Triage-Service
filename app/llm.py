from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import redis

from app.config import (
    llm_api_key,
    llm_backend,
    llm_endpoint,
    llm_model_id,
    llm_stub_host,
    llm_stub_port,
    llm_timeout,
    redis_url,
)

logger = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    pass


@dataclass
class LLMResult:
    value: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    retries: int
    prompt_version: str


class LLMClient:
    """Configurable model boundary with bounded retries and a shared circuit breaker."""

    def __init__(self) -> None:
        self.redis = redis.Redis.from_url(redis_url(), decode_responses=True)
        self.backend = llm_backend()
        self.timeout = llm_timeout()

    def generate(
        self,
        role: str,
        payload: dict[str, Any],
        correlation_id: str,
        prompt_version: str,
    ) -> LLMResult:
        if self.redis.get("llm:circuit:open"):
            raise ProviderUnavailable("provider circuit is open")

        error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._request(role, payload, correlation_id, prompt_version)
                self.redis.delete("llm:circuit:failures")
                return LLMResult(
                    value=result["value"],
                    input_tokens=int(result.get("input_tokens", 0)),
                    output_tokens=int(result.get("output_tokens", 0)),
                    cost_usd=float(result.get("cost_usd", 0.0)),
                    retries=attempt,
                    prompt_version=prompt_version,
                )
            except Exception as exc:
                error = exc
                failures = self.redis.incr("llm:circuit:failures")
                if failures == 1:
                    self.redis.expire("llm:circuit:failures", 45)
                if failures >= 3:
                    self.redis.setex("llm:circuit:open", 30, "1")
                if attempt < 2:
                    time.sleep(0.25 * (2**attempt))

        raise ProviderUnavailable(f"provider call failed after bounded retries: {error}") from error

    def _request(
        self,
        role: str,
        payload: dict[str, Any],
        correlation_id: str,
        prompt_version: str,
    ) -> dict[str, Any]:
        if self.redis.get("demo:provider-outage"):
            raise ProviderUnavailable("provider outage is enabled for demonstration")

        if self.backend == "stub":
            url = f"http://{llm_stub_host()}:{llm_stub_port()}/generate"
            body = {
                "role": role,
                "payload": payload,
                "correlation_id": correlation_id,
                "prompt_version": prompt_version,
            }
            headers = {}
        else:
            endpoint = llm_endpoint()
            model_id = llm_model_id()
            if not endpoint or not model_id:
                raise ProviderUnavailable("external model configuration is incomplete")
            instructions = payload.get("prompt", "")
            request_data = {key: value for key, value in payload.items() if key != "prompt"}
            body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            headers = {"Authorization": f"Bearer {llm_api_key()}"} if llm_api_key() else {}
            url = endpoint

        with httpx.Client(timeout=httpx.Timeout(self.timeout)) as client:
            response = client.post(url, json=body, headers=headers)
            response.raise_for_status()
            result = response.json()

        if self.backend == "stub":
            return {
                "value": result["value"],
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "cost_usd": result.get("cost_usd", 0.0),
            }

        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage") or {}
        value = json.loads(content)
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        input_rate = float(__import__("os").getenv("LLM_INPUT_COST_PER_MILLION", "0"))
        output_rate = float(__import__("os").getenv("LLM_OUTPUT_COST_PER_MILLION", "0"))
        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        return {
            "value": value,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        }
