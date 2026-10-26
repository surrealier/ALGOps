# 배포 가이드

## 개발 (Docker 불필요)
```bash
uv sync
uv run pytest                              # 전체 테스트 (외부 서비스 없음)
uv run algops master                       # http://127.0.0.1:8000 — UI는 패키지에 포함
ALGOPS_SLAVE_MASTER_URL=http://127.0.0.1:8000 uv run algops slave     # 번들 toy 프로젝트 제공
```
UI를 수정했다면 `cd frontend && npm ci && npm run build` → `src/algops/master/static/ui`가 갱신됩니다
(커밋 대상, CI가 최신 여부 확인). 개발 서버는 `npm run dev`(API·WS를 :8000으로 프록시).

### 로컬 프로젝트로 빠르게 반복
```bash
ALGOPS_SLAVE_LOCAL_PROJECTS="vision=/home/me/vision;audio=/home/me/audio" uv run algops slave
```
로컬 프로젝트의 sha는 `HEAD`(+`-dirty-<diff해시>`) 또는 `local-<지문>`이라 코드를 고치면 캐시가 자동 무효화됩니다.
승격(브랜치 커밋)은 커밋된 코드에서만 가능합니다.

## 단일 호스트 데모 (Docker)
```bash
docker compose -f deploy/docker-compose.dev.yml up --build   # master + runner(toy)
```

## 사내망 프로덕션 (서버 A)
```bash
ALGOPS_ENROLL_TOKEN=<secret> ALGOPS_API_TOKEN=<secret> \
  docker compose -f deploy/docker-compose.full.yml up -d --build
# UI: https://<A_IP>/
```
- 스택: Postgres(트래킹 + LangGraph 체크포인트) · Redis(커맨드 버스·진행 채널·Celery) · MinIO(아티팩트) ·
  Qdrant + Neo4j(RAG) · Celery 워커(캠페인·벤치마크) · Traefik(TLS).
- **스케줄러**(cron·git watch)는 API 프로세스 하나에서만 돌립니다 (`ALGOPS_MASTER_SCHEDULER_ENABLED`; 워커는 false).
- **마이그레이션**: `ALGOPS_MASTER_DB_URL=postgresql+psycopg://… uv run alembic upgrade head`.
  개발 경로는 `create_all`로 부트스트랩합니다. v1(0.0.x) 트래킹 DB는 v2 스키마와 호환되지 않으므로 새 DB로 시작하세요.
- **LLM**: `deploy/docker-compose.llm.yml`(vLLM) 또는 외부 OpenAI 호환 엔드포인트를 `ALGOPS_LLM_*`로.
- **승격/PR**: 러너가 push할 수 있으면 `ALGOPS_PROMOTE_PUSH=true`; PR/MR은 마스터가
  `ALGOPS_GIT_PROVIDER=github|gitlab`, `ALGOPS_GIT_PROVIDER_TOKEN`, (사내 GitLab이면) `ALGOPS_GIT_PROVIDER_API_URL=https://gitlab.corp/api/v4`로 생성.

## 러너 (빌드 서버 · GPU 박스 · 개발 PC)
```bash
ALGOPS_MASTER_URL=http://<A_IP>:8000 ALGOPS_ENROLL_TOKEN=<secret> \
ALGOPS_RUNNER_LABELS=gpu,cuda12 ALGOPS_RUNNER_CONCURRENCY=2 \
ALGOPS_DATASETS_DIR=/mnt/datasets \
  docker compose -f deploy/docker-compose.slave.yml up -d
```
또는 컨테이너 없이 `algops slave` (환경변수 `ALGOPS_SLAVE_*`):

| 변수 | 의미 |
| --- | --- |
| `ALGOPS_SLAVE_MASTER_URL`, `ALGOPS_SLAVE_ENROLL_TOKEN` | 마스터 주소, enroll 토큰 |
| `ALGOPS_SLAVE_LABELS` | 라벨 (쉼표). 알고리즘의 `runs_on`과 매칭 |
| `ALGOPS_SLAVE_CONCURRENCY` | 동시 실행 런 수 |
| `ALGOPS_SLAVE_ALLOW_PROJECTS` | 이 러너가 받을 프로젝트 이름 (생략 시 전체) |
| `ALGOPS_SLAVE_LOCAL_PROJECTS` | `name=/path;…` 로컬 프로젝트 |
| `ALGOPS_SLAVE_GIT` | git 프로젝트 처리 여부 (기본 true) |
| `ALGOPS_SLAVE_WORK_DIR`, `ALGOPS_SLAVE_KEEP_CHECKOUTS` | 미러/worktree/런 디렉터리, 프로젝트당 보관 worktree 수 |
| `ALGOPS_SLAVE_SERVE_TOY` | 로컬 프로젝트가 없을 때 toy 제공 (기본 true) |

- 사설 저장소는 러너 호스트의 git 인증(ssh 키, credential helper)을 그대로 씁니다. 프롬프트는 비활성화되어 있어 멈추지 않습니다.
- 데이터셋은 러너 호스트에 두고 스위트 uri에서 `${DATASETS}/…`처럼 환경변수로 참조하세요.
- 저장소가 CUDA·JDK·MATLAB 런타임 등을 요구하면 `FROM algops/slave` 이미지를 확장하세요.

## CI 연동
- 저장소 자체 CI: `algops test --json report.json` (마스터 불필요). 기준 리포트와 비교하려면 `--baseline main.json`.
- 중앙 마스터 CI 게이트: `algops ci --master http://<A_IP>:8000 --project <name> --ref $CI_COMMIT_SHA` (실패 시 exit 1).
