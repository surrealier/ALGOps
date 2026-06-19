# 배포 가이드 (M0 dev → M5 prod)

## 개발 (Docker 불필요)
```bash
uv sync
uv run algops-smoke          # 인프로세스 end-to-end 데모
uv run pytest                # 전체 테스트
uv run algops-master         # http://127.0.0.1:8000 (React UI; frontend/dist 있으면 자동 서빙)
ALGOPS_SLAVE_MASTER_URL=http://127.0.0.1:8000 uv run algops-slave
```
프론트엔드 빌드: `cd frontend && npm install && npm run build` → `frontend/dist` 생성(마스터가 `/`에서 서빙).
빌드 안 하면 마스터는 `src/algops/master/static/index.html`(단일파일 한국어 UI)를 서빙합니다.

## 단일 호스트 데모 (Docker)
```bash
docker compose -f deploy/docker-compose.dev.yml up --build   # master + toy slave
```

## 사내망 프로덕션 (서버 A)
완전한 스택(Postgres + Redis + MinIO + Qdrant + Neo4j + Celery 워커 + Traefik):
```bash
ALGOPS_ENROLL_TOKEN=<secret> docker compose -f deploy/docker-compose.full.yml up -d --build
# UI: https://<A_IP>/   (기본 self-signed + basic-auth algops/algops — 반드시 교체)
```
- LLM: `deploy/docker-compose.llm.yml`로 vLLM을 띄우거나 외부 엔드포인트를 `ALGOPS_LLM_*`로 지정.
- 마이그레이션: 컨테이너는 기동 시 `create_all`로 부트스트랩합니다. 통제된 마이그레이션은
  `uv run alembic revision --autogenerate -m init && uv run alembic upgrade head`.

### 각 개발자 PC (B/C/D…) — Slave
```bash
ALGOPS_MASTER_URL=http://<A_IP>:8000 ALGOPS_PROJECT_NAME=proj-A \
ALGOPS_PROJECT_DIR=/path/to/project ALGOPS_SLAVE_PROJECT_PATH=/workspace/project \
  docker compose -f deploy/docker-compose.slave.yml up -d
```
Slave는 아웃바운드 HTTP만 사용합니다(인바운드 포트 없음).

## 프로덕션 토폴로지 요지
- 실험 루프는 **Celery 워커**에서 실행(`dev_mode=false`), API 프로세스와 **Redis 커맨드 버스/진행 채널**로 통신.
- 내구성은 **Postgres 체크포인트**(`thread_id=experiment_id`) — 워커 재시작 시 캠페인 재개.
- 아티팩트는 **MinIO(S3)** proxied-artifact 모드(Slave는 객체스토리지 크리덴셜 미보유).
- RAG는 **Qdrant + Neo4j**(`rag_backend=qdrant_neo4j`); 리랭커/임베더는 `--extra rag-models`로 모델 탑재.
- 마스터는 **Traefik** 뒤(TLS + basic-auth); 사내망이라도 인증/네트워크 격리 필수.

## GPU
- LLM(chat/embed/rerank)은 마스터측 GPU 또는 외부 vLLM. Slave는 프로젝트 추론 sweep용 GPU(있으면).
- 단일 GPU면 `--gpu-memory-utilization`/양자화/`--max-model-len`로 예산 관리, `/health` 게이팅.
