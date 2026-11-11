from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

app = FastAPI(title="Local model stub", docs_url=None, redoc_url=None)


class StubRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    payload: dict[str, Any]
    correlation_id: str
    prompt_version: str


def _classify(text: str, fnol: dict[str, Any]) -> dict[str, Any]:
    normalized = text.lower()
    if any(word in normalized for word in ("stolen", "theft", "car is missing")):
        category = "theft"
    elif any(word in normalized for word in ("third party", "another vehicle", "pedestrian", "injur")):
        category = "third_party"
    elif any(word in normalized for word in ("total loss", "written off", "beyond repair")):
        category = "total_loss"
    else:
        category = "own_damage"
    use = fnol.get("vehicle_use", "unknown")
    if use == "unknown" and any(word in normalized for word in ("delivery", "deliver", "food order")):
        use = "delivery"
    return {
        "classification": category,
        "entities": {
            "policy_number": fnol.get("policy_number"),
            "registration_number": fnol.get("registration_number"),
            "loss_date": fnol.get("loss_date"),
            "vehicle_use": use,
            "estimate_inr": fnol.get("estimate_inr"),
            "injury_reported": fnol.get("injury_reported", False),
            "police_reported": fnol.get("police_reported", False),
        },
        "risk_flags": [
            flag
            for flag, yes in (
                ("commercial_or_delivery_use", use in ("delivery", "commercial")),
                ("injury_reported", bool(fnol.get("injury_reported"))),
                ("police_notified_for_theft", category == "theft" and bool(fnol.get("police_reported"))),
            )
            if yes
        ],
        "confidence": 0.91,
    }


def _coverage_action(payload: dict[str, Any]) -> dict[str, Any]:
    fnol = payload.get("fnol", {})
    fields = payload.get("fields", {})
    description = str(fnol.get("loss_description", ""))
    lower = description.lower()
    history = payload.get("tool_history", [])
    search_calls = [entry for entry in history if entry.get("tool") == "search_policy"]
    evidence = payload.get("evidence", [])
    insurer = str(fnol.get("insurer", "")).lower()
    product = str(fnol.get("product", "")).lower()

    if "new india" not in insurer or "private car" not in product:
        return {
            "action": "final",
            "summary": "This request is outside the indexed insurer and product corpus, so the policy position cannot be supported here.",
            "position": "outside_corpus",
            "citations": [],
            "route": "investigation",
            "route_reason": "A claims handler must identify and review the applicable policy wording.",
            "verification_notes": "No in-corpus policy evidence applies.",
        }

    if len(search_calls) == 0:
        if fields.get("classification") == "theft":
            query = "private car package policy theft own damage cover Section I"
        else:
            query = "private car package policy accidental external means own damage Section I"
        return {"action": "search_policy", "query": query}

    use = str(fields.get("entities", {}).get("vehicle_use", fnol.get("vehicle_use", "unknown")))
    delivery_case = use in ("delivery", "commercial") or any(
        word in lower for word in ("delivery", "deliver", "food order", "business use", "paid work")
    )
    if len(search_calls) == 1 or len(evidence) < 2:
        query = (
            "private car schedule limitations as to use carriage of goods delivery trade business hire reward"
            if delivery_case
            else "private car general exceptions conditions driver clause claim notice exclusions"
        )
        return {"action": "search_policy", "query": query}

    citations = [item["passage_id"] for item in evidence[:4]]
    if delivery_case:
        return {
            "action": "final",
            "summary": (
                "The accident description may match the own-damage grant for accidental external damage. "
                "The reported food-delivery use also raises a separate private-car permitted-use restriction "
                "about carrying goods in trade or business. The interaction is not resolved here; a claims handler "
                "must review the policy schedule and any endorsements."
            ),
            "position": "potentially_limited",
            "citations": citations,
            "route": "investigation",
            "route_reason": "The own-damage grant and the delivery-use restriction both need a human coverage review.",
            "verification_notes": "Both the apparent cover and the modifying use restriction were retrieved.",
        }

    if fields.get("classification") == "theft" and not fnol.get("police_reported"):
        route = "investigation"
        reason = "The claim guidance calls for immediate police intimation for theft; confirm the report with the handler."
    elif fnol.get("injury_reported") or fields.get("classification") in ("third_party", "total_loss"):
        route = "surveyor_assignment"
        reason = "The reported injury, third-party involvement, or possible total loss needs specialist assessment."
    elif float(fnol.get("estimate_inr") or 0) <= 200_000:
        route = "fast-track"
        reason = "The reported loss is small and the retrieved policy evidence does not show a use conflict."
    else:
        route = "surveyor_assignment"
        reason = "A repair estimate above the low-cost triage band needs surveyor review."

    return {
        "action": "final",
        "summary": (
            "The reported event appears consistent with an insured vehicle-loss peril in the indexed wording, "
            "subject to the complete policy schedule, exclusions, endorsements, and claim evidence."
        ),
        "position": "potentially_supported",
        "citations": citations,
        "route": route,
        "route_reason": reason,
        "verification_notes": "The returned citations come from the current retrieval set.",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/generate")
async def generate(request: StubRequest) -> dict[str, Any]:
    delay = float(os.getenv("LLM_STUB_DELAY_SECONDS", "1.1"))
    await asyncio.sleep(max(0, min(delay, 10)))
    if os.getenv("STUB_FORCE_FAILURE", "").lower() in ("1", "true", "yes"):
        raise HTTPException(status_code=503, detail="demonstration provider outage")

    if request.role == "intake":
        fnol = request.payload.get("fnol", {})
        value = _classify(str(fnol.get("loss_description", "")), fnol)
    elif request.role == "coverage":
        value = _coverage_action(request.payload)
    else:
        raise HTTPException(status_code=400, detail="unknown agent role")

    raw = str(request.payload)
    input_tokens = max(1, len(raw) // 4)
    output_tokens = max(1, len(str(value)) // 4)
    return {
        "value": value,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": 0.0,
    }
