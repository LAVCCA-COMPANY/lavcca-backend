# -*- coding: utf-8 -*-
"""
risk_trigger.py — 위험 신호 → 유사 사례 검색 트리거 빌더

NATAM 분석 결과(A축 커뮤니티 리스크 / B축 플랫폼 리스크)와 위험 문장을 받아
유사 사례 엔진에 넘길 (query_text, keywords, risk_categories) 를 구성한다.

설계 근거
  - 입력 '전체'를 그대로 임베딩하지 않는다. 실제로 위험시된 부분만 모아
    쿼리를 만들어 유사 사례 정확도를 높인다.
  - 코퍼스(NocoDB 사례집)의 '리스크' 10종이 NATAM A축 10개와 1:1 대응한다.
    → A축에서 발화된 항목을 같은 이름의 코퍼스 리스크 카테고리로 직접 매핑.
    → B축(플랫폼)은 코퍼스에 직접 대응 카테고리가 없어 근접 매핑(근사).

사용
  from risk_trigger import build_trigger
  trig = build_trigger(natam_result, risk_sentences=[...], classification={...})
  cases = engine.search(trig["query_text"], keywords=trig["keywords"],
                        risk_categories=trig["risk_categories"], k=5)
"""
from __future__ import annotations

import re
from typing import Optional

# 위험도 순서(낮을수록 안전) — mvp_ver_1_9_2.NATAM_LEVELS 와 동일
NATAM_LEVELS = ["SAFE", "CARE", "ALERT", "DANGER", "CRITICAL"]


def _lvl(x: str) -> int:
    try:
        return NATAM_LEVELS.index((x or "SAFE").upper())
    except ValueError:
        return 0


# 축 ID → 사람이 읽는 이름(엔진 정의와 동일)
AXIS_NAMES = {
    "A-01": "정치·이념 리스크",
    "A-02": "특정 인물·집단 낙인 리스크",
    "A-03": "사생활·윤리 리스크",
    "A-04": "이미지 역반전 리스크",
    "A-05": "맥락 절단·클립화 리스크",
    "A-06": "갈등 증폭 리스크",
    "A-07": "밈화·조롱 소비 리스크",
    "A-08": "브랜드 세이프티 리스크",
    "A-09": "감정 선동 리스크",
    "A-10": "기존 논란 결합 리스크",
    "B-01": "괴롭힘·혐오·차별 표현 리스크",
    "B-02": "폭력·위협·불법행위 리스크",
    "B-03": "저작권 리스크",
    "B-04": "성적 표현·대상화 리스크",
    "B-05": "광고친화성 정책 리스크",
}

# 축 ID → 코퍼스 '리스크' 카테고리(정확한 문자열).
#   A축은 1:1 직접 매핑, B축은 근접 매핑(근사). B-03 저작권은 별도 copyright_detector
#   담당이라 코퍼스 카테고리 필터를 걸지 않는다(빈 리스트).
AXIS_TO_CORPUS_RISK = {
    "A-01": ["정치 이념 리스크"],
    "A-02": ["특정 인물·집단 낙인 리스크"],
    "A-03": ["사생활 윤리 리스크"],
    "A-04": ["이미지 역반전 리스크"],
    "A-05": ["맥락 절단·클립화 리스크"],
    "A-06": ["갈등 증폭 리스크"],
    "A-07": ["밈화·조롱 소비 리스크"],
    "A-08": ["광고주 비친화성 리스크"],
    "A-09": ["감정 선동 리스크"],
    "A-10": ["기존 논란 결합 리스크"],
    # ── B축(플랫폼) 근접 매핑 ──
    "B-01": ["특정 인물·집단 낙인 리스크", "갈등 증폭 리스크"],
    "B-02": ["갈등 증폭 리스크", "감정 선동 리스크"],
    "B-03": [],  # 저작권: 코퍼스 대응 없음(별도 엔진 담당)
    "B-04": [],  # 성적 표현: 낙인·밈 카테고리로 끌면 다른 사건이 올라온다. 의미검색만.
    "B-05": ["광고주 비친화성 리스크"],
}

