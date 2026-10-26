# ALGOps 계약 v2 — 매니페스트 · 실행 프로토콜 · SDK

저장소 하나가 ALGOps에 참여하기 위해 필요한 것은 **루트의 `algops.yaml` 하나**입니다. 알고리즘은 어떤
언어로 작성돼도 되고, ALGOps를 설치할 필요도 없습니다. 러너는 요청을 JSON 파일로 건네고 결과를 JSON
파일로 받습니다. 코딩 에이전트용 단계별 지침은 [`AGENTS.md`](../src/algops/contract/bundle/AGENTS.md)
(`algops init`이 저장소에 복사)에 있습니다.

```
저장소                         러너(슬레이브)                        마스터
algops.yaml ── sync ────────▶ 커밋 고정 · 매니페스트 읽기 ─────────▶ 리비전 저장
 ├ suites[]                    env.setup (커밋당 1회)
 └ algorithms[] ◀── run ────── request.json → 프로그램 → result.json ──▶ 런 · 지표 · 판정
```

## 1. `algops.yaml` 레퍼런스

| 필드 | 설명 |
| --- | --- |
| `algops` | 계약 버전. `2` (v1 레이아웃 `algops/params.yaml + run_adapter.py`는 자동으로 `algorithms: [default]`로 해석) |
| `name` | 표시 이름 (선택) |
| `env.setup` | 러너가 체크아웃마다 **한 번** 실행하는 셸 명령 목록 (빌드·의존성 설치). 커밋·명령이 같으면 캐시 |
| `env.vars` | 모든 실행에 주입할 환경변수 |
| `env.python` | `python:` 엔트리포인트를 실행할 인터프리터 (예: `.venv/bin/python`). 생략 시 러너의 파이썬 |
| `suites[]` | `id`, `uri`, `tags`, `description`. `uri`는 저장소 상대경로, 절대경로, `${VAR}`(러너 호스트에서 확장), `scheme://`(프로그램이 해석) |
| `algorithms[].id` | 소문자 slug, **불변** |
| `algorithms[].params` | 파라미터 파일 경로 또는 인라인 목록 (§2) |
| `algorithms[].entrypoint` | `python: "file.py:func"` / `"pkg.module:func"` 또는 `command: [...]` / `"셸 문자열"`, `cwd`, `env` |
| `algorithms[].suites` | 이 알고리즘에 쓰는 스위트 id (생략 시 전체) |
| `algorithms[].metrics` | `presets: [binary|regression|latency]`, `declare: [{name, direction, unit}]` |
| `algorithms[].gates` | `[{metric, op, value}]` — 벤치마크의 절대 합격선 |
| `algorithms[].regression` | `rel_tolerance`(기본 0.02), `abs_tolerance: {metric: v}`, `metrics`(비교 대상, 기본 = 목표+제약+게이트 지표) |
| `algorithms[].objective` | 캠페인 기본 목표 `{metric, direction?, target?, constraints[]}` |
| `algorithms[].runs_on` | 필요한 러너 라벨 (예: `[gpu]`) |
| `algorithms[].timeout_s` | 런 하나의 제한 시간 (기본 900) |

에디터 자동완성: `algops init`이 `algops/algops.schema.json`을 함께 둡니다.

### 지표 프리셋

| 프리셋 | 지표 (방향) |
| --- | --- |
| `binary` | `tp`↑ `fp`↓ `fn`↓ `tn`↑ `precision`↑ `recall`↑ `f1`↑ `accuracy`↑ `fp_rate`↓ `fn_rate`↓ — `counts`만 보고하면 러너가 계산 |
| `regression` | `mae`↓ `rmse`↓ `mse`↓ `max_abs_error`↓ |
| `latency` | `latency_ms`↓ `latency_p95_ms`↓ `throughput`↑ |

선언하지 않은 지표도 저장·표시됩니다(방향은 이름으로 추정). 목표·게이트에 쓰는 지표는 선언하세요.

## 2. 파라미터

```yaml
- key: detector.confidence_threshold   # 점표기, 전역 고유, 불변
  dtype: float                          # float | int | bool | enum
  default: 0.45                         # git의 이 값이 "진실"
  range: [0.05, 0.95]                   # float/int 필수
  step: 0.05                            # 탐색 단위 (float/int 필수)
  enum: [a, b]                          # dtype=enum 필수
  stage: decision                       # 텔레메트리 stage id
  influence: { fp: high, fn: high }     # 사전 지식 (LLM 플래너 힌트)
  drop_reasons: [below_threshold]       # 이 파라미터가 만드는 dec_reason
```

러너는 실행 전에 **기본값 병합 → 타입 강제 → 범위 클램프 → 미선언 키 거부**를 수행합니다.
프로그램은 항상 완전한 유효 파라미터 집합을 받으므로 별도 검증이 필요 없습니다.

## 3. 실행 프로토콜 `algops.run/v2`

### 요청 (`$ALGOPS_REQUEST`)
```json
{
  "protocol": "algops.run/v2",
  "run_id": "01a1…", "project_id": "…", "experiment_id": null, "benchmark_id": "…",
  "kind": "benchmark",                       // search | validation | benchmark | adhoc
  "algorithm_id": "detector",
  "suite": {"id": "smoke", "uri": "data/smoke", "path": "/abs/checkout/data/smoke", "tags": ["fast"]},
  "params": {"detector.confidence_threshold": 0.45, "...": "..."},
  "output_dir": "/abs/run/out", "result_path": "/abs/run/result.json",
  "repo_root": "/abs/checkout", "commit_sha": "3f73ead…", "seed": 0
}
```

