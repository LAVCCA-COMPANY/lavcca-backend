"""
리포트 부록 '분석에 쓰인 모델' — 설정값이 아니라 실제로 응답한 모델(대체 모델·대체 전사 엔진 포함)을 적는지.

엔진 import 는 무거워서 필요한 정의만 소스에서 뽑아 돌린다(test_gemini_resilience.py 와 같은 방식).
"""
import ast
import threading
import types
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "mvp_ver_1_9_2.py"
TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "report_template_v1_9_3.md"


def _load():
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in {"_fmt_models", "_models_used_placeholders"})
            or (isinstance(n, ast.ClassDef) and n.name == "CostTracker")]
    ns = {"threading": threading, "ENABLE_COST_METERING": True,
          "_PRICE_PER_1K_IN": 0.001, "_PRICE_PER_1K_OUT": 0.002,
          "GEN_MODEL_FALLBACK": "models/gemini-3.6-flash",
          "EMBED_MODEL": "models/gemini-embedding-2", "WHISPER_MODEL": "turbo"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(ENGINE), "exec"), ns)
    return ns


def _resp(model):
    um = types.SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0)
    return types.SimpleNamespace(usage_metadata=um, model_version=model)


def _meta(ns, calls, asr_engine="gemini", index=None):
    cost = ns["CostTracker"]()
    for label, model in calls:
        cost.add(_resp(model), label=label)
    return {"asr_engine": asr_engine, "whisper_model": "turbo", "cost": cost.summary(),
            "similar_index": {"count": index}}


def test_shows_models_that_actually_answered():
    ns = _load()
    meta = _meta(ns, [("transcribe", "gemini-3.8-flash"), ("refine", "gemini-3.8-flash"),
                      ("tier2", "gemini-3.8-flash"), ("tier2", "gemini-3.6-flash"),
                      ("natam", "gemini-3.6-flash"), ("summary", "gemini-3.6-flash")])
    p = ns["_models_used_placeholders"](meta)
    assert p["MODEL_ASR"] == "Gemini · gemini-3.8-flash"
    assert p["MODEL_REFINE"] == "gemini-3.8-flash"
    assert p["MODEL_ANALYSIS"] == "gemini-3.6-flash(대체 모델) 3회, gemini-3.8-flash 1회"
    assert p["MODEL_EMBED"] == "사용 안 함 (사례 인덱스 없음)"


def test_transcription_fallback_engines():
    ns = _load()
    f = ns["_models_used_placeholders"]
    assert f(_meta(ns, [], asr_engine="aws_transcribe"))["MODEL_ASR"] == "AWS Transcribe (Gemini 장애로 대체)"
    assert f(_meta(ns, [], asr_engine="whisper"))["MODEL_ASR"] == "Whisper turbo (Gemini 장애로 대체)"
    old = f({"cost": {}, "similar_index": {"count": 120}})          # 텍스트 입력·옛 리포트
    assert old["MODEL_ASR"] == "—" and old["MODEL_ANALYSIS"] == "—"
    assert old["MODEL_EMBED"] == "gemini-embedding-2 (사례 120건)"


def test_cost_summary_models_are_copies():
    ns = _load()
    cost = ns["CostTracker"]()
    cost.add(_resp("gemini-3.8-flash"), label="refine")
    s = cost.summary()
    cost.add(_resp("gemini-3.6-flash"), label="refine")
    assert s["by_label"]["refine"]["models"] == {"gemini-3.8-flash": 1}   # 앞선 요약은 그대로


def test_template_uses_model_placeholders_and_no_internal_paths():
    t = TEMPLATE.read_text(encoding="utf-8")
    for key in ("MODEL_ASR", "MODEL_REFINE", "MODEL_ANALYSIS", "MODEL_EMBED"):
        assert "{" + key + "}" in t
    for gone in ("TRANSCRIPT_RAW_PATH", "REPORT_JSON_PATH", "GEN_MODEL}", "CASE_DB_SIZE", "GEMINI_BATCH_SIZE"):
        assert gone not in t
