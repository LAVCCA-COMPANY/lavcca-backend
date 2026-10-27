"""
Yandex 역검색의 공개 임시 호스트 업로드 회귀 테스트 (P0 보안 점검).

프레임을 0x0.st / tmpfiles.org 에 올리는 단계는 YANDEX_PUBLIC_UPLOAD_ENABLED=true
일 때만 실행되어야 한다. requests.post 는 가짜로 바꾸고 소켓 연결은 차단한다.
cv2 / config 는 가짜 모듈로 대체한다(실제 .env 를 읽지 않음).
"""
import importlib.util
import socket
import sys
import types
from pathlib import Path

import numpy as np
import pytest

CR_DIR = Path(__file__).resolve().parents[1] / "copyright_detector"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise RuntimeError("network blocked in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def yandex(monkeypatch):
    cfg = types.SimpleNamespace(
        api=types.SimpleNamespace(serpapi_key="TEST_KEY_NOT_REAL"),
        pipeline=types.SimpleNamespace(yandex_max_calls_per_job=15,
                                       yandex_public_upload_enabled=False),
    )
    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace(config=cfg))
    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(
        imencode=lambda ext, img, params=None: (True, np.frombuffer(b"\xff\xd8JPEG", dtype=np.uint8)),
        IMWRITE_JPEG_QUALITY=1))
    mod = _load(CR_DIR / "analyzers" / "yandex_searcher.py", "yandex_searcher_under_test")

    posts = []

    class Resp:
        status_code = 200
        text = "https://0x0.st/test.jpg"

    def fake_post(url, **kw):
        posts.append(url)
        return Resp()

    monkeypatch.setattr(mod.requests, "post", fake_post)
    mod.clear_yandex_cache()
    return types.SimpleNamespace(mod=mod, cfg=cfg, posts=posts,
                                 frame=np.zeros((4, 4, 3), dtype=np.uint8))


def _fake_serpapi(monkeypatch):
    class GoogleSearch:
        def __init__(self, params):
            self.params = params

        def get_dict(self):
            return {"image_results": [], "sites": []}
    monkeypatch.setitem(sys.modules, "serpapi", types.SimpleNamespace(GoogleSearch=GoogleSearch))


def test_upload_disabled_by_default_even_with_key(yandex, monkeypatch):
    _fake_serpapi(monkeypatch)
    r = yandex.mod.yandex_reverse_search(yandex.frame, 0)
    assert yandex.posts == []
    assert r["error"] == "yandex public upload disabled"


def test_no_key_no_upload(yandex):
    yandex.cfg.api.serpapi_key = ""
    yandex.cfg.pipeline.yandex_public_upload_enabled = True
    yandex.mod.yandex_reverse_search(yandex.frame, 0)
    assert yandex.posts == []


def test_enabled_but_serpapi_missing_does_not_upload(yandex, monkeypatch):
    yandex.cfg.pipeline.yandex_public_upload_enabled = True
    monkeypatch.setitem(sys.modules, "serpapi", None)      # import 실패
    r = yandex.mod.yandex_reverse_search(yandex.frame, 0)
    assert yandex.posts == []
    assert r["error"] == "serpapi package not installed"


def test_explicit_opt_in_uploads_and_searches(yandex, monkeypatch):
    _fake_serpapi(monkeypatch)
    yandex.cfg.pipeline.yandex_public_upload_enabled = True
    r = yandex.mod.yandex_reverse_search(yandex.frame, 0)
    assert yandex.posts == ["https://0x0.st"]
    assert r["error"] is None


def test_config_default_is_disabled(monkeypatch, tmp_path):
    # 실제 config.py 의 기본값 확인. load_dotenv 는 가짜로 바꿔 .env 를 읽지 않는다.
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
    monkeypatch.delenv("YANDEX_PUBLIC_UPLOAD_ENABLED", raising=False)
    off = _load(CR_DIR / "config.py", "config_default_under_test")
    assert off.config.pipeline.yandex_public_upload_enabled is False
    monkeypatch.setenv("YANDEX_PUBLIC_UPLOAD_ENABLED", "true")
    on = _load(CR_DIR / "config.py", "config_optin_under_test")
    assert on.config.pipeline.yandex_public_upload_enabled is True
