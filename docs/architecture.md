# ALGOps 아키텍처 (v2)

**모든 저장소의 모든 알고리즘을 같은 방식으로 테스트하고, 회귀를 잡고, 파라미터를 튜닝하는 플랫폼.**
학습 하이퍼파라미터를 다루는 MLOps 위의 층 — 모델 출력을 감싸는 *후처리·결정 알고리즘의 파라미터*를
다룹니다.

## 원칙

| 원칙 | 구현 |
| --- | --- |
| **언어 무관** | 알고리즘은 `request.json → result.json`만 지키면 됨. Python은 무의존 SDK |
| **Git이 진실** | 모든 런은 커밋 sha에 고정. 기본값은 저장소의 params.yaml. 최적값은 브랜치 커밋·패치·PR로 *제안*만 |
| **파라미터만 탐색** | 에이전트는 선언된 파라미터 공간만 탐색, 코드는 절대 변경하지 않음 |
| **판정 = 게이트 + 베이스라인** | 절대 기준(`gates`) 실패 → `gate_failed`, 통과해도 허용폭 이상 악화 → `regressed` |
| **결정은 코드가** | 중단 조건·판정·알림·지표 계산은 결정론 코드. LLM은 제안·분석·요약만 |
| **아웃바운드 전용 러너** | 러너는 마스터로 나가는 HTTP만 사용 (NAT/방화벽 친화) |

## 구성

```
                 ┌──────────────────────────── MASTER (서버 A) ─────────────────────────────┐
  브라우저 ─WS──▶│ FastAPI · React UI      스케줄러 (cron · git watch)                        │
  CI ─algops ci─▶│ Dispatcher ── CommandBus(memory|Redis) ── 벤치마크 서비스 · 캠페인 루프(LangGraph)│
                 │ Tracking(SQLite→Postgres) · Artifacts(local→MinIO) · RAG(memory→Qdrant+Neo4j) │
                 └──────────▲───────────────────────────────────────────────────────────────┘
                 outbound만 │ register · heartbeat · long-poll(run|sync|promote) · result · upload
        ┌──────────────────┴───┐  ┌──────────────────────┐
        │ RUNNER (빌드 서버)     │  │ RUNNER (GPU 박스)      │  러너 N대, 각자 concurrency·labels
        │ git mirror + 커밋별    │  │ labels: gpu,cuda12    │  레포 M개를 모두 처리
        │ worktree · env.setup  │  │                      │
        └──────────────────────┘  └──────────────────────┘
```

### 핵심 개념
- **Project** = 저장소 (git URL, 또는 러너 로컬 경로).
- **Revision** = 해석된 커밋 sha + 그 시점의 매니페스트 스냅샷(알고리즘·스위트·파라미터 스펙).
- **Algorithm** × **Suite** = **셀**. 런 하나 = (알고리즘, 스위트, 파라미터 집합, sha) 평가 1회. `run_id`가 무결성 척추.
- **Benchmark** = 셀 매트릭스를 고정 sha + 저장소 기본값으로 전수 실행 → 셀별 판정.
- **Campaign(Experiment)** = 한 알고리즘의 파라미터 공간 최적화 (튜닝 스위트 + 검증 스위트) → 승격 제안.
- **Runner(Slave)** = 실행 호스트. `labels`, `concurrency`, `allow_projects`, 로컬 프로젝트.

## 디스패치
- 배치 규칙: 로컬 프로젝트는 그것을 선언한 러너에만, git 프로젝트는 git 가능 + allowlist 통과 +
  `runs_on` 라벨을 모두 가진 러너에. 그중 (in-flight / concurrency)가 가장 낮은 러너.
- 실행 중 러너의 하트비트가 끊기면 명령을 회수하고 다른 러너로 재배치 (`run_retries`). 아직 아무도 가져가지
  않은 명령은 언제든 안전하게 재배치됩니다.
- 런 타임아웃은 러너가 명령을 **가져간 시점**부터 계산합니다(큐 대기 시간 제외). 타임아웃은 재배치하지 않습니다 —
  원래 러너에서 아직 돌고 있을 수 있으므로 같은 런을 두 번 실행하지 않습니다.
