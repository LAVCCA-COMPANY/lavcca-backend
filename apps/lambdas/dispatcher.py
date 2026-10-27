"""
dispatcher.py — SQS 작업 메시지를 받아 ECS Fargate 태스크(worker.py)를 띄운다. 초안, 배포 전.

  dispatch(event)      SQS 이벤트 소스. 메시지 {"job_id", "input_key", "include_copyright"}
                       · 돌고 있는 워커가 MAX_RUNNING 이상이면 같은 메시지를 DEFER_SECONDS 뒤로 다시 넣는다
                         (실패로 돌려보내면 수신 횟수가 늘어 DLQ 로 가므로 '재전송'으로 미룬다)
                       · RunTask 실패 → batchItemFailures → SQS 재시도(최대 3회 후 DLQ)
                       · MAX_DEFERS 넘게 밀리면 jobs=retry_later 로 표시하고 메시지를 버린다
  task_stopped(event)  EventBridge "ECS Task State Change"(STOPPED). 워커가 상태를 못 남기고
                       죽은 경우(OOM·강제 종료) queued/running 인 jobs 를 failed 로 바꾼다.

배포 조건: 동시 실행 판단이 경쟁하지 않도록 dispatch Lambda 예약 동시성 1, SQS 배치 크기 1,
          이벤트 소스 매핑에 ReportBatchItemFailures 켜기.

환경변수: CLUSTER, TASK_DEFINITION(family 또는 family:rev), CONTAINER_NAME(기본 worker),
          SUBNETS, SECURITY_GROUPS(쉼표 구분), UPLOAD_BUCKET, UPLOAD_PREFIX(기본 uploads/),
          OUTPUT_BUCKET, OUTPUT_PREFIX(기본 ai-output/), QUEUE_URL, MAX_RUNNING(기본 2),
          DEFER_SECONDS(기본 120, 최대 900), MAX_DEFERS(기본 30), ASSIGN_PUBLIC_IP(기본 ENABLED),
          CAPACITY_PROVIDER(비우면 Fargate 온디맨드, FARGATE_SPOT 이면 Spot — 클러스터에 Spot 용량 공급자 연결 필요.
            회수되면 워커가 SIGTERM 을 받아 retry_later 로 끝난다),
          SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""
import json
import os
import re

from supa import Supabase

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_clients = {}


def _env(name, default=None):
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"환경변수 {name} 없음")
    return v


def _client(name):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def supabase():
    if "supa" not in _clients:
        _clients["supa"] = Supabase(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    return _clients["supa"]


def worker_command(job_id: str, input_key: str, include_copyright: bool) -> list:
    """입력은 고객 업로드 버킷, 결과는 데이터레이크 ai-output/{job_id}/."""
    output = f"s3://{_env('OUTPUT_BUCKET')}/{_env('OUTPUT_PREFIX', 'ai-output/')}{job_id}/"
    cmd = ["--job-id", job_id,
           "--input", f"s3://{_env('UPLOAD_BUCKET')}/{input_key}",
           "--output-dir", f"/tmp/out/{job_id}",
           "--output-s3", output]
    if not include_copyright:
        cmd.append("--no-copyright")
    return cmd


def running_count() -> int:
    """이 태스크 정의(family)로 떠 있는(대기 중 포함) 태스크 수."""
    family = _env("TASK_DEFINITION").split(":")[0]
    n, token = 0, None
    while True:
        kw = dict(cluster=_env("CLUSTER"), family=family, desiredStatus="RUNNING")
        if token:
            kw["nextToken"] = token
        r = _client("ecs").list_tasks(**kw)
        n += len(r.get("taskArns", []))
        token = r.get("nextToken")
        if not token:
            return n


def run_worker(msg: dict) -> str:
    job_id, key = msg["job_id"], msg["input_key"]
    if not JOB_ID_RE.match(job_id) or not key.startswith(f"{_env('UPLOAD_PREFIX', 'uploads/')}{job_id}/"):
        raise ValueError(f"잘못된 메시지: {msg}")
    provider = _env("CAPACITY_PROVIDER", "")
    if provider not in ("", "FARGATE", "FARGATE_SPOT"):
        raise RuntimeError(f"CAPACITY_PROVIDER 는 FARGATE 또는 FARGATE_SPOT: {provider}")
    # 둘 중 하나만: 용량 공급자 전략을 주면 launchType 은 빼야 한다(ECS 규칙)
    launch = ({"capacityProviderStrategy": [{"capacityProvider": provider, "weight": 1}]}
              if provider == "FARGATE_SPOT" else {"launchType": "FARGATE"})
    r = _client("ecs").run_task(
        cluster=_env("CLUSTER"),
        taskDefinition=_env("TASK_DEFINITION"),
        **launch,
        count=1,
        startedBy="lavcca-dispatcher",
        tags=[{"key": "job_id", "value": job_id}],
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": _env("SUBNETS").split(","),
            "securityGroups": _env("SECURITY_GROUPS").split(","),
            "assignPublicIp": _env("ASSIGN_PUBLIC_IP", "ENABLED")}},
        overrides={"containerOverrides": [{
            "name": _env("CONTAINER_NAME", "worker"),
            "command": worker_command(job_id, key, msg.get("include_copyright", True))}]},
    )
    if r.get("failures") or not r.get("tasks"):
        raise RuntimeError(f"RunTask 실패: {r.get('failures')}")
    return r["tasks"][0]["taskArn"]


def dispatch(event, context=None):
    failures = []
    for rec in event.get("Records", []):
        try:
            msg = json.loads(rec["body"])
        except ValueError:
            print(f"버림(JSON 아님): {rec.get('messageId')}")
            continue
        try:
            if running_count() >= int(_env("MAX_RUNNING", "2")):
                defers = int(msg.get("defers", 0)) + 1
                if defers > int(_env("MAX_DEFERS", "30")):
                    supabase().update("jobs", {"id": f"eq.{msg.get('job_id')}", "status": "eq.queued"},
                                      {"status": "retry_later", "error": "대기열 지연 한도 초과"})
                    print(f"retry_later: {msg.get('job_id')}")
                    continue
                _client("sqs").send_message(
                    QueueUrl=_env("QUEUE_URL"), MessageBody=json.dumps(dict(msg, defers=defers)),
                    DelaySeconds=min(int(_env("DEFER_SECONDS", "120")), 900))
                print(f"미룸({defers}회): {msg.get('job_id')}")
                continue
            arn = run_worker(msg)
            print(f"시작: {msg['job_id']} → {arn}")
        except Exception as e:
            print(f"실패: {rec.get('messageId')} {type(e).__name__}: {e}")
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}


def _job_id_from_command(cmd: list) -> str | None:
    for i, a in enumerate(cmd[:-1]):
        if a == "--job-id":
            return cmd[i + 1]
    return None


def task_stopped(event, context=None):
    d = event.get("detail") or {}
    if d.get("lastStatus") != "STOPPED":
        return {"skipped": "not stopped"}
    name = _env("CONTAINER_NAME", "worker")
    cmd = next((o.get("command") or [] for o in (d.get("overrides") or {}).get("containerOverrides", [])
                if o.get("name") == name), [])
    job_id = _job_id_from_command(cmd)
    if not job_id or not JOB_ID_RE.match(job_id):
        return {"skipped": "no job_id"}
    exit_code = next((c.get("exitCode") for c in d.get("containers", []) if c.get("name") == name), None)
    if exit_code == 0:
        return {"job_id": job_id, "ok": True}
    # 워커가 이미 failed 를 남겼으면 바뀌는 행이 없다(조건부 갱신)
    reason = f"태스크 중지(exit={exit_code}): {d.get('stoppedReason', '')}"[:500]
    changed = supabase().update("jobs", {"id": f"eq.{job_id}", "status": "in.(queued,running)"},
                                {"status": "failed", "error": reason})
    return {"job_id": job_id, "marked_failed": bool(changed)}
