"""
worker.py 테스트 — 분석 엔진·AWS·Supabase 는 실행하지 않는다.
S3 클라이언트와 HTTP 세션은 가짜로 바꿔 인자 검사, 업로드 순서, 상태 기록 형식만 본다.

실행:  cd "mvp 1_9_3" && python -m pytest tests -q
"""
import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import worker  # noqa: E402  (가벼운 import 만 한다 — 엔진은 run_job 안에서)


class FakeS3:
    def __init__(self):
        self.uploads = []
        self.downloads = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((bucket, key, ExtraArgs["ContentType"]))

    def download_file(self, bucket, key, path):
        self.downloads.append((bucket, key))
        Path(path).write_bytes(b"video")


class FakeResp:
    status_code = 204
    text = ""

    def __init__(self, headers=None):
        self.headers = headers or {}


class FakeSession:
    def __init__(self, content_range=None):
        self.calls = []
        self.content_range = content_range   # PATCH 응답 Content-Range(None 이면 헤더 없음)

    def request(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append((method, url, json, headers))
        cr = self.content_range if method == "PATCH" else None
        return FakeResp({"Content-Range": cr} if cr else {})


def test_split_s3():
    assert worker.split_s3("s3://b/uploads/j1/a b.mp4") == ("b", "uploads/j1/a b.mp4")
    assert worker.split_s3("s3://b") == ("b", "")
    for bad in ("http://b/k", "s3:///k", "/local/path"):
        with pytest.raises(ValueError):
            worker.split_s3(bad)


def test_parse_args(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    a = worker.parse_args(["--job-id", "j_1-A", "--input", str(video), "--output-dir", str(tmp_path / "o")])
    assert a.input == video.resolve() and a.output_s3 is None

    a = worker.parse_args(["--job-id", "j1", "--input", "s3://b/uploads/j1/v.mp4",
                           "--output-dir", "o", "--output-s3", "s3://b/reports/j1/"])
    assert a.input == "s3://b/uploads/j1/v.mp4"     # s3 입력은 문자열 그대로(로컬 존재 검사 안 함)

    for argv in (["--job-id", "a b", "--input", str(video), "--output-dir", "o"],      # job_id 형식
                 ["--job-id", "../x", "--input", str(video), "--output-dir", "o"],
                 ["--job-id", "j1", "--input", str(tmp_path / "none.mp4"), "--output-dir", "o"],
                 ["--job-id", "j1", "--input", str(video), "--output-dir", "o", "--output-s3", "b/k"]):
        with pytest.raises(SystemExit):
            worker.parse_args(argv)


def test_download_keeps_file_name(tmp_path):
    s3 = FakeS3()
    p = worker.download_input("s3://b/uploads/j1/[ZZZ] 영상.mp4", tmp_path, s3=s3)
    assert p == tmp_path / "[ZZZ] 영상.mp4" and p.read_bytes() == b"video"
    assert s3.downloads == [("b", "uploads/j1/[ZZZ] 영상.mp4")]


def test_upload_layout(tmp_path):
    (tmp_path / "job_result.json").write_text("{}")
    (tmp_path / "report_[ZZZ] 영상.json").write_text("{}")
    (tmp_path / "FINAL_REPORT_ZZZ_영상.md").write_text("#")
    (tmp_path / "FINAL_REPORT_ZZZ_영상.pdf").write_bytes(b"%PDF")
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "x_raw.json").write_text("{}")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "worker_1.log").write_text("log")
    outputs = {"json": str(tmp_path / "report_[ZZZ] 영상.json"),
               "md": str(tmp_path / "FINAL_REPORT_ZZZ_영상.md"),
               "pdf": str(tmp_path / "FINAL_REPORT_ZZZ_영상.pdf"),
               "log": str(tmp_path / "logs" / "worker_1.log")}
    s3 = FakeS3()
    n = worker.upload_outputs(tmp_path, "s3://lake/ai-output/j1", outputs, s3=s3)
    keys = [k for _, k, _ in s3.uploads]
    assert n == 5 and sorted(keys) == sorted([
        "ai-output/j1/report.pdf", "ai-output/j1/report.md", "ai-output/j1/_internal/report.json",
        "ai-output/j1/_internal/transcripts/x_raw.json", "ai-output/j1/_internal/job_result.json"])
    assert keys[-1] == "ai-output/j1/_internal/job_result.json"   # 완료 표시는 맨 마지막
    assert {b for b, _, _ in s3.uploads} == {"lake"}
    ctypes = {k: c for _, k, c in s3.uploads}
    assert ctypes["ai-output/j1/report.pdf"] == "application/pdf"
    assert ctypes["ai-output/j1/report.md"].startswith("text/markdown")


