# Lavcca 온라인 서비스 인프라 (dev)

> 리전은 서울(`ap-northeast-2`), dev 만. 공개 저장소라 계정·VPC 등 ID 는 적지 않는다(`aws sts get-caller-identity` 로 확인, 정책 파일은 `__ACCOUNT_ID__`). AWS CLI 프로필은 `lavcca-admin`(IAM Identity Center SSO, 권한 세트 `Lavcca-Admin`, 로그인 `aws sso login --profile lavcca-admin`).
> 장기 액세스 키(IAM 사용자)는 쓰지 않는다. 배포(CloudFormation·IAM·시크릿)는 `Lavcca-Admin` 으로만 — `AI-Dev`·`Data` 권한 세트에는 배포 권한이 없다(2026-10-22 확인).
> 담당: **PM(최민규)** AWS·Supabase 리소스 생성·시크릿 입력·배포 승인 / **이지후(JH)** AI 엔진·Docker·ECS 워커 코드 / **고균탁** 데이터 수집·라벨링.
> 리소스 생성, 시크릿 입력, DB 마이그레이션 실행은 PM 이 한다(또는 PM 승인 후 Claude Code).
> 버킷은 PM 이 만든 두 개를 쓴다(재생성 금지): 입력 `lavcca-dev-customer-uploads`, 결과 `lavcca-dev-datalake` 의 `ai-output/`.
> 계정은 **유료 플랜(크레딧 없음)**, 월 예산 $30(AWS Budgets 85%·100%·예측 100% 알림).

## 흐름 (최종 목표)

```
웹(Manus) ─ Supabase 로그인
  ① POST /jobs (api Lambda)            → jobs=uploading, S3 presigned POST(크기 제한) 발급
  ② 브라우저가 s3://lavcca-dev-customer-uploads/uploads/{id}/파일 로 직접 업로드
  ③ POST /jobs/{id}/submit             → jobs=queued, SQS lavcca-dev-jobs
  ④ dispatcher Lambda                  → 워커 수 < MAX_RUNNING 이면 ECS RunTask, 아니면 지연 재전송
  ⑤ ECS Fargate 워커(worker.py)       → jobs=running → 분석 → s3://lavcca-dev-datalake/ai-output/{id}/ → jobs=ok|partial|failed
     (워커가 죽으면 task-stopped Lambda 가 jobs=failed)
  ⑥ 웹은 Supabase 에서 jobs 상태를 읽고(RLS), 끝나면 GET /jobs/{id}/files 로 다운로드 링크
```

1차 배포(T3)는 ⑤만: PM 이 `aws ecs run-task` 로 워커를 직접 한 번 실행한다. SQS·Lambda·Supabase 없이 동작한다
(`SUPABASE_URL` 이 없으면 워커는 상태 기록을 건너뛴다).

## 파일

| 경로 | 내용 | 검증 |
|---|---|---|
| `mvp 1_9_3/worker.py` | 작업 1개 실행. 로컬 파일 또는 s3:// 입력, `--output-s3` 업로드, Supabase 상태 기록(선택) | 로컬 실제 영상 1개 실행, 단위 테스트 |
| `mvp 1_9_3/Dockerfile` | 워커 이미지(Python 3.11) | GitHub Actions `worker-image` 빌드·스모크 |
| `infra/aws/cloudformation/lavcca-worker-dev.yaml` | **1차 배포 스택**: ECR, ECS 클러스터·태스크 정의, 로그 그룹, 워커 IAM 역할 2개, GitHub OIDC 푸시 역할, 보안 그룹, 빈 시크릿 | cfn-lint, Access Analyzer |
| `.github/workflows/worker-image.yml` | PR: 빌드·스모크·Trivy 보고 / main: 승인 후 OIDC 로 ECR 푸시(태그 = Git SHA) | actionlint |
| `infra/supabase/migrations/0001_jobs.sql` | jobs·job_steps·feedback·copyright_matches·labels + RLS + anon/authenticated 최소 권한 | **dev 적용됨**(2026-10-21) |
| `infra/supabase/migrations/0002_service_role_grants.sql` | service_role 에 5개 테이블 SELECT/INSERT/UPDATE/DELETE | **dev 적용됨**(2026-10-21) |
| `infra/supabase/tests/`, `rollback/` | 카탈로그 검증(읽기 전용)·RLS 동작 테스트(ROLLBACK)·PostgREST 점검(`api_smoke.py`)·되돌리기 | dev 실행: 검증 8/8, RLS PASS, API 17/17 |
| `infra/aws/lambdas/api.py` | 업로드·제출·다운로드 API (T5) | 단위 테스트(가짜 S3/SQS/Supabase) |
| `infra/aws/lambdas/dispatcher.py` | SQS → ECS RunTask, 동시 실행 제한, 죽은 태스크 처리 (T5) | 단위 테스트(가짜 ECS/SQS) |
| `infra/aws/lambdas/supa.py` | Lambda 용 Supabase REST(표준 라이브러리만) | 단위 테스트 |
| `infra/aws/iam/*.json` | Lambda 역할 2개(api·dispatcher) 정책 + Lambda 신뢰 정책 (T5) | Access Analyzer |
| `infra/aws/cloudformation/lavcca-ml-dev.yaml` | SageMaker 학습 실행 역할 `LavccaSageMakerExecutionRole`(학습 컴퓨트는 만들지 않음) | cfn-lint, Access Analyzer |
| `infra/aws/iam/identity-center/*-inline-policy.json` | 권한 세트 AI-Dev·Data 인라인 정책 **제안본**(PM 이 `sso-admin put-inline-policy-to-permission-set` 로 적용) | Access Analyzer |
| `infra/aws/s3/customer-uploads-lifecycle.json` | 업로드 버킷 수명 주기 전체본(기존 2개 + `transcribe-tmp/` 1일) | — |

