# LAVCCA(라브카) Notion → Obsidian Sync Engine (v1: DRY-RUN · CANARY · ADOPT BASELINE · incremental APPLY)

> 브랜드 변경(NATAM → LAVCCA, 2026-10-28): 코드·메시지·설정 키는 LAVCCA 이름을 쓴다. 아직 옮기지 않은 Vault 의
> `NATAM/` 폴더, `project: NATAM`, `natam_*` 설정 키, `NATAM_*` 환경변수, `~/.natam/secrets.env` 는 그대로 동작한다.
> 실제 Vault 를 옮기는 절차는 `docs/brand-migration.md`.

## 클라우드 실행 (lavcca-backend → docs) — 현재 경로

```
Notion Webhook → worker/ (서명 검증·중복 제거·60초 묶음·실패 시 503) → workflow_dispatch notion-sync.yml
  → ci/docs_publish.py: 변경 검색(ci/run.search_since + 웹훅 힌트) → 바뀐/새 페이지만 조회 → 렌더(canary_apply 렌더러)
  → LAVCCA-COMPANY/docs obsidian-vault/Notion/ 에 쓰기 → 봇 커밋·push (GitHub App 설치 토큰, docs 만)
```
- 엔진(`notion_sync.py`)과 달리 생성·제목 변경·삭제를 처리한다: 경로는 처음 한 번 정하고 바꾸지 않는다(링크 보존),
  삭제는 `_deleted/` 로 옮긴다. 이 폴더는 봇만 쓰므로 충돌 판정 대신 git 기록이 백업이다.
- Environment `notion-sync`: 비밀값 `NOTION_TOKEN`, `NOTION_SYNC_CONFIG`, `NOTION_SYNC_APP_KEY`; 변수 `NOTION_SYNC_APP_ID`,
  `NOTION_SYNC_ROOT_ID`(선택, 시험용 하위 트리). 팀 설정·복구 절차는 docs `notion-obsidian-sync.md`.
- 로컬 실행: `python -m ci.docs_publish --docs <docs clone> --config <config.json> [--full]` (토큰: `~/.lavcca/secrets.env`).
- 종료 코드: 0 성공 · 2 Notion 조회 실패(아무것도 쓰지 않음) · 10 쓴 결과와 state 불일치.

## 클라우드 실행 (OneDrive, 보류 — Microsoft Graph 앱 등록 차단으로 실행 경로에서 뺐다)

```
Notion Webhook → worker/ (Cloudflare Worker: 서명 검증·중복 제거·5분 묶음·실패 시 503)
  → workflow_dispatch notion-sync.yml (Environment notion-sync, main 만)
  → ci/onedrive_sync.py: 토큰 갱신 → OneDrive 원격 잠금 → 미러 복원(sha1 검증) → ci/run.py
       (변경 판단 → notion_sync.py --apply-updates [--targets], exit 9 면 전체 크롤) → 바뀐 파일만 조건부 업로드
```
- 테스트: `cd tools/notion-sync && python -m unittest discover -s tests` (Python 3.12, 표준 라이브러리만) + `node --test worker/index.test.mjs`.
- Environment `notion-sync` 비밀값: `NOTION_TOKEN`, `NOTION_SYNC_CONFIG`(config.json 내용, `config.example.json` 형식 + `owner_roots`),
  `MS_CLIENT_ID`, `MS_REFRESH_TOKEN`, `NOTION_SYNC_SECRETS_PAT`(이 Environment 비밀값 쓰기 전용). 변수: `NOTION_SYNC_REMOTE_ROOT`, `NOTION_SYNC_ROOT_DIR`.
- 공개 저장소: 실제 config·state·로그는 커밋하지 않는다(`.gitignore`). Actions 로그에는 개수만 나오고, 상세 보고서는 OneDrive `_sync/logs`.
- 종료 코드: 0 성공 · 10 state 불일치 감지(업로드 후 실패 처리) · 70 업로드 일부 실패(잠금 유지, 사람 확인) · 71 엔진이 파일 삭제 ·
  73 잠금 보유 중 · 75 일시 오류 · 78 Microsoft 재로그인 필요 · 그 외 엔진 코드.

## 현재 상태

- 기본 실행은 **전체 DRY-RUN**(읽기 전용)이다.
- 유일한 쓰기 경로는 **CANARY APPLY**: 명시적으로 지정한 notion_id **1개**의 기존 active `managed: notion` 파일만 갱신한다.
- 전체 APPLY, 여러 ID APPLY, archive APPLY, 생성(CREATE), 삭제, 이동, 이름 변경, attachment 다운로드, Notion 쓰기는 **존재하지 않는다.**
- CANARY APPLY는 실제 Vault(관리 폴더 `NATAM/`)에서 **1건만** 실행했다: 2026-10-01T16:08:08Z, `<테스트 페이지 id>`(🧪 테스트 페이지). 백업은 `backups/20261001T160808Z/`에 있다.
- **ADOPT BASELINE 실행 완료 (2026-10-01T17:17Z, 1회):** 106개 기록을 추가했다(`record_type: ADOPTED_BASELINE`, `last_successful_sync: null`). canary 기록 1개는 그대로 보존해 state는 1건에서 107건이 됐다. 이전 state 백업은 `backups/20261001T171736Z/state.json`에 있다. markdown 변경은 0건이다. <멤버>.md(빈 external file block)는 제외했다.
- production incremental APPLY(`--apply-updates`)는 구현과 offline 테스트까지 마쳤다. 2026-10-02에 실제 end-to-end 실행에 성공했다. 자동 실행은 아래 "자동 실행" 절을 참고한다.

