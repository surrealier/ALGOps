"""InMemoryRagStore — dependency-free hybrid-ish retrieval + learned parameter influence.

Good enough to give real cross-experiment memory in dev/tests without standing up Qdrant/Neo4j.
Retrieval = dense cosine (hashed embeddings) + recency, then a (noop) rerank. The learned
``param_hints`` are computed by walking run lineage: for each change, did the target metric
improve? — the in-memory analogue of Neo4j ``(:Parameter)-[:INFLUENCES]->(:Metric)``.
"""

from __future__ import annotations

from algops.core import GoalDirection

from .embed import Embedder, HashingEmbedder, cosine
from .models import ParamHint, RagContext, RagQuery, RunReport
from .rerank import NoopReranker, Reranker


def _doc_text(r: RunReport) -> str:
    return f"{r.goal_tag} {' '.join(r.params_changed)} {r.report_md[:800]}"


def _improved(delta: float, direction: GoalDirection) -> bool:
    return delta < 0 if direction is GoalDirection.minimize else delta > 0


class InMemoryRagStore:
    def __init__(self, embedder: Embedder | None = None, reranker: Reranker | None = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.reranker = reranker or NoopReranker()
        self._reports: list[RunReport] = []
        self._vecs: dict[str, list[float]] = {}
        self._seq = 0

    def index_run(self, report: RunReport) -> None:
        self._seq += 1
        report = report.model_copy(update={"timestamp_index": self._seq})
        self._reports.append(report)
        self._vecs[report.run_id] = self.embedder.embed(_doc_text(report))

    def num_docs(self) -> int:
        return len(self._reports)

    def _param_hints(self, project_id: str, target_metric: str) -> list[ParamHint]:
        if not target_metric:
            return []
        direction = GoalDirection.for_metric(target_metric)
        by_id = {r.run_id: r for r in self._reports if r.project_id == project_id}
        agg: dict[tuple[str, str], ParamHint] = {}
        for r in by_id.values():
            parent = by_id.get(r.derived_from) if r.derived_from else None
            delta = r.metric_deltas.get(target_metric)
            if parent is None or delta is None:
                continue
            for key in r.params_changed:
                old, new = parent.params.get(key), r.params.get(key)
                if old is None or new is None or old == new:
                    continue
                dir_ = "up" if float(new) > float(old) else "down"
                hint = agg.setdefault((key, dir_), ParamHint(param_key=key, direction=dir_))
                if _improved(delta, direction):
                    hint.helped += 1
                else:
                    hint.hurt += 1
        ranked = sorted(agg.values(), key=lambda h: (h.helped - h.hurt), reverse=True)
        return [h for h in ranked if h.helped or h.hurt][:5]

    def retrieve(self, query: RagQuery) -> RagContext:
        pool = [r for r in self._reports if r.project_id == query.project_id]
        if not pool:
            return RagContext()
        qvec = self.embedder.embed(f"{query.goal_text} {query.target_metric}")
        scored = []
        for r in pool:
            sim = cosine(qvec, self._vecs[r.run_id])
            recency = r.timestamp_index / (self._seq or 1)
            scored.append((sim + 0.15 * recency, r))
        scored.sort(key=lambda x: -x[0])
        candidates = [(r.run_id, _doc_text(r)) for _, r in scored[: max(query.top_k * 3, query.top_k)]]
        top_ids = self.reranker.rerank(query.goal_text, candidates, query.top_k)
        by_id = {r.run_id: r for r in pool}
        top = [by_id[i] for i in top_ids if i in by_id]
        hints = self._param_hints(query.project_id, query.target_metric)
        return RagContext(text=_render(top, hints, query), anchor_run_ids=top_ids, param_hints=hints)


def _render(top: list[RunReport], hints: list[ParamHint], query: RagQuery) -> str:
    if not top and not hints:
        return ""
    lines = [f"과거 유사 실험 {len(top)}건 참조 (목표 지표 {query.target_metric}):"]
    for r in top:
        changed = ", ".join(r.params_changed) or "(없음)"
        val = f"{r.primary_metric_value:.4f}" if r.primary_metric_value is not None else "-"
        lines.append(f"- iter {r.iteration_index}: 변경=[{changed}] → {r.primary_metric}={val}")
    if hints:
        lines.append(f"\n학습된 파라미터 영향 (목표 {query.target_metric} 개선 기준):")
        for h in hints:
            lines.append(f"- {h.param_key} {h.direction}: 개선 {h.helped}회 / 악화 {h.hurt}회")
    return "\n".join(lines)