테스트: `cd "mvp 1_9_3" && python -m pytest tests -q` · `cd infra/aws/lambdas && python -m pytest tests -q`

## T3. 1차 워커 배포 (PM)

아래 명령은 모두 `--profile lavcca-admin --region ap-northeast-2` 를 붙인다(생략 표기). `aws sso login --profile lavcca-admin` 먼저.

### 0. 사전 준비 (스택 밖, 기존 버킷 설정 변경 — PM 승인 후)
두 버킷은 SSE-S3(AES256, SSE-C 차단)·버킷 정책 없음·퍼블릭 차단 4종·BucketOwnerEnforced 다(2026-10-22 확인). KMS 를 쓰지 않으므로
워커 역할에 `kms:*` 권한은 필요 없다. SSE-KMS 로 바꾸면 태스크 역할에 해당 키의 `kms:Decrypt`(읽기)·`kms:GenerateDataKey`(쓰기)와
키 정책 허용이 함께 필요하다.
- 업로드 버킷 수명 주기에 `transcribe-tmp/` 1일 삭제 규칙 추가(기존 규칙 2개 유지). 워커가 끝나면 지우지만 실패 대비.
- 테스트 영상은 **고객 데이터가 아닌** 것을 `s3://lavcca-dev-customer-uploads/uploads/test-001/<파일>` 로 올린다.

### 1. 스택 생성 (ImageTag=bootstrap)
```
cfn-lint infra/aws/cloudformation/lavcca-worker-dev.yaml
aws cloudformation create-change-set --stack-name lavcca-worker-dev --change-set-name init \
  --change-set-type CREATE --capabilities CAPABILITY_NAMED_IAM \
  --template-body file://infra/aws/cloudformation/lavcca-worker-dev.yaml \
  --parameters ParameterKey=VpcId,ParameterValue=<기본 VPC id> ParameterKey=ImageTag,ParameterValue=bootstrap \
               ParameterKey=GitHubBranch,ParameterValue=chore/lavcca-worker-deploy ParameterKey=CreateGitHubOidcProvider,ParameterValue=true \
  --tags Key=Project,Value=lavcca Key=Env,Value=dev
# GitHubBranch: main 병합 전 1차 배포는 chore/lavcca-worker-deploy, 병합 뒤 main 으로 스택 업데이트.
aws cloudformation describe-change-set --stack-name lavcca-worker-dev --change-set-name init   # 리소스 10개(Add) 확인 — OIDC 공급자를 이미 갖고 있으면 9개
aws cloudformation execute-change-set  --stack-name lavcca-worker-dev --change-set-name init
```
GitHub OIDC 공급자가 이미 계정에 있으면 `CreateGitHubOidcProvider=false`.
과거 실패(2026-10-21): 새 계정에서 ECS 서비스 연결 역할이 클러스터 생성과 동시에 만들어져 `WorkerCluster` 가
"Unable to assume the service linked role" 로 실패했다. 새 계정이면 먼저 `aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com`
후 1~2분 기다린다(이 계정은 `AWSServiceRoleForECS` 가 이미 있음).