def test_google_credentials_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", '{"type": "service_account"}')
    path = worker.install_google_credentials(tmp_path)
    assert path and Path(path).read_text(encoding="utf-8") == '{"type": "service_account"}'
    assert worker.os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == path
    assert "GOOGLE_CREDENTIALS_JSON" not in worker.os.environ          # 하위 프로세스로 내용이 새지 않게

    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "{}")                # 경로가 이미 있으면 건드리지 않음
    assert worker.install_google_credentials(tmp_path) is None

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "not json")
    with pytest.raises(ValueError):
        worker.install_google_credentials(tmp_path)


def test_job_status_disabled_without_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    st = worker.JobStatus.from_env()
    st.session = FakeSession()
    st.update("j1", status="running")
    assert not st.enabled and st.session.calls == []


def test_job_status_requests():
    sess = FakeSession()
    st = worker.JobStatus("https://x.supabase.co/", "k", session=sess)
    st.update("j1", status="ok", llm_cost_usd=0.2)
    st.add_steps("j1", [{"name": "analyze", "sec": 1.0, "maxrss_mb": None}])
    st.add_steps("j1", [])                        # 빈 목록은 호출 안 함
    (m1, u1, b1, h1), (m2, u2, b2, _) = sess.calls
    assert (m1, u1, b1) == ("PATCH", "https://x.supabase.co/rest/v1/jobs?id=eq.j1",
                            {"status": "ok", "llm_cost_usd": 0.2})
    assert h1["Authorization"] == "Bearer k"
    assert (m2, u2) == ("POST", "https://x.supabase.co/rest/v1/job_steps")
    assert b2 == [{"name": "analyze", "sec": 1.0, "maxrss_mb": None, "job_id": "j1"}]



@pytest.mark.parametrize("key, bearer", [("sb_secret_TEST", None), ("eyJ.legacy.test", "Bearer eyJ.legacy.test")])
def test_job_status_auth_headers(key, bearer):
    sess = FakeSession()
    st = worker.JobStatus("https://x.supabase.co", key, session=sess)
    st.update("j1", status="running")
    st.add_steps("j1", [{"name": "download", "sec": 1.0, "maxrss_mb": 10.0}])
    assert len(sess.calls) == 2
    for *_, h in sess.calls:
        assert h["apikey"] == key and h.get("Authorization") == bearer


def test_job_status_rejects_publishable_key(capsys):
    sess = FakeSession()
    st = worker.JobStatus("https://x.supabase.co", "sb_publishable_TEST", session=sess)
    st.update("j1", status="running")
    assert not st.enabled and sess.calls == []
    assert "sb_publishable_TEST" not in capsys.readouterr().out


def test_job_status_http_error_only_warns(capsys):
    class Resp401:
        status_code = 401
        text = '{"message":"Invalid API key"}'

    class Sess(FakeSession):
        def request(self, *a, **k):
            super().request(*a, **k)
            return Resp401()

    st = worker.JobStatus("https://x.supabase.co", "sb_secret_TEST", session=Sess())
    st.update("j1", status="ok")                 # 상태 기록 실패는 작업을 멈추지 않는다
    out = capsys.readouterr().out
    assert "HTTP 401" in out and "sb_secret_TEST" not in out


# ── 상태 기록 관측성: 0행 갱신, 부분 설정, 실패 집계 ─────────────────────
@pytest.mark.parametrize("cr, n", [("0-0/1", 1), ("*/0", 0), ("0-2/*", 3), ("*/*", 0),
                                   (None, None), ("", None), ("bogus", None), ("a-b/*", None)])
def test_affected_rows(cr, n):
    assert worker.affected_rows(cr) == n


@pytest.mark.parametrize("cr, errors, warn", [
    ("0-0/1", 0, None),                     # 정상 갱신
    ("*/0", 1, "0행 갱신"),                  # jobs 행 없음 → 실패로 센다
    (None, 0, "Content-Range 없음"),         # 헤더 누락 → 확인 불가 경고만
])
def test_job_status_patch_rows(cr, errors, warn, capsys):
    sess = FakeSession(content_range=cr)
    st = worker.JobStatus("https://x.supabase.co", "sb_secret_TEST", session=sess)
    st.update("j1", status="running")
    assert "count=exact" in sess.calls[0][3]["Prefer"]
    out = capsys.readouterr().out
    assert st.error_count() == errors
    assert (warn in out) if warn else out == ""
    assert "sb_secret_TEST" not in out


@pytest.mark.parametrize("env, missing", [({"SUPABASE_URL": "https://x.supabase.co"}, "SUPABASE_SERVICE_ROLE_KEY"),
                                          ({"SUPABASE_SERVICE_ROLE_KEY": "sb_secret_TEST"}, "SUPABASE_URL")])
