"""
Google Vision / YouTube Data API 키가 URL·오류 로그에 남지 않는지 확인 (보안 회귀 테스트).

키를 URL 쿼리(?key=)로 보내면 requests 의 HTTPError·ConnectionError 메시지에 URL 이 들어가
그대로 CloudWatch 로그에 찍힌다. 키는 x-goog-api-key 헤더로만 보내야 한다.
실제 requests 로 URL 을 만들고 전송 단계(HTTPAdapter.send)만 가짜로 바꾼다 — 네트워크 없음, 가짜 키만 사용.
cv2 / config / 무거운 분석 모듈은 가짜 모듈로 대체한다(실제 .env 를 읽지 않음).
"""
import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import requests
from structlog.testing import capture_logs

CR_DIR = Path(__file__).resolve().parents[1] / "copyright_detector"
FAKE_KEY = "FAKE-KEY-NOT-REAL-0123456789abcdefghij"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cfg(monkeypatch):
    cfg = types.SimpleNamespace(
        api=types.SimpleNamespace(google_api_key=FAKE_KEY, youtube_api_key=FAKE_KEY),
        pipeline=types.SimpleNamespace(vision_max_calls_per_job=40, api_timeout_seconds=5),
    )
    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace(config=cfg))
    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(
        resize=lambda img, size: np.zeros((size[1], size[0], 3), dtype=np.uint8),
        cvtColor=lambda img, code: img[..., 0], COLOR_BGR2GRAY=6,
        imencode=lambda ext, img, params=None: (True, np.frombuffer(b"\xff\xd8JPEG", dtype=np.uint8)),
        IMWRITE_JPEG_QUALITY=1))
    return cfg


@pytest.fixture
def transport(monkeypatch):
    """전송만 가짜로: mode='http403' → 403 응답, mode='conn' → urllib3 식 연결 오류."""
    state = types.SimpleNamespace(mode="http403", sent=[])

    def send(self, request, **kw):
        state.sent.append(request)
        if state.mode == "conn":
            raise requests.exceptions.ConnectionError(
                f"HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: {request.path_url}")
        resp = requests.Response()
        resp.status_code = 403
        resp.reason = "Forbidden"
        resp.url = request.url
        resp.request = request
        resp._content = b"{}"
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


def _assert_key_only_in_header(state, logs):
    assert state.sent, "요청이 전송되지 않음"
    for req in state.sent:
        assert FAKE_KEY not in req.url
        assert "key=" not in req.url
        assert req.headers.get("x-goog-api-key") == FAKE_KEY
    assert logs, "오류 로그가 남지 않음"
    assert FAKE_KEY[:8] not in repr(logs)


@pytest.fixture
def vision(cfg):
    mod = _load(CR_DIR / "utils" / "google_vision_searcher.py", "google_vision_searcher_under_test")
    return mod.GoogleVisionSearcher(api_key=FAKE_KEY)


@pytest.mark.parametrize("mode", ["http403", "conn"])
def test_vision_search_key_not_in_url_or_logs(vision, transport, mode):
    transport.mode = mode
    with capture_logs() as logs:
        assert vision.search(np.full((32, 32, 3), 7, dtype=np.uint8)) is None
    _assert_key_only_in_header(transport, logs)


@pytest.mark.parametrize("mode", ["http403", "conn"])
def test_vision_logos_key_not_in_url_or_logs(vision, transport, mode):
    transport.mode = mode
    with capture_logs() as logs:
        assert vision.detect_logos(b"\xff\xd8JPEG") == []
    _assert_key_only_in_header(transport, logs)


@pytest.fixture
def youtube(cfg, monkeypatch):
    stubs = {
        "database.db_manager": dict(get_db_manager=lambda: None),
        "utils.video_utils": dict(compute_phash=lambda *a, **k: ""),
        "utils.ocr_engine": dict(safe_ocr=lambda *a, **k: ""),
        "utils.clip_engine": dict(classify_frames_batch=None, VISION_TRIGGER_THRESHOLDS={}, embed_frame=None),
        "utils.google_vision_searcher": dict(search_image_for_copyright=None, clear_vision_cache=None),
        "analyzers.yandex_searcher": dict(yandex_reverse_search=None, clear_yandex_cache=None),
    }
    for name, attrs in stubs.items():
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs))
    mod = _load(CR_DIR / "analyzers" / "video_clip_analyzer.py", "video_clip_analyzer_under_test")
    return mod.YouTubeClient()


def test_youtube_connection_error_key_not_in_url_or_logs(youtube, transport):
    transport.mode = "conn"
    with capture_logs() as logs:
        assert youtube.search_by_title("title") == []
    _assert_key_only_in_header(transport, logs)
    assert "q=title" in transport.sent[0].url        # 나머지 검색 파라미터는 그대로


def test_youtube_parses_results_unchanged(youtube, monkeypatch):
    sent = []

    def send(self, request, **kw):
        sent.append(request)
        resp = requests.Response()
        resp.status_code = 200
        resp.url = request.url
        resp._content = (b'{"items":[{"id":{"videoId":"v1"},"snippet":{"title":"t","channelTitle":"c",'
                         b'"publishedAt":"2026-01-01"}}]}')
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    assert youtube.search_by_title("title") == [
        {"youtube_id": "v1", "title": "t", "channel": "c", "published_at": "2026-01-01"}]
    assert sent[0].headers["x-goog-api-key"] == FAKE_KEY


def test_vision_parses_results_unchanged(vision, monkeypatch):
    def send(self, request, **kw):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = request.url
        resp._content = b'{"responses":[{"logoAnnotations":[{"description":"Nike","score":0.9}]}]}'
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    assert vision.detect_logos(b"\xff\xd8JPEG") == [{"description": "Nike", "score": 0.9}]
