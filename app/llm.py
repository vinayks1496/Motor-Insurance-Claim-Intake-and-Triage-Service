from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import redis

from app.config import (
    llm_api_key,
    llm_backend,
    llm_cost_per_million,
    llm_endpoint,
    llm_json_mode,
    llm_model_id,
    llm_provider,
    llm_stub_host,
    llm_stub_port,
    llm_temperature,
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
    provider: str
    model_id: str


def _chat_completions_url(endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    if not endpoint:
        return ""
    return endpoint if endpoint.endswith("/chat/completions") else f"{endpoint}/chat/completions"


def _retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(10.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
    return min(8.0, 1.0 * (2**attempt))


def _json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str):
        raise ValueError("model response did not contain JSON text")

    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    decoder = json.JSONDecoder()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
        for offset, character in enumerate(text):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[offset:])
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(value, dict):
        raise ValueError("model response was not a JSON object")
    return value


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
        if self.backend not in {"stub", "openai_compatible"}:
            raise ProviderUnavailable(f"unsupported LLM_BACKEND: {self.backend}")
        model_id = "deterministic-stub" if self.backend == "stub" else llm_model_id(role)
        if self.backend == "openai_compatible" and (not llm_endpoint() or not model_id):
            raise ProviderUnavailable(
                f"external model configuration is incomplete for the {role} agent"
            )
        if self.backend == "openai_compatible" and not llm_endpoint().startswith(("http://", "https://")):
            raise ProviderUnavailable("LLM_ENDPOINT must be an HTTP or HTTPS URL")
        if self.redis.get("llm:circuit:open"):
            raise ProviderUnavailable("provider circuit is open")

        error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._request(role, payload, correlation_id, prompt_version, model_id)
                self.redis.delete("llm:circuit:failures")
                return LLMResult(
                    value=result["value"],
                    input_tokens=int(result.get("input_tokens", 0)),
                    output_tokens=int(result.get("output_tokens", 0)),
                    cost_usd=float(result.get("cost_usd", 0.0)),
                    retries=attempt,
                    prompt_version=prompt_version,
                    provider=llm_provider(),
                    model_id=model_id,
                )
            except Exception as exc:
                error = exc
                failures = self.redis.incr("llm:circuit:failures")
                if failures == 1:
                    self.redis.expire("llm:circuit:failures", 45)
                if failures >= 3:
                    self.redis.setex("llm:circuit:open", 30, "1")
                if attempt < 2:
                    time.sleep(_retry_delay(exc, attempt))

        raise ProviderUnavailable(f"provider call failed after bounded retries: {error}") from error

    def _request(
        self,
        role: str,
        payload: dict[str, Any],
        correlation_id: str,
        prompt_version: str,
        model_id: str,
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
            instructions = payload.get("prompt", "")
            request_data = {key: value for key, value in payload.items() if key != "prompt"}
            body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)},
                ],
                "temperature": llm_temperature(),
            }
            if llm_json_mode():
                body["response_format"] = {"type": "json_object"}
            headers = {"Authorization": f"Bearer {llm_api_key()}"} if llm_api_key() else {}
            url = _chat_completions_url(endpoint)

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
        value = _json_object(content)
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        input_rate = llm_cost_per_million(role, "input")
        output_rate = llm_cost_per_million(role, "output")
        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        return {
            "value": value,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        }
