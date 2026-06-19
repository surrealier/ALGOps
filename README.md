# ALGOps

**RAG 기반 알고리즘 파라미터 최적화 MLOps** — 모델 학습 하이퍼파라미터가 아니라, AI 모델을 감싸는 **후처리 알고리즘 파라미터**(threshold, roundness, HSV/histogram 윈도잉 등)의 최적값을 자동 탐색하는 도구.

기존 MLflow/W&B가 "학습 파라미터 → 모델 성능"을 추적한다면, ALGOps는 "후처리 파라미터 변경 → 최적값/알고리즘 서치"를 LLM 에이전트 + RAG로 자동화한다.

> **상태: M0–M5 구현 완료.** Master/Slave + 체크포인트 LangGraph 루프 + 다단계 CSV + RAG(실험 간 기억) +
> LLM 에이전트 + 코딩에이전트 외부화 계약 + HITL 승인 + 프로덕션 스택(Postgres/Redis/MinIO/Qdrant/Neo4j/
> Celery/Traefik)까지. **개발 경로는 외부 서비스·GPU 없이 동작**하도록 기본값이 잡혀 있고(인메모리 RAG,
> SQLite, 인프로세스 루프, 결정론 에이전트), 프로덕션 통합은 설정 한 줄로 전환됩니다(`docs/deployment.md`).
> 전체 설계는 `docs/`와 승인된 계획서를 참고하세요.

## 토폴로지 (사내망 다중 호스트)

- **Master** (서버 A, 고정 IP): 웹 UI · 실험 이력 · LangGraph 루프 · RAG · LLM 레퍼런스 · 아티팩트 저장. 단 1개.
- **Slave** (개발자 PC B/C/D…): 프로젝트마다 1개. Master로 **아웃바운드 HTTP만** — 등록 후 long-poll로 명령을 받아 추출/실행. 인바운드 포트 없음.

## 빠른 시작 (개발, Docker 불필요)

```bash
uv sync                      # 가상환경 + 의존성
uv run algops-smoke          # 인프로세스 end-to-end 데모 (SQLite + 로컬 아티팩트 + 인메모리 커맨드버스)
uv run pytest                # 단위/통합 테스트
```

분산 실행(별도 프로세스, slave-initiated 흐름):

```bash
# 터미널 1 — 마스터
uv run algops-master                       # http://127.0.0.1:8000  (UI: /)
# 터미널 2 — 슬레이브 (번들 toy 프로젝트)
uv run algops-slave                        # 127.0.0.1:8100, 마스터에 등록 후 long-poll
```

브라우저에서 `http://127.0.0.1:8000` → 프로젝트/실험 생성 → Run → 라이브 진행/최종 결과 확인.

## 프로덕션 (서버 A)

```bash
docker compose -f deploy/docker-compose.master.yml up -d   # postgres+redis+minio+api+worker+ui
# 각 개발자 PC:
docker compose -f deploy/docker-compose.slave.yml up -d     # ALGOPS_SLAVE_MASTER_URL=http://<A_IP>:8000
```

## 디렉터리

```
src/algops/
  core/        contracts: ids, enums, params, schemas, config (단일 출처)
  tracking/    SQLAlchemy 모델 · 세션 · 아티팩트 저장 · 리포지토리
  extraction/  지표 채점 · target 판정 · CSV 요약(결정론)
  notify/      Slack/email/console 알림(결정론)
  agents/      LangGraph 루프 (M0: 결정론 스텁)
  master/      FastAPI 컨트롤플레인 + 실험 루프 워커
  slave/       FastAPI per-project 에이전트
  toy_project/ 번들 합성 검출기(손으로 외부화 — M4 코딩에이전트 경로의 골든 레퍼런스)
  master/static/index.html  M0 한국어 단일파일 UI (마스터가 / 에서 서빙, 빌드 불필요)
deploy/        Dockerfile.{master,slave} · docker-compose.{master,slave,dev}.yml
docs/          architecture.md · master-slave-protocol.md
frontend/      (M5) React + TS + Vite 리치 UI — 현재는 master/static 임베드 UI 사용
```

> **M0 UI 메모:** 웹페이지는 마스터가 `/`에서 서빙하는 단일파일 한국어 UI(`src/algops/master/static/index.html`)로 동작합니다 — Node 빌드가 필요 없습니다. 계보 그래프/이력 등 리치 React UI는 M5입니다.
