# 브랜드 변경 NATAM → LAVCCA(라브카): 호환 범위와 Vault 마이그레이션 절차

명칭 규칙: 한글 `라브카` · 영문 `LAVCCA` · 소문자 식별자 `lavcca` · 저장소 `lavcca-notion-sync`.

## 1. 코드에서 바꾼 것 (2026-10-28)
| 대상 | 새 이름 | 기존 이름 처리 |
|---|---|---|
| 식별자·주석·로그·보고서 메시지·README | LAVCCA / lavcca | — |
| 상태 코드 | `MOVED_OUTSIDE_LAVCCA`, `NOT_LAVCCA_DESCENDANT` | 보고서에만 쓰이고 state.json 에 저장되지 않음 → 변환 불필요 |
| config.json 키 | `lavcca_root_path`, `lavcca_root_id` | `natam_root_path`, `natam_root_id` 를 로드할 때 새 키로 바꿔 읽음(`canary_apply.normalize_config`). 두 키가 모두 있고 값이 다르면 실행 거부 |
| 환경변수 | `LAVCCA_NOTION_API_BASE`, `LAVCCA_NETWORK_WAIT_S`, `LAVCCA_E2E_LOOPBACK` | `NATAM_*` 를 대체값으로 읽음(`canary_apply.env`), 새 이름이 우선 |
| 토큰 파일 | `~/.lavcca/secrets.env` | 없으면 `~/.natam/secrets.env` |
| frontmatter `project` | `LAVCCA` 와 `NATAM` 둘 다 허용 | 기존 파일 171개의 값은 그대로 두고, 엔진도 다시 쓰지 않음 |
| 관리 폴더 경로 | `lavcca_root_path` 에서 계산(`ROOT_REL`) | 코드에 있던 `"NATAM/..."` 하드코딩 제거. 기본 폴더명을 짐작하던 코드는 오류로 변경 |
| run_sync.ps1 (저장소 사본) | `%LOCALAPPDATA%\LAVCCA\scheduler-fallback`, `lavcca_sync_*` 임시 파일 | PC 에 배포된 기존 사본은 그대로 |

테스트: 기본 fixture 를 `LAVCCA/` 폴더 + `project: LAVCCA` + `LAVCCA_*` 로 바꿨다. `TestE2ELegacyLayout` 은 기존 배치(`NATAM/`, `project: NATAM`, `natam_*` 키, `NATAM_*` 환경변수)로 같은 동기화를 검증한다.

## 2. 보류 (데이터 손실·링크 손상 가능성)
| 항목 | 현재 값 | 보류 이유 |
|---|---|---|
| Vault 관리 폴더 이름 | `Obsidian Vault/NATAM/` | Vault 안 `[[NATAM/...]]` 링크가 30개 파일에 440건 있다(관리 파일 9개, 사람이 쓴 노트 21개. `_sync` 의 로그·백업 98건은 대상 아님. 이미 깨진 링크 7건은 MOC 노트 2개에 있음). 엔진은 링크를 폴더 경로에서 만든다. 그래서 폴더만 바꾸면 관리 페이지를 다시 렌더링할 때 본문 해시가 기준값과 달라지는데 Notion 수정 시각은 그대로다. 그러면 `CONTENT_HASH_ANOMALY`(자동 적용 안 함)로 분류되어 해당 페이지의 동기화가 멈춘다. |
| `project: NATAM` (171개 파일) | NATAM | 둘 다 허용하므로 급하지 않다. 바꾸면 Vault 파일 171개가 수정된다. 본문 해시는 frontmatter 를 빼고 계산하므로 동기화 판단에는 영향이 없다. |
| `notion_path: "NATAM/..."` | Notion export 당시 경로 기록 | 과거 사실 기록이다. 제목·폴더 추정에만 쓰인다. |
| 실제 파일명 `00_HOME/NATAM.md`, `NATAM IR 피칭 슬라이드.docx`, `99_ARCHIVE/Notion-Raw/Legacy-NATAM/` | — | state.json `obsidian_path`·첨부 `local_path` 와 링크가 이 이름을 가리킨다. 이름을 바꾸면 매핑이 끊긴다. |
| `~/.natam/` 폴더 (secrets·staging·백업·설계 문서) | — | 보존 지시 대상이고, 코드는 두 위치를 모두 읽는다. |
| Windows 예약 작업 이름 `NATAM Notion Obsidian Sync` | — | 예약 작업 변경 금지. GitHub Actions 로 전환(R7)할 때 비활성화하는 대상이다. |
| Notion Integration 이름 (기존 이름) | — | Notion 쪽 표시 이름이다. 바꿔도 토큰에는 영향이 없지만 외부 변경이라 승인이 필요하다. |

## 3. Vault 마이그레이션 절차 (승인 후, 중앙 동기화 R7 안정화 뒤)
| 단계 | 내용 | 검증 |
|---|---|---|
| M0 | 모든 실행 주체 정지: `gh workflow disable`, Windows 작업 비활성화 확인 | 잠금 없음, 실행 중 run 없음 |
| M1 | 새 백업: `NATAM/` tar + sha256 manifest (기존 백업과 별도 폴더) | manifest 전수 일치 |
| M2 | 사전 dry-run: 기존 이름 그대로 `--apply-updates --dry-run` | UPDATE·ANOMALY 0 |
| M3 | 이름 변경 도구 dry-run `python -m ci.brand_migration --vault ... --report <Vault 밖> --expect-links 440`: Vault 전체 `.md` 의 `[[NATAM/` → `[[LAVCCA/` 변경 대상 목록, 파일별 전후 해시, 관리 파일의 새 본문 해시 | 대상 30개 / 440건, state 불일치 0, 새 해시 필요 8개 (2026-10-28 실측) |
| M4 | OneDrive 에서 폴더 이름 변경 `NATAM` → `LAVCCA`, 두 PC 에 반영 확인 | 충돌 사본(-<PC 이름>) 없음 |
| M5 | 이름 변경 도구 적용: 링크 재작성 + 관리 파일의 state.json `local_content_hash`·`last_synced_content_hash`·`api_rendered_content_hash` 를 새 본문 해시로 함께 갱신(하나의 원자적 배치, 백업 포함) | 재작성 후 `ci.plan.audit_mirror` 불일치 0 |
| M6 | config.json: `lavcca_root_path` 를 `...\LAVCCA` 로, `natam_*` 키 제거 | `normalize_config` 통과 |
| M7 | (선택) frontmatter `project: NATAM` → `LAVCCA` | 본문 해시 불변 확인 |
| M8 | dry-run → 변경 0, 링크 깨짐 0(Obsidian 의 해결 안 된 링크 수가 전후 동일) → 수동 1회 적용 → workflow 재개 | 재실행 변경 0 |
| 되돌리기 | M1 백업으로 폴더·state.json·config.json 을 복원하고, 폴더 이름을 `NATAM` 으로 되돌림 | manifest 일치 |

미확인: M5 대신 `--adopt-baseline` 으로 기존 기록의 해시를 다시 등록할 수 있는지(현재는 새 기록만 추가하는 것으로 보임). M3 도구를 작성할 때 확인한다.
