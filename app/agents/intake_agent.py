from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.config import prompt_version
from app.llm import LLMClient


class IntakeState(TypedDict, total=False):
    fnol: dict[str, Any]
    correlation_id: str
    result: dict[str, Any]
    prompt_version: str


def _prompt(fnol: dict[str, Any]) -> str:
    import os
    from pathlib import Path

    path = Path(os.getenv("PROMPT_DIR", "prompts")) / "intake_v1.txt"
    template = path.read_text(encoding="utf-8")
    return template.format(fnol=fnol)


class IntakeAgent:
    """A distinct extraction agent, deployed and queued independently from coverage review."""

    def __init__(self) -> None:
        self.client = LLMClient()
        graph = StateGraph(IntakeState)
        graph.add_node("classify_and_extract", self._classify_and_extract)
        graph.set_entry_point("classify_and_extract")
        graph.add_edge("classify_and_extract", END)
        self.graph = graph.compile()

    def _classify_and_extract(self, state: IntakeState) -> IntakeState:
        version = "intake-v1"
        answer = self.client.generate(
            role="intake",
            payload={"prompt": _prompt(state["fnol"]), "fnol": state["fnol"]},
            correlation_id=state["correlation_id"],
            prompt_version=version,
        )
        result = answer.value
        if result.get("classification") not in {"own_damage", "third_party", "theft", "total_loss"}:
            raise ValueError("intake agent returned an invalid claim classification")
        result["_usage"] = {
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
            "cost_usd": answer.cost_usd,
            "retries": answer.retries,
            "prompt_version": version,
        }
        return {"result": result, "prompt_version": version}

    def invoke(self, fnol: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        return self.graph.invoke(
            {"fnol": fnol, "correlation_id": correlation_id, "prompt_version": "intake-v1"}
        )
