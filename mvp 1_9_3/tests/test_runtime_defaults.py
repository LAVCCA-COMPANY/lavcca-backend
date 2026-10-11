"""
실행 환경 표준화 회귀 테스트.

- 과금·외부 전송 기능(Rekognition, Yandex 공개 업로드)은 환경변수로 켜지 않는 한 꺼져 있다.
- requirements / lock 에 잘못된 serpapi 패키지·CUDA torch 가 들어가지 않는다.
config.py 는 load_dotenv 를 가짜로 바꿔 실제 .env 를 읽지 않는다.
"""
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parent
CR_DIR = APP_DIR / "copyright_detector"


def _load_config(monkeypatch, **env):
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
    for k in ("USE_REKOGNITION", "YANDEX_PUBLIC_UPLOAD_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("config_runtime_defaults", CR_DIR / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.config


def test_paid_external_features_off_by_default(monkeypatch):
    cfg = _load_config(monkeypatch)
    assert cfg.aws.use_rekognition is False
    assert cfg.pipeline.yandex_public_upload_enabled is False


def test_rekognition_requires_explicit_opt_in(monkeypatch):
    assert _load_config(monkeypatch, USE_REKOGNITION="true").aws.use_rekognition is True


def _pkg_names(path):
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            names.add(m.group(1).lower())
    return names


REQ_FILES = [CR_DIR / "requirements.txt", REPO / "requirements" / "main.in",
             REPO / "requirements" / "copyright.in"]
LOCKS = sorted((REPO / "requirements" / "lock").glob("*.txt"))


@pytest.mark.parametrize("path", REQ_FILES + LOCKS, ids=lambda p: p.name)
def test_no_conflicting_packages(path):
    names = _pkg_names(path)
    assert "serpapi" not in names            # GoogleSearch 없음 + google-search-results 와 모듈 충돌
    assert "opencv-python" not in names      # opencv-python-headless 와 cv2 충돌
    assert not any(n.startswith("nvidia-") for n in names)


def test_copyright_requirements_use_google_search_results():
    assert "google-search-results" in _pkg_names(CR_DIR / "requirements.txt")


def test_locks_pin_model_and_cpu_torch():
    assert len(LOCKS) == 4
    for lock in LOCKS:
        text = lock.read_text(encoding="utf-8")
        assert "\nscikit-learn==1.8.0 " in text          # controversy_model.joblib 저장 버전
        assert re.search(r"\ntorch==[\d.]+\+cpu ", text)
        assert "--hash=sha256:" in text
        if lock.name.startswith("linux"):                # CPU 이미지: whisper 의 GPU 전용 의존성 제외
            assert "triton" not in _pkg_names(lock)
