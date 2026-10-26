# CLAUDE.md

이 저장소의 알고리즘은 ALGOps(계약 v2)로 테스트·벤치마크·튜닝됩니다.

@AGENTS.md

위 `AGENTS.md`의 계약을 그대로 따르세요. 요약:

1. `algops introspect .` → `algops/UNDERSTANDING.md` 보강 (알고리즘 목록, 테스트셋, 지표와 방향)
2. `algops.yaml` 작성 — 알고리즘 N개 × 스위트 M개, 지표·게이트·목표 선언 (`algops init`으로 시작)
3. 파라미터를 `params.yaml`(또는 인라인)로 외부화하고 코드가 런타임 값을 읽게 치환 — 기본값은 현재 값 그대로
4. 엔트리포인트: Python은 `algops_sdk`의 `run(ctx)`, 그 외 언어는 `$ALGOPS_RESULT`에 JSON 결과 쓰기
5. `algops verify .` 가 **exit 0** 일 것 — 성공 선언 전 필수. 이후 `algops test .`로 게이트 확인

`key`/알고리즘 id/스위트 id/stage id는 불변(rename 금지). 동작은 바꾸지 마세요.

Claude Code를 쓴다면 post-edit 훅으로 `algops validate .`를, 작업 종료 전에 `algops verify .`를
실행하도록 설정하는 것을 권장합니다.
