# ALGOps 계약 v2 — 코딩 에이전트용 작업 지침 (AGENTS.md)

당신(코딩 에이전트)은 이 저장소의 알고리즘을 **ALGOps가 테스트·벤치마크·튜닝할 수 있게** 만듭니다.
결과물은 저장소 루트의 `algops.yaml` 하나와, 각 알고리즘의 파라미터 파일·엔트리포인트입니다.
**알고리즘의 동작은 절대 바꾸지 않습니다.** 기존 하드코딩 값은 그대로 `default`가 됩니다.

언어 제약은 없습니다. Python이면 의존성 없는 SDK(`import algops_sdk`)를, 그 외 언어(C/C++, Go, Rust,
MATLAB, 셸 …)는 "JSON 파일 하나 읽고 JSON 파일 하나 쓰는" 프로토콜을 직접 구현합니다.

## 0. 이해 & 문서화
1. `algops introspect .` 로 하드코딩된 숫자 리터럴 후보를 `algops/UNDERSTANDING.md`에 뽑습니다.
2. 보강합니다: 이 저장소에 **어떤 알고리즘이 몇 개** 있는지, 각 알고리즘의 입력(테스트셋)·출력·
   정답(ground truth), 후처리 파이프라인 단계(stage id), 외부화할 파라미터 후보(file:line, 현재값,
   타입/범위, 오탐·미탐 영향), 그리고 **무엇이 "좋다"인지**(지표와 방향).

## 1. 매니페스트 — `algops.yaml`
`algops init`이 템플릿을 만듭니다. 핵심 필드:

```yaml
algops: 2
name: <repo-name>
env:
  setup: ["uv sync"]            # 커밋마다 1회 (빌드, 의존성 설치). 예: "make build", "cmake ... && make"
  python: .venv/bin/python      # python 엔트리포인트용 인터프리터 (선택)
suites:                          # 테스트셋. 알고리즘마다 여러 개 가능
  - { id: smoke, uri: data/smoke, tags: [fast] }
  - { id: full,  uri: "${DATASETS}/myrepo/full" }   # 러너 호스트의 환경변수로 확장
algorithms:
  - id: detector                 # 소문자 slug, 불변
    params: algops/detector/params.yaml     # 또는 인라인 목록
    entrypoint: { python: "algops/detector/run.py:run" }   # 또는 { command: ["./build/eval"] }
    suites: [smoke, full]        # 생략 시 전체
    metrics: { presets: [binary], declare: [{ name: latency_ms, direction: minimize }] }
    gates: [{ metric: recall, op: ">=", value: 0.85 }]      # 벤치마크 절대 기준
    regression: { rel_tolerance: 0.02 }                     # 베이스라인 대비 허용폭
    objective: { metric: fp_rate, target: 0.05, constraints: [{ metric: recall, op: ">=", value: 0.85 }] }
    runs_on: [gpu]               # 필요한 러너 라벨 (선택)
    timeout_s: 900
```

- **지표**는 알고리즘이 선언합니다. 프리셋: `binary`(tp/fp/fn/tn → precision, recall, f1, accuracy,
  fp_rate, fn_rate), `regression`(mae, rmse, mse, max_abs_error), `latency`. 그 외는 `declare`에
  이름과 방향(minimize/maximize)으로 추가합니다.
- **게이트**는 "이 값 아래로 떨어지면 실패"인 절대 기준, **목표(objective)**는 최적화 캠페인의 기본 목표입니다.
- 한 저장소에 알고리즘이 여러 개면 모두 `algorithms`에 나열합니다. 테스트셋이 여러 개면 모두 `suites`에.

## 2. 파라미터 외부화
각 파라미터는 **완전 명세 객체**입니다 (`algops/params.schema.json`):

```yaml
params:
  - key: detector.confidence_threshold   # 점표기, 전역 고유, 불변 (rename 금지 — 추가/폐기만)
    dtype: float                          # float | int | bool | enum
    default: 0.45                         # 지금 코드에 있는 값 그대로
    range: [0.05, 0.95]                   # float/int 필수
    step: 0.05                            # 탐색 단위 (float/int 필수)
    stage: decision                       # 텔레메트리 stage id
    influence: { fp: high, fn: high }     # 오탐/미탐 영향 (사전 지식)
    drop_reasons: [below_threshold]       # 이 파라미터가 만드는 dec_reason 값들 (선택)
```

코드의 해당 리터럴을 **런타임에 전달된 값**으로 바꿉니다. 전달 방식(어느 것이든 사용 가능):
- Python SDK: `ctx.params["detector.confidence_threshold"]`
- 환경변수: `ALGOPS_PARAM_DETECTOR_CONFIDENCE_THRESHOLD` (키의 영숫자 외 문자를 `_`로, 대문자)
- 평탄한 JSON: `$ALGOPS_PARAMS_FILE` → `{"detector.confidence_threshold": 0.45, ...}`
- 전체 요청: `$ALGOPS_REQUEST` (request.json)
- command 인자 치환: `{param:detector.confidence_threshold}`

