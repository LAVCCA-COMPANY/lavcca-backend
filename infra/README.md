# NATAM 온라인 서비스 인프라 (dev, 초안)

> 계획 문서: `NATAM_AWS_PIPELINE_HANDOFF_2026-10-12.md` 4·7절. 여기 있는 것은 **아직 아무것도 만들거나 적용하지 않은** 코드·템플릿이다.
> 리소스 생성, 시크릿 입력, DB 마이그레이션 실행은 JH 가 한다(또는 JH 승인 후 Claude Code).
> 리전은 서울(`ap-northeast-2`), dev 만. 이름의 `__ACCOUNT_ID__`·`__BUCKET__`·`__SECRET_ARN__`·`__IMAGE_TAG__` 는 실제 값으로 바꿔 쓴다.

## 흐름

```
웹(Manus) ─ Supabase 로그인
  ① POST /jobs (api Lambda)            → jobs=uploading, S3 presigned POST(크기 제한) 발급
  ② 브라우저가 S3 uploads/{id}/파일 로 직접 업로드
  ③ POST /jobs/{id}/submit             → jobs=queued, SQS natam-dev-jobs
  ④ dispatcher Lambda                  → 워커 수 < MAX_RUNNING 이면 ECS RunTask, 아니면 지연 재전송
  ⑤ ECS Fargate 워커(worker.py)       → jobs=running → 분석 → S3 reports/{id}/ → jobs=ok|partial|failed
     (워커가 죽으면 task-stopped Lambda 가 jobs=failed)
  ⑥ 웹은 Supabase 에서 jobs 상태를 읽고(RLS), 끝나면 GET /jobs/{id}/files 로 다운로드 링크
```

## 파일

| 경로 | 내용 | 검증 |
|---|---|---|
| `mvp 1_9_3/worker.py` | 작업 1개 실행. 로컬 파일 또는 s3:// 입력, `--output-s3` 업로드, Supabase 상태 기록 | 로컬 실제 영상 1개 실행(PDF 단계는 별도 확인), 단위 테스트 |
| `mvp 1_9_3/Dockerfile` | 워커 이미지 | **빌드 미확인**(이 PC 에 Docker 없음) → `.github/workflows/worker-image.yml` 로 확인 예정 |
| `infra/supabase/migrations/0001_jobs.sql` | jobs·job_steps·feedback·copyright_matches·labels + RLS | PostgreSQL 파서 통과. **Supabase 적용 안 함** |
| `infra/aws/lambdas/api.py` | 업로드·제출·다운로드 API | 단위 테스트(가짜 S3/SQS/Supabase) |
| `infra/aws/lambdas/dispatcher.py` | SQS → ECS RunTask, 동시 실행 제한, 죽은 태스크 처리 | 단위 테스트(가짜 ECS/SQS) |
| `infra/aws/lambdas/supa.py` | Lambda 용 Supabase REST(표준 라이브러리만) | 단위 테스트 |
| `infra/aws/ecs/worker-task-definition.json` | Fargate 4 vCPU / 16 GB / 임시 40 GB | JSON 형식만 |
| `infra/aws/iam/*.json` | 역할 4개(워커 실행·워커 태스크·api·dispatcher) 최소 권한 | IAM Access Analyzer 지적 0건 |

테스트: `cd "mvp 1_9_3" && python -m pytest tests -q` · `cd infra/aws/lambdas && python -m pytest tests -q`

## 배포 순서 (JH)

`aws login --profile natam-admin` 후, 아래 명령은 모두 `--profile natam-admin --region ap-northeast-2` 를 붙인다(생략 표기).

### T3. 기반 (고정비가 생기는 단계)

1. **이미지 저장소(ECR)**
   `aws ecr create-repository --repository-name natam-worker --image-scanning-configuration scanOnPush=true`
   이미지가 수 GB 라 보관비가 붙는다(GB·월 $0.10 수준) → 최근 3~5개만 남기는 수명 주기 정책 권장.
2. **서비스 버킷** (SageMaker 연습 버킷과 분리): 예 `natam-dev-<계정ID>-apne2`
   퍼블릭 차단 + SSE-S3 (연습 버킷과 같은 방식). 수명 주기: `uploads/` N일 후 삭제, `reports/` M일 후 삭제(**보관 기간은 JH 결정**).
   웹이 브라우저에서 직접 올리므로 버킷 CORS 에 웹 도메인의 `POST` 허용 필요.
3. **시크릿**: Secrets Manager `natam/dev/worker` (JSON 1개, 월 $0.40). 키 이름은
   `worker-task-definition.json` 의 `secrets` 와 정확히 같아야 한다(없는 키가 하나라도 있으면 태스크가 시작 실패).
   값이 없는 서비스는 빈 문자열로 넣는다. `GOOGLE_CREDENTIALS_JSON` 은 키 파일 **내용** 전체.