# 축 ID → 옛 controversy_type enum(worst_actions_map.yaml 키). worst_actions/
# get_risk_guide 가 옛 taxonomy로 도므로, 발화된 축을 옛 enum으로 환원해 유지한다.
AXIS_TO_CONTROVERSY_TYPE = {
    "A-01": "attitude_issue",      # 정치·이념
    "A-02": "hate_speech",         # 낙인
    "A-03": "privacy_issue",       # 사생활
    "A-04": "attitude_issue",      # 이미지 역반전
    "A-05": "attitude_issue",      # 맥락 절단
    "A-06": "attitude_issue",      # 갈등 증폭
    "A-07": "hate_speech",         # 밈화·조롱
    "A-08": "advertising_issue",   # 브랜드 세이프티
    "A-09": "attitude_issue",      # 감정 선동
    "A-10": "attitude_issue",      # 기존 논란 결합
    "B-01": "hate_speech",         # 괴롭힘·혐오·차별
    "B-02": "illegal_issue",       # 폭력·위협·불법
    "B-03": "attitude_issue",      # 저작권(worst_actions 대응 없음 → 무해한 기본값)
    "B-04": "hate_speech",         # 성적 표현·대상화
    "B-05": "advertising_issue",   # 광고친화성
}

# 옛 controversy_type 별칭에도 쓰는, 코퍼스 리스크 → 옛 enum(유사 사례 표시용)
CORPUS_RISK_TO_CONTROVERSY_TYPE = {
    "정치 이념 리스크":          "attitude_issue",
    "특정 인물·집단 낙인 리스크": "hate_speech",
    "사생활 윤리 리스크":        "privacy_issue",
    "이미지 역반전 리스크":      "attitude_issue",
    "맥락 절단·클립화 리스크":   "attitude_issue",
    "갈등 증폭 리스크":          "attitude_issue",
    "밈화·조롱 소비 리스크":     "hate_speech",
    "광고주 비친화성 리스크":    "advertising_issue",
    "감정 선동 리스크":          "attitude_issue",
    "기존 논란 결합 리스크":     "attitude_issue",
}


def controversy_type_from_signals(natam_result: Optional[dict] = None,
                                  classification: Optional[dict] = None,
                                  cases: Optional[list] = None,
                                  threshold: str = "ALERT") -> str:
    """worst_actions 용 옛 controversy_type 추정.
    우선순위: 가장 심각한 발화 축 → 최상위 유사 사례 리스크 → 기본값(attitude_issue)."""
    # 1) 가장 심각한 발화 축
    best = None
    for axis in ("A", "B"):
        for aid, info in ((natam_result or {}).get(axis) or {}).items():
            info = info or {}
            if _lvl(info.get("level", "SAFE")) >= _lvl(threshold):
                lv = _lvl(info["level"]) if info.get("level") else 0
                if best is None or lv > best[0]:
                    best = (lv, aid)
    if best:
        return AXIS_TO_CONTROVERSY_TYPE.get(best[1], "attitude_issue")
    # 2) 최상위 유사 사례의 코퍼스 리스크
    if cases:
        risk = (cases[0].get("리스크") or "").strip()
        if risk in CORPUS_RISK_TO_CONTROVERSY_TYPE:
            return CORPUS_RISK_TO_CONTROVERSY_TYPE[risk]
    # 3) 기본값
    return "attitude_issue"


# 키워드 추출 시 제거할 조사·불용어(가벼운 규칙 기반)
_STOPWORDS = {
    "그리고", "그러나", "하지만", "또한", "때문", "위해", "대한", "관련", "가능성",
    "리스크", "위험", "표현", "발언", "행동", "가지", "경우", "부분", "이런", "저런",
    "그런", "있음", "없음", "관찰", "감지", "요소", "내용", "상황", "정도", "다는",
    "하는", "되는", "이다", "한다", "된다", "에서", "으로", "에게", "까지", "부터",
}
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def extract_keywords(text: str, max_n: int = 20) -> list:
    """한국어 텍스트에서 내용어 후보를 가볍게 추출(2자 이상, 불용어 제외)."""
    if not text:
        return []
    seen = []
    for tok in _TOKEN_RE.findall(text):
        t = tok.lower()
        if len(t) < 2 or t in _STOPWORDS:
            continue
        if t not in seen:
            seen.append(t)
        if len(seen) >= max_n:
            break
    return seen


