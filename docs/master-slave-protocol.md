# Master ↔ Slave 프로토콜 (M0)

Slave는 **아웃바운드 HTTP만** 사용한다(인바운드 포트 0개, NAT/방화벽 친화). Master가 명령을
큐에 넣으면 Slave가 long-poll로 가져가 실행하고 결과를 되돌려 POST한다.

## 인증
- **Slave → Master**: 등록 시 발급된 project-scoped Bearer 토큰. `register` 외 모든 Slave 엔드포인트에 필요.
- **등록**: UI/설정의 1회용 enroll 토큰(`ALGOPS_MASTER_ENROLL_TOKEN`).

## 엔드포인트

| 메서드 | 경로 | 인증 | 설명 |
|---|---|---|---|
| POST | `/api/v1/slaves/register` | enroll 토큰(body) | 프로젝트 생성/연결 + slave 등록, project-scoped 토큰 반환 |
| PUT | `/api/v1/slaves/{id}/heartbeat` | slave | 생존 신호 |
| GET | `/api/v1/slaves/{id}/next-command` | slave | **long-poll**(≈25s). `{kind: run, command_id, run_spec}` 또는 `{kind: noop}` |
| POST | `/api/v1/slaves/{id}/command-result` | slave | `{command_id, result: RunResult}` 결과 반환 |
| POST | `/api/v1/slaves/{id}/artifacts` | slave | CSV 등 아티팩트 업로드(proxied) → `{uri}` |
| POST | `/api/v1/projects` · GET `…` | – | 프로젝트 CRUD |
| POST | `/api/v1/experiments` | – | 실험 생성 |
| POST | `/api/v1/experiments/{id}/run` | – | 실험 루프 트리거(dev: 인프로세스) |
| GET | `/api/v1/experiments/{id}` · `/runs` · `/final` | – | 실험/런/최종결과 조회 |
| GET | `/api/v1/artifacts?uri=` | – | 리포트/요약 텍스트 조회 |
| WS | `/ws/experiments/{id}` | – | 라이브 진행 이벤트(run_evaluated/iteration_decided/finalized/…) |

## 런 디스패치 흐름 (한 반복)
1. 루프(`run_eval` 노드) → `CommandBus.submit_run(slave_id, RunRequest)` → future 대기.
2. Slave long-poll가 `next-command`로 `run` 명령 수신.
3. Slave가 `run_adapter.run(req, out_dir)` 실행 → `decision.csv` 생성 → `/artifacts`로 업로드 → `csv_manifest_uri` 갱신.
4. Slave가 `command-result`로 `RunResult` POST → Master가 future resolve → 루프 진행.

> M0의 CommandBus는 인메모리(같은 프로세스의 API+루프). 별도 Celery 워커를 쓰는 프로덕션은
> Redis 기반 버스/결과저장으로 교체(M5). 인터페이스는 동일.
