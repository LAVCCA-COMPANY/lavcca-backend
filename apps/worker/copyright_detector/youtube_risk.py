"""
youtube_risk.py - 내부 위험도(risk_score)를 유튜브 스튜디오 관점의
'실제 조치 가능성'으로 번역한다.

왜 유형별로 다른가 (정확성의 핵심):
  유튜브 Content ID는 **오디오/영상 지문**만 자동 매칭한다.
    - 음악(commercial)  → Content ID 커버리지 매우 높음 → 자동 클레임 잘 걸림
    - 방송/영화/스톡영상 → 영상 지문 등록 많음        → 자동 클레임 잘 걸림
    - 정지 이미지/사진  → Content ID 거의 미적용       → 자동 조치보다 '수동 신고/법적' 리스크
    - 로고/상표         → 저작권이 아니라 '상표권' 영역 → 유튜브 저작권 시스템 밖
    - 폰트             → 유튜브 조치 대상 거의 아님

따라서 같은 risk_score라도 음악 0.8과 로고 0.8의 '유튜브에서 실제로 벌어질 일'은
전혀 다르다. 이 모듈이 그 차이를 확률/조치 라벨로 표현한다.

⚠️ 주의: 유튜브 내부 정책(권리자별 차단/수익화/추적)은 공개되지 않으므로
   여기 값은 결정론적 판정이 아니라 **경험적 추정치**다. 리포트에도 '추정'으로 표기한다.
"""
from typing import Dict, List

# 유형별 Content ID 자동 클레임 모델: (기저확률, confidence 가중)
#   claim_prob = min(cap, base + weight * confidence)
_CLAIM_MODEL = {
    "music":      (0.45, 0.50, 0.97),   # 상업음악: 자동 매칭 거의 확실
    "video_clip": (0.35, 0.50, 0.95),   # 방송/영화/스톡 영상: 높음
    "image":      (0.08, 0.25, 0.55),   # 정지 이미지: 자동 매칭 드묾(수동/법적 위주)
    "logo":       (0.03, 0.10, 0.30),   # 상표권 — 유튜브 저작권 시스템 밖
    "font":       (0.02, 0.05, 0.20),   # 유튜브 영향 거의 없음
    "meme":       (0.05, 0.15, 0.40),
}

# 조치 코드 → (한국어 라벨, 이모지, 심각도 정렬키)
OUTCOME_META = {
    "STRIKE":     ("저작권 경고(Strike) 위험", "⛔", 5),
    "BLOCK":      ("차단 가능(일부 국가/전체)", "🚫", 4),
    "DEMONETIZE": ("수익 이전·노란 딱지 가능성 높음", "🟡", 3),
    "REVENUE_RISK": ("수익 영향 가능(부분)", "🟠", 2),
    "MANUAL_RISK": ("수동 신고·법적 리스크(자동 조치 낮음)", "⚖️", 1),
    "TRADEMARK":  ("상표권 이슈(유튜브 저작권 시스템 밖)", "™️", 1),
    "MINIMAL":    ("유튜브 영향 거의 없음", "✅", 0),
    "SAFE":       ("영향 없음 예상", "✅", 0),
}


# 예측 조치 → risk_score 앵커 (하이브리드 재점수용).
# 유튜브에서 '실제로 벌어질 일'의 심각도를 점수로 옮긴 것.
OUTCOME_ANCHOR = {
    "STRIKE":       0.92,   # 저작권 경고 — 채널 위협
    "BLOCK":        0.85,   # 차단
    "DEMONETIZE":   0.65,   # 수익 이전·노란 딱지
    "REVENUE_RISK": 0.50,   # 부분 수익 영향
    "MANUAL_RISK":  0.42,   # 정지 이미지 등 — 자동조치 낮음, 수동/법적 리스크
    "TRADEMARK":    0.18,   # 상표권 — 유튜브 저작권 밖
    "MINIMAL":      0.15,
    "SAFE":         0.0,
}


