"""
뉴스 트렌드 가중치 준비 단계 회귀 테스트 (10/17, trend_signals.py).

off(기본)면 report 를 건드리지 않고, raw 데이터 계약 검사가 위반을 잡아야 한다.
"""
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import trend_signals as ts  # noqa: E402

SAMPLE = APP / "data" / "trends" / "sample_raw.jsonl"
GOOD = {"url": "https://example.com/a", "title": "제목",
        "published_at": "2026-10-16T09:00:00+09:00", "collected_at": "2026-10-17T06:00:00Z"}


def test_off_is_default_and_leaves_report_untouched(monkeypatch):
    monkeypatch.delenv("TREND_MODE", raising=False)
    report = {"natam": {"overall_a": "CARE"}}
    assert ts.enrich(report) is None
    assert report == {"natam": {"overall_a": "CARE"}}


def test_unknown_mode_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("TREND_MODE", "on")   # on 은 아직 미구현
    report = {}
    assert ts.enrich(report) is None and report == {}


def test_shadow_records_without_changing_grades(monkeypatch):
    monkeypatch.setenv("TREND_MODE", "shadow")
    report = {"natam": {"overall_a": "CARE"}}
    res = ts.enrich(report)
    assert res["status"] == "not_ready"
    assert report["trend_shadow"] is res
    assert report["natam"] == {"overall_a": "CARE"}


def test_sample_file_passes_contract():
    rows, errors = ts.load_raw([SAMPLE])
    assert errors == [] and len(rows) == 3


def test_validate_row_catches_violations():
    assert ts.validate_row(GOOD) == []
    assert ts.validate_row({**GOOD, "published_at": None}) == []
    assert any("url" in e for e in ts.validate_row({k: v for k, v in GOOD.items() if k != "url"}))
    assert ts.validate_row({**GOOD, "url": "example.com/a"})
    assert ts.validate_row({**GOOD, "title": "  "})
    assert ts.validate_row({**GOOD, "published_at": "2026-10-16 09:00"})   # 시간대 없음
    assert ts.validate_row({**GOOD, "collected_at": None})
    assert ts.validate_row(["not", "a", "dict"])


def test_load_raw_flags_duplicate_url_and_bad_json(tmp_path):
    f = tmp_path / "2026-10-17.jsonl"
    f.write_text("\n".join([json.dumps(GOOD), json.dumps(GOOD), "{broken"]), encoding="utf-8")
    rows, errors = ts.load_raw([f])
    assert len(rows) == 1
    assert any("중복" in e for e in errors) and any("JSON" in e for e in errors)