### 2. 시크릿 값 입력 (CloudFormation 밖)
스택은 값 없는 시크릿 `lavcca/dev/worker` 만 만든다(삭제 시에도 보존). 값은 **콘솔**(Secrets Manager → lavcca/dev/worker
→ Retrieve secret value → Set secret value → Key/value)에서 아래 3개를 넣는 것을 권장한다. 터미널·채팅·커밋에 붙여 넣지 않는다.

| 키 | 용도 | 비어 있으면 |
|---|---|---|
| `GEMINI_API_KEY` | 전사·교정·위기 판정(필수) | 분석 실패 |
| `GOOGLE_API_KEY` | 저작권: Google Vision 역검색(REST) | 해당 검색 생략 |
| `YOUTUBE_API_KEY` | 저작권: YouTube 유사 영상 대조 | 해당 검색 생략 |

키 이름은 태스크 정의 `Secrets` 와 정확히 같아야 한다(하나라도 없으면 태스크 시작 실패, 값은 빈 문자열 가능).
CLI 로 넣어야 하면 OneDrive 밖 임시 폴더의 파일로 `aws secretsmanager put-secret-value --secret-id lavcca/dev/worker --secret-string file://<파일>` 후 파일을 지운다.
Naver·ACRCloud·Supabase 키는 1차 배포에서 쓰지 않는다(확산 신호·음악 분석 꺼짐, Supabase 기록 생략). 켤 때 태스크 정의 `Secrets` 와 함께 추가한다.

워커 비밀 키 전체(11개, 코드 `os.getenv` 기준). 1차는 앞의 3개만 태스크 정의에 연결한다 — 연결한 키가 시크릿 JSON 에 없으면 태스크가 시작 전에 실패하므로 기능을 켤 때 함께 추가한다.

| 키 | 쓰는 곳 | 켜는 조건 | 단계 |
|---|---|---|---|
| `GEMINI_API_KEY` | 전사·분석 | 항상(필수) | 1차 |
| `GOOGLE_API_KEY` | 저작권 Google Vision | 항상(없으면 생략) | 1차 |
| `YOUTUBE_API_KEY` | 저작권 YouTube 대조 | 항상(없으면 생략) | 1차 |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | jobs 상태 기록(`worker.py`) | 둘 다 있을 때 | T5 |
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 확산 신호(`spread_signals.py`) | `SPREAD_SIGNALS_ENABLED=1` | 후속 |
| `ACRCLOUD_ACCESS_KEY`, `ACRCLOUD_ACCESS_SECRET` | 음악 인식(+비밀 아닌 `ACRCLOUD_HOST` 는 `Environment`) | `MUSIC_ENABLED=true` | 후속 |
| `AUDD_API_TOKEN` | 음악 인식 백업 | `MUSIC_ENABLED=true` | 후속 |
| `SERPAPI_KEY` | 밈 역이미지 검색(Yandex) | `YANDEX_PUBLIC_UPLOAD_ENABLED=true` | 후속 |

`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` 는 넣지 않는다(태스크 역할 사용). `CLOVA_*` 는 `TRANSCRIBE_ENGINE=clova` 전용이라 컨테이너에서 쓰지 않는다.

### 3. GitHub 설정
- Settings → Environments → `lavcca-dev`: Deployment branches = `main` + 1차 배포 동안 `chore/lavcca-worker-deploy`, Required reviewers = PM.
  (2026-10-22 확인: 두 브랜치 정책·Required reviewers 적용됨. OIDC sub 사용자 지정은 2026-10-22 12:33 UTC 적용됨: `use_default=false`, `include_claim_keys=["repo","context","ref"]`, `use_immutable_subject=false` 유지.
  되돌리기: `gh api -X PUT repos/NATAM-KESSOKUBAND/MVP/actions/oidc/customization/sub -F use_default=true`.)
