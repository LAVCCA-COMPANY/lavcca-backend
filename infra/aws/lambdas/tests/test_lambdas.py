"""
api.py · dispatcher.py · supa.py 테스트. AWS·Supabase 는 부르지 않는다(가짜 클라이언트).

실행:  cd infra/aws/lambdas && python -m pytest tests -q
"""
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api          # noqa: E402
import dispatcher   # noqa: E402
from supa import Supabase, SupabaseError   # noqa: E402

USER = {"id": "u-1"}


class FakeSupa:
    def __init__(self, user=USER, jobs=None, used=0):
        self.user, self.jobs, self.used = user, jobs or {}, used
        self.inserted, self.updates = [], []

    def get_user(self, token):
        return self.user if token == "good" else None

    def count(self, table, params):
        return self.used

    def insert(self, table, row):
        self.inserted.append((table, row))

    def select(self, table, params):
        j = self.jobs.get(params["id"][3:])
        return [j] if j and params["user_id"] == f"eq.{j['user_id']}" else []

    def update(self, table, filters, fields):
        self.updates.append((table, filters, fields))
        j = self.jobs.get(filters["id"][3:])
        want = filters.get("status", "")
        if j and (want == f"eq.{j['status']}" or (want.startswith("in.(") and j["status"] in want[4:-1].split(","))):
            j.update(fields)
            return [j]
        return []


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def generate_presigned_post(self, bucket, key, Conditions=None, ExpiresIn=None):
        return {"url": f"https://{bucket}.s3", "fields": {"key": key}, "conditions": Conditions}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": self.objects[Key]}

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None):
        return {"Contents": [{"Key": k, "Size": v} for k, v in self.objects.items() if k.startswith(Prefix)]}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed/{Params['Key']}"


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody, DelaySeconds=0):
        self.sent.append((json.loads(MessageBody), DelaySeconds))


class FakeECS:
    def __init__(self, running=0, fail=False):
        self.running, self.fail, self.runs = running, fail, []

    def list_tasks(self, **kw):
        return {"taskArns": ["arn"] * self.running}

    def run_task(self, **kw):
        self.runs.append(kw)
        if self.fail:
            return {"tasks": [], "failures": [{"reason": "RESOURCE:CPU"}]}
        return {"tasks": [{"taskArn": "arn:task/1"}], "failures": []}


@pytest.fixture
def env(monkeypatch):
    for k, v in {"UPLOAD_BUCKET": "up", "OUTPUT_BUCKET": "lake", "QUEUE_URL": "q", "CLUSTER": "c", "TASK_DEFINITION": "lavcca-worker:3",
                 "SUBNETS": "s1,s2", "SECURITY_GROUPS": "g1", "MAX_RUNNING": "2"}.items():
        monkeypatch.setenv(k, v)


def _ev(method, path, body=None, token="good"):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path,
            "headers": {"authorization": f"Bearer {token}"}, "body": json.dumps(body) if body else None}


# ── api ──────────────────────────────────────────────────────────────
def test_safe_file_name():
    assert api.safe_file_name("../../[ZZZ] 영상.MP4") == "[ZZZ] 영상.mp4"
    assert api.safe_file_name("a\x00b/c.mkv") == "abc.mkv"
    assert api.safe_file_name("x.exe") is None
    assert api.safe_file_name(".mp4") is None
    assert api.safe_file_name(None) is None


def test_api_requires_login(env):
    api._clients = {"supa": FakeSupa()}
    assert api.handler(_ev("POST", "/jobs", {}, token="bad"))["statusCode"] == 401


def test_create_job(env):
    sb, s3 = FakeSupa(), FakeS3()
    api._clients = {"supa": sb, "s3": s3}
    r = api.handler(_ev("POST", "/jobs", {"file_name": "영상.mp4", "size_bytes": 1000}))
    assert r["statusCode"] == 201
    body = json.loads(r["body"])
    table, row = sb.inserted[0]
    assert table == "jobs" and row["status"] == "uploading" and row["user_id"] == "u-1"
    assert row["input_key"] == f"uploads/{body['job_id']}/영상.mp4"
    assert body["upload"]["conditions"][0][0] == "content-length-range"
    assert body["upload"]["url"] == "https://up.s3"   # 고객 업로드 버킷


