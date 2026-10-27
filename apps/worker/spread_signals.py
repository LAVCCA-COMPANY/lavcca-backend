# -*- coding: utf-8 -*-
"""
spread_signals.py — 외부 확산 신호 수집 (v1.9.3 '확산 단계 판정' 보강)

■ 2층 구조 (creator vs. incident 분리)
  A층 — 크리에이터 전반 관심도 (이름만 검색)
        · 네이버 DataLab 검색어트렌드   → NAVER API 키 없으면 '판단 보류'
  B층 — 이번 사안 확산도 (이름 + 사건 지문 + 최신 시점)
        · 2차 유튜브(이슈채널/사이버렉카)  — yt-dlp 검색 (키 불필요)
        · 기사화                          — 네이버 뉴스 API(키) / 없으면 구글뉴스 RSS
        · 커뮤니티 언급                    — 네이버 카페·블로그 API → 키 없으면 '판단 보류'
        · 나무위키 논란 문서               — HTTP best-effort

■ 원칙
  · 모든 네트워크 호출은 실패해도 예외를 밖으로 던지지 않는다(짧은 타임아웃 + try/except).
  · 키·데이터가 없으면 '판단 보류'로 정직하게 표기(가짜 SAFE 금지 — B-03 철학과 동일).
  · 외부 신호는 확산 단계를 '올리기만' 한다(엔진 youtube_meta 판정을 낮추지 않음).

외부에서 쓰는 진입점:
  · enrich(report)          — report["spread_external"] 채우고 spread_stage 단계 상향(blend)
  · build_block_md(report)  — 리포트 3-4 '외부 확산 신호' 섹션 마크다운 생성
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── 설정(환경변수로 조정 가능) ─────────────────────────────────────────
ENABLED          = os.getenv("SPREAD_SIGNALS_ENABLED", "1") != "0"
HTTP_TIMEOUT     = float(os.getenv("SPREAD_HTTP_TIMEOUT", "10"))
YT_SEARCH_N      = int(os.getenv("SPREAD_YT_SEARCH_N", "20"))
NEWS_RECENT_DAYS = int(os.getenv("SPREAD_NEWS_RECENT_DAYS", "30"))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ── HTTP 클라이언트 (requests 우선, 없으면 urllib) ─────────────────────
try:
    import requests
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False
    import urllib.request
    import urllib.parse


def _http_get(url: str, headers: dict | None = None, timeout: float | None = None):
    """(status_code, text) 반환. 실패하면 (None, None)."""
    h = {"User-Agent": _UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    to = timeout or HTTP_TIMEOUT
    try:
        if _HAS_REQUESTS:
            r = requests.get(url, headers=h, timeout=to)
            return r.status_code, r.text
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=to) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")
    except Exception:
        return None, None


def _http_post_json(url: str, headers: dict, payload: dict, timeout: float | None = None):
    """(status_code, text) 반환. 실패하면 (None, None)."""
    h = {"User-Agent": _UA, "Content-Type": "application/json"}
    h.update(headers or {})
    to = timeout or HTTP_TIMEOUT
    body = json.dumps(payload).encode("utf-8")
    try:
        if _HAS_REQUESTS:
            r = requests.post(url, headers=h, data=body, timeout=to)
            return r.status_code, r.text
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=to) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")
    except Exception:
        return None, None


def _quote(s: str) -> str:
    try:
        if _HAS_REQUESTS:
            from urllib.parse import quote
            return quote(s)
        return urllib.parse.quote(s)
    except Exception:
        return s


# ── NAVER API 키 로드 (os.environ → 로컬 .env 순) ──────────────────────
def _naver_keys() -> tuple[str, str]:
    cid  = (os.getenv("NAVER_CLIENT_ID") or "").strip()
    csec = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
    if cid and csec:
        return cid, csec
    envf = SCRIPT_DIR / ".env"
    if envf.exists():
        try:
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "NAVER_CLIENT_ID" and not cid:
                    cid = v
                elif k == "NAVER_CLIENT_SECRET" and not csec:
                    csec = v
        except Exception:
            pass
    return cid, csec


def _has_naver() -> bool:
    cid, csec = _naver_keys()
    return bool(cid and csec)


# ── 확산 단계 랭크 (외부 신호는 '올리기만') ────────────────────────────
_STAGE_RANK = {"Unknown": -1, "Early": 0, "Mid": 1, "Late": 2}


def _max_stage(a: str, b: str) -> str:
    ra, rb = _STAGE_RANK.get(a, -1), _STAGE_RANK.get(b, -1)
    return a if ra >= rb else b


# ════════════════════════════════════════════════════════════════════
# 1) 사건 지문(fingerprint) — 크리에이터명 + 이번 사안 키워드
# ════════════════════════════════════════════════════════════════════
# 제목에서 키워드로 쓰기엔 너무 일반적인 단어(사건 특정력 낮음)
_KW_STOP = {
    # 논란/미디어 일반어
    "논란", "영상", "유튜브", "유튜버", "해명", "사건", "공식", "입장", "근황",
    "전말", "정리", "이슈", "속보", "충격", "결국", "라이브", "방송", "관련",
    "때문", "그것", "오늘", "지금", "이번", "저격", "사과", "feat", "shorts",
    "풀버전", "하이라이트", "리액션", "반응", "모음", "정주행", "최초", "단독",
    "공개", "발표", "소식", "출연", "인터뷰", "예능", "화보", "직캠",
    # 연예인/크리에이터 페이지에 흔해 '사건 특정력' 없는 단어 (오탐 유발)
    "광고", "계약", "협찬", "데뷔", "활동", "컴백", "신곡", "앨범", "콘서트",
    "공연", "채널", "구독", "조회수", "댓글", "브이로그", "일상", "먹방",
}


_TAGRE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _TAGRE.sub("", s or "")


def _kw_hit(text: str, keywords: list[str]) -> bool:
    """사건 키워드가 텍스트(제목/스니펫)에 실제로 들어있는지. 키워드 없으면 True(전반)."""
    if not keywords:
        return True
    t = text or ""
    return any(k in t for k in keywords)


def extract_fingerprint(report: dict) -> dict:
    """report → {creator, title, upload_date(YYYYMMDD), video_id, keywords[]}"""
    yt = report.get("youtube_meta") or {}
    creator = (yt.get("channel") or "").strip()
    title   = (yt.get("title") or "").strip()
    upload  = (yt.get("upload_date") or "").strip()

    toks = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    seen, kws = set(), []
    for t in toks:
        low = t.lower()
        if low in _KW_STOP or t == creator or low in seen:
            continue
        seen.add(low)
        kws.append(t)
        if len(kws) >= 3:
            break

    return {
        "creator":     creator,
        "title":       title,
        "upload_date": upload,
        "video_id":    yt.get("video_id", ""),
        "keywords":    kws,
    }


# ════════════════════════════════════════════════════════════════════
# 2) B층 — 2차 유튜브(이슈채널/사이버렉카) : yt-dlp 검색 (키 불필요)
# ════════════════════════════════════════════════════════════════════
def collect_secondary_youtube(creator: str, keywords: list[str], n: int = YT_SEARCH_N) -> dict:
    """원작자 본인 채널을 제외한 '다른 채널'이 올린 관련 영상 수·합산 조회수."""
    try:
        import yt_dlp
    except Exception:
        return {"status": "na", "count": 0, "note": "yt-dlp 미설치"}

    kw = keywords[0] if keywords else ""
    # 이번 사안으로 좁히기: 크리에이터 + 사건 키워드. 키워드 없으면 크리에이터 전반(정확도↓).
    if creator and kw:
        query, scope = f"{creator} {kw} 논란", "incident"
    elif creator:
        query, scope = f"{creator} 논란", "creator"
    elif kw:
        query, scope = f"{kw} 논란", "incident"
    else:
        return {"status": "na", "count": 0, "note": "검색어 없음"}

    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
        entries = info.get("entries") or []
    except Exception as e:
        return {"status": "error", "count": 0, "note": f"검색 실패: {e}"}

    cnorm = re.sub(r"\s+", "", creator).lower()
    others = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ch = (e.get("channel") or e.get("uploader") or "").strip()
        if not ch:
            continue
        if creator and re.sub(r"\s+", "", ch).lower() == cnorm:
            continue  # 원작자 본인 업로드 제외
        others.append({
            "title":   (e.get("title") or "").strip(),
            "channel": ch,
            "views":   int(e.get("view_count") or 0),
            "url":     e.get("url") or "",
        })

    others.sort(key=lambda x: -x["views"])

    # ── 이번 사안 특정: YouTube 검색은 느슨해 아무 키워드로도 크리에이터 영상을
    #    N개 반환한다. 따라서 '제목에 사건 키워드가 실제로 들어간' 영상만 센다.
    if scope == "incident" and keywords:
        relevant = [o for o in others if any(k in o["title"] for k in keywords)]
    else:
        relevant = others  # 키워드 없음 → 크리에이터 전반(정확도↓)

    counted     = relevant if (scope == "incident" and keywords) else others
    total_views = sum(o["views"] for o in counted)
    return {
        "status":       "ok",
        "count":        len(counted),          # 사건 특정 개수(또는 전반 개수)
        "raw_count":    len(others),           # 검색이 반환한 전체(참고)
        "total_views":  total_views,
        "items":        (counted or others)[:5],
        "query":        query,
        "scope":        scope,                  # incident=사건 특정 / creator=전반
        "capped":       scope == "creator" and len(others) >= n,
    }


# ════════════════════════════════════════════════════════════════════
# 3) B층 — 기사화 : 네이버 뉴스 API(키) / 없으면 구글뉴스 RSS
# ════════════════════════════════════════════════════════════════════
def _parse_rfc822(s: str):
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def collect_news(creator: str, keywords: list[str], recent_days: int = NEWS_RECENT_DAYS) -> dict:
    base = creator or (" ".join(keywords[:2]) if keywords else "")
    if not base:
        return {"status": "na", "count": 0, "note": "검색어 없음"}
    kw = keywords[0] if keywords else ""
    query = f'"{base}" {kw} 논란'.replace("  ", " ").strip()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=recent_days)

    # (a) 네이버 뉴스 검색 API
    cid, csec = _naver_keys()
    if cid and csec:
        url = ("https://openapi.naver.com/v1/search/news.json"
               f"?query={_quote(query)}&display=50&sort=date")
        st, txt = _http_get(url, headers={
            "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec})
        if st == 200 and txt:
            try:
                data = json.loads(txt)
                items = data.get("items", [])
                recent, tops = 0, []
                for it in items:
                    title = _strip_tags(it.get("title", ""))
                    dt = _parse_rfc822(it.get("pubDate", ""))
                    # 최근 + 제목에 사건 키워드 포함(= 이번 사안 특정)
                    is_recent = bool(dt and dt >= cutoff) and _kw_hit(title, keywords)
                    if is_recent:
                        recent += 1
                    if len(tops) < 5:
                        tops.append({"title": title, "date": it.get("pubDate", ""),
                                     "recent": is_recent})
                return {"status": "ok", "source": "naver",
                        "total": int(data.get("total", len(items))),
                        "recent": recent, "items": tops, "query": query,
                        "scope": "incident" if keywords else "creator"}
            except Exception:
                pass  # 파싱 실패 시 RSS 폴백

    # (b) 구글뉴스 RSS (키 불필요)
    rss = (f"https://news.google.com/rss/search?q={_quote(query)}"
           "&hl=ko&gl=KR&ceid=KR:ko")
    st, txt = _http_get(rss)
    if st != 200 or not txt:
        return {"status": "error", "count": 0, "note": "뉴스 검색 실패", "query": query}
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(txt.encode("utf-8"))
        items = root.findall(".//item")
        recent, tops = 0, []
        for it in items:
            title = (it.findtext("title") or "").strip()
            dt = _parse_rfc822(it.findtext("pubDate") or "")
            is_recent = bool(dt and dt >= cutoff) and _kw_hit(title, keywords)
            if is_recent:
                recent += 1
            if len(tops) < 5:
                tops.append({"title": title, "date": it.findtext("pubDate") or "",
                             "recent": is_recent})
        return {"status": "ok", "source": "gnews",
                "total": len(items), "recent": recent, "items": tops, "query": query,
                "scope": "incident" if keywords else "creator"}
    except Exception as e:
        return {"status": "error", "count": 0, "note": f"RSS 파싱 실패: {e}", "query": query}


# ════════════════════════════════════════════════════════════════════
# 4) A층 — 검색 관심도 급등 : 네이버 DataLab (키 필요, 없으면 판단 보류)
# ════════════════════════════════════════════════════════════════════
def collect_search_interest(creator: str, recent_days: int = NEWS_RECENT_DAYS) -> dict:
    if not creator:
        return {"status": "na", "note": "크리에이터명 없음"}
    cid, csec = _naver_keys()
    if not (cid and csec):
        return {"status": "na", "note": "NAVER API 키 없음 → 판단 보류"}

    end = datetime.now()
    start = end - timedelta(days=max(recent_days, 30))
    payload = {
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate":   end.strftime("%Y-%m-%d"),
        "timeUnit":  "date",
        "keywordGroups": [{"groupName": creator, "keywords": [creator]}],
    }
    st, txt = _http_post_json(
        "https://openapi.naver.com/v1/datalab/search",
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
        payload=payload)
    if st == 401 or st == 403:
        return {"status": "na",
                "note": "DataLab(검색어트렌드) API 미승인 — 네이버 앱에 '데이터랩' 추가 필요"}
    if st != 200 or not txt:
        return {"status": "error", "note": f"DataLab 호출 실패(status={st})"}
    try:
        data = json.loads(txt)
        pts = (data.get("results") or [{}])[0].get("data", [])
        ratios = [float(p.get("ratio", 0)) for p in pts]
        if len(ratios) < 5:
            return {"status": "na", "note": "검색량 데이터 부족"}
        recent = ratios[-3:]
        base = ratios[:-3] or ratios
        base_mean = (sum(base) / len(base)) or 1.0
        spike = (sum(recent) / len(recent)) / base_mean
        level = "급등" if spike >= 2.0 else ("상승" if spike >= 1.3 else "평이")
        return {"status": "ok", "spike": round(spike, 2), "level": level,
                "recent_mean": round(sum(recent) / len(recent), 1),
                "base_mean": round(base_mean, 1)}
    except Exception as e:
        return {"status": "error", "note": f"DataLab 파싱 실패: {e}"}


# ════════════════════════════════════════════════════════════════════
# 5) B층 — 커뮤니티 언급 : 네이버 카페·블로그 API (키 필요, 없으면 판단 보류)
# ════════════════════════════════════════════════════════════════════
def collect_community(creator: str, keywords: list[str]) -> dict:
    cid, csec = _naver_keys()
    if not (cid and csec):
        return {"status": "na", "note": "NAVER API 키 없음 → 판단 보류"}
    base = creator or (" ".join(keywords[:2]) if keywords else "")
    if not base:
        return {"status": "na", "note": "검색어 없음"}
    kw = keywords[0] if keywords else ""
    query = f"{base} {kw} 논란".replace("  ", " ").strip()
    hdr = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}

    # 네이버 검색 total 은 느슨(이름만 맞아도 집계)하므로, 반환 글의 제목/스니펫에
    #   '사건 키워드'가 실제로 들어간 글만 센다(= 이번 사안 특정). 키워드 없으면 전반.
    breakdown, raw_totals = {}, {}
    for kind, api in (("카페", "cafearticle"), ("블로그", "blog")):
        url = (f"https://openapi.naver.com/v1/search/{api}.json"
               f"?query={_quote(query)}&display=30&sort=date")
        st, txt = _http_get(url, headers=hdr)
        rel = 0
        if st == 200 and txt:
            try:
                data = json.loads(txt)
                raw_totals[kind] = int(data.get("total", 0))
                for it in data.get("items", []):
                    blob = _strip_tags(it.get("title", "")) + " " + _strip_tags(it.get("description", ""))
                    if _kw_hit(blob, keywords):
                        rel += 1
            except Exception:
                pass
        breakdown[kind] = rel
    count = sum(breakdown.values())
    return {"status": "ok", "count": count, "breakdown": breakdown,
            "raw_total": sum(raw_totals.values()), "query": query,
            "scope": "incident" if keywords else "creator"}


# ════════════════════════════════════════════════════════════════════
# 6) B층 — 나무위키 논란 문서 : HTTP best-effort (키 불필요, SPA라 근사)
# ════════════════════════════════════════════════════════════════════
def collect_namuwiki(creator: str, keywords: list[str] | None = None) -> dict:
    keywords = keywords or []
    if not creator:
        return {"status": "na", "note": "크리에이터명 없음"}
    url = f"https://namu.wiki/w/{_quote(creator)}"
    st, txt = _http_get(url)
    if st != 200 or not txt:
        return {"status": "na", "note": f"접근 실패/문서 없음(status={st})"}
    exists = len(txt) > 5000
    low = txt.lower()

    # SPA — 본문이 유니코드 이스케이프(\uxxxx)로 박혀 있을 수 있어 두 형태 모두 탐색
    def _has(term: str) -> bool:
        if not term:
            return False
        if term in txt:
            return True
        try:
            return term.encode("unicode_escape").decode().lower() in low
        except Exception:
            return False

    # controversy_section = 크리에이터가 '논란 이력'을 가졌는가(전반, 과거 포함)
    has_controversy = any(_has(m) for m in ("논란", "사건 사고", "사건·사고"))
    # incident_mentioned = '이번 사안' 키워드가 문서에 실제로 기록됐는가(사건 특정)
    matched = [k for k in keywords if _has(k)]
    return {"status": "ok", "exists": exists,
            "controversy_section": bool(has_controversy),
            "incident_mentioned": bool(matched),
            "matched_keywords": matched, "url": url}


# ════════════════════════════════════════════════════════════════════
# 7) 종합 판정 (수집 → 2층 결과 + 단계 블렌딩)
# ════════════════════════════════════════════════════════════════════
def assess(report: dict) -> dict:
    if not ENABLED:
        return {"status": "disabled"}

    fp = extract_fingerprint(report)
    creator, kws = fp["creator"], fp["keywords"]
    if not creator and not kws:
        return {"status": "na", "note": "크리에이터/키워드 없음(로컬 파일·텍스트 입력)",
                "fingerprint": fp}

    a_search = collect_search_interest(creator)
    b_yt     = collect_secondary_youtube(creator, kws)
    b_news   = collect_news(creator, kws)
    b_comm   = collect_community(creator, kws)
    b_namu   = collect_namuwiki(creator, kws)

    # ── B층 신호 카운트(실제 확산 근거가 켜진 카테고리 수) ──
    c2   = b_yt.get("count", 0) if b_yt.get("status") == "ok" else 0
    nrec = b_news.get("recent", 0) if b_news.get("status") == "ok" else 0
    comm = b_comm.get("count", 0) if b_comm.get("status") == "ok" else 0
    # 나무위키: '이번 사안' 키워드가 문서에 기록됐는지(사건 특정)만 확산 신호로 인정.
    #   단순 '논란 이력 섹션 존재'는 과거 논란일 수 있어 단계 상향에 쓰지 않는다.
    namu_incident = bool(b_namu.get("incident_mentioned")) if b_namu.get("status") == "ok" else False
    spike = a_search.get("spike") if a_search.get("status") == "ok" else None

    # 커뮤니티는 흔한 단어 우연매칭 노이즈가 있어 가장 보수적으로(≥3만 신호로 인정).
    fired = sum([c2 >= 1, nrec >= 1, comm >= 3, namu_incident])

    # ── 외부 확산 단계(올리기만) ──
    #   count 는 모두 '사건 키워드 실매칭' 기준(느슨 total 아님). 커뮤니티는 샘플(≤60) 중 매칭수.
    #   커뮤니티는 단독으로 단계를 올리려면 ≥10 필요(블로그 우연매칭 방지).
    ext = "Early"
    reasons = []
    if c2 >= 2 or nrec >= 2 or comm >= 10 or (spike and spike >= 2.0):
        ext = "Mid"
    if namu_incident or (nrec >= 5 and c2 >= 3):
        ext = "Late"

    if c2 >= 1:
        cap = "+" if b_yt.get("capped") else ""
        scope_note = "" if b_yt.get("scope") == "incident" else "(전반검색)"
        reasons.append(f"2차 유튜브 {c2}{cap}개(타 채널){scope_note}"
                       + (f"·합산 조회 {b_yt.get('total_views', 0):,}회" if b_yt.get("total_views") else ""))
    if nrec >= 1:
        src = "네이버뉴스" if b_news.get("source") == "naver" else "구글뉴스"
        reasons.append(f"기사화 최근 {nrec}건({src})")
    if comm >= 3:
        reasons.append(f"커뮤니티 관련글 {comm}건")
    if namu_incident:
        mk = ", ".join(b_namu.get("matched_keywords", []))
        reasons.append(f"나무위키에 이번 사안 기록({mk})")
    if spike is not None and spike >= 1.3:
        reasons.append(f"검색 관심도 {a_search.get('level')}({spike}x)")

    stage_reason = " · ".join(reasons) if reasons else "외부 확산 신호 뚜렷하지 않음"

    return {
        "status":       "ok",
        "fingerprint":  fp,
        "layer_a":      {"search_interest": a_search},
        "layer_b":      {"secondary_youtube": b_yt, "news": b_news,
                         "community": b_comm, "namuwiki": b_namu},
        "signals_fired": fired,
        "external_stage": ext,
        "reasons":      reasons,
        "stage_reason": stage_reason,
    }


# ════════════════════════════════════════════════════════════════════
# 8) 진입점 — report 에 반영 + 단계 상향
# ════════════════════════════════════════════════════════════════════
def _strip_links_for_report(res: dict) -> dict:
    """리포트(JSON)에 저장하기 전, 기사/영상 '원문 링크'와 개별 항목 목록을 제거한다.
       리포트는 집계 건수만 사용하므로 url·items 를 빼도 표시에는 영향이 없다."""
    if not isinstance(res, dict):
        return res
    for layer in ("layer_a", "layer_b"):
        for sig in (res.get(layer) or {}).values():
            if isinstance(sig, dict):
                sig.pop("items", None)   # 기사/영상 제목 목록
                sig.pop("url", None)     # 원문 링크(2차 유튜브·나무위키 등)
    return res


def enrich(report: dict) -> dict:
    """외부 확산 신호를 수집해 report['spread_external'] 저장 + spread_stage 상향(blend)."""
    try:
        res = assess(report)
    except Exception as e:
        res = {"status": "error", "note": f"확산 신호 수집 예외: {e}"}
    report["spread_external"] = _strip_links_for_report(res)

    if res.get("status") == "ok":
        sp  = report.setdefault("spread_stage", {})
        cur = sp.get("stage", "Unknown")
        new = _max_stage(cur, res["external_stage"])
        sp["external"] = True
        if _STAGE_RANK.get(new, -1) > _STAGE_RANK.get(cur, -1):
            sp["stage"] = new
            sp.setdefault("reasons", []).append(
                f"[외부 확산] {res['stage_reason']} → 단계 {cur}→{new} 상향")
        elif res["signals_fired"] > 0:
            sp.setdefault("reasons", []).append(f"[외부 확산] {res['stage_reason']}")
    return res


# ════════════════════════════════════════════════════════════════════
# 9) 리포트 3-4 '외부 확산 신호' 섹션 마크다운
# ════════════════════════════════════════════════════════════════════
def _md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def build_block_md(report: dict) -> str:
    res = report.get("spread_external")
    if not res or res.get("status") != "ok":
        note = (res or {}).get("note", "외부 확산 신호 수집을 수행하지 않았습니다.")
        return (f"> ⚪ **외부 확산 신호 미수집** — {note}\n>\n"
                "> (유튜브 URL 입력 시 크리에이터명 기반으로 외부 신호를 교차 확인합니다.)")

    fp = res.get("fingerprint", {})
    a  = res["layer_a"]["search_interest"]
    b  = res["layer_b"]
    yt, news, comm, namu = b["secondary_youtube"], b["news"], b["community"], b["namuwiki"]

    kw_disp = ", ".join(fp.get("keywords", [])) or "—"
    lines = []
    lines.append(f"**대상**: 크리에이터 `{_md_escape(fp.get('creator') or '—')}`  ·  "
                 f"이번 사안 키워드 `{_md_escape(kw_disp)}`\n")

    # A층
    lines.append("**A층 — 크리에이터 전반 관심도** (이름 검색)\n")
    if a.get("status") == "ok":
        lines.append(f"- 🔎 검색 관심도: **{a.get('level')}** "
                     f"(최근/평시 {a.get('spike')}배)")
    else:
        lines.append(f"- ⚪ 검색 관심도: **판단 보류** — {a.get('note', 'N/A')}")
    lines.append("")

    # B층 표
    lines.append("**B층 — 이번 사안 확산도** (이름 + 사건 키워드 + 최신 시점)\n")
    lines.append("| 신호 | 결과 | 판정 |")
    lines.append("|------|------|------|")

    # 2차 유튜브
    if yt.get("status") == "ok":
        c2 = yt.get("count", 0)
        v2 = yt.get("total_views", 0)
        cap = "+" if yt.get("capped") else ""
        scope_note = "" if yt.get("scope") == "incident" else " · ⚠️키워드없어 전반검색"
        verdict = "🚨 확산" if c2 >= 2 else ("⚠️ 감지" if c2 >= 1 else "✅ 미미")
        lines.append(f"| 2차 유튜브(이슈채널) | {c2}{cap}개(타 채널) · 합산 조회 {v2:,}회{scope_note} | {verdict} |")
    else:
        lines.append(f"| 2차 유튜브(이슈채널) | ⚪ {yt.get('note', 'N/A')} | 판단 보류 |")

    # 기사화
    if news.get("status") == "ok":
        src = "네이버뉴스" if news.get("source") == "naver" else "구글뉴스"
        nrec, ntot = news.get("recent", 0), news.get("total", 0)
        verdict = "🚨 기사화" if nrec >= 2 else ("⚠️ 감지" if nrec >= 1 else "✅ 미미")
        lines.append(f"| 기사화 | 최근 {nrec}건 / 검색 {ntot}건 ({src}) | {verdict} |")
    else:
        lines.append(f"| 기사화 | ⚪ {news.get('note', 'N/A')} | 판단 보류 |")

    # 커뮤니티 (샘플 중 사건 키워드 실매칭 글 수)
    if comm.get("status") == "ok":
        cc = comm.get("count", 0)
        bd = comm.get("breakdown", {})
        bd_s = " · ".join(f"{k} {v}" for k, v in bd.items()) if bd else ""
        verdict = "🚨 확산" if cc >= 10 else ("⚠️ 감지" if cc >= 3 else "✅ 미미")
        lines.append(f"| 커뮤니티 언급(카페·블로그) | 관련글 {cc}건 ({bd_s}) | {verdict} |")
    else:
        lines.append(f"| 커뮤니티 언급(카페·블로그) | ⚪ {comm.get('note', 'N/A')} | 판단 보류 |")

    # 나무위키 — '이번 사안' 기록(사건 특정)만 정착 신호, '논란 이력'은 참고
    if namu.get("status") == "ok":
        if namu.get("incident_mentioned"):
            mk = ", ".join(namu.get("matched_keywords", [])) or "키워드"
            lines.append(f"| 나무위키 | 이번 사안 기록됨({_md_escape(mk)}) | 🚨 정착 |")
        elif namu.get("controversy_section"):
            lines.append("| 나무위키 | 논란 이력 문서 존재(이번 사안은 미확인) | ⚠️ 참고 |")
        elif namu.get("exists"):
            lines.append("| 나무위키 | 문서 존재(논란 이력 미확인) | ✅ 미미 |")
        else:
            lines.append("| 나무위키 | 문서 없음/미미 | ✅ 미미 |")
    else:
        lines.append(f"| 나무위키 | ⚪ {namu.get('note', 'N/A')} | 판단 보류 |")

    lines.append("")
    fired = res.get("signals_fired", 0)
    if fired > 0:
        lines.append(f"> 🔺 **종합**: 이번 사안 외부 확산 신호 **{fired}종** 감지 — {res.get('stage_reason')}")
    else:
        lines.append("> ✅ **종합**: 이번 사안의 뚜렷한 외부 확산 신호는 아직 감지되지 않았습니다.")

    if not _has_naver():
        lines.append(">")
        lines.append("> ⚪ *'판단 보류' 항목(검색 관심도·커뮤니티)은 `.env` 에 NAVER API 키를 넣으면 "
                     "자동으로 채워집니다.*")

    return "\n".join(lines)


def external_signal_label(report: dict) -> str:
    """2-4 '외부 확산 신호' 한 줄 라벨."""
    res = report.get("spread_external") or {}
    if res.get("status") != "ok":
        return "⚪ 미수집"
    fired = res.get("signals_fired", 0)
    return f"🚨 {fired}종 감지" if fired > 0 else "✅ 없음"
