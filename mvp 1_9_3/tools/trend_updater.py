# -*- coding: utf-8 -*-
"""
trend_updater.py  —  캔슬컬처/유튜버 논란 트렌드 자동 수집기

흐름:
  [1] 네이버 뉴스 API 로 최신 기사 수집
  [2] Gemini 로 기사 → {신규 차단 키워드, 신규 논란 사례} 구조화
  [3] 기존 rules.yaml / case_db.json 과 대조해 중복 제거
  [4] pending_updates.json 에 '검토 대기' 로 적재  (자동 반영 안 함)

수동 실행:
    set NAVER_CLIENT_ID=...
    set NAVER_CLIENT_SECRET=...
    python tools/trend_updater.py

이후 tools/review_updates.py 로 사람이 승인한 것만 실제 데이터에 반영한다.
"""

import os
import re
import sys
import json
import time
import html
from pathlib import Path
from datetime import datetime

import requests
import yaml
from google import genai

# 기존 시스템과 동일한 키/모델 재사용
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # mvp 1_9_3/ (paths·엔진 모듈)
import paths
from mvp_ver_1_9_2 import API_KEY, GEN_MODEL, _api_is_transient, _install_gemini_defaults, _thinking_config

# ────────────────────────────────────────────────
# 설정
# ────────────────────────────────────────────────
RULES_PATH      = paths.RULES_YAML
CASE_DB_PATH    = paths.CASE_DB_JSON
# case_db.json 은 1_9_3 에서 폐기됨(_deprecated/) — 유사 사례는 similar_case_engine + casedb/ 가 담당.
# 사례(cases)는 더 이상 pending 에 적재하지 않는다. 키워드·학습문장만 갱신.
CASES_ENABLED   = False
SAMPLES_PATH    = paths.SAMPLES_JSON      # ML 분류 학습 문장
PENDING_PATH    = paths.PENDING_UPDATES
SEEN_PATH       = paths.TREND_SEEN        # 이미 본 기사 링크 기록(중복 호출 방지)

# 분류 라벨 (controversy_samples.json 학습 문장용)
#   L05(기만)·L10(저작권)은 별도 Copyright Detector 담당 → 문장 분류 제외
SAMPLE_LABELS = {
    "L01": "직접적 욕설", "L02": "인신공격/비하", "L03": "혐오 표현",
    "L04": "허위 정보",   "L06": "위험/자해 행동", "L07": "정치적 편향",
    "L08": "사생활 침해", "L09": "성적 불쾌감",   "L11": "피해자 조롱",
    "L12": "해당 없음",
}

# ────────────────────────────────────────────────
# 검색 쿼리 — 크리에이터 논란/캔슬 신호 (네이버 '뉴스' 검색용)
#   ※ 네이버 뉴스 API는 '언론 기사'만 검색한다. 더쿠/네이트판/디시 등 커뮤니티,
#     영어권 소스는 닿지 않으므로 COMMUNITY_QUERIES / ENGLISH_QUERIES 로 따로 보관
#     (추후 커뮤니티/해외 수집기에서 사용). 실제 호출되는 건 SEARCH_QUERIES 뿐이다.
#   ※ 인물명 리스트가 없으므로 '[이름] 논란' 템플릿 대신 직군명(유튜버/스트리머…)으로 타깃팅.
# ────────────────────────────────────────────────
SEARCH_QUERIES = [
    # ── 1) 기본 신호 조합 (직군명 + 핵심 신호어) ──
    "유튜버 논란", "유튜버 사과문", "유튜버 입장문", "유튜버 해명",
    "유튜버 하차", "유튜버 출연 정지", "유튜버 활동 중단", "유튜버 잠정 은퇴",
    "유튜버 손절", "유튜버 의혹", "유튜버 허위",
    "인플루언서 논란", "인플루언서 사과", "인플루언서 의혹", "인플루언서 손절",
    "스트리머 논란", "스트리머 사과", "스트리머 하차",
    "BJ 논란", "BJ 사과",
    "크리에이터 논란", "연예인 캔슬",

    # ── 2) 논란 유형별 (직군명으로 묶어 크리에이터 사건만 좁힘) ──
    "유튜버 학폭", "유튜버 음주운전", "유튜버 마약", "유튜버 도박",
    "유튜버 사기", "유튜버 갑질", "유튜버 막말", "유튜버 미투",
    "유튜버 성희롱", "유튜버 동물학대", "유튜버 정치색", "유튜버 역사왜곡",
    "인플루언서 학폭", "인플루언서 표절", "인플루언서 조작",
    "스트리머 도박", "스트리머 막말",

    # ── 3) 캔슬 발생(결과·제재) 신호 — 양성 라벨 핵심 ──
    "뒷광고 논란", "유료광고 미표기", "유튜버 광고 중단", "유튜버 모델 교체",
    "유튜버 채널 폐쇄", "유튜버 영상 삭제", "유튜버 노란딱지",
    "연예인 불매", "연예인 퇴출", "방송 하차",

    # ── 4) 커뮤니티발 사건이 '뉴스화'된 신호 (뉴스 API로 잡히는 범위) ──
    "사이버 렉카 논란", "유튜버 폭로", "학폭 폭로",
]