러너가 기본값 병합·범위 클램프·타입 검증·**미선언 키 거부**를 먼저 하므로, 프로그램은 항상
완전한 파라미터 집합을 받습니다.

## 3. 엔트리포인트 (실행 프로토콜 `algops.run/v2`)
러너는 `request.json`을 쓰고 프로그램을 실행한 뒤 `result.json`을 읽습니다.

**Python** (`entrypoint: { python: "algops/detector/run.py:run" }`):
```python
import algops_sdk as sdk          # 러너가 주입 — 이 저장소에 ALGOps 설치 불필요

def run(ctx: sdk.Context):
    thr = ctx.params["detector.confidence_threshold"]
    dec = ctx.stage("decision", ["dec_kept", "dec_reason", "gt_match"])
    ...  # ctx.suite_path 의 테스트셋을 평가, dec.row(item_id, frame_index, entity_id, ...)
    return sdk.counts(tp=tp, fp=fp, fn=fn, tn=tn, latency_ms=lat)   # 또는 sdk.result(metrics={...})
```

**그 외 언어** (`entrypoint: { command: ["./build/eval"] }`): `$ALGOPS_RESULT` 경로에 다음을 씁니다.
```json
{"status": "ok", "metrics": {"latency_ms": 3.2}, "counts": {"tp": 90, "fp": 4, "fn": 10, "tn": 96}, "n_items": 200}
```
파일 쓰기가 어려우면 stdout 마지막 줄에 `::algops-result::{...json...}` 을 출력해도 됩니다.
실패는 `{"status": "failed", "error": "..."}` 또는 0이 아닌 종료 코드입니다.

입력 환경변수: `ALGOPS_REQUEST`, `ALGOPS_RESULT`, `ALGOPS_OUTPUT_DIR`(산출물/CSV 저장 위치),
`ALGOPS_PARAMS_FILE`, `ALGOPS_PARAM_*`, `ALGOPS_SUITE_PATH`, `ALGOPS_SUITE_URI`, `ALGOPS_SUITE_ID`,
`ALGOPS_RUN_ID`, `ALGOPS_REPO_ROOT`, `ALGOPS_COMMIT_SHA`, `ALGOPS_SEED`.
command 치환자: `{request} {result} {output_dir} {params} {suite_path} {suite_uri} {run_id} {repo_root} {param:<key>}`.

## 4. 텔레메트리 (권장)
파이프라인 단계마다 CSV 1개를 `ALGOPS_OUTPUT_DIR`에 쓰고 `manifest.json`으로 단계를 선언합니다
(SDK의 `ctx.stage(...)`가 자동 처리). 공통 선행 컬럼: `run_id,item_id,frame_index,entity_id,stage`.
`entity_id`는 AI 출력→특징→결정을 같은 객체로 잇는 척추입니다. 컬럼 프리픽스: `in_*` 설정,
`ai_*` 모델 출력, `feat_*` 특징, `dfeat_*` 파생/윈도우 특징, `dec_*` 결정, `gt_*` 정답.
`decision` 단계에 `dec_kept`·`gt_match`·`dec_reason`이 있으면 마스터가 혼동행렬을 독립 재계산하고,
제거 사유를 `drop_reasons`로 파라미터에 귀속합니다.

## 5. 검증 (필수)
```bash
algops validate .        # 정적 검사: 스키마, 파라미터, 엔트리포인트 파일, 지표 선언
algops verify .          # validate + 모든 알고리즘을 기본값으로 1회 실행 — exit 0 이어야 완료
algops run . -a detector -s smoke --set detector.confidence_threshold=0.6   # 셀 하나
algops test .            # 모든 알고리즘 × 스위트 + 게이트. --json 으로 리포트, --baseline 으로 회귀 비교
```
**`algops verify .`가 exit 0이 될 때까지 완료를 선언하지 마세요.** 기본값 실행 결과가 외부화 이전
코드의 결과와 같아야 합니다(동작 보존). 가능하면 외부화 전후의 `algops test --json` 결과를 비교하세요.

## 제약
- 저장소 내부만 수정합니다. 단계마다 commit 합니다.
- 코드에 없는 파라미터를 만들지 않습니다. 알고리즘 로직을 바꾸지 않습니다.
- `key`, 알고리즘 `id`, 스위트 `id`, stage id는 불변입니다 (추가/폐기만 허용) — 바꾸면 계보·학습된
  영향·베이스라인 비교가 끊깁니다.
- 테스트셋 경로는 하드코딩하지 말고 `suites[].uri`로 선언하고 `ALGOPS_SUITE_PATH`로 읽습니다.