- Environment 변수 `LAVCCA_ECR_PUSH_ROLE_ARN` = 스택 출력 `GitHubEcrPushRoleArn`(비밀값 아님).
- **OIDC sub 에 브랜치 포함**(저장소 관리자, 한 번): `gh api -X PUT repos/NATAM-KESSOKUBAND/MVP/actions/oidc/customization/sub -F use_default=false -f "include_claim_keys[]=repo" -f "include_claim_keys[]=context" -f "include_claim_keys[]=ref"`
  → 토큰 sub = `repo:NATAM-KESSOKUBAND/MVP:environment:lavcca-dev:ref:refs/heads/<GitHubBranch>`. 역할 신뢰 정책은 이 값만 받는다
  (Environment 설정이 빠져도 다른 브랜치·워크플로는 AWS 에서 거부). 이 설정 전에는 푸시가 실패한다(fail closed).
  이 저장소의 다른 OIDC 사용처가 생기면 그쪽 신뢰 정책도 새 sub 형식에 맞춰야 한다.

### 4. 이미지 푸시
main 에 병합되면(병합 전 1차 배포는 `chore/lavcca-worker-deploy` 에서 `workflow_dispatch` 수동 실행) `worker-image` 의 `publish` 잡이 승인을 기다린다 → 승인 → 빌드·스모크·Trivy(fixable CRITICAL 이면 중단) → ECR `lavcca-worker:<Git SHA>` 푸시
→ ECR 스캔 결과를 잡 요약에 남긴다. 태그는 변경 불가(IMMUTABLE), 최근 5개만 보관.

### 4-1. 실행 전 점검 (값은 출력하지 않는다)
```
python infra/aws/tools/check_worker_secret.py
# 기대: GEMINI_API_KEY: set / GOOGLE_API_KEY: set|EMPTY / YOUTUBE_API_KEY: set|EMPTY / OK
# 값은 스크립트 프로세스 안에서만 다룬다(파이프·셸 변수·화면에 안 남김). 키 목록은 템플릿 Secrets 에서 읽는다.
```
- 값이 한 번도 안 들어간 시크릿 → 태스크가 컨테이너 시작 전에 실패(ResourceInitializationError), 비용 거의 없음.
- 키는 있고 `GEMINI_API_KEY` 가 빈 값 → 워커가 다운로드·분석 전에 `failed`(종료 코드 1)로 끝난다(`worker.require_env`).
- `ImageTag=bootstrap` 동안은 이미지가 없어 실행 자체가 안 된다. 5단계는 점검 통과 뒤에만.

### 5. 태스크 정의에 이미지 반영
```
aws cloudformation create-change-set --stack-name lavcca-worker-dev --change-set-name image-<sha7> \
  --use-previous-template --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=ImageTag,ParameterValue=<Git SHA> \
    ParameterKey=VpcId,UsePreviousValue=true ParameterKey=TaskCpu,UsePreviousValue=true \
    ParameterKey=TaskMemory,UsePreviousValue=true ParameterKey=UploadBucket,UsePreviousValue=true \
    ParameterKey=UploadPrefix,UsePreviousValue=true ParameterKey=OutputBucket,UsePreviousValue=true \
    ParameterKey=OutputPrefix,UsePreviousValue=true ParameterKey=GitHubOrg,UsePreviousValue=true \
    ParameterKey=GitHubRepo,UsePreviousValue=true ParameterKey=GitHubEnvironment,UsePreviousValue=true \
    ParameterKey=GitHubBranch,UsePreviousValue=true ParameterKey=CreateGitHubOidcProvider,UsePreviousValue=true
aws cloudformation describe-change-set --stack-name lavcca-worker-dev --change-set-name image-<sha7> \
  --query '[Status,Parameters[?ParameterKey==`ImageTag`||ParameterKey==`GitHubBranch`].[ParameterKey,ParameterValue],Changes[].ResourceChange.[Action,LogicalResourceId,Replacement]]'
# 기대: CREATE_COMPLETE, ImageTag=<Git SHA>, GitHubBranch=스택의 현재 값, 변경은 [Modify, WorkerTaskDefinition, True] 하나뿐 → execute-change-set
# 그 외 변경(특히 GitHubEcrPushRole·WorkerExecutionRole)이 보이면 실행하지 말고 delete-change-set
```
`--parameters` 에 빠진 파라미터는 기존 값이 아니라 **템플릿 기본값**으로 돌아간다(예: `GitHubBranch` 기본값 `main` →
푸시 역할 신뢰 정책이 바뀌어 다음 이미지 푸시가 거부됨). 그래서 `ImageTag` 외 12개 파라미터를 전부 `UsePreviousValue=true` 로 넘긴다.
템플릿에 파라미터를 추가하면 이 목록에도 추가한다.