# (참고용) 분류기 TF-IDF 트리거 사전에 직접 넣으면 좋은 '그 자체로 위험한' 어휘.
#   trend_updater 는 LLM 추출을 이 주제로 유도하는 힌트로만 사용한다(아래 프롬프트 참고).
TRIGGER_KEYWORDS = [
    # 과거사/인성
    "학폭", "학교폭력", "일진", "미투", "성추문", "성희롱", "성폭력", "데이트폭력", "가스라이팅",
    # 법적/범죄
    "음주운전", "도박", "마약", "탈세", "사기", "횡령", "명예훼손", "고소",
    # 발언/태도
    "막말", "실언", "갑질", "차별 발언", "혐오 발언", "비하", "조롱", "2차 가해",
    # 정치/이념
    "정치색", "역사왜곡", "일베", "극우", "페미 논란", "젠더 논란", "친일", "동북공정",
    # 콘텐츠 자체
    "뒷광고", "광고 미표기", "표절", "조작", "주작", "자막 조작", "허위 정보", "선정성", "동물학대",
    # 캔슬 결과/제재
    "중도하차", "광고 계약 해지", "모델 교체", "불매", "보이콧", "퇴출", "구독 취소",
    "채널 폐쇄", "영상 삭제", "노란딱지", "수익 정지",
]

# (미사용 — 네이버 '뉴스' API로는 닿지 않음. 추후 커뮤니티/해외 수집기에서 활용)
COMMUNITY_QUERIES = [
    "더쿠 폭로", "네이트판 유튜버", "디시 유튜버 박제",
    "유튜버 목격담", "유튜버 지인 인증", "사이버 렉카",
]
ENGLISH_QUERIES = [
    "youtuber controversy", "creator apology", "youtuber cancelled",
    "youtuber backlash", "called out", "exposed",
    "dropped by sponsor", "demonetized controversy", "under fire",
]

NAVER_NEWS_URL  = "https://openapi.naver.com/v1/search/news.json"
ITEMS_PER_QUERY = 12          # 쿼리당 가져올 기사 수 (쿼리가 많아져 1개당은 줄임, 최대 100)
MAX_ARTICLES_TO_LLM = 80      # LLM 에 넘길 최대 기사 수 (헤드라인 다이제스트라 토큰 저렴)

# 1차(최신)에서 새로 학습할 게 없을 때 → 예전 기사를 뒤지는 2차 수집용 페이지 오프셋.
#   네이버 검색 API의 start(1~1000)를 키워 더 뒤쪽(=예전) 기사까지 훑는다.
#   얕은 페이지(21~101)는 직전 실행에서 이미 본 경우가 많으므로, 점점 더 깊이(예전)까지 내려간다.
#   ※ 이미 본 기사는 .trend_seen.json 으로 걸러지므로 깊이 파도 중복은 안 쌓인다.
DEEP_START_OFFSETS = (21, 61, 101, 161, 241, 341, 461, 601, 781, 981)