def build_trigger(
    natam_result:   Optional[dict] = None,
    risk_sentences: Optional[list] = None,
    classification: Optional[dict] = None,
    base_text:      str = "",
    threshold:      str = "ALERT",
    max_query_len:  int = 2000,
) -> dict:
    """
    위험 신호를 모아 유사 사례 검색용 트리거를 만든다.

    Parameters
      natam_result   : assess_natam_risk() 반환({"A":{...},"B":{...},...})
      risk_sentences : 위험 문장 리스트(자막 세그먼트 중 위험시된 문장 등)
      classification : classify_controversy() 반환({"primary","labels","reason"})
      base_text      : 보조 컨텍스트(사건 개요/요약). 위험 문장이 없을 때 유용
      threshold      : 이 단계 이상이면 '발화'로 간주(기본 ALERT)

    Returns dict:
      query_text      : 의미검색용 질의문
      keywords        : 키워드 부스트용 토큰
      risk_categories : 코퍼스 리스크 카테고리 필터/부스트
      fired_axes      : [{axis, name, level, reason}] 발화 항목(심각도순)
    """
    natam_result = natam_result or {}
    risk_sentences = [s for s in (risk_sentences or []) if s and s.strip()]

    # ── 발화된 축 항목 수집(threshold 이상) ──
    fired = []
    for axis in ("A", "B"):
        for aid, info in (natam_result.get(axis) or {}).items():
            info = info or {}
            level = info.get("level", "SAFE")
            if _lvl(level) >= _lvl(threshold):
                fired.append({
                    "axis":   aid,
                    "name":   AXIS_NAMES.get(aid, aid),
                    "level":  level,
                    "reason": (info.get("reason") or "").strip(),
                })
    fired.sort(key=lambda x: _lvl(x["level"]), reverse=True)

    # ── 코퍼스 리스크 카테고리(발화 축 → 매핑, 순서·중복 정리) ──
    risk_categories = []
    for f in fired:
        for c in AXIS_TO_CORPUS_RISK.get(f["axis"], []):
            if c not in risk_categories:
                risk_categories.append(c)

    # ── 질의문 구성: 위험 문장 + 발화 축(이름:근거) + 분류 근거 + 보조 텍스트 ──
    q_parts = []
    q_parts.extend(risk_sentences)
    for f in fired:
        q_parts.append(f["name"] + (f": {f['reason']}" if f["reason"] else ""))
    if classification:
        if classification.get("reason"):
            q_parts.append(str(classification["reason"]))
        for lb in (classification.get("labels") or []):
            q_parts.append(str(lb))
    # 확정 위험 문장이 있으면 사건 개요는 넣지 않는다. 개요가 임베딩을 묽게 만든다.
    # 위험 문장이 없을 때만 개요를 짧게 붙인다(텍스트 입력·무논란 영상).
    if base_text and not risk_sentences:
        q_parts.append(base_text[:800])
    query_text = "\n".join(p for p in q_parts if p and p.strip())[:max_query_len]

    # ── 키워드: 위험 문장 + 발화 축 이름/근거 + 분류 라벨에서 추출 ──
    kw = []
    for src in risk_sentences + [f["name"] for f in fired] + [f["reason"] for f in fired]:
        for t in extract_keywords(src):
            if t not in kw:
                kw.append(t)

    return {
        "query_text":      query_text or base_text[:max_query_len],
        "keywords":        kw[:30],
        "risk_categories": risk_categories,
        "fired_axes":      fired,
    }


# ════════════════════════════════════════════════════════════════
# 자체 테스트
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    demo_natam = {
        "A": {
            "A-02": {"level": "DANGER", "reason": "특정 성별을 비하하는 표현이 반복적으로 등장"},
            "A-06": {"level": "ALERT",  "reason": "댓글에서 집단 갈등이 증폭될 조짐"},
            "A-01": {"level": "CARE",   "reason": "약한 정치적 뉘앙스"},
        },
        "B": {
            "B-01": {"level": "ALERT",  "reason": "집단 대상 비하·혐오 표현 포함"},
            "B-03": {"level": "SAFE",   "reason": "저작권 요소 없음"},
        },
    }
    demo_sents = ["방송 중 특정 성별을 싸잡아 조롱하는 발언을 함"]
    demo_cls   = {"primary": "L03", "labels": ["L03 혐오 표현"], "reason": "혐오·비하 표현"}

    trig = build_trigger(demo_natam, risk_sentences=demo_sents, classification=demo_cls)
    print(json.dumps(trig, ensure_ascii=False, indent=2))