@pytest.mark.parametrize("body,code", [
    ({"file_name": "a.txt", "size_bytes": 10}, 400),
    ({"file_name": "a.mp4", "size_bytes": 0}, 400),
    ({"file_name": "a.mp4", "size_bytes": 10**12}, 400),
    ({"file_name": "a.mp4", "size_bytes": "10"}, 400),
])
def test_create_job_rejects(env, body, code):
    api._clients = {"supa": FakeSupa(), "s3": FakeS3()}
    assert api.handler(_ev("POST", "/jobs", body))["statusCode"] == code


def test_create_job_daily_limit(env):
    sb = FakeSupa(used=5)
    api._clients = {"supa": sb, "s3": FakeS3()}
    assert api.handler(_ev("POST", "/jobs", {"file_name": "a.mp4", "size_bytes": 10}))["statusCode"] == 429
    assert sb.inserted == []


def _job(status="uploading", user="u-1"):
    return {"id": "j1", "user_id": user, "status": status, "input_key": "uploads/j1/a.mp4",
            "include_copyright": False}


def test_submit_job_once(env):
    sb, sqs = FakeSupa(jobs={"j1": _job()}), FakeSQS()
    api._clients = {"supa": sb, "s3": FakeS3({"uploads/j1/a.mp4": 123}), "sqs": sqs}
    assert api.handler(_ev("POST", "/jobs/j1/submit"))["statusCode"] == 202
    assert sb.jobs["j1"]["status"] == "queued" and sb.jobs["j1"]["input_bytes"] == 123
    assert sqs.sent == [({"job_id": "j1", "input_key": "uploads/j1/a.mp4", "include_copyright": False}, 0)]
    assert api.handler(_ev("POST", "/jobs/j1/submit"))["statusCode"] == 409    # 두 번째는 거절
    assert len(sqs.sent) == 1


def test_submit_job_checks(env):
    api._clients = {"supa": FakeSupa(jobs={"j1": _job(user="other")}), "s3": FakeS3(), "sqs": FakeSQS()}
    assert api.handler(_ev("POST", "/jobs/j1/submit"))["statusCode"] == 404    # 남의 작업
    api._clients = {"supa": FakeSupa(jobs={"j1": _job()}), "s3": FakeS3(), "sqs": FakeSQS()}
    assert api.handler(_ev("POST", "/jobs/j1/submit"))["statusCode"] == 400    # 업로드 안 됨


def test_job_files(env):
    objs = {"ai-output/j1/report.md": 2, "ai-output/j1/report.pdf": 3,
            "ai-output/j1/_internal/report.json": 1, "ai-output/j1/_internal/job_result.json": 4,
            "ai-output/j1x/report.pdf": 6}
    api._clients = {"supa": FakeSupa(jobs={"j1": _job(status="ok")}), "s3": FakeS3(objs)}
    r = api.handler(_ev("GET", "/jobs/j1/files"))
    names = sorted(f["name"] for f in json.loads(r["body"])["files"])
    assert names == ["report.md", "report.pdf"]   # _internal/·다른 작업 파일 제외

    api._clients = {"supa": FakeSupa(jobs={"j1": _job(status="running")}), "s3": FakeS3(objs)}
    assert api.handler(_ev("GET", "/jobs/j1/files"))["statusCode"] == 409


def test_unknown_route(env):
    api._clients = {"supa": FakeSupa()}
    assert api.handler(_ev("DELETE", "/jobs/j1"))["statusCode"] == 404
    assert api.handler(_ev("POST", "/jobs/../x/submit"))["statusCode"] == 404


# ── dispatcher ───────────────────────────────────────────────────────
def _sqs_event(*msgs):
    return {"Records": [{"messageId": f"m{i}", "body": json.dumps(m)} for i, m in enumerate(msgs)]}


MSG = {"job_id": "j1", "input_key": "uploads/j1/a b.mp4", "include_copyright": False}


