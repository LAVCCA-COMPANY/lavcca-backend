# 팀 배포 계층 설계 (T, 2026-10-28 — 설계만, 팀원 기기·Vault 접근 없음)

전제: v3(`~/.natam/github-actions-sync-realtime-validation.md`)와 R1/R2 구현을 그대로 둔다.
팀원은 각자 독립 Vault 를 쓰고, 회사 Notion 문서만 자기 Vault 의 지정 폴더(`<출력 폴더>`, 팀원별 설정)로 단방향 복제받는다.
구현 시점: 중앙 동기화 안정화(v3 R7) 이후.

## 1. 구조
```
GitHub Actions (중앙, 1회 실행)
  1 Notion API + Markdown 변환  ← 기존 엔진 그대로 (전체 / --targets 증분)
  2 소유자 Vault 관리 폴더 반영 ← v2/v3 그대로 (OneDrive Graph)
  3 [신규] export_channels      ← 2가 성공한 경우에만, 같은 러너 미러에서
       채널별 트리 생성 → OneDrive /LAVCCA-Notion-Export/<channel>/ 에 차이만 업로드·삭제
팀원 (Notion 토큰·엔진 없음, 읽기 전용 공유 폴더를 받기만)
  Windows : rclone sync <공유폴더> "<Vault>\<출력 폴더>"   (작업 스케줄러 2분 주기)
  Android : FolderSync "To local folder" → <Vault>/<출력 폴더>
```
- Notion 호출·변환은 1에서 한 번뿐이다. 채널이 늘어나도 API 호출은 늘지 않는다.
- 팀원 쪽 도구가 쓰는 위치는 `<Vault>/<출력 폴더>/` 하나뿐이다. 개인 노트, `.obsidian`, 다른 폴더는 경로상 건드릴 수 없다.
- 출력 폴더 이름은 팀원(구독자)마다 설정한다. 엔진은 페이지 링크를 Vault 루트 기준 wikilink `[[<관리 폴더>/...]]` 로 만든다(소유자 Vault 는 아직 `NATAM/`, 38개 파일). 그래서 내보낼 때 링크 앞부분 `<관리 폴더>/` 를 `<출력 폴더>/` 로 바꿔 쓴다. 바꿔 쓴 결과는 구독자마다 달라지므로 OneDrive 에는 (채널, 출력 폴더) 조합마다 폴더를 하나씩 만든다. 첨부 링크는 상대 경로라 바꿀 필요가 없다.

## 2. 내보내기 규칙 (`ci/export_channels.py`, 엔진 밖)
| 규칙 | 이유 |
|---|---|
| `managed: notion` 이면서 본문 해시가 state.json `local_content_hash` 와 같은 파일만 | 소유자의 로컬 편집이나 사람이 쓴 노트(`managed: obsidian` 20개)가 밖으로 나가지 않게 |
| 첨부는 state.json `attachments[].parent_notion_id` 가 내보낸 페이지인 것만 | 다른 채널의 파일이 섞이지 않게 |
| 채널 소속은 `channels.json` 의 관리 폴더 기준 상대 경로 접두사로 판정, 기본은 거부 | 아래 §3 |
| 채널 밖 페이지로 가는 `[[<관리 폴더>/...]]` 링크는 `🔒(권한 없음)` 으로 바꿈 | 링크 텍스트로 권한 없는 페이지 제목이 노출되는 것을 막음 |
| 채널 폴더 전체를 동기화가 소유: 채널에서 빠진 파일은 원격에서 삭제 | 권한 회수가 팀원 기기에 반영되도록 |
| 채널 루트에 `_LAVCCA_SYNC.md` 기록 (게시 시각, run_id, 파일 수) | 팀원이 Obsidian 안에서 최신 여부 확인 |
| 업로드 후 원격 sha1Hash 를 다시 조회해 기대값과 비교 | 성공으로 기록됐는데 실제 파일이 안 바뀐 경우를 감지 (v3 요구 7과 같은 검사) |

`channels.json` 예시 (저장소에 커밋, 비밀 아님):
```json
{"channels": {
  "all-team": {"include": ["00_HOME/", "01_SHARED/", "05_TEAM/"], "members": ["팀원A", "팀원B"]},
  "lee":      {"include": ["03_MEMBER_B/"], "members": ["박멤버"]}
},
 "subscribers": {"박멤버": {"channels": ["all-team", "lee"], "output_folder": "LAVCCA"}}}
```
`members` 는 기록용이다. 실제 접근 통제는 OneDrive 폴더 공유가 한다(§3).

