# ALGOps 아키텍처 (요약)

후처리 **알고리즘 파라미터**의 최적값을 LLM 에이전트 + RAG로 자동 탐색하는 MLOps. 2-tier,
REST-first. LLM은 OpenAI 호환 `/v1`로만 접근(외부 vLLM/Ollama/자체 기동을 단일 설정으로 통일).

```
                 ┌──────────────────────────── MASTER (서버 A) ────────────────────────────┐
  브라우저  ──WS──│  FastAPI API · 한국어 UI                                                  │
                 │  실험 루프(LangGraph) ── CommandBus ── 체크포인터(SQLite→Postgres)         │
                 │  Tracking(SQLite→Postgres) · Artifacts(local→MinIO)                       │
                 │  [M3] Qdrant · Neo4j · reranker     [M1] vLLM(chat/embed)                  │
                 └───────────────▲───────────────────────────────────────────────────────────┘
                outbound HTTP만   │  register / heartbeat / long-poll / result / artifact-upload
        ┌────────┴─────────┐  ┌───┴────────────┐
        │ SLAVE (PC B)     │  │ SLAVE (PC C) …  │   프로젝트당 1개. run_adapter로 테스트셋 평가→CSV.
        │ run_adapter+코드 │  └────────────────┘
        └──────────────────┘
```

## 루프 (LangGraph, plan §4)
`rag_retrieve → pm_propose → developer → run_eval → extract_csv → pandas_summarize → analyst → pm_decide ─(continue|finalize)`
- **정지는 라우터에서** 강제: `pm_action==stop` OR `iteration>=max_iteration` OR `target_reached`.
- 체크포인트 `thread_id == experiment_id` → 워커 재시작 시 캠페인 중간 재개.
- M0 에이전트는 결정론 스텁(단일 파라미터 hill-climb). M1에서 vLLM 기반 PM/분석가/개발자로 교체.

## 핵심 무결성: `run_id` spine
Master가 `run_id`(UUIDv7) 발급 → Slave `run_adapter` → 모든 CSV 행/아티팩트/(M3) Qdrant·Neo4j에
동일 id로 흐름. 적재 시 일치 단언.

## 마일스톤 (M0–M5 구현 완료)
- **M0** Master/Slave + 체크포인트 루프 + CSV/지표 + 알림 end-to-end.
- **M1** OpenAI 호환 LLM 클라이언트 + LLM 플래너(PM/분석가/요약), 실패 시 결정론 폴백. `algops.llm`.
- **M2** 다중 stage CSV 규약(`in_/ai_/feat_/dfeat_/dec_/gt_` + entity_id 척추 + manifest) + 결정론 집계·민감도.
- **M3** Qdrant+Neo4j 하이브리드 RAG + 리랭크 (`algops.rag`); 인메모리 폴백으로 실험 간 기억·학습된 파라미터 영향.
- **M4** 코딩에이전트 외부화 계약(`algops.contract`): AGENTS.md/CLAUDE.md + params.schema.json + 템플릿 + verify 게이트 + introspect.
- **M5** S3/MinIO·Postgres 체크포인트·Redis 커맨드버스·Celery 워커·Traefik+TLS·React UI·HITL 승인·MCP 알림.

> 기본값은 외부 인프라 없이 도는 개발 경로(인메모리 RAG, SQLite, 인프로세스 루프, 결정론 에이전트)이며,
> 프로덕션 통합은 설정으로 전환됩니다. 상세: [deployment.md](deployment.md).

자세한 설계·근거는 승인된 계획서를 참조. 엔드포인트 계약은 [master-slave-protocol.md](master-slave-protocol.md).