## 실행 방법

```
python notion_sync.py                              # 전체 DRY-RUN (기본)
python notion_sync.py --notion-id <ID>             # 단일 페이지 DRY-RUN: 검사 + 렌더링 미리보기만 (logs/canary_preview_*.md)
python notion_sync.py --apply --notion-id <ID>     # CANARY APPLY (단일 페이지 실제 쓰기)
python notion_sync.py --apply                      # 거부 (exit 3) -- 전체 APPLY 없음
python notion_sync.py --adopt-baseline --dry-run   # baseline adoption preview (쓰기 없음)
python notion_sync.py --adopt-baseline             # baseline adoption: state.json만 씀 (markdown은 절대 쓰지 않음, gate 실패 시 전체 중단)
python -m unittest discover -s tests -v            # 전체 offline 테스트 (122)
python -m unittest tests.test_baseline -v          # baseline offline 테스트
python -m unittest tests.test_canary_apply -v      # offline 테스트 (임시 fixture, 네트워크 없음)
```

`--apply` 단독, `--notion-id` 2개 이상, 32자리 hex가 아닌 ID는 `.env`를 읽기 **전에** 거부된다(exit 3).

**Token 위치 (OneDrive 밖):** `NOTION_TOKEN`은 vault에 두지 않는다. 조회 순서는 다음과 같다(`secret_loader.py`).
1. 프로세스 환경변수 `NOTION_TOKEN`
2. `<사용자 홈>/.lavcca/secrets.env`(`NOTION_TOKEN=...` 한 줄). 없으면 이전 위치 `<사용자 홈>/.natam/secrets.env` 를 읽는다. 2026-10-02 기준 ACL은 현재 사용자 전용이다.
3. 둘 다 없으면 `TOKEN_NOT_CONFIGURED`로 API 호출 전에 종료한다.
- vault root `.env`는 **fallback으로도 쓰지 않으며 삭제됐다.** `.env.example`(`NOTION_TOKEN=`)만 남겨 둔다.
- token 값은 코드/`config.json`/`state.json`/`logs/` 어디에도 저장되거나 출력되지 않는다. 로그에는 출처 경로만 남는다.

## CANARY APPLY (`canary_apply.py`)

**쓰기 허용 조건 (하나라도 실패하면 쓰기 거부, 파일·state 무변경):**

| 검사 | 거부 코드 |
|---|---|
| ID가 32-hex 1개 | `INVALID_NOTION_ID` |
| `state.json` 읽기 가능 | `STATE_UNREADABLE` |
| vault에 그 notion_id를 가진 파일이 정확히 1개 | `NOT_FOUND_LOCAL` / `DUPLICATE_LOCAL_ID` |
| `managed: notion` (managed:obsidian 아님) | `MANAGED_NOT_NOTION` |
| active 위치 (99_ARCHIVE·06_KNOWLEDGE·_sync·_migration 아님, LAVCCA root 밖이나 symlink 아님) | `ARCHIVE_TARGET` / `PROTECTED_TARGET` |
| frontmatter 엄격 파싱, identity 키(`project/owner/source/managed/notion_id/notion_path`) 정상 | `MALFORMED_FRONTMATTER` / `INVALID_UTF8` |
| direct retrieve HTTP 200, `archived=false`, `in_trash=false` | `NOT_FOUND_REMOTE` / `PERMISSION_UNKNOWN` / `ARCHIVED_REMOTE` / `TRASHED_REMOTE` |
| parent chain이 LAVCCA root에 도달 | `NOT_LAVCCA_DESCENDANT` |
| local 편집 흔적 없음 | `CONFLICT` |
| block tree 전체 조회 성공 | `CONTENT_FETCH_ERROR` |
| renderer가 모든 block을 안전하게 표현 | `RENDER_UNSUPPORTED` |
| 새 파일 검증(identity 키 불변, notion_id 재확인, UTF-8 round-trip, body hash 일치) | `VALIDATION_FAILED` |
| 실행 중 원본이 바뀌지 않음 | `CONCURRENT_MODIFICATION` |

**CONFLICT 판정:**
- `state.json`에 baseline이 있으면: local body hash ≠ `last_synced_content_hash`이면 CONFLICT.
- baseline이 없고 frontmatter에만 `last_synced_content_hash`가 있으면: 그 값과 비교한다.
- 둘 다 없는 첫 동기화(bootstrap)이면: 파일 mtime이 `config.json`의 `bootstrap_migration_completed_at`(2026-09-30T22:11:23+09:00, migration 완료 시각)보다 나중이면 local 편집 가능성으로 보고 CONFLICT. `manifest.csv`의 sha256은 ZIP 원본 기준이라 baseline으로 쓸 수 없다(0/139 일치).