def test_job_status_partial_config_warns(env, missing, monkeypatch, capsys):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    st = worker.JobStatus.from_env()
    out = capsys.readouterr().out
    assert missing in out and "sb_secret_TEST" not in out
    assert not st.enabled and st.error_count() is None      # 꺼짐(None) ≠ 실패 0회


def test_job_status_exception_hides_message(capsys):
    class Boom(FakeSession):
        def request(self, *a, **k):
            raise ConnectionError("https://x.supabase.co apikey=sb_secret_TEST")

    st = worker.JobStatus("https://x.supabase.co", "sb_secret_TEST", session=Boom())
    st.update("j1", status="ok")
    st.add_steps("j1", [{"name": "x"}])
    out = capsys.readouterr().out
    assert st.error_count() == 2 and "ConnectionError" in out and "sb_secret_TEST" not in out

# ── retry_later (Gemini 일시 장애) ──────────────────────────────────────
class TransientAPIError(RuntimeError):
    """엔진(mvp_ver_1_9_2)의 같은 이름 예외 흉내 — worker 는 retry_after 속성으로 판정한다."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def test_transient_error():
    assert worker.transient_error(TransientAPIError("x", 30.0)) == (True, 30.0)
    assert worker.transient_error(RuntimeError("503 UNAVAILABLE")) == (False, None)   # 문자열만으로는 판정 안 함
    errors = pytest.importorskip("google.genai.errors")
    body = {"error": {"code": 0, "message": "m", "status": "S"}}
    assert worker.transient_error(errors.ClientError(429, body))[0]
    assert worker.transient_error(errors.ServerError(503, body))[0]
    assert not worker.transient_error(errors.ClientError(400, body))[0]   # 잘못된 요청·키 → failed


def test_quality_issues():
    report = {"meta": {"natam_evaluation_status": "ok"},
              "two_tier": {"adjudicated": [{"adjudication": "unverified"}, {"is_controversy": True}]}}
    summary, retry, fail = worker.quality_issues(report)
    assert summary == {"natam_evaluation_status": "ok", "tier2_unverified": 1, "tier2_transient": 0}
    assert retry == [] and fail == ["Tier2 판정 보류 1건"]

    report = {"meta": {"natam_evaluation_status": "ok"},
              "two_tier": {"adjudicated": [{"adjudication": "unverified", "transient": True}]}}
    summary, retry, fail = worker.quality_issues(report)
    assert summary["tier2_transient"] == 1 and retry == ["Tier2 일시 장애 1건"] and fail == []

    # 영구 실패가 섞이면 일시 장애가 있어도 재시도하지 않는다
    report = {"meta": {"natam_evaluation_status": "failed", "natam_transient": True},
              "two_tier": {"adjudicated": [{"adjudication": "unverified"}]}}
    _, retry, fail = worker.quality_issues(report)
    assert retry == []
    assert any("NATAM" in s for s in fail) and any("Tier2" in s for s in fail)

    report = {"meta": {"natam_evaluation_status": "failed"}, "two_tier": {"adjudicated": []}}
    assert worker.quality_issues(report)[1] == []
    assert worker.quality_issues(report)[2] == ["NATAM 평가 실패(전 항목 미판정)"]
    assert worker.quality_issues({"meta": {"natam_evaluation_status": "coarse"}})[1] == []


def _fake_engine(monkeypatch, analyze):
    """run_job 이 import 하는 mvp_ver_1_9_3 을 가짜로 — 분석 대신 analyze(out_dir) 결과를 쓴다."""
    import types

    def analyze_only(system, video, on_video_ready=None, transcript_dir=None):
        return analyze(Path(system.report_dir))

    def finalize_reports(report, json_path, copyright_results, include_copyright, report_dir):
        md, pdf = Path(report_dir) / "FINAL.md", Path(report_dir) / "FINAL.pdf"
        md.write_text("#")
        pdf.write_bytes(b"%PDF")
        return str(md), str(pdf)

    class System:
        def __init__(self, report_dir):
            self.report_dir = report_dir
            self.cost = types.SimpleNamespace(summary=lambda: {"est_usd": 0.01})

    fake = types.SimpleNamespace(mvp=types.SimpleNamespace(CrisisConsultantSystem=System),
                                 run_logging=None, CopyrightRunner=None,
                                 analyze_only=analyze_only, finalize_reports=finalize_reports)
    monkeypatch.setitem(sys.modules, "mvp_ver_1_9_3", fake)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")   # worker.require_env 통과용(가짜 엔진)


def _report(out, natam="ok", adjudicated=(), natam_transient=False):
    p = out / "report_x.json"
    p.write_text("{}")
    return {"meta": {"natam_evaluation_status": natam, "natam_transient": natam_transient},
            "two_tier": {"adjudicated": list(adjudicated)}}, str(p)


def _run(tmp_path, monkeypatch):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GOOGLE_CREDENTIALS_JSON"):
        monkeypatch.delenv(k, raising=False)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    out = tmp_path / "out"
    code = worker.main(["--job-id", "j1", "--input", str(video), "--output-dir", str(out), "--no-copyright"])
    import json
    return code, json.loads((out / "job_result.json").read_text(encoding="utf-8"))


def test_run_ok(tmp_path, monkeypatch):
    _fake_engine(monkeypatch, lambda out: _report(out, adjudicated=[{"is_controversy": False}]))
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"]) == (0, "ok")
    assert res["quality"] == {"natam_evaluation_status": "ok", "tier2_unverified": 0, "tier2_transient": 0}
    assert res["status_write_errors"] is None                     # 기록 꺼짐


def test_run_records_status_write_errors(tmp_path, monkeypatch, capsys):
    _fake_engine(monkeypatch, lambda out: _report(out, adjudicated=[{"is_controversy": False}]))
    sess = FakeSession(content_range="*/0")                   # jobs 행이 없는 수동 실행 흉내
    monkeypatch.setattr(worker.JobStatus, "from_env",
                        classmethod(lambda cls: cls("https://x.supabase.co", "sb_secret_TEST", session=sess)))
    uploads = []
    monkeypatch.setattr(worker, "upload_outputs",
                        lambda out_dir, uri, outputs, s3=None, only=None: uploads.append(only) or 1)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    out = tmp_path / "out"
    code = worker.main(["--job-id", "j1", "--input", str(video), "--output-dir", str(out), "--no-copyright",
                        "--output-s3", "s3://b/ai-output/j1/"])
    raw = (out / "job_result.json").read_text(encoding="utf-8")
    import json
    res = json.loads(raw)
    assert code == 0 and res["status"] == "ok"                  # 기록 실패는 분석 결과를 바꾸지 않는다
    assert res["status_write_errors"] == 2                      # running·최종 PATCH 둘 다 0행
    assert [m for m, *_ in sess.calls] == ["PATCH", "POST", "PATCH"]
    assert uploads == [None, {"job_result.json"}]               # 최종 실패 뒤 job_result.json 만 재업로드
    assert "sb_secret_TEST" not in raw and "sb_secret_TEST" not in capsys.readouterr().out


def test_run_retry_later_on_transient_error(tmp_path, monkeypatch):
    def analyze(out):
        raise TransientAPIError("Gemini 전사 일시 장애: 429 RESOURCE_EXHAUSTED", 42.0)
    _fake_engine(monkeypatch, analyze)
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"], res["retry_after_sec"]) == (75, "retry_later", 42.0)
    assert "429" in res["error"]


def test_run_failed_on_permanent_gap(tmp_path, monkeypatch):
    _fake_engine(monkeypatch, lambda out: _report(out, natam="failed",
                                                  adjudicated=[{"adjudication": "unverified"}]))
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"]) == (1, "failed")
    assert "판정 누락" in res["error"] and "retry_after_sec" not in res
    assert res["outputs"]["pdf"]                 # 리포트는 남긴다(로그·디버깅용)


def test_run_retry_later_on_transient_gap(tmp_path, monkeypatch):
    _fake_engine(monkeypatch, lambda out: _report(
        out, natam="failed", natam_transient=True,
        adjudicated=[{"adjudication": "unverified", "transient": True}]))
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"]) == (75, "retry_later")
    assert "일시 장애" in res["error"]


def test_run_failed_on_other_error(tmp_path, monkeypatch):
    def analyze(out):
        raise RuntimeError("Gemini 전사 실패: 400 INVALID_ARGUMENT")
    _fake_engine(monkeypatch, analyze)
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"]) == (1, "failed") and "retry_after_sec" not in res


def test_scope_transcribe_prefix(monkeypatch):
    monkeypatch.delenv("AWS_TRANSCRIBE_S3", raising=False)
    assert worker.scope_transcribe_prefix("J1") == ""
    monkeypatch.setenv("AWS_TRANSCRIBE_S3", "s3://up/transcribe-tmp/")
    assert worker.scope_transcribe_prefix("J1") == "s3://up/transcribe-tmp/J1/"
    assert os.environ["AWS_TRANSCRIBE_S3"] == "s3://up/transcribe-tmp/J1/"


def test_require_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  ")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        worker.require_env(worker.REQUIRED_ENV)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    worker.require_env(worker.REQUIRED_ENV)
