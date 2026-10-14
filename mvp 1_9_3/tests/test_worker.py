"""
worker.py 테스트 — 분석 엔진·AWS·Supabase 는 실행하지 않는다.
S3 클라이언트와 HTTP 세션은 가짜로 바꿔 인자 검사, 업로드 순서, 상태 기록 형식만 본다.

실행:  cd "mvp 1_9_3" && python -m pytest tests -q
"""
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


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append((method, url, json, headers))
        return FakeResp()


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


def test_upload_puts_job_result_last(tmp_path):
    (tmp_path / "job_result.json").write_text("{}")
    (tmp_path / "report_x.json").write_text("{}")
    (tmp_path / "FINAL.md").write_text("#")
    (tmp_path / "FINAL.pdf").write_bytes(b"%PDF")
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "x_raw.json").write_text("{}")
    s3 = FakeS3()
    n = worker.upload_outputs(tmp_path, "s3://b/reports/j1", s3=s3)
    assert n == 5
    keys = [k for _, k, _ in s3.uploads]
    assert keys[-1] == "reports/j1/job_result.json"
    assert "reports/j1/transcripts/x_raw.json" in keys
    ctypes = {k: c for _, k, c in s3.uploads}
    assert ctypes["reports/j1/FINAL.pdf"] == "application/pdf"
    assert ctypes["reports/j1/FINAL.md"].startswith("text/markdown")


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
    assert worker.quality_issues(report) == (
        {"natam_evaluation_status": "ok", "tier2_unverified": 1}, ["Tier2 판정 보류 1건"])
    report = {"meta": {"natam_evaluation_status": "failed"}, "two_tier": {"adjudicated": []}}
    assert worker.quality_issues(report)[1] == ["NATAM 평가 실패(전 항목 미판정)"]
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


def _report(out, natam="ok", adjudicated=()):
    p = out / "report_x.json"
    p.write_text("{}")
    return {"meta": {"natam_evaluation_status": natam},
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
    assert res["quality"] == {"natam_evaluation_status": "ok", "tier2_unverified": 0}


def test_run_retry_later_on_transient_error(tmp_path, monkeypatch):
    def analyze(out):
        raise TransientAPIError("Gemini 전사 일시 장애: 429 RESOURCE_EXHAUSTED", 42.0)
    _fake_engine(monkeypatch, analyze)
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"], res["retry_after_sec"]) == (75, "retry_later", 42.0)
    assert "429" in res["error"]


def test_run_retry_later_on_missing_verdicts(tmp_path, monkeypatch):
    _fake_engine(monkeypatch, lambda out: _report(out, natam="failed",
                                                  adjudicated=[{"adjudication": "unverified"}]))
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"]) == (75, "retry_later")
    assert res["error"] == "Gemini 장애로 판정 누락: NATAM 평가 실패(전 항목 미판정), Tier2 판정 보류 1건"
    assert res["outputs"]["pdf"]                 # 리포트는 남긴다(로그·디버깅용), 웹 다운로드는 ok/partial 만


def test_run_failed_on_other_error(tmp_path, monkeypatch):
    def analyze(out):
        raise RuntimeError("Gemini 전사 실패: 400 INVALID_ARGUMENT")
    _fake_engine(monkeypatch, analyze)
    code, res = _run(tmp_path, monkeypatch)
    assert (code, res["status"]) == (1, "failed") and "retry_after_sec" not in res