**쓰기 순서:** 렌더링 → 새 파일 검증 → 원본 재확인 → `backups/<UTC timestamp>/<관리 폴더 상대경로>`에 byte-for-byte 백업(덮어쓰기 금지, 검증) → 같은 디렉터리의 temp 파일에 쓰고 flush/fsync → temp 재검증 → `os.replace` 원자 교체 → 교체 결과 재검증 → `state.json` 원자 쓰기.
- temp 쓰기나 교체가 실패하면: 원본은 그대로이고 temp는 삭제된다.
- state 쓰기가 실패하면: markdown을 백업 바이트로 원자 복구하고 `STATE_WRITE_FAILED_ROLLED_BACK`로 보고한다(성공으로 기록하지 않음).

**frontmatter:** 기존 줄은 순서와 내용 그대로 보존한다(알 수 없는 키 포함). `notion_last_edited_time`, `last_synced_content_hash`, `last_synced_at`만 추가하거나 갱신한다. `notion_id`는 절대 바꾸지 않는다. `last_synced_content_hash`는 frontmatter 닫는 줄 이후 body의 sha256이다.

**state.json 항목:** `notion_id`, `last_seen_edited_time`, `last_synced_content_hash`, `obsidian_path`, `last_successful_sync`. 완전 성공 후에만 기록한다.

**Renderer:**
- 지원 block: paragraph, heading_1/2/3, bulleted/numbered list, to_do, toggle, quote, callout(`> [!note]`), code, divider, bookmark/embed/link_preview, image/file/pdf/video/audio, child_page, link_to_page, table/table_row, column_list/column/synced_block, equation.
- 지원 rich_text: bold, italic, strikethrough, code, hyperlink, page mention(→ wikilink), equation.
- page 링크는 migration과 같은 `[[<관리 폴더>/<경로>|제목]]` 형식이다(관리 폴더는 `lavcca_root_path` 의 Vault 기준 이름, 현재 Vault 는 `NATAM`). vault에 없는 페이지는 눈에 보이는 HTML 주석 placeholder로 남긴다.
- `child_database`, `table_of_contents`, `breadcrumb`는 placeholder 주석으로 남긴다(내용이 다른 페이지에 있거나 원래 내용이 없는 block).
- **그 외 모든 type(`unsupported`, `ai_block`, 알 수 없는 type 등)은 critical → `RENDER_UNSUPPORTED`로 쓰기 중단**한다(조용히 버리지 않음).
- **Notion-hosted 첨부(서명 URL)는 쓰지 않는다.** 현재 local 파일에 같은 이름의 첨부 링크가 정확히 1개 있으면 그 local 경로를 재사용하고, 없거나 모호하면 critical로 중단한다. external URL은 그대로 링크한다.

**DRY-RUN과의 일관성:** 전체 DRY-RUN은 `state.json` baseline이 있는 페이지를 같은 canary renderer로 다시 렌더링해 비교한다(`UNCHANGED` / `UPDATE_CONFIRMED` / local 변경 시 `CONFLICT`).

## ADOPT BASELINE / Incremental 판정 (`baseline.py`)

**목적:** 기존 문서를 훼손하지 않고 baseline을 만든다. 첫 migration은 ZIP export 렌더러, sync는 API 렌더러를 쓰므로 두 hash는 원래 다르다. 그래서 API-rendered markdown으로 덮어써서 baseline을 만들지 **않는다.**

- `python notion_sync.py --adopt-baseline --dry-run`: preview만 한다. 전체 crawl, health gate, fallback까지 실행한 뒤 후보를 판정하고, `logs/adopt_baseline_preview_<run>.json`과 `logs/adopt_candidates_<run>.csv`를 남긴다. 실행 전후로 markdown과 state.json의 SHA-256을 비교해 보고한다.
- `--adopt-baseline`(실제 쓰기)는 같은 평가를 실시간으로 다시 수행한 뒤, pre-write gate를 확인하고 state.json만 원자적으로 쓴다.
  - gate 조건: crawl health PASS, API 오류 0, CONTENT_FETCH_ERROR 0, local conflict 0, 비결정 렌더링 0, 중복 ID/경로 0, 모든 후보의 결정성 증명.
  - 쓰기 후 검증: 바이트 일치, 재파싱, 기록 수 = 이전 + 신규, 기존 기록 byte-equivalent, 신규 기록 일치.
  - 쓰기 전후로 vault 전체 markdown을 비교하고, 하나라도 바뀌었으면 state를 백업으로 롤백한다.
  - 백업 위치는 `backups/<ts>/state.json`이다.
