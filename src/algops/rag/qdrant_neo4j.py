"""QdrantNeo4jRagStore — production hybrid RAG (plan §3.3). Lazy imports so the default dev
path needs neither server nor the 'rag' extra.

Qdrant holds run-report documents (dense vector, payload pre-filtered by project_id). Neo4j
holds the graph: Run / Parameter / Metric with CHANGED, DERIVED_FROM, and INFLUENCES edges whose
``importance`` is refined empirically from observed metric deltas. Retrieval = vector candidates
-> rerank -> graph expansion for learned parameter influence.

This module is exercised only when ``rag_backend='qdrant_neo4j'`` is configured against live
servers; it is import-safe (and a no-op-friendly) without them.
"""

from __future__ import annotations

from algops.core import GoalDirection, MasterSettings

from .embed import HashingEmbedder
from .models import ParamHint, RagContext, RagQuery, RunReport
from .rerank import NoopReranker

COLLECTION = "algops_run_reports"


class QdrantNeo4jRagStore:  # pragma: no cover - requires live Qdrant + Neo4j
    def __init__(self, settings: MasterSettings) -> None:
        self.s = settings
        self.embedder = (
            HashingEmbedder() if settings.rag_embed == "hashing" else _fastembed(settings)
        )
        self.reranker = NoopReranker() if settings.rag_rerank == "noop" else _bge(settings)
        self._qc = None
        self._neo = None
        self._ensured = False

    # --- clients (lazy) ---
    def _qdrant(self):
        if self._qc is None:
            from qdrant_client import QdrantClient

            self._qc = QdrantClient(url=self.s.qdrant_url, api_key=self.s.qdrant_api_key or None)
        return self._qc

    def _neo4j(self):
        if self._neo is None:
            from neo4j import GraphDatabase

            self._neo = GraphDatabase.driver(
                self.s.neo4j_uri, auth=(self.s.neo4j_user, self.s.neo4j_password)
            )
        return self._neo

    def _ensure(self) -> None:
        if self._ensured:
            return
        from qdrant_client import models

        qc = self._qdrant()
        if not qc.collection_exists(COLLECTION):
            qc.create_collection(
                COLLECTION,
                vectors_config=models.VectorParams(
                    size=self.embedder.dim, distance=models.Distance.COSINE
                ),
            )
        self._ensured = True

    # --- write ---
    def index_run(self, report: RunReport) -> None:
        self._ensure()
        from qdrant_client import models

        text = f"{report.goal_tag}\n{report.report_md}"
        self._qdrant().upsert(
            COLLECTION,
            points=[
                models.PointStruct(
                    id=report.run_id,
                    vector=self.embedder.embed(text),
                    payload={
                        "run_id": report.run_id,
                        "experiment_id": report.experiment_id,
                        "project_id": report.project_id,
                        "iteration_index": report.iteration_index,
                        "goal_tag": report.goal_tag,
                        "params_changed": report.params_changed,
                        "metric_deltas": report.metric_deltas,
                        "primary_metric": report.primary_metric,
                        "primary_metric_value": report.primary_metric_value,
                        "text": text,
                    },
                )
            ],
        )
        self._write_graph(report)

    def _write_graph(self, r: RunReport) -> None:
        direction = GoalDirection.for_metric(r.primary_metric) if r.primary_metric else None
        cypher = """
        MERGE (p:Project {project_id:$project_id})
        MERGE (e:Experiment {experiment_id:$experiment_id})-[:PART_OF]->(p)
        MERGE (run:Run {run_id:$run_id})
          SET run.iteration_index=$iteration_index, run.primary_metric_value=$pmv
        MERGE (run)-[:OF_EXPERIMENT]->(e)
        FOREACH (k IN $params_changed |
          MERGE (par:Parameter {param_key:k})
          MERGE (par)-[:OWNED_BY]->(p)
          MERGE (run)-[:CHANGED]->(par))
        WITH run
        FOREACH (_ IN CASE WHEN $derived_from IS NULL THEN [] ELSE [1] END |
          MERGE (prev:Run {run_id:$derived_from})
          MERGE (run)-[:DERIVED_FROM]->(prev))
        """
        with self._neo4j().session() as s:
            s.run(
                cypher,
                project_id=r.project_id, experiment_id=r.experiment_id, run_id=r.run_id,
                iteration_index=r.iteration_index, pmv=r.primary_metric_value,
                params_changed=r.params_changed, derived_from=r.derived_from,
            )
            # Refine INFLUENCES.importance from this run's observed metric delta.
            if direction is not None and r.primary_metric in r.metric_deltas:
                improved = (
                    r.metric_deltas[r.primary_metric] < 0
                    if direction is GoalDirection.minimize
                    else r.metric_deltas[r.primary_metric] > 0
                )
                s.run(
                    """
                    UNWIND $params AS k
                    MERGE (par:Parameter {param_key:k})
                    MERGE (m:Metric {name:$metric})
                    MERGE (par)-[inf:INFLUENCES]->(m)
                      ON CREATE SET inf.helped=0, inf.hurt=0
                      SET inf.helped = inf.helped + $h, inf.hurt = inf.hurt + $u
                    """,
                    params=r.params_changed, metric=r.primary_metric,
                    h=1 if improved else 0, u=0 if improved else 1,
                )

    # --- read ---
    def retrieve(self, query: RagQuery) -> RagContext:
        self._ensure()
        from qdrant_client import models

        qvec = self.embedder.embed(f"{query.goal_text} {query.target_metric}")
        hits = self._qdrant().query_points(
            COLLECTION,
            query=qvec,
            query_filter=models.Filter(
                must=[models.FieldCondition(key="project_id",
                                            match=models.MatchValue(value=query.project_id))]
            ),
            limit=max(query.top_k * 3, query.top_k),
            with_payload=True,
        ).points
        cands = [(h.payload["run_id"], h.payload.get("text", "")) for h in hits]
        top_ids = self.reranker.rerank(query.goal_text, cands, query.top_k)
        payloads = {h.payload["run_id"]: h.payload for h in hits}
        hints = self._graph_hints(query)
        lines = [f"과거 유사 실험 {len(top_ids)}건 참조 (목표 {query.target_metric}):"]
        for rid in top_ids:
            pl = payloads.get(rid, {})
            lines.append(
                f"- iter {pl.get('iteration_index')}: 변경={pl.get('params_changed')} → "
                f"{pl.get('primary_metric')}={pl.get('primary_metric_value')}"
            )
        if hints:
            lines.append(f"\n학습된 파라미터 영향 (목표 {query.target_metric}):")
            lines += [f"- {h.param_key}: 개선 {h.helped} / 악화 {h.hurt}" for h in hints]
        return RagContext(text="\n".join(lines), anchor_run_ids=top_ids, param_hints=hints)

    def _graph_hints(self, query: RagQuery) -> list[ParamHint]:
        cypher = """
        MATCH (par:Parameter)-[:OWNED_BY]->(:Project {project_id:$pid})
        MATCH (par)-[inf:INFLUENCES]->(:Metric {name:$metric})
        RETURN par.param_key AS key, inf.helped AS helped, inf.hurt AS hurt
        ORDER BY (inf.helped - inf.hurt) DESC LIMIT 5
        """
        with self._neo4j().session() as s:
            rows = s.run(cypher, pid=query.project_id, metric=query.target_metric)
            return [
                ParamHint(param_key=r["key"], direction="?", helped=r["helped"] or 0, hurt=r["hurt"] or 0)
                for r in rows
            ]


def _fastembed(settings):  # pragma: no cover
    from .embed import FastEmbedEmbedder

    return FastEmbedEmbedder()


def _bge(settings):  # pragma: no cover
    from .rerank import BgeReranker

    return BgeReranker()
