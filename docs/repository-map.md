# LAVCCA 저장소 지도

2026-10-27 재구축 이후. 상세 이전 매핑·백업 기록: docs `migration/2026-10-27-mapping.md`.

| 저장소 | 책임 | 이전 위치(lavcca-backend) |
|---|---|---|
| **lavcca-backend** (ID 1285873680) | 워커 진입점·Lambda·Supabase·AWS 인프라·이미지 빌드·배포 | — |
| **ai** | 분석 엔진·저작권 감지기·정책 YAML·모델·엔진 테스트·평가·학습 도구 | `apps/worker/` (옛 `mvp 1_9_3/`) 대부분 |
| **data** | 학습·평가 데이터, 스키마, 검증·라벨링·수집 도구 | `apps/worker/data/`, `tools/label_golden.py` 등 4개 |
| **frontend** | 사용자 웹(코드 없음, 연동 문서만) | — |
| **docs** | 아키텍처·계약·위험 기준·운영·ADR·온보딩 | `docs/`·README 일부 |

이 저장소에서 지운 레거시(`mvp 1_9_2/`, 루트 `copyright_detector/`, `apps/worker/_deprecated/`)는 이력과 검증된 백업 번들에 남아 있다.

## 분리 후 로컬 작업 폴더 정리

Git 은 추적 파일만 지운다. `.gitignore` 대상 로컬 파일(`.env`, `copyright_detector/.env`, `*.sqlite`, `casedb/`, `reports/` …)은
옛 폴더(`mvp 1_9_3/` 또는 `apps/worker/`)에 남는다. ai 를 옆에 클론한 뒤 엔진용 파일을 그쪽으로 옮긴다(덮어쓰기 없음):

```powershell
robocopy "apps\worker" "..\ai" /E /XC /XN /XO /MOVE /XF worker.py Dockerfile .dockerignore ai-engine.ref
```
