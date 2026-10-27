# AGENTS.md — lavcca-backend 자동 개발(GPT ↔ Claude Code) 규칙

모든 에이전트(Codex/GPT, Claude Code)가 따른다. 저장소 구조·실행 방법은 `README.md`·`CLAUDE.md`, 저장소 간 계약은 lavcca-docs 가 기준이다.

## 역할

| 주체 | 하는 일 | 하지 않는 일 |
|---|---|---|
| GPT (기획·리뷰) | 작업 명세(목표, 허용 파일, 완료 기준·테스트), diff·테스트 결과 리뷰, 다음 작업 제안 | 코드 push, PR 병합, 설정·인프라 변경 |
| Claude Code (구현·테스트) | 허용 파일 안 구현, 로컬 테스트, 오류 수정, 변경 보고 | 범위 밖 수정, PR 병합, 배포 |
| PM (사람) | 명세 승인, PR 병합, 배포·비용·저장소 삭제·권한 변경 승인 | — |

## 브랜치·PR

- 작업 브랜치 `ai/<작업ID>-<짧은-설명>`, 기준은 `main`(브랜치 묶음이 main 에 병합되기 전까지는 `chore/lavcca-worker-deploy`). 작업 1개 = 브랜치 1개 = PR 1개.
- `main` 직접 커밋·push·병합 금지. 강제 push·히스토리 재작성 금지. 병합은 PM 만 한다.
- PR 본문: 목적, 변경 파일, 실행한 테스트 명령과 통과 수, 남은 위험(`.github/pull_request_template.md`).

## 검증 루프와 한도 (초안 기본값 — PM 확정 필요)

1. 명세의 허용 파일만 수정한다. 범위 밖 수정이 필요하면 멈추고 보고한다.
2. 테스트: `python -m pytest apps/worker/tests apps/lambdas/tests .claude/hooks -q`. 실패하면 고치고 다시 실행한다.
3. 테스트-수정 반복 최대 3회, GPT 리뷰 왕복 최대 2회. 넘으면 멈추고 실패 보고(재현 명령, 로그 요약, 추정 원인).
4. 작업당 60분, Claude 비용 상한(`--max-budget-usd`)과 OpenAI 호출 수는 오케스트레이터 설정을 따른다. 한도에 닿으면 즉시 중단.
5. 완료 기준: 로컬 테스트 통과 + PR CI 통과 + diff 가 명세 범위 안.

## 오케스트레이터(lavcca-orchestrator) 실행 방식

- 오케스트레이터는 저장소 밖에 두고, 작업마다 새 worktree(`git worktree add <폴더> -b ai/<작업ID>-<설명> main`)를 `--repo` 로 준다. 미추적 파일이 있는 폴더에서는 시작하지 않는다.
- 러너는 커밋·push·PR 을 하지 않는다. `runs/*.json` 과 diff 를 사람이 확인한 뒤 커밋하고 PR 을 연다.
- Claude 는 `--restricted`(셸·웹 도구 없음) + `--settings .claude/settings.json`(restricted 는 프로젝트 설정을 무시하므로) + `--max-budget-usd` 로 실행한다.
- 작업의 `allowed_paths` 는 이미 추적 중인 파일만. 기획·리뷰(OpenAI)와 구현(Claude)은 유료 — 실행 전 PM 승인, `max_rounds` ≤ 2.

## 승인 없이 하지 않는 것

- 수정: `.github/`, `.claude/`, `infra/`, `apps/worker/Dockerfile`, `apps/worker/ai-engine.ref`, 의존성 파일
- 클라우드·배포 명령(`aws`, `supabase`, `docker push`), 저장소 설정·권한 변경, 저장소 삭제
- 레거시·대량 파일 이동·삭제

AWS 배포는 `worker-image` publish 잡 + `lavcca-dev` 환경 승인(사람)으로만 한다. 이 저장소만 AWS(OIDC) 권한을 가진다.

## 비밀정보

- `.env`, `*credentials*.json`, `*.pem`, `*.key`, `*.sqlite` 를 읽거나 출력하지 않는다(`.claude/hooks/guard.py` 가 셸 접근도 막는다).
- 키·토큰·계정 정보를 코드, 로그, PR, 리뷰에 쓰지 않는다. 테스트는 모의 객체만 쓰고 실제 API·AWS·Supabase 를 부르지 않는다.