# rules.yaml 에 이미 존재하는 정책 카테고리 (LLM 이 이 안에서만 분류하도록 강제)
RULE_CATEGORIES = [
    "violent_threat", "discrimination", "deceptive_marketing",
    "accountability_evasion", "illegal_promotion", "appearance_insult",
]
CASE_TYPES = [
    "advertising_issue", "hate_speech", "attitude_issue", "illegal_issue",
]


# ────────────────────────────────────────────────
# 1) 네이버 뉴스 수집
# ────────────────────────────────────────────────
def _strip_tags(text: str) -> str:
    """네이버가 돌려주는 <b> 태그 / HTML 엔티티 제거."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def fetch_naver_news(client_id: str, client_secret: str,
                     sort: str = "date", start_offsets=(1,)) -> list:
    """
    네이버 뉴스 수집. 이미 본 기사(.trend_seen.json)는 제외.
      sort="date"  : 최신순,  start_offsets=(1,)          → 최신 기사
      sort="date"  : 최신순,  start_offsets=DEEP_START... → 더 뒤(예전) 기사
    """
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    seen_links = _load_seen()
    per_query   = {}        # query → [기사…]  (쿼리별로 모아 라운드로빈 분배)
    picked_links = set()    # 같은 실행 내 중복 방지

    for query in SEARCH_QUERIES:
        bucket = []
        for start in start_offsets:
            params = {"query": query, "display": ITEMS_PER_QUERY,
                      "sort": sort, "start": start}
            try:
                resp = requests.get(NAVER_NEWS_URL, headers=headers, params=params, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                print(f"   ⚠️ '{query}' (start={start}) 검색 실패: {e}")
                continue

            items = resp.json().get("items", [])
            if not items:
                break   # 더 뒤 페이지는 없음 → 다음 쿼리로

            for item in items:
                link = item.get("link", "")
                if not link or link in seen_links or link in picked_links:
                    continue
                picked_links.add(link)
                bucket.append({
                    "query":   query,
                    "title":   _strip_tags(item.get("title", "")),
                    "summary": _strip_tags(item.get("description", "")),
                    "link":    link,
                    "pubDate": item.get("pubDate", ""),
                })
            time.sleep(0.2)   # API 매너
        per_query[query] = bucket

    # 쿼리별 라운드로빈 병합 → 특정 쿼리가 LLM 예산을 독점하지 않게 공평 분배
    merged, i = [], 0
    while True:
        added = False
        for q in SEARCH_QUERIES:
            b = per_query.get(q) or []
            if i < len(b):
                merged.append(b[i]); added = True
        if not added:
            break
        i += 1

    selected = merged[:MAX_ARTICLES_TO_LLM]
    # LLM 에 실제로 넘긴 기사만 'seen' 으로 기록 → 잘린 나머지는 다음 실행에서 재시도(유실 방지)
    _save_seen(seen_links | {a["link"] for a in selected})
    print(f"📰 신규 기사 {len(merged)}건 발견 → LLM 분석 {len(selected)}건 (공평 분배·중복 제외)")
    return selected


# ────────────────────────────────────────────────
# 2) Gemini 구조화
# ────────────────────────────────────────────────
def structure_with_gemini(articles: list) -> dict:
    if not articles:
        return {"keywords": [], "cases": [], "samples": []}

    client = _install_gemini_defaults(genai.Client(api_key=API_KEY))
    digest = "\n".join(
        f"- [{a['title']}] {a['summary']}" for a in articles
    )

    label_desc = ", ".join(f"{k} {v}" for k, v in SAMPLE_LABELS.items())
    known_hint = ", ".join(TRIGGER_KEYWORDS)
    prompt = (
        "너는 한국 유튜버/인플루언서 논란(캔슬컬처)을 모니터링하는 분석가다.\n"
        "아래 최신 뉴스 헤드라인/요약을 보고 세 가지를 JSON 으로 추출하라.\n\n"
        "1) keywords: 콘텐츠 필터에 추가할 만한 '신규 유행 위험 용어/밈/표현'. "
        "일반 명사 말고, 비하·혐오·기만·위협 맥락에서 새로 퍼지는 표현만.\n"
        f"   - 이미 알려진 위험 어휘({known_hint})는 그대로 다시 넣지 말고, "
        "이런 주제의 '새로운 변형·은어·밈·신조어'가 보이면 그것을 우선 추출.\n"
        f"   - category 는 반드시 다음 중 하나: {RULE_CATEGORIES}\n"
        "   - severity 는 CRITICAL/HIGH/MEDIUM 중 하나.\n\n"
        "2) cases: 이번에 새로 불거진 '구체적 논란 사례'. "
        "유튜버/인플루언서 본인의 논란만. 단순 사회 뉴스 제외.\n"
        f"   - controversy_type 는 반드시 다음 중 하나: {CASE_TYPES}\n"
        "   - spread_stage 는 early/community/media/legal 중 하나.\n\n"
        "3) samples: ML 분류기 학습용 '예시 문장'. 이번 논란에서 문제가 된 발언을 "
        "재구성하거나, 같은 유형의 위험 발화를 자연스러운 한국어 구어체 한 문장으로 작성. "
        "실제 영상 자막에 나올 법한 말투로. (뉴스 문어체 X)\n"
        f"   - labels 는 다음 중 1~3개: {label_desc}\n"
        "   - 위험하지 않은 평범한 문장이면 labels=[\"L12\"].\n\n"
        "[출력 형식 — JSON 객체만, 설명·마크다운 없이]\n"
        '{"keywords":[{"keyword":"","category":"","severity":"","rationale":""}],'
        '"cases":[{"title":"","controversy_type":"","summary":"","spread_stage":""}],'
        '"samples":[{"text":"","labels":["L03"]}]}\n\n'
        f"[뉴스]\n{digest}"
    )

    # JSON 깨짐 재시도 + 마크다운 펜스 제거. 503/429 재시도·대체 모델은 클라이언트 래퍼가 한다.
    data = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=GEN_MODEL,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                    "max_output_tokens": 8192,
                    "thinking_config": _thinking_config(False),
                },
            )
            txt = (resp.text or "").strip()
            txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', txt, flags=re.MULTILINE).strip()
            data = json.loads(txt)
            break
        except Exception as e:
            if attempt < 3 and not _api_is_transient(e):
                print(f"   ⚠️ Gemini 구조화 재시도 ({attempt+1}/3): {e} — 2초 후")
                time.sleep(2)
                continue
            print(f"   ⚠️ Gemini 구조화 최종 실패: {e}")
            return {"keywords": [], "cases": [], "samples": []}

    return {
        "keywords": data.get("keywords", []) or [],
        "cases":    data.get("cases", []) or [],
        "samples":  data.get("samples", []) or [],
    }


# ────────────────────────────────────────────────
# 3) 중복 제거 (기존 데이터 대조)
# ────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def load_existing():
    keywords = set()
    if RULES_PATH.exists():
        rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
        for pol in (rules.get("risk_policies") or {}).values():
            for kw in pol.get("keywords", []):
                keywords.add(_norm(kw))

    case_titles = set()
    if CASE_DB_PATH.exists():
        cases = json.loads(CASE_DB_PATH.read_text(encoding="utf-8"))
        for c in cases:
            case_titles.add(_norm(c.get("title", "")))

    sample_texts = set()
    if SAMPLES_PATH.exists():
        samples = json.loads(SAMPLES_PATH.read_text(encoding="utf-8-sig"))
        for s in samples:
            sample_texts.add(_norm(s.get("text", "")))
    return keywords, case_titles, sample_texts


def dedup(structured: dict) -> dict:
    existing_kw, existing_titles, existing_samples = load_existing()

    new_keywords = []
    for k in structured["keywords"]:
        kw = (k.get("keyword") or "").strip()
        cat = k.get("category")
        if not kw or cat not in RULE_CATEGORIES:
            continue
        if _norm(kw) in existing_kw:
            continue
        existing_kw.add(_norm(kw))  # 같은 배치 내 중복도 차단
        new_keywords.append(k)

    new_cases = []
    for c in (structured["cases"] if CASES_ENABLED else []):
        title = (c.get("title") or "").strip()
        if not title or c.get("controversy_type") not in CASE_TYPES:
            continue
        if _norm(title) in existing_titles:
            continue
        existing_titles.add(_norm(title))
        new_cases.append(c)

    new_samples = []
    for s in structured.get("samples", []):
        text   = (s.get("text") or "").strip()
        labels = [l for l in (s.get("labels") or []) if l in SAMPLE_LABELS]
        if not text or not labels:
            continue
        if _norm(text) in existing_samples:
            continue
        existing_samples.add(_norm(text))
        new_samples.append({"text": text, "labels": labels})

    return {"keywords": new_keywords, "cases": new_cases, "samples": new_samples}


# ────────────────────────────────────────────────
# seen-link 영속화
# ────────────────────────────────────────────────
def _load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(links: set):
    # 너무 커지지 않게 최근 2000개만 유지
    SEEN_PATH.write_text(
        json.dumps(list(links)[-2000:], ensure_ascii=False), encoding="utf-8"
    )


# ────────────────────────────────────────────────
# 4) pending 적재
# ────────────────────────────────────────────────
def append_to_pending(deduped: dict):
    pending = {"runs": []}
    if PENDING_PATH.exists():
        try:
            pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    pending.setdefault("runs", [])

    if not deduped["keywords"] and not deduped["cases"] and not deduped["samples"]:
        print("✨ 신규 항목 없음. pending 변경 없음.")
        return

    pending["runs"].append({
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",          # pending → review_updates.py 에서 처리
        "keywords": deduped["keywords"],
        "cases": deduped["cases"],
        "samples": deduped["samples"],
    })
    PENDING_PATH.write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"📥 검토 대기 적재 → {PENDING_PATH.name}")
    print(f"   신규 키워드 {len(deduped['keywords'])}개 / 신규 사례 {len(deduped['cases'])}개"
          f" / 신규 학습문장 {len(deduped['samples'])}개")
    print("   다음 단계:  python tools/review_updates.py")


# ────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────
def main():
    cid = os.getenv("NAVER_CLIENT_ID")
    csec = os.getenv("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        print("❌ 환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 필요합니다.")
        print("   https://developers.naver.com 에서 '검색' API 등록 후 발급.")
        print('   PowerShell:  $env:NAVER_CLIENT_ID="..."; $env:NAVER_CLIENT_SECRET="..."')
        return

    print("=" * 60)
    print("🔎 캔슬컬처 트렌드 수집 시작")
    print("=" * 60)

    def _collect(label, **fetch_kw):
        print(f"\n── {label} ──")
        articles   = fetch_naver_news(cid, csec, **fetch_kw)
        structured = structure_with_gemini(articles)
        print(f"🤖 Gemini 추출: 키워드 {len(structured['keywords'])} / 사례 {len(structured['cases'])}"
              f" / 학습문장 {len(structured['samples'])} (중복 제거 전)")
        return dedup(structured)

    def _is_empty(d):
        return not d["keywords"] and not d["cases"] and not d["samples"]

    # 1차: 최신 기사
    deduped = _collect("1차 · 최신 기사", sort="date", start_offsets=(1,))

    # 2차: 최신에서 새로 학습할 게 없으면 → 예전(더 뒤 페이지) 기사 수집
    if _is_empty(deduped):
        print("\n💡 최신 기사에 신규 정보 없음 → 예전 기사에서 추가 수집 시도")
        deduped = _collect("2차 · 예전 기사", sort="date", start_offsets=DEEP_START_OFFSETS)

    append_to_pending(deduped)


if __name__ == "__main__":
    main()