### 환경변수와 치환자
| 환경변수 | 내용 |
| --- | --- |
| `ALGOPS_REQUEST` / `ALGOPS_RESULT` | 요청 파일 / 결과를 쓸 경로 |
| `ALGOPS_OUTPUT_DIR` | 산출물(CSV, 이미지 …) 디렉터리 — 전부 마스터로 업로드 |
| `ALGOPS_PARAMS_FILE` | 평탄한 `{key: value}` JSON |
| `ALGOPS_PARAM_<KEY>` | 파라미터별 값 (`detector.confidence_threshold` → `ALGOPS_PARAM_DETECTOR_CONFIDENCE_THRESHOLD`) |
| `ALGOPS_SUITE_ID` / `_URI` / `_PATH` | 스위트 |
| `ALGOPS_RUN_ID`, `ALGOPS_ALGORITHM_ID`, `ALGOPS_REPO_ROOT`, `ALGOPS_COMMIT_SHA`, `ALGOPS_SEED` | 기타 |

`command`의 각 인자(또는 셸 문자열)에서 `{request} {result} {output_dir} {params} {suite_path}
{suite_uri} {run_id} {repo_root} {param:<key>}`가 치환됩니다 (셸 문자열은 자동 quoting).

### 결과 (`$ALGOPS_RESULT`)
```json
{"status": "ok",
 "metrics": {"latency_ms": 3.2},
 "counts": {"tp": 90, "fp": 4, "fn": 10, "tn": 96},
 "n_items": 200,
 "telemetry": "manifest.json",
 "extra": {}}
```
파일 대신 stdout 마지막 줄 `::algops-result::{json}`도 허용됩니다.

### 실패 분류 (`error_kind`)
| 값 | 의미 | 재시도 |
| --- | --- | --- |
| `params` | 미선언 키 / 범위·타입 오류 / 스위트 불일치 — 실행 전 거부 | ✗ |
| `program` | 0이 아닌 종료 코드, `status: failed`, 결과 작성 후 비정상 종료 | ✗ |
| `protocol` | 결과 없음, 잘못된 JSON, 지표 없음 | ✗ |
| `timeout` | `timeout_s` 초과 (프로세스 트리 종료) | ✗ |
| `setup` | `env.setup` 실패 / 매니페스트 오류 | ✗ |
| `infra` | 러너·git 문제, 러너 하트비트 중단 | ✓ 다른 러너로 재배치 (`run_retries`) |

## 4. Python SDK

러너가 의존성 없는 단일 파일 `algops_sdk.py`를 `PYTHONPATH`에 주입합니다 — 저장소 가상환경에
ALGOps를 설치할 필요가 없습니다 (Python ≥ 3.8).

```python
import algops_sdk as sdk

def run(ctx: sdk.Context):
    ctx.params            # 유효 파라미터
    ctx.suite_path        # 스위트 로컬 경로 (원격 uri면 None → ctx.suite_uri)
    ctx.output_dir        # 산출물 위치
    st = ctx.stage("decision", ["dec_kept", "dec_reason", "gt_match"], depends_on=["ai_inference"])
    st.row(item_id, frame_index, entity_id, dec_kept=True, dec_reason="kept", gt_match=1)
    return sdk.counts(tp=..., fp=..., fn=..., tn=..., latency_ms=...)   # 또는 sdk.result(metrics={...})
```

v1 어댑터 `run(req: RunRequest, output_dir) -> RunResult`도 그대로 동작합니다(이 경우 러너의 파이썬과
ALGOps 설치 필요).

## 5. 텔레메트리 규약
- 단계마다 CSV 1개 + `manifest.json` (`stages: [{stage, csv, grain, depends_on}]`).
- 공통 선행 컬럼 `run_id,item_id,frame_index,entity_id,stage`; `entity_id`가 단계 간 조인 키.
- 컬럼 프리픽스 `in_` `ai_` `feat_` `dfeat_` `dec_` `gt_`.
- `decision` 단계의 `dec_kept`+`gt_match`로 마스터가 혼동행렬을 **독립 재계산**하고, `dec_reason`을
  파라미터의 `drop_reasons`로 귀속해 민감도를 계산합니다(LLM 플래너 입력).

## 6. CLI

| 명령 | 용도 |
| --- | --- |
| `algops init [path] -a ID --lang python|command` | 스캐폴드 (매니페스트, 파라미터, 엔트리포인트, 샘플 스위트, AGENTS.md) |
| `algops validate [path]` | 정적 검사 |
| `algops verify [path]` | validate + 모든 알고리즘 기본값 1회 실행 — 외부화 완료 게이트 |
| `algops run [path] -a ID -s SUITE --set k=v` | 셀 하나 실행 (`--log`, `--json`) |
| `algops test [path] [-a] [-s] [-t TAG] [--baseline r.json] [--json out.json] [-j N]` | 전체 셀 + 게이트 + 베이스라인 회귀. 실패 시 exit 1 |
| `algops ci --master URL -p NAME --ref SHA` | 마스터 벤치마크를 걸고 판정으로 exit (CI 게이트) |
| `algops master` / `algops slave` | 서버 / 러너 실행 |

## 7. 예제
- [`examples/py-multi-algo`](../examples/py-multi-algo) — 알고리즘 2개(이진 분류 후처리 + 회귀 스무더), 스위트 4개, 인라인/파일 파라미터
- [`examples/c-threshold`](../examples/c-threshold) — C 프로그램, `env.setup`에서 컴파일, 환경변수로 파라미터