- 쓰기 함수 `baseline.adopt_baselines()`는 구현과 offline 테스트까지 마쳤다. 순서는 state.json 백업(`backups/<ts>/_sync/state.json`) → 새 state 작성 → 검증 → temp + fsync → 원자 교체 → 재검증이고, 실패하면 원본 state를 복구한다. **markdown은 절대 쓰지 않으며, 이미 있는 state 항목(canary 포함)도 바꾸지 않는다.**

**baseline 항목:** `notion_id`, `notion_last_edited_time`, `api_rendered_content_hash`, `local_content_hash`, `obsidian_path`, `baseline_adopted_at`, `last_successful_sync`(adoption만 했다면 `null`). 두 hash는 서로 달라도 된다.

**adoption 조건:**
- active `managed: notion`이고, notion_id가 파일 1개와 정확히 매핑되며, frontmatter가 정상이다.
- remote가 `archived=false`이고 `in_trash=false`이며, LAVCCA descendant로 확인됐다.
- bootstrap CONFLICT가 없고, 본문 조회와 렌더링이 성공했다.
- 제외 대상: 99_ARCHIVE, managed:obsidian, 06_KNOWLEDGE, REVIEW, EXCLUDED, ARCHIVED/TRASHED, fallback 미확정 상태(`MOVED_OUTSIDE_LAVCCA`, `PERMISSION_UNKNOWN`, `NOT_FOUND_UNKNOWN`, `DESCENDANT_UNKNOWN`).
- QUERY_HIDDEN_ACTIVE는 따로 보고하고, adoption 가능 여부(`adoptable`)를 명시한다.
- 이미 state가 있는 페이지는 `ALREADY_BASELINED`다(canary 항목은 예전 필드명을 그대로 두고 읽을 때 매핑한다).

**baseline 이후 판정 (전체 DRY-RUN과 preview 공통):**

| 조건 | 상태 | 처리 |
|---|---|---|
| local body hash ≠ baseline `local_content_hash` | `LOCAL_MODIFICATION_CONFLICT` | 자동 overwrite 금지 (다른 판정보다 우선) |
| last_edited 동일, API hash 동일 | `UNCHANGED` | 제외 |
| last_edited 변경, API hash 변경 | `UPDATE_CONFIRMED` | 향후 production APPLY 후보 |
| last_edited만 변경 | `METADATA_ONLY_CHANGE` | 본문 overwrite 안 함 |
| API hash만 변경 | `CONTENT_HASH_ANOMALY` | 자동 APPLY 금지, 검토 |

**향후 production incremental APPLY (미구현):** crawl → state 비교 → `UPDATE_CONFIRMED`만 후보로 남김 → conflict/anomaly/error 제외 → 페이지별 백업 → 원자 교체 → 성공한 notion_id의 state만 갱신(새 `api_rendered_content_hash` = `local_content_hash` = 쓴 body hash). CREATE는 `CREATE_CANDIDATE`, 삭제는 direct retrieve 검증 후 review, MOVE/RENAME은 `PATH_POLICY_UNRESOLVED`로 남기고 자동 처리하지 않는다.

## Production incremental APPLY (`production_apply.py`)

```
python notion_sync.py --apply-updates --dry-run   # preview: 전체 crawl + plan + 후보별 실시간 preflight, 쓰기 없음
python notion_sync.py --apply-updates             # 실제 batch APPLY (UPDATE_CONFIRMED만)
```

- **기본 실행(`python notion_sync.py`)은 계속 DRY-RUN이다.** `--apply-updates`가 없으면 production write는 일어나지 않는다. canary(`--apply --notion-id`)도 그대로 유지된다.
- **쓰기 대상은 `UPDATE_CONFIRMED`뿐이다.** `QUERY_HIDDEN_ACTIVE`는 content 상태가 `UPDATE_CONFIRMED`이고 이번 실행의 direct retrieve와 descendant 검증을 통과한 경우에만 후보가 된다. 그 외 모든 상태(`UNCHANGED`, `METADATA_ONLY_CHANGE`, `CONTENT_HASH_ANOMALY`, `LOCAL_MODIFICATION_CONFLICT`, `RENDER_UNSUPPORTED`, `CONTENT_FETCH_ERROR`, `CREATE_CANDIDATE`, `PATH_POLICY_UNRESOLVED`, 삭제 review 계열 등)는 절대 쓰지 않는다.
- **batch 전체 중단:** crawl health 실패나 API 오류(plan 단계 포함)가 하나라도 있으면 batch 전체를 ABORT한다.
- **preflight (후보별, 읽기 전용, 실시간):** 대상 파일이 정확히 1개인지와 위치·frontmatter identity, state의 `obsidian_path`가 실제 경로와 같은지 확인한다. 이어서 direct retrieve(active, not archived/trashed), NATAM descendant, block tree 전체 fetch와 렌더링(속성·첨부 포함)을 수행하고, baseline 기준으로 **다시 `UPDATE_CONFIRMED`인지** 확인한다. local body hash가 baseline과 다르면 `LOCAL_MODIFICATION_CONFLICT`다. 마지막으로 새 파일을 구성·검증한다(canary와 같은 `compose_synced_file`). 실패한 후보는 제외하고 보고하며, 통과한 후보만 `SAFE_APPLY_CANDIDATE`가 된다.
- **batch transaction:**
  1. `backups/<ts>/`에 모든 대상 원본(상대경로 유지)과 `state.json`을 백업한다. 디렉터리를 재사용하지 않고 덮어쓰지도 않는다. 백업이 하나라도 실패하면 쓰기를 시작하지 않는다.
  2. 원본이 preflight 이후 바뀌지 않았는지 재확인한다.
  3. 각 markdown을 원자적으로 쓰고(temp, fsync, `os.replace`) 다시 읽어 확인한다.
  4. 성공한 페이지만 state 항목을 갱신한다(`notion_last_edited_time`, `api_rendered_content_hash` = `local_content_hash` = 쓴 body hash, `obsidian_path`, `last_successful_sync`, canary 호환 필드, `record_type: SYNCED`). 그리고 `state.json`을 한 번 원자적으로 commit한다.
  5. commit 후 불변식을 검증한다: 기록 수 불변, 대상이 아닌 기록 불변, state hash = 실제 body hash, 대상이 아닌 markdown 변경 0.