- `infra` 실패만 재시도 — 알고리즘 자체의 실패(`program`/`protocol`/`timeout`/`params`/`setup`)는 그대로 기록.
- 러너는 커밋별 worktree를 체크아웃과 동시에 "사용 중"으로 잡고, 같은 미러의 worktree 추가·삭제·승격을 직렬화합니다.
  `env.setup` 완료 표시는 체크아웃의 정체성(.git 항목)까지 포함해, 정리된 뒤 다시 만든 worktree는 다시 셋업합니다.

## 벤치마크
`sync(ref→sha)` → 셀 확장(필터: 알고리즘·스위트·태그) → 러너 용량만큼 병렬 실행 → 셀별
`judge(gates, baseline)` → 요약·알림. 베이스라인 = 같은 셀의 가장 최근 *수용된* 결과(통과/개선, 또는
사람이 승인한 셀). 회귀는 고쳐지거나 승인될 때까지 계속 보고됩니다. 추적 지표가 기준보다 허용폭 이상
나빠지거나(허용폭 하한 1e-9), 기준에 있던 지표가 더는 보고되지 않으면(`lost`) 회귀입니다.

## 캠페인 루프 (LangGraph)

```
rag_retrieve → pm_propose → developer(HITL) → run_eval(배치 병렬) → extract_csv → pandas_summarize
             → analyst → pm_decide ─(continue)→ rag_retrieve
                                    └(stop | max_iter | target)→ validate(held-out) → finalize
```
- iteration 1은 항상 **저장소 기본값**(베이스라인)을 평가.
- 제안 = (선택) LLM 플래너의 다중 파라미터 변경 1개 + 결정론 전략(`coordinate`/`random`/`grid`)이
  배치를 채움. 이미 평가한 조합은 다시 제안하지 않음.
- 순위 = **제약 만족 우선** → 위반량 → 목표 지표. 목표 도달은 라우터가 강제.
- 시작 시 sha를 고정하고 시도(attempt)마다 새 체크포인트 스레드(`<experiment_id>:<attempt>`) — 재실행이 이력을 섞지 않음.
- `validate`가 최적값과 기본값을 검증 스위트에서 다시 평가 → 과적합·제약 위반을 드러냄.

## 승격 (Git으로 제안)
러너가 최종 결과가 실제로 실행된 sha(`FinalResult.commit_sha` — 재실행으로 캠페인이 새 커밋에 고정돼도
그대로)에서 임시 worktree + 새 브랜치를 만들고, 변경된 파라미터의 `default:` 스칼라만
바이트 단위로 교체(주석·포맷 보존, 값은 선언된 `dtype`으로 기록하고 다시 읽어 검증) → 커밋 → `format-patch`. 설정 시 push, 마스터가 GitHub/GitLab
PR/MR 생성. 기본 브랜치는 절대 건드리지 않음.

## 트리거
UI 수동 · cron 스케줄 · git watch(기본 브랜치 HEAD 폴링, 새 sha면 해당 프로젝트만 벤치마크) ·
`algops ci`(API) · 저장소 안의 `algops test`(마스터 불필요, 같은 엔진·같은 판정 규칙).

## 모듈
| 패키지 | 역할 |
| --- | --- |
| `algops.core` | 계약: 매니페스트, 지표/목표/제약, 파라미터, 와이어 스키마, 설정 |
| `algops.sdk` | 무의존 Python SDK (`algops_sdk.py`) |
| `algops.runner` | 실행 엔진: 프로토콜, git 워크스페이스, env setup, 승격, 로컬 테스트 |
| `algops.slave` | 러너 프로세스 (등록·하트비트·워커·업로드) |
| `algops.search` | 결정론 탐색 전략 |
| `algops.agents` | LangGraph 캠페인 루프 |
| `algops.extraction` | 텔레메트리 집계, 리포트, 판정(verdict) |
| `algops.master` | API, 디스패처, 버스, 서비스(벤치마크·캠페인·승격·스케줄러) |
| `algops.tracking` | ORM, 리포지토리, 아티팩트 저장소 (+ `alembic/`) |
| `algops.rag` / `algops.llm` / `algops.notify` | 실험 간 기억, LLM 플래너, 결정론 알림 |

자세한 계약은 [contract-v2.md](contract-v2.md), 엔드포인트는 [master-slave-protocol.md](master-slave-protocol.md),
배포는 [deployment.md](deployment.md).
