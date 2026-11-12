from __future__ import annotations

import os
from urllib.parse import quote_plus, urlsplit


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def integer(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def number(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def database_url() -> str:
    user = quote_plus(env("DATABASE_USER", "postgres"))
    password = quote_plus(env("DATABASE_PASSWORD"))
    auth = user + (f":{password}" if password else "")
    host = env("DATABASE_HOST", "postgres")
    port = integer("DATABASE_PORT", 5432)
    name = env("DATABASE_NAME", "claims")
    return f"postgresql+psycopg://{auth}@{host}:{port}/{name}"


def redis_url() -> str:
    return f"redis://{env('REDIS_HOST', 'redis')}:{integer('REDIS_PORT', 6379)}/0"


def llm_timeout() -> float:
    return number("LLM_TIMEOUT_SECONDS", 20.0)


def llm_backend() -> str:
    return env("LLM_BACKEND", "stub").lower()


def llm_provider() -> str:
    configured = env("LLM_PROVIDER")
    if configured:
        return configured
    return urlsplit(llm_endpoint()).hostname or llm_backend()


def llm_endpoint() -> str:
    return env("LLM_ENDPOINT")


def llm_model_id(role: str | None = None) -> str:
    if role:
        role_model = env(f"LLM_{role.upper()}_MODEL_ID")
        if role_model:
            return role_model
    return env("LLM_MODEL_ID")


def llm_json_mode() -> bool:
    return env("LLM_JSON_MODE", "true").lower() not in {"0", "false", "no", "off"}


def llm_temperature() -> float:
    return number("LLM_TEMPERATURE", 0.0)


def llm_cost_per_million(role: str, direction: str) -> float:
    role_key = f"LLM_{role.upper()}_{direction.upper()}_COST_PER_MILLION"
    global_key = f"LLM_{direction.upper()}_COST_PER_MILLION"
    raw = env(role_key) or env(global_key, "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def llm_api_key() -> str:
    return env("LLM_API_KEY")


def llm_stub_host() -> str:
    return env("LLM_STUB_HOST", "llm-stub")


def llm_stub_port() -> int:
    return integer("LLM_STUB_PORT", 8080)


def prompt_version() -> str:
    return env("PROMPT_VERSION", "coverage-v2")


def index_dir() -> str:
    return env("INDEX_DIR", "data/index")


def corpus_dir() -> str:
    return env("CORPUS_DIR", "corpus")
