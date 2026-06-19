# CLAUDE.md

이 저장소는 ALGOps로 후처리 파라미터를 최적화합니다.

@AGENTS.md

위 `AGENTS.md`의 ALGOps 외부화 계약을 그대로 따르세요. 요약:

1. `algops-introspect .` → `algops/UNDERSTANDING.md` 보강
2. `algops/params.yaml` + `algops/config.py` 작성, 코드의 리터럴을 외부화된 값으로 치환
3. 단계별 CSV + `manifest.json` 텔레메트리 배선 (`run_id,item_id,frame_index,entity_id,stage` + `in_/ai_/feat_/dfeat_/dec_/gt_`)
4. `algops/run_adapter.py`의 `run(req, output_dir)` 구현
5. `python algops/verify_algops.py .` 가 **exit 0** 일 것 — 성공 선언 전 필수

`key`/stage id는 불변(rename 금지). 동작은 바꾸지 마세요. 자세한 내용은 `AGENTS.md`를 참조하세요.