### 6. 실행 (영상 1개)
```
aws ecs run-task --cluster lavcca-dev --launch-type FARGATE --task-definition lavcca-worker \
  --network-configuration "awsvpcConfiguration={subnets=[<기본 VPC 퍼블릭 서브넷>],securityGroups=[<SecurityGroupId>],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"worker","command":["--job-id","test-001",
     "--input","s3://lavcca-dev-customer-uploads/uploads/test-001/<파일>",
     "--output-dir","/tmp/out","--output-s3","s3://lavcca-dev-datalake/ai-output/test-001/"]}]}'
```
퍼블릭 IP 는 외부 API(Gemini·Google·ECR·S3) 접근용이다. 보안 그룹은 인바운드 없음·아웃바운드 443 만이라 외부에서 태스크로 들어올 수 없다.
기본 VPC 서브넷 4개(2a·2b·2c·2d)는 모두 기본 라우팅 테이블(`0.0.0.0/0 → 인터넷 게이트웨이`)을 쓰는 퍼블릭 서브넷이다.
플랫폼 버전은 지정하지 않으면 `LATEST`(=1.4.0)이고, 임시 저장소 40 GiB 는 1.4.0 이상에서만 된다.
Fargate On-Demand vCPU 할당량은 30(2026-10-22) → 4 vCPU 태스크 최대 7개, T5 의 `MAX_RUNNING=2`(8 vCPU)는 여유 있음.
S3 Gateway 엔드포인트는 두지 않는다: 같은 리전 S3 전송은 원래 무료라 절감이 없고, Gemini 등 외부 API 때문에 퍼블릭 IP 는 어차피 필요하며,
기본 VPC 라우팅 테이블(기존 리소스)을 바꿔야 한다. NAT 를 도입할 때 함께 다시 본다.

### 7. 검증
1. `aws ecs describe-tasks` → `containers[0].exitCode` 0(ok·partial), 75 면 Gemini 일시 장애(retry_later), 1 이면 실패.
2. `aws s3 ls s3://lavcca-dev-datalake/ai-output/test-001/ --recursive` → `report.pdf`, `report.md`, `_internal/report.json`, `_internal/job_result.json`.
3. `job_result.json` 의 `status`, 단계별 소요·메모리(`steps`), `llm_cost`.
4. CloudWatch Logs `/lavcca/dev/worker` 에 키 형태 문자열(`AIza` 등)이 없는지 검색.
5. PDF 한글 렌더링 확인. 메모리 최고치를 보고 4 vCPU / 16 GB 를 줄일 수 있는지 판단.

### 롤백
- 실행 중 태스크: `aws ecs stop-task --cluster lavcca-dev --task <arn>`.
- 잘못된 이미지: 5단계를 이전 SHA 로 다시(태스크 정의 새 리비전). ECR 이미지는 남는다.
- 전체 철거: `aws cloudformation delete-stack --stack-name lavcca-worker-dev`
  — ECR 이미지는 **복구 불가로 삭제**(`EmptyOnDelete`; Git SHA 로 다시 빌드 가능), IAM 역할·OIDC 공급자(스택이 만든 경우)·
  보안 그룹·클러스터·로그 그룹도 삭제, 태스크 정의는 INACTIVE.
  (ECR 은 이미지째 삭제, 로그 그룹 삭제. **시크릿은 보존**됨 → 필요하면 `aws secretsmanager delete-secret --secret-id lavcca/dev/worker --recovery-window-in-days 7`.
  시크릿을 남긴 채 스택을 다시 만들면 이름 충돌 → 먼저 시크릿을 지우거나 복구 기간이 끝나야 한다.)
- 테스트 결과물: `ai-output/test-*` 만 삭제. 고객 경로는 건드리지 않는다.

### 비용 감 (서울 온디맨드 단가, Pricing API 2026-10-22)
- 고정: Secrets Manager $0.40/월, ECR 보관 이미지 5~8 GB × 최대 5개 × $0.10/GB·월, 로그·S3 소량. NAT·ECS Service 없음.
- 태스크 1개 시간당 약 $0.276 = vCPU 4 × $0.04656 + 메모리 16 GB × $0.00511 + 임시 저장소 초과분 20 GB × $0.000127 + 퍼블릭 IPv4 $0.005 (초 단위, 최소 1분).
- 영상 1개(5분 내외): 10~20분 실행 ≈ $0.05~0.09.
- AWS 밖(예산 $30 에 안 잡힘): Gemini 약 $0.11/영상(2026-10-13 실측), Google Vision 작업당 최대 40회.

