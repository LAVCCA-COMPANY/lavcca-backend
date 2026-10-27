"""
Gemini 일시 장애 대응 회귀 테스트: 같은 모델 재시도 → 대체 모델 → 그래도 실패면 예외.

엔진 import 는 무거워서(Whisper·torch) 필요한 정의만 소스에서 뽑아 가짜 클라이언트로 돌린다.
네트워크·실제 대기는 쓰지 않는다(time.sleep 은 기록만).
"""
import ast
import json
import random
import re
import types
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "mvp_ver_1_9_2.py"
_FUNCS = {"_install_gemini_defaults", "_gemini_retry_wait", "_err_label", "_api_is_transient",
          "_parse_429_error", "_classify_429_kind", "_safe_json_parse", "_coerce_flag"}
_CLASSES = {"TransientAPIError"}
_ASSIGNS = {"_TRANSIENT_HTTP", "_TRANSIENT_WORDS"}
_METHODS = {"_tier2_adjudicate_batch", "_tier2_hold", "_tier2_window_text"}


def _load(**overrides):
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _FUNCS)
            or (isinstance(n, ast.ClassDef) and n.name in _CLASSES)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in _ASSIGNS for t in n.targets))]
    engine_cls = next(n for n in tree.body
                      if isinstance(n, ast.ClassDef) and n.name == "CrisisConsultantSystem")
    keep.append(ast.ClassDef(name="Engine", bases=[], keywords=[], decorator_list=[],
                             body=[n for n in engine_cls.body
                                   if isinstance(n, ast.FunctionDef) and n.name in _METHODS]))
    sleeps = []
    ns = {
        "re": re, "json": json, "random": random,
        "time": types.SimpleNamespace(sleep=sleeps.append),
        "SAFETY_SETTINGS": [], "_DEFAULT_MAX_OUTPUT_TOKENS": 8192, "_GEMINI_SEED": 42,
        "_thinking_config": lambda j=False: {"thinking_level": "medium" if j else "low"},
        "_repair_truncated_json": lambda s: None,
        "GEN_MODEL": "models/main", "GEN_MODEL_FALLBACK": "models/backup",
        "GEMINI_RETRIES": 4, "GEMINI_RETRY_BASE_SEC": 3.0, "GEMINI_RETRY_MAX_SEC": 30.0,
        "TIER2_CONTEXT_WINDOW": 2, "LABEL_INFO": "L01 …",
        "_seg_timestamp": lambda s: "00:00–00:01",
    }
    ns.update(overrides)
    exec(compile(ast.fix_missing_locations(ast.Module(body=keep, type_ignores=[])), str(ENGINE), "exec"), ns)
    return ns, sleeps


class ApiError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"{code} {msg}")
        self.code = code


class FakeModels:
    """script[model] 의 항목을 차례로 소비: 예외면 raise, 아니면 응답 text 로 돌려준다."""

    def __init__(self, **script):
        self.script = {k.replace("_", "/", 1): list(v) for k, v in script.items()}
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, dict(config)))
        step = self.script[model].pop(0) if self.script.get(model) else ApiError(503, "UNAVAILABLE")
        if isinstance(step, BaseException):
            raise step
        return types.SimpleNamespace(text=step, usage_metadata=None, model_version=model)


def _client(ns, models):
    return ns["_install_gemini_defaults"](types.SimpleNamespace(models=models))


def test_retries_same_model_with_backoff_then_succeeds():
    ns, sleeps = _load()
    models = FakeModels(models_main=[ApiError(503), ApiError(503), ApiError(503), "ok"])
    r = _client(ns, models).models.generate_content(model="models/main", contents="x", config={})
    assert r.text == "ok" and [m for m, _ in models.calls] == ["models/main"] * 4
    # 3·6·12초 × 지터(0.5~1)
    assert len(sleeps) == 3
    for w, base in zip(sleeps, (3, 6, 12)):
        assert base * 0.5 <= w <= base


