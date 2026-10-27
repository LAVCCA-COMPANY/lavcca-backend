# LAVCCA 저장소 지도

2026-10-27 기준. 조직 `LAVCCA-COMPANY`(옛 `NATAM-KESSOKUBAND`).

## 이 저장소(lavcca-backend) 구조

| 경로 | 내용 | 비고 |
|---|---|---|
| `apps/worker/` | 분석 워커(위기 엔진 + 저작권 감지기), 로컬 Flask 화면, 학습·평가 도구(`tools/`), 학습 데이터(`data/`), 모델(`models/`) | 옛 `mvp 1_9_3/`. Docker 빌드 컨텍스트. 내부 상대 경로는 `paths.py` 한 곳 |
| `apps/worker/copyright_detector/` | 저작권 감지기(별도 프로세스, 자기 폴더가 import 루트) | 쪼개지 않는다 |
| `apps/lambdas/` | API·dispatcher Lambda(미배포 초안) + `supa.py` | 옛 `infra/aws/lambdas/` |
| `infra/aws/` | CloudFormation, IAM 정책, S3 수명 주기 | 배포는 PM 승인 |
| `infra/supabase/` | 마이그레이션·롤백·검증 SQL | |
| `docs/` | 저장소 문서 | |
| `mvp 1_9_2/`, 루트 `copyright_detector/` | 레거시·고아 경로(보존) | 수정·삭제 보류 |

AI 학습 관련 코드(전처리·라벨링·학습·평가·실험 설정)는 지금 `apps/worker/tools/`와
`apps/worker/data/`에 있고, 워커 엔진(`classifier_model.py`, `paths.py`)을 직접 import 한다.
분리하면 이 결합과 `eval-gate`(모델·데이터·코드 동시 검사)가 깨지므로 같은 저장소에 둔다.
학습 실험이 엔진과 독립된 수명 주기를 갖게 되면 그때 `lavcca-ai`로 분리한다.

## 데이터 원칙

- 원본 영상, 고객 업로드, 대용량 데이터셋, 임베딩·사례 인덱스(`casedb/`)는 **S3**(`lavcca-dev-datalake`, `lavcca-dev-customer-uploads`)에 둔다.
- GitHub에는 코드, 스키마, 설정, 정책 YAML, 작은 샘플·골든셋만 둔다.

## 조직 저장소 분류 (2026-10-27 감사)

| 저장소 | 상태 | 분류 | 권장 |
|---|---|---|---|
| `lavcca-backend` | 실제 코드 전부 | KEEP | — |
| `docs` | 비어 있음(커밋 0, 설명: 기획서·회의록·API 문서) | KEEP | 사업 문서 보관소로 사용 |
| `web` | 비어 있음(설명: 웹사이트) | RENAME CANDIDATE | 프런트엔드 착수 시 `lavcca-frontend` |
| `AI` | 비어 있음(설명: LLM 분석 로직) | DELETE CANDIDATE | 해당 코드는 `lavcca-backend`. 필요해지면 `lavcca-ai` 새로 생성 |
| `data` | 비어 있음(설명: 데이터셋·리스크 기준) | DELETE CANDIDATE | 데이터는 S3, 기준·라벨은 `lavcca-backend` |
| `backend` | 비어 있음(설명: API 서버) | DELETE CANDIDATE | `lavcca-backend`와 이름 혼동 |
| `NATAM` | `.gitkeep` 1개 | DELETE CANDIDATE | 백업 번들 보관 |
| `shopping-list-app` | 개인 학습 JS 앱(커밋 1) + Claude Code Actions 실험 PR #1 | ARCHIVE CANDIDATE | 개인 계정으로 이전 권장 |

삭제·보관·이전·이름 변경은 PM 승인 후에만 한다. 실행 전 해당 저장소의 mirror 번들을 만들고 검증한다.

## `mvp 1_9_3/` → `apps/worker/` 이동 후 로컬 작업 폴더 정리

Git 은 추적 파일만 옮긴다. `.gitignore` 대상(로컬 `.env`, `copyright_detector/.env`, `*.sqlite`, `casedb/`,
`reports/`, `logs/`, `downloads/`, `transcripts/`, `data/trends/raw/` 등)은 옛 `mvp 1_9_3/` 폴더에 남는다.
브랜치를 바꾼 뒤 남은 파일을 직접 옮긴다(덮어쓰기 없음):

```powershell
robocopy "mvp 1_9_3" "apps\worker" /E /XC /XN /XO /MOVE
```

옮긴 뒤 `mvp 1_9_3/` 가 비었는지 확인하고 지운다.
