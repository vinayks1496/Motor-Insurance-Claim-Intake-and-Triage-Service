from __future__ import annotations

import re

INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"(reveal|print|show|expose)\s+(the\s+)?(system\s+)?prompt", re.I),
    re.compile(r"call\s+(the\s+)?(tool|function)\s+", re.I),
    re.compile(r"act\s+as\s+(the\s+)?system", re.I),
    re.compile(r"disregard\s+(all\s+)?(rules|guardrails|instructions)", re.I),
)

TOXIC_PATTERNS = (
    re.compile(r"\bkill yourself\b", re.I),
    re.compile(r"\bi will kill\b", re.I),
    re.compile(r"\b(you are|you're) (a )?(worthless|subhuman)\b", re.I),
    re.compile(r"\b(cunt|bitch|bastard|idiot|moron)\b", re.I),
)

UNAUTHORIZED_PATTERNS = (
    re.compile(r"\b(claim is|claim has been) (approved|accepted|rejected|denied|settled)\b", re.I),
    re.compile(r"\bwe (approve|reject|deny|accept|settle) (the )?claim\b", re.I),
    re.compile(r"\bpayable amount is\b", re.I),
)


def inspect_text(value: str) -> dict[str, bool]:
    return {
        "prompt_injection": any(pattern.search(value) for pattern in INJECTION_PATTERNS),
        "toxicity": any(pattern.search(value) for pattern in TOXIC_PATTERNS),
    }


def safe_free_text(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value)
    return re.sub(r"\s+", " ", cleaned).strip()


def unauthorized_output(value: str) -> bool:
    return any(pattern.search(value) for pattern in UNAUTHORIZED_PATTERNS)


def toxic_output(value: str) -> bool:
    return any(pattern.search(value) for pattern in TOXIC_PATTERNS)