## 3. 권한
- Notion API 는 페이지별 사용자 권한(누가 볼 수 있는지)을 제공하지 않는다. 그래서 자동으로 판정할 수 없고, `channels.json` 이 명시적인 배포 승인 목록이 된다.
- 실제로 막는 지점은 채널별 OneDrive 폴더 공유다. "특정 사용자, 보기 전용"으로만 공유하고 링크 공유는 쓰지 않는다. 팀원 기기의 도구는 받은 폴더만 볼 수 있다.
- 개인 폴더(`02_MEMBER_A/`, `03_MEMBER_B/`, `04_MEMBER_C/`)는 본인 채널 외에는 넣지 않는다. 새 최상위 폴더는 어느 채널에도 넣지 않는다(기본 거부).
- 저장소 권한: 팀원은 기본 Read/Triage. 변경 절차: `channels.json` 이슈 또는 fork PR → 관리자(Write/Admin) 병합 → 다음 실행에서 반영. 공유 대상 추가·삭제는 관리자가 OneDrive 에서 직접 한다.
- 한계: 이미 팀원 기기에 복사된 파일은 회수할 수 없다. 권한을 회수하면 다음 동기화 때 삭제될 뿐이다.
- 확인 필요: Webhook 페이로드의 `accessible_by` 로 채널 설정과 Notion 권한이 어긋나는지 경고만 하는 보조 검사. 필드가 항상 오는지 미확인.

## 4. 자동 반영·실패·복구
| 구간 | 방식 |
|---|---|
| 중앙 | v3 그대로 (Webhook → dispatch, 실패 시 재시도, 3시간 대조, 매일 전체 크롤). export 는 매 실행 끝에 채널 전체를 다시 계산(멱등) → 한 번 실패해도 다음 실행이 복구 |
| export 실패 | 소유자 Vault 반영은 이미 끝난 상태. export 만 실패로 보고하고 다음 실행에서 재시도 |
| 팀원 수신 | rclone / FolderSync 주기 실행(멱등). 팀원 PC가 꺼져 있으면 켜질 때 따라잡음 |
| 팀원 쪽 감지 | `_LAVCCA_SYNC.md` 의 게시 시각. 중앙은 채널별 게시 성공을 healthchecks 에 ping |

지연: 소유자 반영 + export 업로드(수 초) + 팀원 수신 주기(Windows 2분, Android 는 FolderSync 주기).

## 5. 비용
추가 비용 0. 실행 1회에 수 초가 늘 뿐이다. OneDrive 사용량은 채널마다 최대 약 84 MB이며, 무료 5 GB 안에 들어간다.
팀원은 무료 Microsoft 계정 1개, rclone 또는 FolderSync(무료)를 준비하고 최초 1회 로그인한다.

## 6. 보안 판단 (승인 전 결정)
1. Microsoft refresh token 은 소유자 개인 OneDrive 전체에 쓰기 권한이 있다. 팀이 저장소를 공동 관리하면 이 토큰이 가장 큰 위험이다.
   권장: 회사 전용 Microsoft 계정(예: lavcca-sync@…)이 내보내기 폴더를 소유하게 하고, 소유자 개인 OneDrive 는 지금 v3 그대로 둔다(토큰 2개).
   1단계는 소유자 계정 하나로 시작하고, 팀원에게 공유하기 전에 전환한다.
2. 무료 조직의 private 저장소는 environment secrets·필수 리뷰어·브랜치 보호를 쓸 수 없다. 그래서 쓰기 권한이 있으면 아무 브랜치에서나 워크플로를 실행해 Secrets 를 읽을 수 있다.
   → 팀원에게는 Read(또는 Triage)만 준다. 코드 기여는 이슈 또는 fork PR 로 한다(fork 에서 실행되는 워크플로는 Secrets 를 받지 못함). Write/Admin 은 관리자만 가진다.
   (유료 Team 플랜이면 브랜치 보호와 environment 로 이 제한을 풀 수 있다. 지금은 권장하지 않는다.)

## 7. 구현 단계
| 단계 | 내용 | 외부 변경 |
|---|---|---|
| T1 | `ci/export_channels.py` + 테스트: 해시 일치 필터, 채널 접두사, 링크 가리기, 첨부 소속, 삭제 계산, 상태 파일 | 없음 |
| T2 | 업로드·재조회 검증을 v2 업로더에 연결 (모의 Graph 테스트) | 없음 |
| T3 ⚠ | (선택) 회사 전용 Microsoft 계정, 테스트 채널 폴더 1개 | OneDrive |
| T4 ⚠ | 테스트 채널을 PM 본인의 두 번째 테스트 Vault 로 수신 (rclone·FolderSync 각 1회) | PM 기기만 |
| T5 ⚠ | 팀원별 채널 공유 (PM 이 팀원 계정 확인 후) | OneDrive 공유 |