4. **로그 그룹**: `aws logs create-log-group --log-group-name /natam/dev/worker` + 보관 14일(`put-retention-policy`).
5. **IAM 역할 4개** (`infra/aws/iam/`):
   - `NatamWorkerExecutionRole` (신뢰: ecs-tasks) ← `worker-execution-role-policy.json`
   - `NatamWorkerTaskRole` (신뢰: ecs-tasks) ← `worker-task-role-policy.json`
   - `NatamApiLambdaRole` (신뢰: lambda) ← `api-lambda-policy.json`
   - `NatamDispatcherLambdaRole` (신뢰: lambda) ← `dispatcher-lambda-policy.json`
6. **네트워크**: 기본 VPC 의 퍼블릭 서브넷 + 인바운드 없는 보안 그룹(아웃바운드만). NAT Gateway 는 만들지 않는다(고정비).
7. **ECS 클러스터** `natam-dev` (Fargate 전용, 고정비 없음) + 태스크 정의 등록:
   `aws ecs register-task-definition --cli-input-json file://infra/aws/ecs/worker-task-definition.json`
8. **이미지 올리기**: Docker 로 빌드 후 ECR 푸시(Docker Desktop 설치 필요) 또는 GitHub Actions 에 OIDC 역할을 만들어 푸시(나중에 추가).

### T4. Supabase (dev 프로젝트)

1. dev 프로젝트 생성(리전: 서울 권장). **마이그레이션 실행 전 확인**: `0001_jobs.sql` 의 테이블·RLS.
2. SQL Editor 에서 `0001_jobs.sql` 실행.
3. `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` → 시크릿 `natam/dev/worker` 와 Lambda 환경변수. `SUPABASE_ANON_KEY` → api Lambda 만.
   service_role 키는 RLS 를 우회한다 — 웹(프런트) 코드에 절대 넣지 않는다.
4. `copyright_db.sqlite` → Postgres 이전은 별도 작업(먼저 SQLite 백업).

### T5. 큐·Lambda

1. SQS: DLQ `natam-dev-jobs-dlq` → 본 큐 `natam-dev-jobs` (redrive 최대 3회, 가시성 타임아웃 ≥ dispatcher 제한시간 × 6).
2. Lambda 3개 (Python 3.12, 같은 zip: `cd infra/aws/lambdas && zip ../lambdas.zip supa.py api.py dispatcher.py`):
   - `natam-dev-dispatcher` 핸들러 `dispatcher.dispatch` — **예약 동시성 1**, SQS 트리거 배치 크기 1, `ReportBatchItemFailures` 켬
   - `natam-dev-task-stopped` 핸들러 `dispatcher.task_stopped` — EventBridge 규칙: source `aws.ecs`, detail-type `ECS Task State Change`, `lastStatus=STOPPED`, `clusterArn=natam-dev`
   - `natam-dev-api` 핸들러 `api.handler` — Function URL(인증 NONE, 인증은 Supabase 토큰으로 코드에서) + CORS(웹 도메인만)
   환경변수는 각 파일 맨 위 설명 참조. `MAX_RUNNING` 은 2 로 시작.
3. 완료 기준(계획 문서): 영상 10개 연속 처리, 일부러 실패시킨 작업이 DLQ·failed 로 가는지 확인.

### T6. 웹 연결 (Manus)

웹이 할 일: Supabase 로그인 → `POST /jobs` → 받은 `upload.url`/`upload.fields` 로 multipart POST 업로드
→ `POST /jobs/{id}/submit` → Supabase `jobs` 구독/폴링 → `GET /jobs/{id}/files`.

## 비용 감 (계획 문서 수치 기준, 서울 단가 미확인)

- 고정: Secrets Manager $0.40/월, ECR 보관 수 GB, 로그·S3 소량. NAT 없음. Fargate·Lambda·SQS 는 쓸 때만.
- 영상 1개: Gemini 약 $0.11(4분 35초, 2026-10-13 로컬 실측, 저작권 API 별도) + Fargate 4 vCPU/16 GB 약 3분(US-East 단가로 1센트대).
- 무료 플랜 계정이라 크레딧($140)에서 차감된다. 무료 플랜에서 막힌 서비스가 있는지는 생성 시 확인.

## 아직 안 한 것 (코드)

- Gemini 429·일시 장애를 `retry_later` 로 되돌리기(지금은 failed) — 계획 4절.
- W0 영상 해시 캐시(`video_sha256` 컬럼만 준비).
- 리포트 JSON → `copyright_matches` 적재, 피드백 야간 export(T7).
- 사례 인덱스(`casedb/`)를 S3 에서 받아오기 — 인덱스 자체가 아직 없다(빌드는 유료 임베딩 ~10만 건, JH 승인 대기).
- lazy import 로 이미지 줄이기(T9).
