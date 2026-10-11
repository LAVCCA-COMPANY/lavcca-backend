"""
P1 안정성 회귀 테스트.

- 리포트 MD → HTML 변환 결과에서 스크립트·이벤트 속성·javascript: 링크가 제거되고
  표/제목/코드/강조/링크 서식은 유지된다 (XSS).
- 동시 분석 수가 한도를 넘으면 429 를 돌려주고, 끝난(성공·실패) 작업은 슬롯을 반납한다.
- analyze_only 가 공유 엔진/모듈의 함수를 바꿔치기하지 않는다 (요청 간 간섭 없음).

외부 API·네트워크는 쓰지 않는다(test_web_security 의 가짜 엔진/소켓 차단 재사용).
"""
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from test_web_security import web, no_network, YT_URL  # noqa: F401 (fixture 재사용)

APP_DIR = Path(__file__).resolve().parents[1]


# ── 1. 리포트 HTML XSS ──────────────────────────────────────────────
MALICIOUS_MD = """# 리포트 <script>alert(1)</script>

영상 제목: <img src=x onerror=alert(2)> **굵게** *기울임*

<svg onload=alert(3)><circle/></svg>

[링크](javascript:alert(4)) [정상](https://example.com/a?b=1&c=2)

<a href="JaVaScRiPt:alert(5)">x</a> <iframe src="https://evil"></iframe>

| 항목 | 값 |
|:-----|---:|
| <b onclick="x()">A</b> | 1 |

```python
print("<script>코드</script>")
```

<div style="background:url(javascript:alert(6))">div</div>
"""


def _report(web, text):
    md = web.tmp / "report.md"
    md.write_text(text, encoding="utf-8")
    return web.app._report_html(str(md))


def test_report_html_strips_active_content(web):
    out = _report(web, MALICIOUS_MD).lower()
    for bad in ("<script", "<img", "<svg", "<iframe", "<div", "onerror", "onload",
                "onclick", "javascript:", "style=\"background"):
        assert bad not in out, bad


def test_report_html_keeps_formatting(web):
    out = _report(web, MALICIOUS_MD)
    assert "<h1>" in out and "<strong>굵게</strong>" in out and "<em>기울임</em>" in out
    assert "<table>" in out and '<td style="text-align: right;">1</td>' in out
    assert '<a href="https://example.com/a?b=1&amp;c=2" rel="noopener noreferrer">정상</a>' in out
    # 코드 블록 안의 태그는 글자로 보인다(이중 이스케이프 없음)
    assert "&lt;script&gt;코드&lt;/script&gt;" in out and "&amp;lt;" not in out
    assert '<code class="language-python">' in out


def test_report_html_plain_report_unchanged(web):
    md = "## 요약\n\n- 위험도: **ALERT**\n- 점수 3 > 2 & 1 < 2\n\n> 인용\n"
    import markdown
    raw = markdown.markdown(md, extensions=['tables', 'fenced_code', 'sane_lists'])
    assert _report(web, md) == raw


# ── 2. 동시 분석 수 제한 ─────────────────────────────────────────────
@pytest.fixture
def blocking(web, monkeypatch):
    gate = threading.Event()
    real = sys.modules["mvp_ver_1_9_3"].analyze_only

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(sys.modules["mvp_ver_1_9_3"], "analyze_only", slow)
    yield gate
    gate.set()


def _post(web):
    return web.client.post("/analyze", json={"input": YT_URL})


def _wait_done(web, job_id):
    import time
    for _ in range(300):
        if web.client.get(f"/status/{job_id}").get_json()["status"] in ("done", "error"):
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_concurrent_limit_returns_429(web, blocking):
    limit = web.app.MAX_CONCURRENT_JOBS
    ids = [_post(web).get_json()["job_id"] for _ in range(limit)]
    r = _post(web)
    assert r.status_code == 429 and r.get_json()["error"]
    blocking.set()
    for jid in ids:
        _wait_done(web, jid)
    assert _post(web).status_code == 200            # 끝나면 슬롯 반납


def test_failed_job_releases_slot(web):
    web.calls["fail"] = RuntimeError("boom")
    for _ in range(web.app.MAX_CONCURRENT_JOBS + 2):
        r = _post(web)
        assert r.status_code == 200
        _wait_done(web, r.get_json()["job_id"])


def test_thread_start_failure_releases_slot(web, monkeypatch):
    def boom(self):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(web.app.threading.Thread, "start", boom)
    web.app.app.config["PROPAGATE_EXCEPTIONS"] = False
    for _ in range(web.app.MAX_CONCURRENT_JOBS + 1):
        assert _post(web).status_code == 500
    monkeypatch.undo()
    r = _post(web)                                   # 슬롯이 새지 않았으면 다시 받을 수 있다
    assert r.status_code == 200
    _wait_done(web, r.get_json()["job_id"])


def test_rejected_request_creates_no_job(web, blocking):
    for _ in range(web.app.MAX_CONCURRENT_JOBS):
        _post(web)
    before = len(web.app.jobs)
    assert _post(web).status_code == 429
    assert len(web.app.jobs) == before


# ── 3. analyze_only 의 공유 함수 바꿔치기 제거 ─────────────────────────
@pytest.fixture
def v193(monkeypatch):
    fake = types.ModuleType("mvp_ver_1_9_2")
    fake.CrisisReportEngine = type("E", (), {"__init__": lambda self, **kw: None})
    fake.NATAM_LEVELS = ["SAFE", "CARE", "ALERT", "DANGER", "CRITICAL"]
    fake._gen_pdf = lambda report, output_dir="reports": "real.pdf"
    monkeypatch.setitem(sys.modules, "mvp_ver_1_9_2", fake)
    for name in ("spread_signals", "pdf_from_md", "run_logging"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.syspath_prepend(str(APP_DIR))
    cwd = os.getcwd()
    sys.modules.pop("mvp_ver_1_9_3", None)
    try:
        import mvp_ver_1_9_3 as mod
    finally:
        os.chdir(cwd)
    yield mod
    sys.modules.pop("mvp_ver_1_9_3", None)


def test_analyze_only_does_not_patch_shared_functions(v193):
    def create_report(report):
        return "real.md"
    engine = types.SimpleNamespace(create_report=create_report)
    seen = []
    barrier = threading.Barrier(2, timeout=5)

    class System:
        report_engine = engine

        def analyze_video_full(self, **kw):
            barrier.wait()                    # 두 요청이 동시에 분석 중인 시점
            seen.append((kw.get("make_reports"), engine.create_report, v193.mvp._gen_pdf))
            return {"r": kw["video_input"]}, "x.json", None, None

    orig_pdf = v193.mvp._gen_pdf
    results = []
    ts = [threading.Thread(target=lambda v=v: results.append(v193.analyze_only(System(), v)))
          for v in ("a.mp4", "b.mp4")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)

    assert sorted(r[0]["r"] for r in results) == ["a.mp4", "b.mp4"]
    assert all(mk is False and cr is create_report and pdf is orig_pdf for mk, cr, pdf in seen)
    assert len(seen) == 2
    assert engine.create_report is create_report and v193.mvp._gen_pdf is orig_pdf


def test_analyze_video_full_has_make_reports_switch():
    # 실제 엔진은 import 가 무거워(Gemini/Whisper) 소스로 계약만 확인한다
    src = (APP_DIR / "mvp_ver_1_9_2.py").read_text(encoding="utf-8")
    assert "make_reports: bool = True" in src
    assert "if not make_reports:\n            return report, json_path, None, None" in src
