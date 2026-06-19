# ALGOps 외부화 계약 (AGENTS.md)

당신(코딩 에이전트)은 이 프로젝트의 **후처리 알고리즘 파라미터를 ALGOps가 실험할 수 있도록 외부화**합니다.
**동작은 절대 바꾸지 않습니다.** 아래 단계를 순서대로 수행하고, 각 단계의 검사를 통과시키세요.

## 목표
프로젝트 전반에 흩어진 후처리 파라미터(threshold, roundness, HSV/window 크기 등)를 단 하나의
`algops/params.yaml`(스키마 `algops/params.schema.json` 준수)로 빼내고, 코드가 그 값을 읽도록 바꿉니다.

## 전제
- `pip install algops`(또는 슬레이브 번들)이 설치되어 `algops` 모듈을 import할 수 있어야 합니다.
- 모든 산출물은 프로젝트 루트의 `algops/` 디렉터리에 둡니다.

## Phase 0 — 이해 & 문서화
1. `algops introspect`(또는 `algops-introspect .`)를 실행해 `algops/UNDERSTANDING.md` 초안을 만듭니다.
2. 초안을 보강합니다: 프로젝트 목적, 후처리 목표, **파이프라인 단계(stage id)**, 외부화할 파라미터 후보
   (각 파라미터의 file:line·현재값·추정 타입/범위/오탐·미탐 영향).
   - stage id는 §CSV 규약과 일치해야 합니다(예: `ai_inference`, `postproc_shape`, `decision`).

## Phase 1 — 외부화
1. `algops/params.yaml` 작성 — 외부화할 각 파라미터를 **완전 명세 객체**로 적습니다(`templates/` 참고):
   ```yaml
   params:
     - key: detector.confidence_threshold   # 점표기, 전역 고유, 불변(rename 금지)
       dtype: float                          # float|int|bool|enum
       default: 0.45
       range: [0.05, 0.95]                   # float/int 필수
       step: 0.05                            # 탐색 granularity
       stage: decision                       # CSV stage id와 일치
       influence: { fp: high, fn: high }     # 오탐/미탐 영향(a-priori)
   ```
2. `algops/config.py` 생성 — `templates/config.py.tmpl`를 그대로 사용(파라미터를 검증하며 로드).
3. 코드 내 **모든 해당 리터럴**을 `algops.contract.effective_params(...)`/`load_params()` 값으로 치환합니다.
   - `key`는 영구 불변입니다(이름을 바꾸면 Neo4j 계보·학습 가중치가 깨집니다 — 추가/폐기만 허용).

## Phase 2 — 텔레메트리 배선 (CSV 규약)
파이프라인 단계마다 CSV 1개를 `output_dir`에 생성합니다. 공통 선행 컬럼:
`run_id, item_id, frame_index, entity_id, stage`. `entity_id`는 AI출력→특징→결정을 같은 객체로 잇는 척추입니다.
타입 프리픽스: `in_*`(설정), `ai_*`(모델 출력), `feat_*`(특징), `dfeat_*`(파생/윈도우), `dec_*`(결정), `gt_*`(정답).
모든 단계를 선언하는 `manifest.json`도 함께 생성합니다(아래 run_adapter 템플릿 참고).

## Phase 3 — Run 어댑터
`algops/run_adapter.py` 작성 — `templates/run_adapter.py.tmpl`를 채웁니다:
`run(req, output_dir) -> RunResult` 가 ① 유효 파라미터 적용 ② 모델+후처리를 테스트셋에 실행
③ 단계별 CSV+manifest 생성 ④ 정답 대비 지표 계산 ⑤ `RunResult` 반환을 수행해야 합니다.

## 검증 (필수)
```
python algops/verify_algops.py .      # 또는 algops-verify .
```
**exit 0 이어야 성공입니다.** 게이트는 다음을 강제합니다:
- `params.yaml`이 스키마에 맞고 파라미터가 오류 없이 로드됨
- `run_adapter.run()`이 self-check 요청에서 스키마 유효한 `RunResult`를 반환함(= 동작 보존)
- (권고) 외부화 대상 리터럴이 코드에 남아있지 않음
Claude Code post-edit hook이 이 게이트를 자동 실행하도록 권장합니다.

## 제약
- 저장소 내부만 수정합니다. 단계마다 commit 하세요.
- 코드에 없는 파라미터를 만들지 않습니다. 알고리즘 로직을 바꾸지 않습니다.
- `key`/`entity_id`/stage id는 불변으로 취급합니다(추가/폐기만).
