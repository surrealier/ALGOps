"""LLM-backed PM / Analyst / report authoring, behind a planner interface.

The loop nodes call an optional ``LLMPlanner``. When none is wired (LLMConfig.mode='none',
the M0 default) the nodes use their deterministic stubs. Each method is defensive: on any LLM
error it returns ``None`` so the node falls back to the deterministic path — a flaky open model
can never crash a campaign. Stop authority still lives in the router, never the LLM.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from algops.core import ParamSpec


class ParamProposal(BaseModel):
    """The PM's next candidate: a multi-parameter change (``changes``) applied on top of the
    best parameters so far. ``param_key``/``new_value`` is the v1 single-change shape."""

    changes: dict[str, float | int | bool | str] = Field(default_factory=dict)
    param_key: str | None = None
    new_value: float | int | bool | str | None = None
    rationale: str = ""

    def as_changes(self) -> dict[str, Any]:
        out = dict(self.changes)
        if self.param_key and self.new_value is not None and self.param_key not in out:
            out[self.param_key] = self.new_value
        return out


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


def _objective_text(state: dict) -> str:
    obj = state.get("objective") or {}
    text = f"{state['target_metric']} {state['goal_direction']}"
    if state.get("target_value") is not None:
        text += f" (목표값 {state['target_value']})"
    cons = obj.get("constraints") or []
    if cons:
        text += " / 제약: " + ", ".join(f"{c['metric']} {c['op']} {c['value']}" for c in cons)
    return text


def _history_table(state: dict) -> str:
    obj = state.get("objective") or {}
    watch = [state.get("target_metric")] + [c["metric"] for c in (obj.get("constraints") or [])]
    rows = []
    for h in [h for h in state.get("history", []) if h.get("kind", "search") == "search"][-10:]:
        params = ", ".join(f"{k}={v}" for k, v in h["params"].items())
        if h.get("status", "evaluated") != "evaluated":
            rows.append(f"- iter {h['iteration']}: [{params}] → 실패 ({h.get('error_kind')})")
            continue
        m = h.get("metrics") or {}
        vals = ", ".join(f"{n}={m[n]:.4f}" for n in watch if n and n in m)
        rows.append(f"- iter {h['iteration']}: [{params}] → {vals}")
    return "\n".join(rows) or "(아직 실험 없음)"


def _spec_table(spec: ParamSpec, keys: list[str] | None = None) -> str:
    return "\n".join(
        f"- {p.key} ({p.dtype.value}, 범위 {p.range or p.enum}, step {p.step}, "
        f"영향 fp={p.influence.fp.value}/fn={p.influence.fn.value})"
        + (f" — {p.description}" if p.description else "")
        for p in spec.params
        if not keys or p.key in keys
    )


def _metrics_line(m: dict) -> str:
    return ", ".join(f"{k}={v:.4f}" for k, v in list(m.items())[:14]) or "-"


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
            "당신은 알고리즘 파라미터를 최적화하는 PM입니다. 제약을 지키면서 목표 지표를 "
            "개선하도록, 지금까지의 최선 파라미터에서 바꿀 값들을 changes 에 {키: 새 값} 으로 "
            "제안하세요. 반드시 허용 범위 안의 값만, 나열된 키만 사용합니다. 이미 평가한 조합은 피하세요."
        )
        user = (
            f"목표: {state.get('goal_text') or '-'} — {_objective_text(state)}\n"
            f"조정 가능한 파라미터:\n{_spec_table(spec, state.get('search_keys'))}\n\n"
            f"현재 최선 파라미터: {state.get('best_params') or state.get('current_params')}\n"
            f"지금까지의 실험:\n{_history_table(state)}\n\n"
            f"파라미터별 제거 기여(최근 런): {state.get('csv_sensitivity') or '없음'}\n"
            f"참고(과거 유사 실험): {state.get('rag_context') or '없음'}\n\n"
            f"다음에 평가할 changes 와 rationale 을 제안하세요."
        )
        result = await self._structured(ParamProposal, system, user)
        return result if isinstance(result, ParamProposal) else None

    async def analyze(self, state: dict) -> str | None:
        system = "당신은 알고리즘 분석가입니다. 파라미터 변화가 지표(특히 목표·제약 지표)에 미친 영향을 1-3문장으로 분석하세요."
        user = (
            f"현재 파라미터: {state.get('current_params')}\n"
            f"지표: {_metrics_line(state.get('last_metrics') or {})}\n"
            f"목표: {_objective_text(state)}\n"
            f"제거 사유 분포: {state.get('csv_drop_reasons') or '-'}\n"
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
            f"목표: {_objective_text(state)}\n"
            f"분석가 의견: {state.get('analyst_findings')}\n"
            f"이력:\n{_history_table(state)}"
        )
        result = await self._structured(Decision, system, user)
        if isinstance(result, Decision) and result.action in ("continue", "stop"):
            return result
        return None

    async def summarize(self, state: dict) -> str | None:
        system = "당신은 실험 리포트를 작성합니다. 결과를 한국어 마크다운으로 간결히 정리하세요. 숫자는 주어진 값만 쓰세요."
        user = (
            f"프로젝트: {state.get('project_name')} / 알고리즘 {state.get('algorithm_id')} / "
            f"iteration {state['iteration']}\n"
            f"파라미터: {state.get('current_params')}\n"
            f"지표: {_metrics_line(state.get('last_metrics') or {})}\n"
            f"목표: {_objective_text(state)}\n"
            f"제거 사유 분포: {state.get('csv_drop_reasons')}"
        )
        return await self._text(system, user)