def hybrid_risk_score(finding: Dict) -> tuple:
    """
    법적 risk_score를 '유튜브 스튜디오 실제 조치' 기준으로 재조정(하이브리드).

    - 음악/영상클립: Content ID 자동조치 대상 → 법적점수와 조치앵커 중 높은 쪽(유지~강화)
    - 정지 이미지/밈: 유튜브 자동조치 낮음 → 유형 기반 낮은 밴드로.
                     단 스톡/뉴스/스튜디오 등 '메이저'면 바닥값(≈MEDIUM 경계) 유지.
    - 로고: 상표권 → LOW 로 캡.

    반환: (조정된 risk_score, 설명에 덧붙일 주석)
    """
    from config import config
    ftype  = finding.get("finding_type") or finding.get("type") or "image"
    legal  = float(finding.get("risk_score", 0) or 0)
    src    = str(finding.get("source", ""))
    holder = str(finding.get("rights_holder", "") or finding.get("author", ""))
    outcome = youtube_impact_for_finding(finding)["yt_outcome"]
    anchor  = OUTCOME_ANCHOR.get(outcome, 0.15)
    major   = _is_major_rights_holder(src, holder)

    if ftype in ("music", "video_clip"):
        # 유튜브가 자동으로 잡는 유형 — 낮추지 않는다
        return round(max(legal, anchor), 3), ""
    if ftype in ("image", "meme"):
        # 정지 이미지 — 유튜브 자동조치 낮음. 메이저(게티/뉴스/스튜디오)면 경계값, 아니면 낮게.
        base = 0.45 if major else 0.30
        new  = min(config.pipeline.image_risk_cap, base)
        return round(new, 3), " [유튜브 자동조치 가능성 낮음 · 권리자 수동 신고(DMCA) 시 리스크]"
    if ftype == "logo":
        # 상표권 영역(유튜브 저작권 밖) → LOW 로만 보이게 (SAFE로 숨기지 않음)
        return round(min(legal, 0.20), 3), " [상표권 이슈 · 유튜브 저작권 시스템 밖]"
    return round(min(legal, 0.15), 3), ""


def _claim_probability(ftype: str, confidence: float, risk_score: float) -> float:
    """Content ID(또는 수동) 클레임이 걸릴 확률 추정 (0~1)."""
    base, weight, cap = _CLAIM_MODEL.get(ftype, (0.05, 0.15, 0.40))
    conf = confidence if confidence and confidence > 0 else risk_score
    return round(min(cap, base + weight * float(conf or 0)), 3)


def _is_major_rights_holder(source: str, rights_holder: str) -> bool:
    """방송/영화/스톡 등 '차단·경고' 정책을 자주 쓰는 권리자인지 (경험적)."""
    blob = f"{source} {rights_holder}".lower()
    hard = ("broadcast", "방송", "kbs", "mbc", "sbs", "jtbc", "tvn",
            "netflix", "disney", "hbo", "warner", "universal", "sony",
            "movie", "영화", "getty", "reuters", "ap ", "afp", "연합")
    return any(k in blob for k in hard)


def youtube_impact_for_finding(finding: Dict) -> Dict:
    """
    단일 finding → 유튜브 조치 예측.

    Returns:
        {yt_claim_prob: float, yt_outcome: code, yt_outcome_label: str, yt_emoji: str}
    """
    ftype = finding.get("finding_type") or finding.get("type") or "image"
    risk = float(finding.get("risk_score", 0) or 0)
    conf = float(finding.get("confidence_score", 0) or 0)
    source = str(finding.get("source", ""))
    holder = str(finding.get("rights_holder", "") or finding.get("author", ""))

    claim_prob = _claim_probability(ftype, conf, risk)
    major = _is_major_rights_holder(source, holder)

    if ftype in ("music", "video_clip"):
        if claim_prob >= 0.55:
            # 방송/영화 계열 + 매우 높은 위험 → 차단/경고 가능성 부각
            if major and risk >= 0.85 and ftype == "video_clip":
                outcome = "BLOCK"
            elif major and risk >= 0.9 and ftype == "video_clip":
                outcome = "STRIKE"
            else:
                outcome = "DEMONETIZE"
        elif claim_prob >= 0.3:
            outcome = "REVENUE_RISK"
        else:
            outcome = "MINIMAL"
    elif ftype in ("image", "meme"):
        # 스톡 에이전시 역검색 히트(예: Getty)는 자동 조치보다 수동/법적 리스크.
        # 정지 이미지는 claim_prob가 구조적으로 낮으므로 임계값도 낮게 잡는다.
        outcome = "MANUAL_RISK" if claim_prob >= 0.22 else "MINIMAL"
    elif ftype == "logo":
        outcome = "TRADEMARK"
    else:  # font 등
        outcome = "MINIMAL"

    label, emoji, _ = OUTCOME_META[outcome]
    return {
        "yt_claim_prob": claim_prob,
        "yt_outcome": outcome,
        "yt_outcome_label": label,
        "yt_emoji": emoji,
    }