def test_dispatch_runs_task(env):
    ecs = FakeECS(running=1)
    dispatcher._clients = {"ecs": ecs, "sqs": FakeSQS()}
    assert dispatcher.dispatch(_sqs_event(MSG)) == {"batchItemFailures": []}
    run = ecs.runs[0]
    assert run["taskDefinition"] == "lavcca-worker:3"
    assert run["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == ["s1", "s2"]
    assert run["overrides"]["containerOverrides"][0]["command"] == [
        "--job-id", "j1", "--input", "s3://up/uploads/j1/a b.mp4", "--output-dir", "/tmp/out/j1",
        "--output-s3", "s3://lake/ai-output/j1/", "--no-copyright"]


def test_dispatch_prefix_env(env, monkeypatch):
    monkeypatch.setenv("UPLOAD_PREFIX", "")
    monkeypatch.setenv("OUTPUT_PREFIX", "out/")
    ecs = FakeECS()
    dispatcher._clients = {"ecs": ecs, "sqs": FakeSQS()}
    msg = dict(MSG, input_key="j1/a.mp4")
    assert dispatcher.dispatch(_sqs_event(msg)) == {"batchItemFailures": []}
    cmd = ecs.runs[0]["overrides"]["containerOverrides"][0]["command"]
    assert cmd[3] == "s3://up/j1/a.mp4" and cmd[7] == "s3://lake/out/j1/"


def test_dispatch_defers_when_busy(env):
    sqs = FakeSQS()
    dispatcher._clients = {"ecs": FakeECS(running=2), "sqs": sqs}
    assert dispatcher.dispatch(_sqs_event(MSG)) == {"batchItemFailures": []}
    assert sqs.sent == [(dict(MSG, defers=1), 120)]


def test_dispatch_gives_up_after_max_defers(env, monkeypatch):
    monkeypatch.setenv("MAX_DEFERS", "3")
    sb, sqs = FakeSupa(jobs={"j1": {"id": "j1", "status": "queued"}}), FakeSQS()
    dispatcher._clients = {"ecs": FakeECS(running=2), "sqs": sqs, "supa": sb}
    dispatcher.dispatch(_sqs_event(dict(MSG, defers=3)))
    assert sqs.sent == [] and sb.jobs["j1"]["status"] == "retry_later"


def test_dispatch_reports_failures(env):
    dispatcher._clients = {"ecs": FakeECS(fail=True), "sqs": FakeSQS()}
    bad_key = dict(MSG, input_key="uploads/OTHER/a.mp4")
    r = dispatcher.dispatch(_sqs_event(MSG, bad_key))
    assert r == {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]}


def _stopped(exit_code, job_id="j1"):
    return {"detail": {"lastStatus": "STOPPED", "stoppedReason": "OutOfMemoryError",
                       "overrides": {"containerOverrides": [{"name": "worker", "command": ["--job-id", job_id]}]},
                       "containers": [{"name": "worker", "exitCode": exit_code}]}}


def test_task_stopped_marks_failed(env):
    sb = FakeSupa(jobs={"j1": {"id": "j1", "status": "running"}})
    dispatcher._clients = {"supa": sb}
    assert dispatcher.task_stopped(_stopped(137)) == {"job_id": "j1", "marked_failed": True}
    assert sb.jobs["j1"]["status"] == "failed" and "137" in sb.jobs["j1"]["error"]


def test_task_stopped_keeps_worker_status(env):
    sb = FakeSupa(jobs={"j1": {"id": "j1", "status": "partial"}})
    dispatcher._clients = {"supa": sb}
    assert dispatcher.task_stopped(_stopped(1))["marked_failed"] is False      # 워커가 남긴 상태 유지
    assert dispatcher.task_stopped(_stopped(0)) == {"job_id": "j1", "ok": True}
    assert dispatcher.task_stopped(_stopped(1, job_id="../x")) == {"skipped": "no job_id"}


