"""LLM-backed PM / Analyst / report authoring, behind a planner interface.

The loop nodes call an optional ``LLMPlanner``. When none is wired (LLMConfig.mode='none',
the M0 default) the nodes use their deterministic stubs. Each method is defensive: on any LLM
error it returns ``None`` so the node falls back to the deterministic path — a flaky open model
can never crash a campaign. Stop authority still lives in the router, never the LLM.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from algops.core import Metrics, ParamSpec


class ParamProposal(BaseModel):
    param_key: str
    new_value: float
    rationale: str = ""


class Decision(BaseModel):
    action: str  # "continue" | "stop"
    stop_reason: str | None = None
    rationale: str = ""


@runtime_checkable
class LLMPlanner(Protocol):
    async def propose(self, state: dict, spec: ParamSpec) -> ParamProposal | None: ...
    async def analyze(self, state: dict) -> str | None: ...
    async def decide(self, state: dict) -> Decision | None: ...
    async def summarize(self, state: dict) -> str | None: ...


def _history_table(state: dict) -> str:
    rows = []
    for h in state.get("history", [])[-8:]:
        params = ", ".join(f"{k}={v}" for k, v in h["params"].items())
        rows.append(f"- iter {h['iteration']}: [{params}] → objective={h['objective']:.4f}")
    return "\n".join(rows) or "(아직 실험 없음)"


def _spec_table(spec: ParamSpec) -> str:
    return "\n".join(
        f"- {p.key} ({p.dtype.value}, 범위 {p.range}, step {p.step}, "
        f"영향 fp={p.influence.fp.value}/fn={p.influence.fn.value})"
        for p in spec.params
    )


class OpenAILLMPlanner:
    """Drives the agents via an OpenAI-compatible chat model + structured outputs."""

    def __init__(self, chat: Any) -> None:
        self.chat = chat

    async def _structured(self, schema: type[BaseModel], system: str, user: str) -> Any | None:
        for method in ("json_schema", "function_calling"):
            try:
                runnable = self.chat.with_structured_output(schema, method=method)
                return await runnable.ainvoke(
                    [("system", system), ("human", user)]
                )
            except Exception:
                continue
        return None

    async def _text(self, system: str, user: str) -> str | None:
        try:
            resp = await self.chat.ainvoke([("system", system), ("human", user)])
            return resp.content if hasattr(resp, "content") else str(resp)
        except Exception:
            return None

    async def propose(self, state: dict, spec: ParamSpec) -> ParamProposal | None:
        system = (
            "당신은 후처리 알고리즘 파라미터를 최적화하는 PM입니다. "
            "목표 지표를 개선하도록 다음에 평가할 파라미터 값을 하나 제안하세요. "
            "반드시 허용 범위 안에서 한 번에 한 파라미터만 조정합니다."
        )
        user = (
            f"목표: {state.get('goal_text')} (지표 {state['target_metric']} "
            f"{state['goal_direction']} 목표값 {state['target_value']})\n"
            f"조정 가능한 파라미터:\n{_spec_table(spec)}\n\n"
            f"지금까지의 실험:\n{_history_table(state)}\n\n"
            f"참고(과거 유사 실험): {state.get('rag_context') or '없음'}\n\n"
            f"다음에 평가할 param_key 와 new_value 를 제안하세요."
        )
        result = await self._structured(ParamProposal, system, user)
        return result if isinstance(result, ParamProposal) else None

    async def analyze(self, state: dict) -> str | None:
        m = Metrics(**state["last_metrics"])
        system = "당신은 알고리즘 분석가입니다. 파라미터 변화가 오탐/미탐에 미친 영향을 1-3문장으로 분석하세요."
        user = (
            f"현재 파라미터: {state.get('current_params')}\n"
            f"지표: fp_rate={m.fp_rate:.4f}, fn_rate={m.fn_rate:.4f}, "
            f"precision={m.precision:.4f}, recall={m.recall:.4f}, f1={m.f1:.4f}\n"
            f"목표: {state['target_metric']} {state['goal_direction']} {state['target_value']}\n"
            f"직전 이력:\n{_history_table(state)}"
        )
        return await self._text(system, user)

    async def decide(self, state: dict) -> Decision | None:
        system = (
            "당신은 PM입니다. 분석 결과를 보고 실험을 계속할지(continue) 멈출지(stop) 결정하세요. "
            "이미 목표를 달성했거나 더 개선 여지가 없으면 stop 하세요. "
            "최대 반복/목표 도달은 시스템이 별도로 강제하므로, 당신은 개선 가능성만 판단합니다."
        )
        user = (
            f"목표: {state['target_metric']} {state['goal_direction']} {state['target_value']}\n"
            f"분석가 의견: {state.get('analyst_findings')}\n"
            f"이력:\n{_history_table(state)}"
        )
        result = await self._structured(Decision, system, user)
        if isinstance(result, Decision) and result.action in ("continue", "stop"):
            return result
        return None

    async def summarize(self, state: dict) -> str | None:
        system = "당신은 실험 리포트를 작성합니다. 결과를 한국어 마크다운으로 간결히 정리하세요."
        m = Metrics(**state["last_metrics"])
        user = (
            f"프로젝트: {state.get('project_name')} / iteration {state['iteration']}\n"
            f"파라미터: {state.get('current_params')}\n"
            f"지표: fp_rate={m.fp_rate:.4f}, fn_rate={m.fn_rate:.4f}, f1={m.f1:.4f}, "
            f"precision={m.precision:.4f}, recall={m.recall:.4f}\n"
            f"목표: {state['target_metric']} {state['goal_direction']} {state['target_value']}\n"
            f"제거 사유 분포: {state.get('csv_drop_reasons')}"
        )
        return await self._text(system, user)
