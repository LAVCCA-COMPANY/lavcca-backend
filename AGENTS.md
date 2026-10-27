# AGENTS.md — 자동 개발(GPT ↔ Claude Code) 작업 규칙

이 저장소에서 일하는 모든 에이전트(Codex/GPT, Claude Code)가 따른다.
**프로젝트 구조·실행 방법·금지 파일은 `CLAUDE.md`가 단일 기준**이다. 작업 전에 반드시 읽는다.
이 파일은 에이전트 간 역할·브랜치·검증 루프·한도만 정한다.

## 역할

| 주체 | 하는 일 | 하지 않는 일 |
|---|---|---|
| GPT (기획·리뷰) | 작업 명세 작성(목표, 범위 파일, 완료 기준·테스트), PR diff 리뷰, 수정 요청 | 코드 push, PR 병합, 설정·인프라 변경 |
| Claude Code (구현·테스트) | 작업 브랜치 생성, 범위 안 구현, 테스트 실행, PR 생성(draft) | 범위 밖 수정, PR 병합, 배포 |
| PM (사람) | 명세 승인, PR 병합, 배포·AWS·권한 변경 승인 | — |

## 브랜치·PR

- 브랜치: `ai/<작업ID>-<짧은-설명>` (예: `ai/T12-worker-timeout-log`). 작업 1개 = 브랜치 1개 = PR 1개.
- 기준 브랜치는 작업 명세가 정한다. 브랜치 묶음이 main 에 병합되기 전까지 기본값은 `chore/lavcca-worker-deploy`.
- `main` 직접 커밋·push·병합 금지. 강제 push·히스토리 재작성 금지. PR 병합은 PM 만 한다.
- PR 본문: 목적, 변경 파일, 실행한 테스트 명령과 결과(통과 수), 남은 위험.

## 검증 루프와 한도 (초안 기본값 — PM 확정 필요)

1. 명세의 범위 파일만 수정한다. 범위 밖 수정이 필요하면 멈추고 GPT/PM 에 보고한다.
2. 테스트를 실행한다(명령은 `CLAUDE.md` Testing 절). 실패하면 원인을 고치고 다시 실행한다.
3. **테스트-수정 반복 최대 3회.** 넘으면 멈추고 실패 보고(재현 명령, 실패 로그 요약, 추정 원인)를 남긴다.
4. **GPT 리뷰 왕복 최대 2회.** 넘으면 PM 에게 넘긴다.
5. 작업 1개당 실행 시간 60분, 비용 한도는 오케스트레이터 설정값을 따른다. 한도에 닿으면 즉시 중단·보고한다.
6. 완료 기준: 로컬 테스트 통과 + PR 의 `tests` 체크 통과 + diff 가 명세 범위 안.

## 승인 없이 하지 않는 것

- `.github/workflows/`, `.github/actions/`, `.claude/`, `.codex/`, `infra/aws/`, `infra/supabase/` 수정
- 정책 파일(`rules.yaml`, `controversy_labels.yaml`, `worst_actions_map.yaml`, 리포트 템플릿) 값 변경
- 의존성 추가·버전 변경(`requirements*.txt`, `requirements-dev.in`)
- `aws`, `supabase`, `docker push`, 배포·클라우드 쓰기 명령. AWS 배포는 `worker-image` publish 잡 + `lavcca-dev` 환경 승인(사람)으로만 한다.
- 레거시(`mvp 1_9_2/`, 루트 `copyright_detector/`) 수정·삭제, 파일 대량 이동

## 비밀정보

- `.env`, `*credentials*.json`, `*.pem`, `*.key`, `*.sqlite` 를 읽거나 출력하지 않는다(`.claude/hooks/guard.py` 가 셸 접근도 막는다).
- 키·토큰·JWT·계정 정보를 코드, 로그, PR 본문, 리뷰 코멘트에 쓰지 않는다. 테스트는 모의 객체만 쓰고 실제 API·AWS·Supabase 를 부르지 않는다.