## T4. Supabase (dev 프로젝트)

**적용 기록 (dev, ref `vbfurkpxfudyksfuzusw`)** — SQL Editor(postgres)로 실행해 Supabase 쪽 마이그레이션 이력(`supabase_migrations`)은 없다. 이 표가 기록이다.

| 날짜 | 실행 | 결과 |
|---|---|---|
| 2026-10-21 | `0001_jobs.sql` | 성공. 검증 7/8 — service_role 에 SELECT/INSERT/UPDATE/DELETE 없음(이 프로젝트의 postgres 소유 테이블 기본 권한은 MAINTAIN·REFERENCES·TRIGGER·TRUNCATE 뿐) |
| 2026-10-21 | `0002_service_role_grants.sql` | 성공. 검증 **8/8** (시퀀스 GRANT 불필요: id 는 IDENTITY) |
| 2026-10-21 | `tests/0001_jobs_rls_test.sql` | `PASS`, 남은 테스트 데이터 0 (auth.users·5개 테이블 0행 확인, MCP 읽기 전용) |
| 2026-10-21 | `tests/api_smoke.py` (PostgREST, 새 sb_ 키를 apikey 헤더로만) | **17/17 PASS** — publishable: 5개 테이블 HTTP 401 / secret: 5개 테이블 HTTP 200·`count=exact` 0행 / 없는 id PATCH: 204·`*/0` / jobs 전후 0→0 (MCP 로 재확인) |

`api_smoke.py` 는 PM 터미널에서 `python infra/supabase/tests/api_smoke.py` 로 실행한다. 키는 숨김 입력(getpass)으로만 받고, 대상이 dev 가 아니면 키를 묻기 전에 중단한다.
쓰기는 무작위 없는 id 에 대한 PATCH 1건뿐이다(0행). 출력은 HTTP 코드·행 수·Content-Range 만.

Supabase MCP 는 `read_only=true` 로 둔다. 다음 마이그레이션도 새 테이블마다 권한을 명시적으로 GRANT 한다.

1. dev 프로젝트 생성(리전: 서울 권장). **마이그레이션 실행 전 확인**: `0001_jobs.sql` 의 테이블·RLS.
2. SQL Editor 에서 `0001_jobs.sql` → `0002_service_role_grants.sql` 순서로 실행(파일 전체가 한 트랜잭션, 0001 재실행은 가드가 막음).
   이 프로젝트는 postgres 가 만든 테이블에 SELECT/INSERT/UPDATE/DELETE 기본 권한을 주지 않는다 → 0002 없으면 서버 쓰기 42501.
   검증: `tests/0001_jobs_verify.sql`(읽기 전용, 전부 ok=true) → `tests/0001_jobs_rls_test.sql`(끝에서 ROLLBACK, `PASS`).
   되돌리기: `rollback/0001_jobs_down.sql`(테이블이 비어 있을 때만).
3. `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` → 시크릿 `lavcca/dev/worker`(+ 태스크 정의 `Secrets`)와 Lambda 환경변수. `SUPABASE_ANON_KEY` → api Lambda 만.
   값은 새 키를 쓴다: `SUPABASE_SERVICE_ROLE_KEY` = secret 키(`sb_secret_…`), `SUPABASE_ANON_KEY` = publishable 키(`sb_publishable_…`).
   레거시 JWT 키는 2026년 말 종료. 새 sb_ 키는 `apikey` 헤더로만 보낸다(`supa.py`·`worker.py` 가 키 형식을 보고 처리).
   service_role 키는 RLS 를 우회한다 — 웹(프런트) 코드에 절대 넣지 않는다.
4. `copyright_db.sqlite` → Postgres 이전은 별도 작업(먼저 SQLite 백업).

## T5. 큐·Lambda

1. SQS: DLQ `lavcca-dev-jobs-dlq` → 본 큐 `lavcca-dev-jobs` (redrive 최대 3회, 가시성 타임아웃 ≥ dispatcher 제한시간 × 6).
2. IAM 역할(`infra/aws/iam/`, 신뢰 정책 `lambda-trust-policy.json`):
   - `LavccaApiLambdaRole` ← `api-lambda-policy.json`
   - `LavccaDispatcherLambdaRole` ← `dispatcher-lambda-policy.json` (워커 역할 2개 PassRole, `lavcca-worker` RunTask)