def summarize_youtube_impact(findings: List[Dict]) -> Dict:
    """
    영상 전체 → 유튜브 스튜디오 관점 요약.

    Returns dict:
        claim_probability   : int(%)  — Content ID/수동 클레임이 하나라도 걸릴 확률
        monetization_impact : "높음|중간|낮음|없음" — 노란 딱지/수익 이전 가능성
        block_risk          : "있음|낮음|없음"
        strike_risk         : "있음|낮음|없음"
        primary_outcome     : code
        primary_label       : str
        primary_emoji       : str
        headline            : 한 줄 요약
        advice              : 권장 조치
    """
    if not findings:
        return {
            "claim_probability": 0,
            "monetization_impact": "없음",
            "block_risk": "없음",
            "strike_risk": "없음",
            "primary_outcome": "SAFE",
            "primary_label": OUTCOME_META["SAFE"][0],
            "primary_emoji": OUTCOME_META["SAFE"][1],
            "headline": "✅ 저작권 자동 조치 위험 낮음 — 문제될 항목 미검출",
            "advice": "현재 검출 기준으로는 수익화/저작권 조치 위험이 낮습니다.",
        }

    per = [youtube_impact_for_finding(f) for f in findings]

    # 클레임이 '하나라도' 걸릴 확률 = 1 - ∏(1 - p_i)  (독립 가정)
    no_claim = 1.0
    for p in per:
        no_claim *= (1.0 - p["yt_claim_prob"])
    claim_prob = min(0.97, 1.0 - no_claim)

    # 가장 심각한 예측 조치 선택
    primary = max(per, key=lambda x: (OUTCOME_META[x["yt_outcome"]][2],
                                      x["yt_claim_prob"]))
    outcomes = {p["yt_outcome"] for p in per}

    # 수익화 영향 등급 (노란 딱지 = 수익 이전/광고 제한)
    if claim_prob >= 0.6 or {"DEMONETIZE", "BLOCK", "STRIKE"} & outcomes:
        monet = "높음"
    elif claim_prob >= 0.3 or "REVENUE_RISK" in outcomes:
        monet = "중간"
    elif claim_prob >= 0.1:
        monet = "낮음"
    else:
        monet = "없음"

    block_risk = "있음" if "BLOCK" in outcomes else (
        "낮음" if any(p["yt_claim_prob"] >= 0.7 and
                     (findings[i].get("finding_type") or findings[i].get("type"))
                     == "video_clip" for i, p in enumerate(per)) else "없음")
    strike_risk = "있음" if "STRIKE" in outcomes else (
        "낮음" if "BLOCK" in outcomes else "없음")

    label, emoji, _ = OUTCOME_META[primary["yt_outcome"]]

    # 한 줄 요약
    pct = round(claim_prob * 100)
    if monet == "높음":
        headline = f"{emoji} 수익화 영향 가능성 높음 — Content ID 클레임 확률 약 {pct}%"
    elif monet == "중간":
        headline = f"🟠 수익 영향 가능 — 클레임 확률 약 {pct}%"
    elif monet == "낮음":
        headline = f"🔵 자동 조치 가능성 낮음 — 클레임 확률 약 {pct}%"
    else:
        headline = f"✅ 저작권 자동 조치 위험 낮음 — 클레임 확률 약 {pct}%"

    # 권장 조치
    tips = []
    if "music" in {f.get("finding_type") or f.get("type") for f in findings}:
        tips.append("음악 구간은 로열티프리/유튜브 오디오 보관함으로 교체 시 수익화 회복 가능")
    if block_risk != "없음" or strike_risk != "없음":
        tips.append("방송/영화 클립은 차단·경고 위험 → 해당 구간 삭제 권장")
    if primary["yt_outcome"] == "MANUAL_RISK":
        tips.append("이미지/사진은 자동 조치보다 권리자 수동 신고 대비(출처·라이선스 확인) 필요")
    advice = " · ".join(tips) if tips else "검출 구간을 검토해 필요 시 교체/삭제하세요."

    return {
        "claim_probability": pct,
        "monetization_impact": monet,
        "block_risk": block_risk,
        "strike_risk": strike_risk,
        "primary_outcome": primary["yt_outcome"],
        "primary_label": label,
        "primary_emoji": emoji,
        "headline": headline,
        "advice": advice,
    }