- **어느 단계든 실패하면:** 이번 batch에서 쓴 markdown 전부와 `state.json`을 백업 바이트로 복원한다. 결과는 `ALL_APPLIED`나 `ROLLED_BACK` 둘 중 하나다(복원 자체가 실패하면 `ROLLBACK_FAILED`와 백업 경로를 보고한다). 부분 성공은 남기지 않는다.
- **METADATA_ONLY_CHANGE:** markdown은 쓰지 않는다. baseline 필드도 바꾸지 않으므로 본문 sync 성공으로 오인되지 않는다. `--apply-updates` 실행 시 `observed_last_edited_time` / `observed_at`만 같은 state commit에 기록한다. 판정은 계속 baseline 기준이라 이후 실제 본문 변경은 정상적으로 `UPDATE_CONFIRMED`가 된다. (`last_seen_edited_time`은 canary 기록에서 baseline 시각으로 쓰이므로 관찰용으로 쓰지 않는다.)
- **CREATE / DELETE / MOVE / RENAME:** 자동화하지 않는다. `CREATE_CANDIDATE`, 삭제 review 계열(`delete_review_required`: ARCHIVED, TRASHED, NOT_FOUND_UNKNOWN, PERMISSION_UNKNOWN, MOVED_OUTSIDE_LAVCCA), `PATH_POLICY_UNRESOLVED`로 남긴다.
- **single-instance lock `_sync/.sync.lock`:** 쓰기 모드(canary apply, adopt-baseline, apply-updates)에서만 API 호출 전에 획득하고 종료 시 해제한다(본인 lock만 해제).
  - 같은 host에서 holder PID가 살아 있으면 거부한다(exit 7).
  - 같은 host에서 holder PID가 죽었으면 stale로 보고 자동 회수하고, 그 사실을 기록한다.
  - 다른 host이거나 lock을 읽을 수 없으면 거부한다. 6시간이 넘었으면 `STALE_UNVERIFIED`로 알리지만 **자동 삭제하지 않는다.** 사람이 확인한 뒤 지워야 한다. 다른 PC의 PID는 여기서 확인할 수 없기 때문이다.
  - 주의: OneDrive가 lock 파일을 다른 PC로 동기화하는 데는 지연이 있다. lock은 같은 PC 안의 동시 실행은 확실히 막지만, 두 PC에서 동시에 실행하는 상황까지 완전히 막지는 못한다. 실행은 한 PC에서만 하도록 운영한다.

## 자동 실행 (Windows Task Scheduler)

