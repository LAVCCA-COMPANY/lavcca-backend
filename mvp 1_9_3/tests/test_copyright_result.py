"""
run_copyright_detection() 회귀 테스트 (Ponytail Audit #2).

실제 copyright_detector 는 실행하지 않는다. subprocess.run 을 가짜로 바꿔
main.py 의 결과 저장 동작(--output 폴더, 없으면 cwd/results)만 흉내 낸다.

실행:  cd "mvp 1_9_3" && python -m pytest tests -q
"""
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def v193():
    # mvp_ver_1_9_3 는 import 시 무거운 엔진(mvp_ver_1_9_2: Gemini 등)을 불러오고
    # 작업 폴더를 바꾼다 → 엔진은 가짜 모듈로 대체하고, cwd 는 끝나고 되돌린다.
    fake = types.ModuleType("mvp_ver_1_9_2")

    class _Engine:
        def __init__(self, template_path=None, output_dir=None):
            pass

    fake.CrisisReportEngine = _Engine
    fake.NATAM_LEVELS = ["SAFE", "CARE", "ALERT", "DANGER", "CRITICAL"]
    saved = {n: sys.modules.get(n) for n in
             ("mvp_ver_1_9_2", "spread_signals", "pdf_from_md", "run_logging")}
    sys.modules["mvp_ver_1_9_2"] = fake
    for name in ("spread_signals", "pdf_from_md", "run_logging"):
        sys.modules[name] = None          # import 실패 → 모듈 쪽에서 None 처리
    sys.path.insert(0, str(APP_DIR))
    cwd = os.getcwd()
    try:
        import mvp_ver_1_9_3 as mod
        yield mod
    finally:
        os.chdir(cwd)
        sys.path.remove(str(APP_DIR))
        sys.modules.pop("mvp_ver_1_9_3", None)
        for name, m in saved.items():
            if m is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = m


@pytest.fixture
def detector(v193, tmp_path, monkeypatch):
    """copyright_detector 폴더를 tmp 로 바꾸고, 가짜 실행기를 설치할 수 있게 한다."""
    cr_dir = tmp_path / "copyright_detector"
    (cr_dir / "results").mkdir(parents=True)
    (cr_dir / "main.py").write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(v193, "COPYRIGHT_DIR", cr_dir)
    monkeypatch.setattr(v193, "COPYRIGHT_MAIN", cr_dir / "main.py")
    if hasattr(v193, "COPYRIGHT_RESULTS"):
        monkeypatch.setattr(v193, "COPYRIGHT_RESULTS", cr_dir / "results")
    monkeypatch.setattr(v193, "run_logging", None)

    state = {"out_dirs": []}

    def install(rc=0, write=True, payload=None, raw=None, delay=0.0,
                extra=None, raise_exc=None):
        def fake_run(cmd, cwd=None, check=False):
            if raise_exc:
                raise raise_exc
            video = cmd[2]
            if "--output" in cmd:
                out = Path(cmd[cmd.index("--output") + 1])
            else:                                   # main.py 기본값: ./results
                out = Path(cwd) / "results"
            state["out_dirs"].append(out)
            time.sleep(delay)
            if write:
                out.mkdir(parents=True, exist_ok=True)
                body = raw if raw is not None else json.dumps(
                    payload or _result(video), ensure_ascii=False)
                (out / f"result_JOB_{time.time_ns()}.json").write_text(body, encoding="utf-8")
            if extra:                 # 이번 결과를 쓴 '뒤' 다른 작업이 끝나는 상황
                time.sleep(0.02)
                extra()
            return types.SimpleNamespace(returncode=rc)

        monkeypatch.setattr(v193.subprocess, "run", fake_run)

    state["install"] = install
    state["shared_results"] = cr_dir / "results"
    return state


def _result(video_path, level="LOW"):
    return {"video_path": video_path,
            "summary": {"overall_risk_level": level, "overall_risk_score": 10,
                        "total_issues_found": 1}}


