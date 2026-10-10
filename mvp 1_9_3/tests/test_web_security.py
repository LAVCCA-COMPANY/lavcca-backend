"""
app.py 웹 보안 회귀 테스트 (P0 보안 점검).

- 웹 입력의 서버 로컬 파일 경로를 분석 대상으로 쓰지 않는다(텍스트로만 취급).
- YouTube / Google Drive URL 분석 경로는 그대로 유지된다.
- 오류 응답에 예외 상세·서버 경로가 노출되지 않는다.
- 직접 실행 시 localhost(127.0.0.1)에만 바인딩한다.

실제 엔진(mvp_ver_1_9_3 → Gemini 등)은 가짜 모듈로 대체하고, 소켓 연결은 차단한다.
실행:  cd "mvp 1_9_3" && python -m pytest tests -q
"""
import json
import os
import runpy
import socket
import sys
import time
import types
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
YT_URL = "https://www.youtube.com/watch?v=abcDEF12345"
GD_URL = "https://drive.google.com/file/d/FILEID123/view"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise RuntimeError("network blocked in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def web(tmp_path, monkeypatch):
    """가짜 mvp_ver_1_9_3 로 app.py 를 import 하고 호출 기록을 돌려준다."""
    calls = {"analyze_only": [], "copyright": [], "classify": [], "gdrive": [], "fail": None}
    gd_video = tmp_path / "gdrive_FILEID123.mp4"
    gd_video.write_bytes(b"\0")

    class System:
        client = None
        system_instruction = ""

        def __init__(self, **kw):
            pass

        def classify_controversy(self, text):
            calls["classify"].append(text)
            return {"label": "test"}

        def find_similar_cases(self, **kw):
            return [], [], ""

    def analyze_only(system, video_input, download_dir=None, fresh=False):
        calls["analyze_only"].append(video_input)
        if calls["fail"]:
            raise calls["fail"]
        return {"youtube_meta": {"video_path": str(gd_video)}}, str(tmp_path / "r.json")

    def finalize_reports(report, json_path, cr, include_copyright=True):
        md = tmp_path / f"final_{time.time_ns()}.md"
        md.write_text("# 리포트", encoding="utf-8")
        return str(md), None

    class Engine:
        def __init__(self, **kw):
            pass

        def create_report(self, report):
            md = tmp_path / f"text_{time.time_ns()}.md"
            md.write_text("# 텍스트 리포트", encoding="utf-8")
            return str(md)

    mvp = types.SimpleNamespace(
        is_youtube_url=lambda t: "youtube.com/watch" in t or "youtu.be/" in t,
        is_google_drive_url=lambda t: "drive.google.com/" in t,
        CrisisConsultantSystem=System, GEN_MODEL="stub",
        assess_natam_risk=lambda **kw: {},
        _save_json=lambda obj, path: None,
        download_google_drive_video=lambda url, d: calls["gdrive"].append(url) or {"video_path": str(gd_video)},
    )
    fake = types.ModuleType("mvp_ver_1_9_3")
    fake.__dict__.update(mvp=mvp, analyze_only=analyze_only, finalize_reports=finalize_reports,
                         run_copyright_detection=lambda p, force=False: calls["copyright"].append(p),
                         CrisisReportEngineV193=Engine, pdf_from_md=None)
    monkeypatch.setitem(sys.modules, "mvp_ver_1_9_3", fake)
    monkeypatch.syspath_prepend(str(APP_DIR))
    cwd = os.getcwd()
    sys.modules.pop("app", None)
    try:
        import app as appmod                 # import 시 os.chdir(APP_DIR)
    finally:
        os.chdir(cwd)
    monkeypatch.chdir(tmp_path)              # 텍스트 분기의 reports/ 생성은 tmp 에서
    client = appmod.app.test_client()

    def run(text):
        job_id = client.post("/analyze", json={"input": text}).get_json()["job_id"]
        for _ in range(200):
            job = client.get(f"/status/{job_id}").get_json()
            if job["status"] in ("done", "error"):
                return job_id, job
            time.sleep(0.01)
        raise AssertionError("job did not finish")

    yield types.SimpleNamespace(app=appmod, client=client, run=run, calls=calls, tmp=tmp_path)
    sys.modules.pop("app", None)


# ── 1. 서버 로컬 경로는 파일로 분석하지 않는다 ─────────────────────────
def test_existing_server_file_path_is_treated_as_text(web):
    canary = web.tmp / "outside" / "canary.txt"
    canary.parent.mkdir()
    canary.write_text("CANARY-SECRET-0001", encoding="utf-8")
    _, job = web.run(str(canary))
    assert job["status"] == "done"
    assert web.calls["analyze_only"] == []           # 파일 분석(→ Gemini 업로드) 경로 미진입
    assert web.calls["copyright"] == []              # 저작권 서브프로세스 미실행
    assert web.calls["classify"] == [str(canary)]    # 텍스트로만 처리
    assert "CANARY-SECRET-0001" not in json.dumps(job, ensure_ascii=False)


def test_existing_and_missing_paths_take_same_branch(web):
    # 존재 여부에 따라 처리 분기가 달라지면 서버 파일 존재 여부를 알아낼 수 있다
    real = web.tmp / "exists.mp4"
    real.write_bytes(b"\0")
    _, a = web.run(str(real))
    _, b = web.run(str(web.tmp / "missing.mp4"))
    assert web.calls["analyze_only"] == []
    assert set(a) == set(b) and a["status"] == b["status"] == "done"


def test_relative_path_is_not_read(web):
    _, job = web.run("copyright_detector/.env")
    assert web.calls["analyze_only"] == [] and web.calls["copyright"] == []
    assert job["status"] == "done"


# ── 2. 기존 URL 분석 기능 유지 ───────────────────────────────────────
def test_youtube_url_still_analyzed(web):
    _, job = web.run(YT_URL)
    assert job["status"] == "done"
    assert web.calls["analyze_only"] == [YT_URL]
    assert len(web.calls["copyright"]) == 1


def test_google_drive_url_still_analyzed(web):
    _, job = web.run(GD_URL)
    assert job["status"] == "done"
    assert web.calls["gdrive"] == [GD_URL]
    assert web.calls["analyze_only"] == [str(web.tmp / "gdrive_FILEID123.mp4")]


def test_empty_input_rejected(web):
    r = web.client.post("/analyze", json={"input": "   "})
    assert r.status_code == 400


# ── 3. 오류 응답에 내부 정보 미노출 ───────────────────────────────────
def test_error_response_hides_exception_detail(web):
    web.calls["fail"] = PermissionError(13, "Permission denied", r"C:\srv\natam\secret_dir")
    _, job = web.run(YT_URL)
    assert job["status"] == "error"
    assert "secret_dir" not in job["error"] and "Errno" not in job["error"]
    assert job["error"]                                  # 일반 안내 문구는 있음


def test_report_read_failure_hides_path(web):
    html = web.app._report_html(str(web.tmp / "no_such_dir" / "report.md"))
    assert "no_such_dir" not in html and "Errno" not in html


def test_unknown_job_and_file_type_return_404(web):
    assert web.client.get("/status/nope").status_code == 404
    job_id, _ = web.run(YT_URL)
    assert web.client.get(f"/download/{job_id}/report").status_code == 404


# ── 4. localhost 바인딩 ─────────────────────────────────────────────
def test_main_binds_to_localhost_only(web, monkeypatch):
    seen = {}
    monkeypatch.setattr("flask.Flask.run", lambda self, **kw: seen.update(kw))
    cwd = os.getcwd()
    try:
        runpy.run_path(str(APP_DIR / "app.py"), run_name="__main__")
    finally:
        os.chdir(cwd)
    assert seen.get("host") == "127.0.0.1"
    assert seen.get("debug") is False