def test_switches_to_fallback_model_after_retries():
    ns, sleeps = _load()
    models = FakeModels(models_main=[ApiError(503)] * 4, models_backup=[ApiError(500), "from backup"])
    r = _client(ns, models).models.generate_content(model="models/main", contents="x", config={})
    assert r.text == "from backup" and r.model_version == "models/backup"
    assert [m for m, _ in models.calls] == ["models/main"] * 4 + ["models/backup"] * 2
    assert len(sleeps) == 4   # 주 모델 사이 3번 + 대체 모델 사이 1번, 전환할 때는 바로


def test_raises_last_transient_error_when_both_models_fail():
    ns, _ = _load()
    models = FakeModels()
    with pytest.raises(ApiError) as ei:
        _client(ns, models).models.generate_content(model="models/main", contents="x", config={})
    assert ns["_api_is_transient"](ei.value)
    assert len(models.calls) == 8


def test_permanent_error_is_raised_at_once_without_fallback():
    ns, sleeps = _load()
    models = FakeModels(models_main=[ApiError(400, "INVALID_ARGUMENT")])
    with pytest.raises(ApiError):
        _client(ns, models).models.generate_content(model="models/main", contents="x", config={})
    assert len(models.calls) == 1 and sleeps == []


def test_429_waits_for_server_retry_delay():
    ns, sleeps = _load()
    err = ApiError(429, "RESOURCE_EXHAUSTED {'retryDelay': '7s'}")
    models = FakeModels(models_main=[err, "ok"])
    _client(ns, models).models.generate_content(model="models/main", contents="x", config={})
    assert 7 <= sleeps[0] <= 9


@pytest.mark.parametrize("fallback", ["", "models/main"])
def test_no_switch_when_fallback_is_empty_or_same(fallback):
    ns, _ = _load(GEN_MODEL_FALLBACK=fallback)
    models = FakeModels()
    with pytest.raises(ApiError):
        _client(ns, models).models.generate_content(model="models/main", contents="x", config={})
    assert [m for m, _ in models.calls] == ["models/main"] * 4


def test_defaults_are_still_injected_and_kept_for_fallback():
    ns, _ = _load()
    models = FakeModels(models_main=[ApiError(503)] * 4, models_backup=["ok"])
    _client(ns, models).models.generate_content(
        model="models/main", contents="x", config={"temperature": 0.1, "thinking_config": {"thinking_level": "medium"}})
    cfg = models.calls[-1][1]
    assert cfg["seed"] == 42 and cfg["max_output_tokens"] == 8192 and cfg["temperature"] == 0.1
    assert cfg["thinking_config"] == {"thinking_level": "medium"}


def test_json_parse_error_is_not_transient():
    ns, _ = _load()
    assert not ns["_api_is_transient"](ValueError("JSON 파싱 실패"))
    assert not ns["_api_is_transient"](RuntimeError("failed after 1500ms"))
    assert ns["_api_is_transient"](RuntimeError("model is overloaded, high demand"))


def _tier2_engine(ns, models):
    eng = ns["Engine"]()
    eng.client = types.SimpleNamespace(models=models)
    eng.cost = types.SimpleNamespace(add=lambda resp, label="": None)
    return eng


def _trig(i):
    return {"idx": i, "hits": [{"label": "L02", "prob": 0.4}]}


SEGS = [{"text": f"문장{i}", "start": float(i), "end": i + 0.9} for i in range(6)]


def test_tier2_batch_transient_failure_holds_all_without_single_calls():
    ns, _ = _load()
    models = FakeModels(models_main=[ApiError(503)])
    out = _tier2_engine(ns, models)._tier2_adjudicate_batch(SEGS, [_trig(1), _trig(3)], [], [])
    assert len(models.calls) == 1          # 단건 폴백을 보내지 않았다
    assert [o["adjudication"] for o in out] == ["unverified", "unverified"]
    assert all(o["transient"] and o["is_controversy"] is None for o in out)


def test_tier2_batch_permanent_failure_still_falls_back_to_single():
    ns, _ = _load()
    models = FakeModels(models_main=[ApiError(400, "INVALID_ARGUMENT")])
    eng = _tier2_engine(ns, models)
    singles = []
    eng._tier2_adjudicate = lambda segs, t, rh, st: singles.append(t["idx"]) or {"segment_idx": t["idx"]}
    eng._tier2_adjudicate_batch(SEGS, [_trig(1), _trig(3)], [], [])
    assert singles == [1, 3]