3. Lambda 3개 (Python 3.12, 같은 zip: `cd infra/aws/lambdas && zip ../lambdas.zip supa.py api.py dispatcher.py`):
   - `lavcca-dev-dispatcher` 핸들러 `dispatcher.dispatch` — **예약 동시성 1**, SQS 트리거 배치 크기 1, `ReportBatchItemFailures` 켬
   - `lavcca-dev-task-stopped` 핸들러 `dispatcher.task_stopped` — EventBridge 규칙: source `aws.ecs`, detail-type `ECS Task State Change`, `lastStatus=STOPPED`, `clusterArn=lavcca-dev`
   - `lavcca-dev-api` 핸들러 `api.handler` — Function URL(인증 NONE, 인증은 Supabase 토큰으로 코드에서) + CORS(웹 도메인만)
   환경변수는 각 파일 맨 위 설명 참조. `MAX_RUNNING` 은 2 로 시작. `TASK_DEFINITION=lavcca-worker`, `CLUSTER=lavcca-dev`.
   api·dispatcher 공통: `UPLOAD_BUCKET=lavcca-dev-customer-uploads`, `OUTPUT_BUCKET=lavcca-dev-datalake`
   (`UPLOAD_PREFIX` 기본 `uploads/`, `OUTPUT_PREFIX` 기본 `ai-output/`).
4. 완료 기준: 영상 10개 연속 처리, 일부러 실패시킨 작업이 DLQ·failed 로 가는지 확인.

## T6. 웹 연결 (Manus)

웹이 할 일: Supabase 로그인 → `POST /jobs` → 받은 `upload.url`/`upload.fields` 로 multipart POST 업로드
→ `POST /jobs/{id}/submit` → Supabase `jobs` 구독/폴링 → `GET /jobs/{id}/files`.
업로드 버킷 CORS 에 웹 도메인의 `POST` 허용 필요.

## Gemini 장애 대응 순서 (워커)

Gemini 가 429·5xx·네트워크 오류를 내면 아래 순서로 넘어간다. 설정은 태스크 정의 `Environment`.

1. 같은 모델로 최대 `GEMINI_RETRIES`(기본 4)번, 3·6·12초 × 지터. 429 가 대기 시간을 주면 그만큼.
2. `GEN_MODEL_FALLBACK`(기본 `models/gemini-3.6-flash`)으로 같은 횟수. 쓰였는지는 `job_result.json` 의 `llm_cost.by_model`.
3. 전사만: `TRANSCRIBE_FALLBACK=aws` 이면 오디오를 `AWS_TRANSCRIBE_S3` 에 올려 AWS Transcribe(ko-KR)로 받아쓰고 지운다.
   어떤 엔진이 받아썼는지는 전사 결과 `video_info.asr_engine`(gemini | aws_transcribe | whisper).
4. 그래도 실패하면 `retry_later`(종료 코드 75). 컨테이너는 로컬 Whisper 를 쓰지 않는다(`WHISPER_FALLBACK=0`).

Transcribe 는 쓸 때만 과금(분 단위). 고객 음성이라 임시 오디오는 고객 업로드 버킷에 둔다
(`s3://lavcca-dev-customer-uploads/transcribe-tmp/`). 워커 태스크 역할에 그 경로의 S3 권한과
`lavcca-asr-*` Transcribe 작업 권한이 들어 있다(CloudFormation `WorkerTaskRole`).

## 워커 제한 시간·중지

Fargate 는 태스크 실행 시간 제한이 없다. 멈춘 작업이 계속 과금되지 않게 워커가 스스로 끝낸다.

- `WORKER_TIMEOUT_SEC`(CloudFormation 태스크 정의 3600, 0=끔): 넘으면 분석을 끊고 `failed`("제한 시간 … 초과")를 남기고 올린다.
  그래도 300초 안에 안 끝나면 강제 종료(종료 코드 124) → task-stopped Lambda 가 `failed` 로 표시.
- SIGTERM(ECS 중지, Fargate Spot 회수): 분석을 끊고 `retry_later`(75). 업로드·상태 기록 중에는 무시한다.
  ECS 는 SIGTERM 뒤 `stopTimeout`(120초, Fargate 최대) 후 SIGKILL.
