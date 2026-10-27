"""
음악 저작권 분석 on/off 회귀 테스트 (10/17, MUSIC_ENABLED).

꺼져 있으면 MusicAnalyzer.analyze 가 영상·API 를 건드리지 않고 [] 를 돌려줘야 한다.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "copyright_detector"))

from analyzers import music_analyzer as ma  # noqa: E402


class _Boom:
    def identify_from_file(self, *_a, **_k):
        raise AssertionError("음악 분석이 꺼져 있는데 API 를 호출함")


class _Progress:
    def __init__(self):
        self.done_calls = []

    def done(self, name, note=""):
        self.done_calls.append((name, note))


def test_disabled_skips_api_and_returns_empty(monkeypatch):
    monkeypatch.setattr(ma.config.pipeline, "music_enabled", False)
    monkeypatch.setattr(ma, "get_video_info",
                        lambda *_: (_ for _ in ()).throw(AssertionError("영상을 읽음")))
    analyzer = object.__new__(ma.MusicAnalyzer)   # __init__(DB·스레드풀) 생략
    analyzer.acrcloud = analyzer.audd = _Boom()
    progress = _Progress()

    result = asyncio.run(analyzer.analyze("no_such.mp4", "JOB", progress=progress))

    assert result == []
    assert progress.done_calls and progress.done_calls[0][0] == "music"


def test_default_is_disabled():
    import os
    if "MUSIC_ENABLED" in os.environ:
        return  # 로컬에서 켜 둔 경우는 기본값 검사 대상 아님
    assert ma.config.pipeline.music_enabled is False
