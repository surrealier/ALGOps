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
  `dev_mode`(단일 프로세스)에서 마스터가 재시작되면 이전 프로세스에서 돌던 캠페인·벤치마크를 실패로 정리합니다
  (승인 대기 중인 캠페인은 체크포인트에서 이어집니다).
- **LLM**: `deploy/docker-compose.llm.yml`(vLLM) 또는 외부 OpenAI 호환 엔드포인트를 `ALGOPS_LLM_*`로.
- **보안 기본값** (`dev_mode=false`일 때):
  - `ALGOPS_MASTER_ENROLL_TOKEN`이 기본값(`dev-enroll-token`)이면 마스터가 **기동을 거부**합니다.
  - `ALGOPS_MASTER_API_TOKEN`을 설정하면 **조회를 포함한 모든 API**·WebSocket·아티팩트가 토큰을 요구합니다
    (`Authorization: Bearer …`, 브라우저 링크/WS는 `?token=`). UI는 401을 받으면 토큰 입력 창을 띄웁니다.
    러너 프로토콜은 별도의 러너 토큰을 씁니다.
  - CORS는 기본적으로 꺼져 있습니다(UI는 마스터가 같은 출처로 제공). 다른 출처에서 API를 쓰려면
    `ALGOPS_MASTER_CORS_ORIGINS=https://a.corp,https://b.corp`.
- **타임아웃**: 런 타임아웃(`timeout_s`)은 러너가 커맨드를 **가져간 시점부터** 셉니다. 러너가 하나도 가져가지 않은 채
  `ALGOPS_MASTER_QUEUE_TIMEOUT_S`(기본 1일)가 지나면 포기합니다. 타임아웃 난 런은 다른 러너로 **재배치하지 않습니다**
  (이중 실행 방지) — 재배치는 러너가 죽었을 때만(`ALGOPS_MASTER_RUN_RETRIES`).
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
| `ALGOPS_SLAVE_PASS_ENV` | 저장소 코드에 **예외적으로 전달할** 환경변수 glob (예: `AWS_*,HF_TOKEN`) |
| `ALGOPS_SLAVE_WITHHOLD_ENV` | 저장소 코드에서 숨길 환경변수 glob 목록 (생략 시 기본값, 아래 참고) |

- 사설 저장소는 러너 호스트의 git 인증(ssh 키, credential helper)을 그대로 씁니다. 프롬프트는 비활성화되어 있어 멈추지 않습니다.
- **환경변수 위생**: 러너는 자기 설정(`ALGOPS_SLAVE_*`, `ALGOPS_MASTER_*`)과 자격증명처럼 보이는 변수
  (`*TOKEN`, `*TOKEN_*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`, `*PRIVATE_KEY*`, `*CREDENTIAL*`)를
  `env.setup`·엔트리포인트에 **넘기지 않습니다**. 기동 시 숨긴 변수 이름을 출력하니, 알고리즘이 정말 필요로 하는
  것(예: S3 데이터셋의 `AWS_SECRET_ACCESS_KEY`)만 `ALGOPS_SLAVE_PASS_ENV`로 허용하세요.
- **로컬 프로젝트 고정**: 마스터가 sync한 sha와 러너 디스크의 코드가 달라지면(편집, 저장소 안에 파일을 쓰는 프로그램)
  런이 `setup` 오류로 실패합니다. 산출물은 `$ALGOPS_OUTPUT_DIR`에 쓰고 빌드 산출물은 `.gitignore`에 넣으세요.
- 데이터셋은 러너 호스트에 두고 스위트 uri에서 `${DATASETS}/…`처럼 환경변수로 참조하세요.
- 저장소가 CUDA·JDK·MATLAB 런타임 등을 요구하면 `FROM algops/slave` 이미지를 확장하세요.

## CI 연동
- 저장소 자체 CI: `algops test --json report.json` (마스터 불필요). 기준 리포트와 비교하려면 `--baseline main.json`.
- 중앙 마스터 CI 게이트: `algops ci --master http://<A_IP>:8000 --project <name> --ref $CI_COMMIT_SHA` (실패 시 exit 1).
