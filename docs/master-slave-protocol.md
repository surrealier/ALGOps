# Master ↔ Runner 프로토콜 · API (v2)

러너(슬레이브)는 **아웃바운드 HTTP만** 사용합니다. 마스터가 명령을 특정 러너의 큐에 넣으면 러너가
long-poll로 가져가 실행하고 결과를 POST합니다. 러너는 `concurrency`개의 워커로 동시에 poll합니다.

## 인증
- **등록**: enroll 토큰 (`ALGOPS_MASTER_ENROLL_TOKEN`).
- **러너 → 마스터**: 등록 시 발급된 러너 bearer 토큰 (DB에는 sha256만 저장).
- **API 변경 요청**: `ALGOPS_MASTER_API_TOKEN`이 설정되면 모든 변경 엔드포인트(POST/PATCH/DELETE, 러너 프로토콜 제외)에 `Authorization: Bearer <token>` 필요. 조회는 개방.

## 러너 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/v1/slaves/register` | `{enroll_token, host_label, labels[], concurrency, allow_projects?, git, local_projects[{name, sha, manifest}]}` → `{slave_id, slave_token, project_ids}`. 로컬 프로젝트는 자동 등록 + 리비전 기록. v1 페이로드(`project_name`+`param_spec`)도 허용 |
| PUT | `/api/v1/slaves/{id}/heartbeat` | `{status, inflight}` — 워커가 모두 바빠도 생존 신호 유지 |
| GET | `/api/v1/slaves/{id}/next-command` | long-poll(≈25s) → `{kind: run|sync|promote|noop, command_id, run_spec|sync_spec|promote_spec}` |
| POST | `/api/v1/slaves/{id}/command-result` | `{command_id, result | sync_result | promote_result}` |
| POST | `/api/v1/slaves/{id}/artifacts` | multipart `key`, `file` → `{uri}` (proxied — 러너는 객체 저장소 자격증명 불필요) |
| GET | `/api/v1/slaves` | 러너 목록 (온라인, 라벨, 동시 실행, in-flight) |

### 명령
- **run** — `RunRequest{run_id, project_id, repo{kind, name, url, sha}, algorithm_id, suite_id, param_values, kind, seed, timeout_s, experiment_id?, benchmark_id?}` → `RunResult{status, metrics{}, counts?, csv_manifest_uri, log_uri, error, error_kind, commit_sha, duration_s, param_values(유효값)}`.
- **sync** — `{project_id, repo{ref}}` → `{ok, sha, dirty, manifest}` (해석된 sha의 매니페스트, 파라미터 스펙 포함).
- **promote** — `{repo{sha=기준}, algorithm_id, params{key: new}, branch, message, push}` → `{ok, branch, commit_sha, changed{}, files, patch, pushed}`.

### 런 흐름
1. 디스패처가 적격 러너 중 가장 한가한 러너 큐에 `run` 적재 → 5초 단위로 결과 대기하며 러너 하트비트 확인.
2. 러너: 미러 fetch(필요 시) → 커밋 worktree → `env.setup`(최초 1회) → 프로그램 실행 → 산출물 업로드 → 결과 POST.
3. 하트비트가 끊기면 명령 회수 후 다른 러너로 재배치 (`run_retries`, 기본 1).

## 제어 API (UI · CI)

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST/GET | `/api/v1/projects` | git 프로젝트 등록 `{name, repo_url, default_ref?, watch}` / 목록 |
| GET/PATCH | `/api/v1/projects/{id|name}` | 조회 / `watch`, `default_ref` 수정 |
| POST | `/api/v1/projects/{id}/sync?ref=` | ref 해석 + 매니페스트 스냅샷 → 리비전 |
| GET | `/api/v1/projects/{id}/revisions` · `/manifest?sha=` · `/history?algorithm_id=&suite_id=` | 리비전 / 매니페스트 요약 / 셀 벤치마크 추이 |
| POST/GET | `/api/v1/benchmarks` | `{name, scope?[{project_id, ref?, algorithms?, suites?, tags?}], baseline: previous|none|<id>, trigger}` / 목록 (`?project_id=`) |
| GET | `/api/v1/benchmarks/{id}` | 셀 포함 상세 (verdict, gates, deltas, baseline) |
| POST | `/api/v1/benchmarks/{id}/accept?cell_id=` | 셀(또는 전체)을 새 베이스라인으로 승인 |
| POST/GET | `/api/v1/experiments` | 캠페인 생성 `{project_id, algorithm_id, suite_id, validation_suite_ids, objective{metric, direction, target, constraints}, strategy, search_keys, batch_size, max_iteration, ref}` (v1 필드 `target_metric/target_value/primary_param_key`도 허용) |
| POST | `/api/v1/experiments/{id}/run` | 실행/재실행 (새 attempt) |
| POST | `/api/v1/experiments/{id}/approve?approved=` | HITL 승인/거절 |
| GET | `/api/v1/experiments/{id}` · `/runs?attempt=&all_attempts=` · `/iterations` · `/final` | 상태 / 런 / 판단 기록 / 최종 결과(검증, 변경 파라미터, 승격) |
| POST | `/api/v1/experiments/{id}/promote` | `{push?, pull_request?, title?, base?}` → 브랜치·커밋·패치(·PR) |
| GET | `/api/v1/runs/{run_id}` · `/api/v1/artifacts?uri=` | 런 / 아티팩트 텍스트 |
| POST/GET/PATCH/DELETE | `/api/v1/schedules` · `/{id}` · `/{id}/fire` | cron·watch 스케줄, 즉시 실행 |
| WS | `/ws/experiments/{id}` · `/ws/benchmarks/{id}` | 라이브 이벤트 (`run_evaluated`, `iteration_decided`, `finalized`, `cell_done`, `benchmark_done` …) |
