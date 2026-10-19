"""
api.py — 웹(Manus 프런트)이 부르는 작업 API. Lambda Function URL 또는 API Gateway HTTP API(payload 2.0).
초안이며 배포 전이다. 모든 요청은 Authorization: Bearer <Supabase access token> 필요.

  POST /jobs                {"file_name", "size_bytes", "include_copyright"?}
       → 일일 한도 확인, jobs 행(uploading) 생성, S3 presigned POST(크기 제한 포함) 발급
  POST /jobs/{id}/submit    업로드 끝난 뒤 호출 → 객체 확인, jobs=queued, SQS 에 넣음
  GET  /jobs/{id}/files     ok/partial 작업의 고객용 리포트(report.pdf·report.md) presigned GET 링크
                            (같은 폴더의 _internal/ 은 내부용이라 주지 않는다)

상태 조회는 웹이 Supabase 에서 직접 jobs 를 읽는다(RLS: 자기 것만).

환경변수: UPLOAD_BUCKET(고객 업로드, 예 lavcca-dev-customer-uploads), UPLOAD_PREFIX(기본 uploads/),
          OUTPUT_BUCKET(결과, 예 lavcca-dev-datalake), OUTPUT_PREFIX(기본 ai-output/),
          QUEUE_URL, SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY,
          DAILY_JOB_LIMIT(기본 5), MAX_UPLOAD_MB(기본 500)
"""
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from supa import Supabase

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}   # mvp_ver_1_9_3 CLI 와 같음
CUSTOMER_FILES = ("report.pdf", "report.md")   # worker.py 가 ai-output/{id}/ 바로 아래에 두는 고객용 파일
JOB_PATH_RE = re.compile(r"^/jobs/([A-Za-z0-9_-]{1,64})/(submit|files)$")
UPLOAD_TTL = 15 * 60
DOWNLOAD_TTL = 10 * 60
KST = timezone(timedelta(hours=9))

_clients = {}


def _env(name, default=None):
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"환경변수 {name} 없음")
    return v


def s3():
    if "s3" not in _clients:
        import boto3
        _clients["s3"] = boto3.client("s3")
    return _clients["s3"]


def sqs():
    if "sqs" not in _clients:
        import boto3
        _clients["sqs"] = boto3.client("sqs")
    return _clients["sqs"]


def supabase():
    if "supa" not in _clients:
        _clients["supa"] = Supabase(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"),
                                    _env("SUPABASE_ANON_KEY"))
    return _clients["supa"]


def _resp(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(body, ensure_ascii=False)}


def safe_file_name(name: str) -> str | None:
    """경로·제어문자를 지운 파일명(한글 유지). 확장자가 영상/오디오가 아니면 None."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", str(name or "")).strip().lstrip(".")
    stem, ext = os.path.splitext(name)
    if ext.lower() not in VIDEO_EXTS or not stem:
        return None
    return stem[:150] + ext.lower()


def _today_start_kst_utc() -> str:
    now = datetime.now(KST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def _owned_job(user_id, job_id):
    rows = supabase().select("jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user_id}",
                                      "select": "id,status,input_key,include_copyright"})
    return rows[0] if rows else None


def create_job(user, body):
    name = safe_file_name(body.get("file_name"))
    if not name:
        return _resp(400, {"error": f"지원하지 않는 파일 형식. 허용: {sorted(VIDEO_EXTS)}"})
    max_bytes = int(_env("MAX_UPLOAD_MB", "500")) * 1024 * 1024
    size = body.get("size_bytes")
    if not isinstance(size, int) or not 0 < size <= max_bytes:
        return _resp(400, {"error": f"size_bytes 는 1 ~ {max_bytes} 사이 정수"})

    limit = int(_env("DAILY_JOB_LIMIT", "5"))
    used = supabase().count("jobs", {"user_id": f"eq.{user['id']}",
                                     "created_at": f"gte.{_today_start_kst_utc()}"})
    if used >= limit:
        return _resp(429, {"error": f"오늘 분석 한도({limit}건)를 다 썼습니다."})

    job_id = uuid.uuid4().hex
    key = f"{_env('UPLOAD_PREFIX', 'uploads/')}{job_id}/{name}"
    supabase().insert("jobs", {"id": job_id, "user_id": user["id"], "status": "uploading",
                               "input_key": key, "input_name": name,
                               "include_copyright": bool(body.get("include_copyright", True))})
    post = s3().generate_presigned_post(
        _env("UPLOAD_BUCKET"), key, Conditions=[["content-length-range", 1, max_bytes]],
        ExpiresIn=UPLOAD_TTL)
    return _resp(201, {"job_id": job_id, "upload": post, "expires_in": UPLOAD_TTL})


def submit_job(user, job_id):
    job = _owned_job(user["id"], job_id)
    if not job:
        return _resp(404, {"error": "작업 없음"})
    if job["status"] != "uploading":
        return _resp(409, {"error": f"이미 제출됨(status={job['status']})"})
    try:
        head = s3().head_object(Bucket=_env("UPLOAD_BUCKET"), Key=job["input_key"])
    except Exception:
        return _resp(400, {"error": "업로드된 파일이 없습니다."})

    # status 조건부 갱신 — 동시에 두 번 눌러도 큐에는 한 번만 들어간다
    changed = supabase().update("jobs", {"id": f"eq.{job_id}", "status": "eq.uploading"},
                                {"status": "queued", "input_bytes": head.get("ContentLength")})
    if not changed:
        return _resp(409, {"error": "이미 제출됨"})
    sqs().send_message(QueueUrl=_env("QUEUE_URL"), MessageBody=json.dumps({
        "job_id": job_id, "input_key": job["input_key"],
        "include_copyright": job["include_copyright"]}))
    return _resp(202, {"job_id": job_id, "status": "queued"})


def job_files(user, job_id):
    job = _owned_job(user["id"], job_id)
    if not job:
        return _resp(404, {"error": "작업 없음"})
    if job["status"] not in ("ok", "partial"):
        return _resp(409, {"error": f"아직 결과 없음(status={job['status']})"})
    bucket, prefix = _env("OUTPUT_BUCKET"), f"{_env('OUTPUT_PREFIX', 'ai-output/')}{job_id}/"
    listed = s3().list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    files = []
    for obj in listed.get("Contents", []):
        name = obj["Key"][len(prefix):]
        if name in CUSTOMER_FILES:
            files.append({"name": name, "bytes": obj.get("Size"), "url": s3().generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": obj["Key"]}, ExpiresIn=DOWNLOAD_TTL)})
    return _resp(200, {"job_id": job_id, "status": job["status"], "files": files,
                       "expires_in": DOWNLOAD_TTL})


def handler(event, context=None):
    http = (event.get("requestContext") or {}).get("http") or {}
    method, path = http.get("method", ""), event.get("rawPath", "")
    auth = (event.get("headers") or {}).get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    user = supabase().get_user(token)
    if not user:
        return _resp(401, {"error": "로그인이 필요합니다."})

    if method == "POST" and path == "/jobs":
        try:
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                import base64
                raw = base64.b64decode(raw).decode("utf-8")
            body = json.loads(raw)
        except ValueError:
            return _resp(400, {"error": "JSON 본문 필요"})
        return create_job(user, body if isinstance(body, dict) else {})
    m = JOB_PATH_RE.match(path)
    if m and method == "POST" and m.group(2) == "submit":
        return submit_job(user, m.group(1))
    if m and method == "GET" and m.group(2) == "files":
        return job_files(user, m.group(1))
    return _resp(404, {"error": "없는 경로"})