# ── supa ─────────────────────────────────────────────────────────────
class FakeHTTP:
    def __init__(self, status=200, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.reqs = []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if self.status >= 400:
            raise urllib.error.HTTPError(req.full_url, self.status, "err", self.headers, io.BytesIO())
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_supa_get_user_uses_user_token():
    http = FakeHTTP(body=b'{"id": "u-1"}')
    sb = Supabase("https://x.supabase.co/", "service", "anon", opener=http)
    assert sb.get_user("tok") == {"id": "u-1"}
    req = http.reqs[0]
    assert req.full_url == "https://x.supabase.co/auth/v1/user"
    assert req.get_header("Authorization") == "Bearer tok" and req.get_header("Apikey") == "anon"
    assert Supabase("https://x", "s", "a", opener=FakeHTTP(status=401)).get_user("tok") is None
    assert sb.get_user("") is None


def test_supa_count_and_errors():
    sb = Supabase("https://x", "s", opener=FakeHTTP(status=206, headers={"Content-Range": "0-0/7"}))
    assert sb.count("jobs", {"user_id": "eq.u"}) == 7
    sb = Supabase("https://x", "s", opener=FakeHTTP(status=200, headers={"content-range": "*/0"}))
    assert sb.count("jobs", {}) == 0
    with pytest.raises(SupabaseError):
        Supabase("https://x", "s", opener=FakeHTTP(status=500)).insert("jobs", {})


def test_supa_update_filters_in_url():
    http = FakeHTTP(body=b"[]")
    Supabase("https://x", "s", opener=http).update(
        "jobs", {"id": "eq.j1", "status": "in.(queued,running)"}, {"status": "failed"})
    assert http.reqs[0].full_url == "https://x/rest/v1/jobs?id=eq.j1&status=in.(queued,running)"
    assert http.reqs[0].get_method() == "PATCH"


# 키 형식별 인증 헤더 — 가짜 키 문자열만 쓴다
SECRET, PUBLISHABLE, LEGACY = "sb_secret_TEST", "sb_publishable_TEST", "eyJ.legacy.test"


def _calls(sb):
    sb.insert("jobs", {"id": "j1"})
    sb.update("jobs", {"id": "eq.j1"}, {"status": "ok"})
    sb.select("jobs", {"id": "eq.j1"})


def test_supa_secret_key_goes_in_apikey_only():
    http = FakeHTTP(status=200, body=b"[]", headers={"content-range": "*/0"})
    sb = Supabase("https://x", SECRET, PUBLISHABLE, opener=http)
    _calls(sb)
    sb.count("jobs", {})
    assert len(http.reqs) == 4
    for req in http.reqs:
        assert req.get_header("Apikey") == SECRET
        assert req.get_header("Authorization") is None


def test_supa_legacy_jwt_key_also_bearer():
    http = FakeHTTP(status=200, body=b"[]")
    _calls(Supabase("https://x", LEGACY, opener=http))
    for req in http.reqs:
        assert req.get_header("Apikey") == LEGACY
        assert req.get_header("Authorization") == f"Bearer {LEGACY}"


def test_supa_get_user_keeps_user_jwt_with_new_keys():
    http = FakeHTTP(body=b'{"id": "u-1"}')
    sb = Supabase("https://x", SECRET, PUBLISHABLE, opener=http)
    assert sb.get_user("user.jwt") == {"id": "u-1"}
    req = http.reqs[0]
    assert req.get_header("Apikey") == PUBLISHABLE                  # 서버 secret 키가 아니라 publishable
    assert req.get_header("Authorization") == "Bearer user.jwt"


def test_supa_rejects_publishable_as_server_key():
    with pytest.raises(SupabaseError, match="publishable"):
        Supabase("https://x", PUBLISHABLE)


@pytest.mark.parametrize("status", [401, 403])
def test_supa_auth_errors_raise_without_key(status):
    sb = Supabase("https://x", SECRET, opener=FakeHTTP(status=status))
    for call in (lambda: sb.insert("jobs", {}), lambda: sb.update("jobs", {"id": "eq.j"}, {}),
                 lambda: sb.select("jobs", {}), lambda: sb.count("jobs", {})):
        with pytest.raises(SupabaseError) as e:
            call()
        assert str(status) in str(e.value) and SECRET not in str(e.value)
