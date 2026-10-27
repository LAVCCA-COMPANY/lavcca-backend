"""
mvp_ver_1_9_2.normalize_text / rule_engine 회귀 테스트.

엔진 import 는 무거워서(Gemini·Whisper·torch) 필요한 정의만 소스에서 뽑아 실행한다.
"""
import ast
import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "mvp_ver_1_9_2.py"
_NAMES = {"ABBREVIATION_MAP", "_ABBR_RE", "normalize_text", "rule_engine"}


def _load():
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _NAMES)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) in _NAMES for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(ENGINE), "exec"), ns)
    return ns


NS = _load()
normalize_text = NS["normalize_text"]
rule_engine = NS["rule_engine"]


def test_abbreviation_inside_word_is_untouched():
    # 예전에는 부분 문자열 치환으로 단어가 망가졌다
    assert normalize_text("넘어져가지고 움직이게 엔젤 디 코스튬") == "넘어져가지고 움직이게 엔젤 디 코스튬"


def test_whole_word_abbreviation_is_expanded():
    assert normalize_text("근데 젤 넘 좋아") == "그런데 가장 너무 좋아"
    assert normalize_text("이거 안됌") == "이것 안 됨"   # 긴 키(안됌)가 짧은 키(됌)보다 먼저
    assert normalize_text("됌") == "됨"


def test_abbreviation_next_to_punctuation():
    assert normalize_text("근데, 맨날!") == "그런데, 매일!"


def test_percent_is_kept():
    assert normalize_text("30% 세일") == "30% 세일"


def test_existing_cleanup_still_applies():
    assert normalize_text("ㅋㅋㅋㅋㅋ 대박~~ ♥") == "ㅋㅋ 대박"


def test_rule_keyword_with_abbreviation_matches():
    rules = {"광고": {"keywords": ["추천받은 거 아님"], "action": "REVIEW", "severity": "MEDIUM"}}
    hit = rule_engine(normalize_text("이건 추천받은 거 아님 진짜로"), rules)
    assert hit["hit"] and hit["matched_word"] == "추천받은 거 아님"