def _video(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x00")
    return str(p)


def _seed_other_result(results_dir, video="OTHER_VIDEO.mp4"):
    """다른 영상의 과거 결과가 공용 results/ 에 남아 있는 상황."""
    p = results_dir / "result_OLDJOB_20260101_000000.json"
    p.write_text(json.dumps(_result(video, "HIGH")), encoding="utf-8")
    return p


# ── 1. 실패 시 과거 결과 미사용 ────────────────────────────────────────
@pytest.mark.parametrize("case", [
    dict(rc=1, write=False),                 # 검사기 크래시, 파일 없음
    dict(rc=0, write=False),                 # 정상 종료인데 파일 없음
    dict(rc=1, write=True),                  # 비정상 종료 + 파일 존재 → 신뢰 안 함
    dict(rc=0, raw="{not json"),             # 깨진 JSON
    dict(rc=0, payload={"no": "summary"}),   # 형식이 틀린 결과
    dict(raise_exc=OSError("spawn failed")),  # 실행 자체 실패
])
def test_failure_never_reuses_other_video_result(v193, detector, tmp_path, case):
    _seed_other_result(detector["shared_results"])
    detector["install"](**case)
    assert v193.run_copyright_detection(_video(tmp_path, "X.mp4")) is None


# ── 2. 실행별 결과 격리 ───────────────────────────────────────────────
def test_reads_only_this_runs_result(v193, detector, tmp_path):
    _seed_other_result(detector["shared_results"])
    # 이번 실행이 결과를 쓴 직후, 다른 작업이 공용 폴더에 더 새로운 결과를 써도 무시해야 한다
    def newer():
        p = detector["shared_results"] / f"result_OTHER_{time.time_ns()}.json"
        p.write_text(json.dumps(_result("CONCURRENT.mp4", "HIGH")), encoding="utf-8")
    detector["install"](extra=newer)
    video = _video(tmp_path, "X.mp4")
    out = v193.run_copyright_detection(video)
    assert out is not None and out["video_path"] == video


def test_temp_result_dir_is_unique_and_removed(v193, detector, tmp_path):
    detector["install"]()
    v193.run_copyright_detection(_video(tmp_path, "A.mp4"))
    detector["install"](rc=1, write=False)
    v193.run_copyright_detection(_video(tmp_path, "B.mp4"))
    detector["install"](raise_exc=OSError("boom"))
    v193.run_copyright_detection(_video(tmp_path, "C.mp4"))
    dirs = detector["out_dirs"]
    assert len(dirs) == 2 and dirs[0] != dirs[1]          # C 는 실행 전 실패
    assert dirs[0] != detector["shared_results"]
    assert not any(d.exists() for d in dirs)               # 성공/실패 모두 정리


# ── 3. 동시 실행 ──────────────────────────────────────────────────────
def test_concurrent_runs_get_their_own_results(v193, detector, tmp_path, monkeypatch):
    # A 가 결과를 쓰고 프로세스가 끝나기 전(로그 정리 등) B 가 결과를 쓰는 상황
    # → 옛 코드: A 가 '가장 최근 파일'인 B 의 결과를 가져감
    va, vb = _video(tmp_path, "A.mp4"), _video(tmp_path, "B.mp4")
    real_sleep = time.sleep

    def fake_run(cmd, cwd=None, check=False):
        video = cmd[2]
        out = (Path(cmd[cmd.index("--output") + 1]) if "--output" in cmd
               else Path(cwd) / "results")
        real_sleep(0.05 if video == va else 0.2)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"result_{time.time_ns()}.json").write_text(
            json.dumps(_result(video)), encoding="utf-8")
        if video == va:
            real_sleep(0.4)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(v193.subprocess, "run", fake_run)
    got = {}
    ta = threading.Thread(target=lambda: got.__setitem__("A", v193.run_copyright_detection(va)))
    tb = threading.Thread(target=lambda: got.__setitem__("B", v193.run_copyright_detection(vb)))
    ta.start(); real_sleep(0.05); tb.start()
    ta.join(); tb.join()
    assert got["A"]["video_path"] == va
    assert got["B"]["video_path"] == vb


# ── 4. 실패가 NATAM 리포트에 '판단 보류'로 반영 ────────────────────────
def test_failure_marks_report_as_pending(v193):
    report = {"natam_risk": {"B": {"B-01": {"level": "CARE"},
                                   "B-03": {"level": "DANGER", "reason": "LLM 추측"}},
                             "overall_b": "DANGER"},
              "copyright": None}
    v193._merge_copyright_into_natam_b03(report)
    b03 = report["natam_risk"]["B"]["B-03"]
    assert b03["level"] == "N/A" and "판단 보류" in b03["reason"]
    assert report["natam_risk"]["overall_b"] == "CARE"     # 추측값 DANGER 가 빠짐
    status = v193.build_copyright_placeholders(None)["CR_STATUS"]
    assert "실패" in status
