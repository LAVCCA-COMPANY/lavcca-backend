# lavcca-backend

LAVCCA 백엔드·AWS 인프라: API·인증 연동(Supabase)·S3 업로드·SQS·Lambda·ECS 워커·ECR·CloudFormation·배포 자동화.

| 경로 | 내용 |
|---|---|
| `apps/worker/` | ECS 워커 진입점 `worker.py`, 이미지 `Dockerfile`, 엔진 버전 고정 `ai-engine.ref`, 워커 테스트 |
| `apps/lambdas/` | API(`api.py`)·dispatcher(`dispatcher.py`) Lambda(미배포 초안), Supabase REST(`supa.py`) |
| `infra/aws/` | CloudFormation(`lavcca-worker-dev` 배포됨), IAM 정책, S3 수명 주기 |
| `infra/supabase/` | 마이그레이션·롤백·검증 SQL |
| `.github/` | `worker-image`(빌드·스모크·Trivy·ECR 배포), `tests`(pytest), CODEOWNERS |

분석 엔진은 [ai](https://github.com/LAVCCA-COMPANY/ai), 데이터는 [data](https://github.com/LAVCCA-COMPANY/data),
아키텍처·계약·운영 문서는 [docs](https://github.com/LAVCCA-COMPANY/docs) 에 있다.

## 개발

```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements-dev.txt
python -m pytest apps/worker/tests apps/lambdas/tests .claude/hooks -q
```

워커를 실제로 돌리려면 ai 를 `ai-engine.ref` 커밋으로 받아 `apps/worker/` 에 겹쳐 놓고(CI 의 `build-worker` 와 같음)
ai 의 `requirements.txt` 를 설치한다. 배포 절차: docs `deployment.md`, 인프라 상세: `infra/README.md`.

## 보안

`apps/worker/app.py`(로컬 웹 화면)는 ai 로 옮겨졌고 127.0.0.1 전용이다. 비밀값은 실행 환경변수·Secrets Manager 로만 넣는다.