- **task:** `NATAM Notion Obsidian Sync`(기존 PC 에 등록된 이름 그대로. 이름 변경은 `docs/brand-migration.md`). 현재 사용자로 대화형 로그인 상태에서만, 제한된 권한으로 실행된다.
- **실행 명령:** `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<_sync>\run_sync.ps1"`. working directory는 `_sync`다.
- **trigger:** 로그인 시 2분 delay 후 시작해 이후 30분마다 무기한 반복한다. **반복은 로그인 trigger에 묶여 있어 등록 후 다음 로그인부터 시작된다.**
- **겹침 방지:** `MultipleInstances = IgnoreNew`로 실행 중이면 새 instance를 시작하지 않는다. 그 위에 `.sync.lock`이 한 번 더 막는다(exit 7).
- **기타 설정:** 실행 제한 2시간, 놓친 실행은 가능할 때 시작, 네트워크가 있을 때만, 배터리 상태에서도 실행.
- **`run_sync.ps1`:**
  - 고정된 Python 절대경로로 `notion_sync.py --apply-updates`를 실행한다.
  - 로그는 `logs/scheduler/sync_YYYYMMDD_HHMMSS.log`에 남는다(started_at, sync_dir, python, command, stdout/stderr, finished_at, exit_code).
  - Python exit code를 그대로 Task Scheduler에 넘긴다(LastTaskResult). wrapper 자체 오류는 exit 9다.
  - 로그 파일을 만들 수 없으면 `%LOCALAPPDATA%\LAVCCA\scheduler-fallback\`(이전 버전 run_sync.ps1: `%LOCALAPPDATA%\NATAM\...`)를 쓰고, 그것도 안 되면 로그 없이 sync만 실행한다(sync 자체 JSON 보고서는 `logs/`에 남는다). token은 wrapper가 읽거나 기록하지 않는다.
- **Python 경로를 바꿀 때:** `run_sync.ps1`의 `$PythonExe` 기본값을 수정한다(PATH에 의존하지 않음).
- **중지·재개:** `Disable-ScheduledTask -TaskName "NATAM Notion Obsidian Sync"` / `Enable-ScheduledTask ...`. 제거는 `Unregister-ScheduledTask ...`.
- **로그 증가:** 30분마다 실행되므로 `logs/scheduler/*.log`와 `logs/apply_updates_*.json`이 하루 약 48개씩 쌓인다. 정리 정책은 아직 없다.

## 안전 범위

- 동기화 대상: **active managed:notion**만
- 보호(읽기 전용): `06_KNOWLEDGE/**`, `99_ARCHIVE/**`, `_migration/**`, `_sync/**` 자기 자신, `.obsidian`, `1. 일간`~`4. 연간`, `무제.canvas`, `managed: obsidian` 파일, 원본 ZIP
- excluded/review 항목은 이번 버전은 물론 향후 APPLY에서도 **자동 생성 대상이 아님**(`config.json`의 `excluded_ids`/`review_ids`)
- former-team/legacy(`99_ARCHIVE/Notion-Raw/*`)는 자동 active sync 대상에서 제외(별도 정책 필요)

## 아키텍처

1. **Crawl** — `2b0de6a2...dd82e`(LAVCCA root)부터 `child_page`/`child_database`/일반 block을 재귀 탐색. Notion-Version `2025-09-03` 기준으로 database는 data source의 컨테이너이므로, `child_database`를 만나면 `GET /databases/{id}` metadata의 `data_sources[]`에서 data source ID를 찾아 각각 `POST /data_sources/{id}/query`로 끝까지 조회한다. 구형 `POST /databases/{id}/query`는 쓰지 않는다. 특정 DB/data source ID를 하드코딩하지 않으며, metadata에 `data_sources`가 없으면 미완료로 처리한다. query 결과 중 page가 아닌 객체는 버리지 않고 리포트의 `non_page_query_results`에 기록한다. 모든 pagination은 `paginate()` 제너레이터를 통해 처리하며 다음을 방어한다:
   - `next_cursor` 반복 감지 → `PaginationError`
   - `has_more=true`인데 `next_cursor` 없음 → `PaginationError`
   - `max_pagination_pages`(기본 500) 초과 → `PaginationError`
   - 한 번의 paginated 결과(DB query 또는 block children) 안에 같은 id가 두 번 나오면 → `PaginationError`
   - API 오류(HTTP 오류, 네트워크/타임아웃, 응답 파싱 실패) 발생 시 그 컬렉션 전체를 미완료로 표시(조용히 넘어가지 않음)
2. **Crawl health check** — 특정 DB의 row 수를 정상값으로 하드코딩하지 **않는다**(예전의 "일정 DB = 20개" gate는 잘못된 전제라 제거함. Notion query 결과에서 정상 페이지가 빠지는 현상이 실제로 확인됐기 때문). 대신 구조적 조건만 검사한다: root 도달, API 오류 0건, pagination/중복 id/예외 관련 미완료 사유 0건.
3. **Hard gate** — health check가 하나라도 실패하면 change plan 단계로 진입하지 않고 `INCOMPLETE_CRAWL` 상태로 종료한다(exit code 2).
4. **Obsidian inventory** — 관리 폴더(`lavcca_root_path`, 현재 `NATAM/`) 아래 보호 경로를 제외한 `managed: notion` markdown 전수를 읽어 notion_id/notion_path/owner/SHA-256/정규화 텍스트를 수집.
5. **Direct-retrieve fallback** — active Obsidian inventory에는 있지만 크롤에서 발견되지 않은 notion_id에 **한해** `GET /pages/{id}`를 read-only로 호출하고, parent chain을 LAVCCA root까지 추적해 분류한다(아래 "누락 페이지 판정 정책"). `QUERY_HIDDEN_ACTIVE`로 확인된 페이지는 하위 트리도 크롤한 뒤 health gate를 한 번 더 적용한다.
6. **Change plan** — notion_id를 유일한 키로 사용해 각 Notion page를 `UNCHANGED / CREATE / ARCHIVED_LOCAL_PRESENT / UPDATE_CONFIRMED / UPDATE_UNVERIFIED / CONTENT_FETCH_ERROR / RENAME / PATH_POLICY_UNRESOLVED / QUERY_HIDDEN_ACTIVE / ARCHIVED / TRASHED / MOVED_OUTSIDE_LAVCCA / DESCENDANT_UNKNOWN / PERMISSION_UNKNOWN / NOT_FOUND_UNKNOWN / EXCLUDED / REVIEW / DUPLICATE_ID / CONFLICT / ERROR` 중 하나로 분류. `MOVE`/`RENAME_AND_MOVE`는 path-policy가 정의되기 전까지 생성되지 않는다.

## CREATE 판정 — vault 전체 identity inventory

active inventory(동기화 대상)와 별도로, vault 전체의 `managed: notion` markdown에서 `notion_id`만 읽는 identity inventory를 만든다(`.obsidian`, `_sync`, `_migration` 제외). 위치는 `active` / `archive`(`<관리 폴더>/99_ARCHIVE/**`) / `other`로 구분한다.

- live Notion에서 발견됐지만 active에 없고 **99_ARCHIVE에 이미 있는** notion_id → `ARCHIVED_LOCAL_PRESENT`. "Notion에서 archived=true"라는 뜻이 아니라 "Obsidian 로컬 아카이브에 이미 있다"는 뜻이다. 99_ARCHIVE 파일은 읽기만 하고 수정하지 않는다.
- active와 archive 양쪽에 같은 notion_id가 있으면 → `DUPLICATE_ID`(자동 정리하지 않음).
- `CREATE`는 vault 어디에도 그 notion_id가 없을 때만 나온다.

## Content 비교 — UPDATE_CONFIRMED vs UPDATE_UNVERIFIED (중요)

현재 Obsidian 파일은 ZIP export의 markdown 렌더러로 만들어졌고, 이 엔진은 API block JSON을 자체 렌더러로 텍스트화한다. **서로 다른 렌더러의 결과가 다르다는 사실만으로는 실제 편집이 있었다고 확정할 수 없다.**

| 조건 | 분류 |
|---|---|
| `state.json`에 이 엔진이 기록한 `last_synced_content_hash`(같은 렌더러) 있음 + 현재 hash와 같음 | `UNCHANGED` |
| 같은 렌더러 baseline 있음 + 현재 hash와 다름 | `UPDATE_CONFIRMED` |
| baseline 없음 + 양쪽을 같은 방식으로 정규화한 텍스트가 완전히 같음 | `UNCHANGED` |
| baseline 없음 + 텍스트 다름 | `UPDATE_UNVERIFIED` |
| 본문(block tree) 조회가 retry 후에도 실패하거나 depth guard에 걸림 | `CONTENT_FETCH_ERROR` |

- 첫 production APPLY 전에는 `state.json`이 비어 있으므로 `UPDATE_CONFIRMED`는 0건이 정상이다.
- 각 plan 항목에는 이번에 계산한 `notion_content_sha256`을 남긴다(향후 baseline 후보. 단 `state.json`에는 기록하지 않음).
- `last_edited_time`은 판정에 쓰지 않는다.
- **`CONTENT_FETCH_ERROR` 페이지는 향후 APPLY에서도 절대 overwrite하지 않는다.** 부분적으로만 가져온 본문으로 판정하거나 쓰지 않는다.
- 네트워크 오류(연결 끊김, 타임아웃)와 HTTP 502/503/504는 최대 3회(1s/2s/4s) 재시도한다. 429는 `Retry-After`를 따른다. 재시도 후에도 실패하면 `api_errors`에 남는다.

## Rename / Move 판정 방식

**Notion title을 파일명과 직접 비교하지 않는다.** 원래 migration의 파일명 규칙을 그대로 적용한 결과끼리 비교한다.

- migration 규칙(원본 스크립트가 보존되지 않아 데이터로 도출·검증함): id가 있는 `managed: notion` 파일 138개 전부 `파일명 = export title`(frontmatter `notion_path`의 제목 부분)이며, 목적지 충돌이 있던 파일만 ` (<32자리 notion_id>)`가 붙는다.
- export title 정규화: Windows 금지 문자 `\ / : * ? " < > |`와 제어 문자는 공백으로 바꾸고, 연속 공백은 하나로 줄이고, 앞뒤 공백과 끝의 `.`를 제거한다. 빈 제목은 `제목 없음`이 된다.
- `normalize(live title) == normalize(export title)`이면 RENAME이 아니다. 다르면 `RENAME`이며, 제안 경로는 **현재 폴더 유지 + 새 정규화 제목(+기존 충돌 접미사 유지)**이다.
- **Notion hierarchy ≠ Obsidian taxonomy.** migration의 notion_id → obsidian_path 매핑을 canonical destination으로 본다. Notion parent title이 바뀌었다는 이유(예: DB 이름 `제목 없음` → `New database`)만으로 Obsidian 폴더를 옮기지 않는다. 이런 항목은 `PATH_POLICY_UNRESOLVED`로 분류하고 MOVE 후보에서 뺀다. title과 parent가 둘 다 바뀐 경우도 `PATH_POLICY_UNRESOLVED`다.
- 실제 `MOVE`/`RENAME_AND_MOVE`는 별도 path-policy가 명시적으로 정의된 뒤에만 도입한다.
- 이번 버전에서는 이름 변경이나 이동을 실제로 실행하지 않는다.


## 누락 페이지 판정 정책 (삭제 판정 포함)

API 트리/query에서 사라졌다는 이유만으로 삭제로 판정하지 않는다. Active 버킷의 Obsidian 파일 중 크롤에서 발견되지 않은 notion_id는 **반드시 `GET /pages/{id}` direct retrieve로 재확인**한다.

| 결과 | 분류 |
|---|---|
| HTTP 200, `in_trash=true` | `TRASHED` |
| HTTP 200, `archived=true` | `ARCHIVED` |
| HTTP 200, active, parent chain이 LAVCCA root에 도달 | `QUERY_HIDDEN_ACTIVE` — query에만 안 보일 뿐 정상 페이지. 삭제/오류로 취급하지 않고 **active source로 유지** |
| HTTP 200, active, parent chain이 root를 거치지 않고 workspace에 도달 | `MOVED_OUTSIDE_LAVCCA` |
| HTTP 200, active, parent chain 추적 실패(중간 조회 실패, 순환, 미지원 parent 타입, hop 한도 초과) | `DESCENDANT_UNKNOWN` — active로 추측하지 않음 |
| HTTP 403 | `PERMISSION_UNKNOWN` |
| HTTP 404 | `NOT_FOUND_UNKNOWN` |
| 그 외 | `ERROR` |

- HTTP 200이라는 사실만으로 LAVCCA 소속이라고 간주하지 않는다. parent chain(page / database / data source / block parent)을 read-only로 따라가서, LAVCCA root 또는 **이번 실행에서 root부터 크롤해 도달한 조상**을 만나야만 descendant로 인정한다.
- 403/404는 자동 삭제 근거가 아니다(권한 변경과 삭제를 구분할 수 없음).
- fallback 단계의 403/404는 "예상 가능한 분류 결과"이므로 crawl health의 API 오류로 세지 않는다. 대신 리포트의 `direct_retrieve_fallback_results`에 HTTP 상태와 함께 전부 기록된다.
- 특정 notion_id를 예외로 하드코딩하지 않는다. 예를 들어 `<일정 DB 행 id>`(일정 DB 행, query에서는 누락되지만 direct GET은 정상)도 위의 일반 규칙으로만 분류된다.

## Discovery 한계 (중요)

- **이미 Obsidian에 notion_id가 있는 페이지**는 query에서 빠지더라도 위 direct-retrieve fallback으로 보호할 수 있다.
- 하지만 **완전히 새로운 Notion 페이지**가 query/search/block-tree 어디에도 나타나지 않으면, polling crawler는 그 notion_id 자체를 알 방법이 없으므로 **발견을 보장할 수 없다.** 그런 페이지는 `CREATE`로 잡히지 않고 조용히 누락된다.
- 이 한계는 코드로 해결되지 않았고, 숨기지도 않는다. 크롤 결과의 `CREATE` 0건이 "새 페이지가 없다"는 증명은 아니다.
- 이 한계를 줄이는 후보는 Notion webhook(페이지 생성 이벤트로 id를 직접 받는 방식)이나 search API 교차 확인이지만, 둘 다 아직 구현하지 않았다.

## Attachment 처리

첨부파일은 `id`를 갖지 않는 block이라 notion_id 기준 매칭이 불가능하다. v1에서는 (부모 페이지 notion_id, block 순번) 조합과 `_migration/manifest.csv`의 원본 경로 문자열 대조로 **후보**만 제시하며, signed URL은 identity로도 로그로도 사용하지 않는다. 다운로드/덮어쓰기는 구현되어 있지 않다.

## state.json

`schema_version`, `item_schema`, 빈 `items: {}`만 존재한다. 실제 APPLY가 한 번도 수행되지 않았으므로 `last_successful_sync`가 채워진 항목은 없다(거짓 기록 금지 원칙).

## Knowledge Layer와의 관계

이 엔진은 `06_KNOWLEDGE/`를 **절대 자동으로 재생성하지 않는다.** 향후 계획은:

```
Notion sync 성공 (APPLY) → changed_notion_ids 목록 산출 → 별도의 "Knowledge rebuild" 작업 트리거
```

이 리포지토리의 이번 버전은 그 첫 단계(sync)만 다루며, 두 번째 단계(rebuild 트리거)는 아직 구현하지 않았다.

## 향후 (전체) APPLY 설계 원칙 (아직 구현 안 됨)

- **All-or-safe-stop**: crawl/inventory가 하나라도 불완전하거나 API 오류가 있으면 write phase 자체에 진입하지 않는다(DRY-RUN에서 이미 이 게이트를 구현·검증함).
- CONFLICT는 "로컬이 마지막 동기화 이후 독립적으로 바뀌었고 + 동시에 원격도 바뀐 경우"를 위해 예약된 상태다. 이번 첫 실행은 `state.json`에 이전 동기화 기록이 전혀 없어(부트스트랩) 이 조건을 판정할 기준선이 없으므로, 이번 DRY-RUN 결과에는 CONFLICT가 등장하지 않는 것이 정상이다.