- Fargate Spot(선택, dev 비용 절감): 클러스터 `lavcca-dev` 에 `FARGATE_SPOT` 용량 공급자가 연결돼 있다(고정비 없음).
  쓰려면 dispatcher 환경변수 `CAPACITY_PROVIDER=FARGATE_SPOT`. 비우면 온디맨드. 회수된 작업은 `retry_later` 로 남는다(재투입은 아래 T5 미구현).

## 이미지 받는 시간 (미해결)

Fargate 는 이미지를 캐시하지 않아 작업마다 약 5GB 를 ECR 에서 받는다(시작 시간 실측 없음).
줄이는 방법은 ECR 에 올린 뒤에야 할 수 있다:
- SOCI 인덱스(지연 로딩, 플랫폼 1.4.0) — 이미지 푸시 뒤 인덱스를 만들어 같은 저장소에 올린다.
  CI 에서 한다면 `worker-image.yml` publish 단계(OIDC 역할 `GitHubEcrPushRole`)에 추가 — 그 역할에 인덱스 푸시 권한도 필요.
- 이미지 자체 줄이기(T9 lazy import, 모델 가중치 분리).
첫 배포 후 `job_result.json` 의 단계 시간과 ECS 태스크의 `pullStartedAt`~`pullStoppedAt` 으로 먼저 잰다.

## 아직 안 한 것 (코드)

- `retry_later` 재투입: 워커는 Gemini 429·5xx 일시 장애(전사 실패, NATAM 평가 실패, Tier2 판정 보류)면
  `jobs.status=retry_later`·종료 코드 75 로 끝낸다(`job_result.json` 에 `retry_after_sec`·`quality`).
  그 작업을 언제·몇 번까지 큐에 다시 넣을지는 아직 없다(T5).
- W0 영상 해시 캐시(`video_sha256` 컬럼만 준비).
- 리포트 JSON → `copyright_matches` 적재, 피드백 야간 export(T7).
- 사례 인덱스(`casedb/`)를 S3 에서 받아오기 — 인덱스 자체가 아직 없다(빌드는 유료 임베딩 ~10만 건, 승인 대기).
- lazy import 로 이미지 줄이기(T9).
- Python 3.12 통합(`chore/py312-reproducible-env`, 해시 고정 lock)은 워커 1차 배포 뒤 별도 PR.

## 권한·데이터 경계 (설계, PM 승인 후 적용)

사람(Identity Center 권한 세트)과 서비스 실행 역할(CloudFormation)을 분리한다. 고객 데이터(`uploads/`, `ai-output/`, `raw/`)의
학습 재사용은 동의·익명화 검토 전까지 SageMaker 역할과 AI-Dev 에서 **명시적으로 거부(Deny)** 한다.

| 주체 | 읽기 | 쓰기 | 그 밖 |
|---|---|---|---|
| AI-Dev (이지후) | processed·labeled·datasets·models·experiments, `ai-output/test-*` | experiments/ | ECR lavcca-worker push/pull, 워커·SageMaker 로그, ECS 태스크 조회, `lavcca-*` 학습 작업(인스턴스 4종·최대 4시간), SageMaker 역할 PassRole, Service Quotas 읽기, Transcribe `lavcca-asr-dev-*` |
| Data (고균탁) | raw·processed·labeled·datasets·ai-output | raw·processed·labeled·datasets | 삭제 권한 없음(데이터레이크 버전 관리 전) |
| ECS 워커 태스크 | uploads/ | ai-output/, transcribe-tmp/(삭제 포함) | Transcribe `lavcca-asr-*`, Rekognition DetectLabels(기본 꺼짐) |
| ECS 실행 역할 | — | — | ECR pull, 로그, 시크릿 `lavcca/dev/worker` 하나 |
| SageMaker 실행 역할 | datasets/controversy/ | models/, experiments/ | 학습 로그·지표, `lavcca-*` ECR pull |

S3 경로: 입력 `uploads/{job_id}/파일`(30일), Transcribe 임시 `transcribe-tmp/{job_id}/`(1일, 워커가 job_id 를 붙인다),
결과 `ai-output/{job_id}/report.*` + `_internal/`, 학습 `raw/ processed/ labeled/ datasets/controversy/{version}/ models/ experiments/`.
`ai-output/` 보관 기간은 미적용(제안: 고객용 report.* 1년, `_internal/` 90일 — 접두사가 같아 객체 태그 기반 규칙과 워커 태깅이 필요).
