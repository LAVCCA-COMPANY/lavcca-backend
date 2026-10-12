"""
analyzers/yandex_searcher.py - Yandex 이미지 역검색 (SerpAPI)
Yandex는 공개 URL만 지원 → 이미지를 임시 공개 호스트에 업로드 후 검색.
업로드 실패 시 빈 결과 반환 (에러 로그 없이 조용히 스킵).
기본 비활성: YANDEX_PUBLIC_UPLOAD_ENABLED=true 일 때만 업로드·검색한다.
"""
import base64
import json
import os
import threading
from typing import Dict, List, Optional
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
import structlog

from config import config

logger = structlog.get_logger()

# ─────────────────────────────────────────────
# 무료 출처 도메인 (false-positive 억제)
# ─────────────────────────────────────────────
_FREE_DOMAINS = {
    "unsplash.com", "pexels.com", "pixabay.com",
    "commons.wikimedia.org", "wikipedia.org", "creativecommons.org",
    "flickr.com",
}

# ─────────────────────────────────────────────
# 고위험 저작권 출처 도메인
# ─────────────────────────────────────────────
_HIGH_RISK_DOMAINS = {
    "gettyimages.com", "gettyimages.co.kr",
    "shutterstock.com",
    "istockphoto.com",
    "stock.adobe.com",
    "alamy.com",
    "depositphotos.com",
    "dreamstime.com",
    "123rf.com",
    "pond5.com",
    "storyblocks.com",
    "reuters.com",
    "apimages.com",
    "afp.com",
    "yonhapnewstv.co.kr", "yna.co.kr",
    "newsis.com",
    "news1.kr",
    "netflix.com", "disneyplus.com",
    "nba.com", "fifa.com", "nfl.com",
}

# 임시 이미지 업로드 엔드포인트 (우선순위 순)
_TEMP_HOSTS = [
    "https://0x0.st",
    "https://tmpfiles.org/api/v1/upload",
]


def _domain(url: str) -> str:
    # lstrip("www.")은 문자 집합 제거라서 "wikipedia.org" → "ikipedia.org"가 됨
    # 반드시 접두사 제거 방식 사용
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


# ─────────────────────────────────────────────
# phash 결과 캐시 (동일/유사 프레임 재검색 방지)
# SerpAPI는 호출당 과금 + 임시 호스트 업로드도 느림 → 캐시 필수
# ─────────────────────────────────────────────
_result_cache: Dict[str, Dict] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 200
_call_count = 0          # 작업당 실제 SerpAPI 호출 수 (캐시 히트 제외)
_budget_warned = False


def clear_yandex_cache() -> None:
    """새 분석 작업 시작 시 호출: 캐시 + 호출 예산 초기화."""
    global _call_count, _budget_warned
    with _cache_lock:
        _result_cache.clear()
        _call_count = 0
        _budget_warned = False


def _consume_budget() -> bool:
    """작업당 SerpAPI 호출 예산 차감. 예산 소진 시 False."""
    global _call_count, _budget_warned
    with _cache_lock:
        if _call_count >= config.pipeline.yandex_max_calls_per_job:
            if not _budget_warned:
                _budget_warned = True
                logger.warning("yandex_budget_exhausted",
                               limit=config.pipeline.yandex_max_calls_per_job)
            return False
        _call_count += 1
        return True


def _frame_phash(frame_bgr: np.ndarray) -> str:
    try:
        from utils.video_utils import compute_phash
        return compute_phash(frame_bgr)
    except Exception:
        return ""


def _cache_get(ph: str) -> Optional[Dict]:
    if not ph:
        return None
    with _cache_lock:
        if ph in _result_cache:
            return _result_cache[ph]
        try:
            ph_int = int(ph, 16)
            for cached_ph, result in _result_cache.items():
                if bin(ph_int ^ int(cached_ph, 16)).count("1") <= 8:
                    _result_cache[ph] = result
                    return result
        except Exception:
            pass
    return None


def _cache_set(ph: str, result: Dict) -> None:
    if not ph:
        return
    with _cache_lock:
        if len(_result_cache) >= _CACHE_MAX:
            for key in list(_result_cache.keys())[:50]:
                del _result_cache[key]
        _result_cache[ph] = result


# ─────────────────────────────────────────────
# 임시 공개 URL 업로드
# ─────────────────────────────────────────────
def _upload_image(img_bytes: bytes, timeout: int = 10) -> Optional[str]:
    """
    이미지를 무료 임시 호스트에 업로드 → 공개 URL 반환.
    Yandex 역검색은 공개 URL이 필요하므로 업로드 필수.
    """
    for host in _TEMP_HOSTS:
        try:
            resp = requests.post(
                host,
                files={"file": ("img.jpg", img_bytes, "image/jpeg")},
                timeout=timeout,
                # 0x0.st는 기본 python-requests UA를 차단(403) →
                # 업로드 실패로 Yandex 검색 전체가 조용히 스킵되는 원인이었음
                headers={"User-Agent": "copyright-detector/1.0 (analysis tool)"},
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                # 0x0.st → 직접 URL 반환
                if text.startswith("https://"):
                    logger.debug("temp_upload_ok", host=host, url=text[:60])
                    return text
                # tmpfiles.org → JSON {"data": {"url": "..."}}
                try:
                    data = resp.json()
                    url = data.get("data", {}).get("url", "")
                    if url.startswith("http"):
                        logger.debug("temp_upload_ok", host=host, url=url[:60])
                        return url
                except Exception:
                    pass
        except Exception as e:
            logger.debug("temp_upload_failed", host=host, error=str(e))
            continue

    return None


# ─────────────────────────────────────────────
# 메인 검색 함수
# ─────────────────────────────────────────────
def yandex_reverse_search(frame_bgr: np.ndarray,
                           timestamp_ms: float = 0.0) -> Dict:
    """
    Yandex 이미지 역검색 (SerpAPI yandex_images 엔진).
    내부적으로 프레임을 임시 공개 URL에 업로드한 뒤 검색.

    Returns:
        {
            "matches":       List[Dict],  # url / title / source / is_high_risk / is_free
            "top_domains":   List[str],
            "has_high_risk": bool,
            "rights_holder": str,
            "error":         str | None,
        }
    """
    api_key = config.api.serpapi_key
    if not api_key:
        return _empty_result(error="SERPAPI_KEY not configured")
    # 프레임을 공개 임시 호스트에 올리는 단계가 필수 → 명시적으로 켠 경우에만 실행
    if not config.pipeline.yandex_public_upload_enabled:
        return _empty_result(error="yandex public upload disabled")
    # 검색이 불가능한데 업로드만 먼저 하는 일이 없도록 패키지를 업로드 전에 확인
    try:
        from serpapi import GoogleSearch
    except ImportError:
        return _empty_result(error="serpapi package not installed")

    # ── 캐시 확인 (유사 프레임 재검색 방지) ──
    ph = _frame_phash(frame_bgr)
    cached = _cache_get(ph)
    if cached is not None:
        logger.debug("yandex_cache_hit", ts=timestamp_ms)
        return cached

    # ── 작업당 호출 예산 확인 (SerpAPI는 검색당 과금 — 비용 상한) ──
    if not _consume_budget():
        return _empty_result(error="yandex per-job budget exhausted")

    # ── 이미지 인코딩 ──
    ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return _empty_result(error="imencode failed")
    img_bytes = buf.tobytes()

    # ── 임시 공개 URL 업로드 ──
    temp_url = _upload_image(img_bytes)
    if not temp_url:
        # 업로드 실패 → 조용히 스킵 (네트워크 환경 문제일 수 있음)
        logger.debug("yandex_skip_no_url", ts=timestamp_ms)
        return _empty_result(error="temp image upload failed")

    # ── SerpAPI Yandex Images 역검색 ──
    try:
        params = {
            "engine": "yandex_images",
            "api_key": api_key,
            "url":     temp_url,   # Yandex visual search (역이미지 검색)
        }

        search = GoogleSearch(params)

        try:
            raw = search.get_dict()
        except json.JSONDecodeError:
            # API 응답이 비어 있거나 파싱 실패 → 조용히 스킵
            logger.debug("yandex_empty_response", ts=timestamp_ms)
            return _empty_result(error="empty API response")

        result = _parse_yandex_result(raw, timestamp_ms)
        _cache_set(ph, result)   # 정상 결과만 캐싱 (일시 오류는 재시도 가능하게)
        return result

    except Exception as e:
        logger.warning("yandex_search_error", ts=timestamp_ms, error=str(e))
        return _empty_result(error=str(e))


# ─────────────────────────────────────────────
# 응답 파싱
# ─────────────────────────────────────────────
def _parse_yandex_result(raw: dict, timestamp_ms: float) -> Dict:
    matches: List[Dict] = []
    top_domains: List[str] = []
    seen_domains: set = set()
    has_high_risk = False
    best_rights_holder = ""
    best_risk_weight = 0.0

    # image_results (개별 이미지)
    for item in raw.get("image_results", []):
        url   = item.get("original", "") or item.get("link", "")
        title = item.get("title", "")
        if not url:
            continue
        dom   = _domain(url)
        is_hr = dom in _HIGH_RISK_DOMAINS
        is_fr = dom in _FREE_DOMAINS
        matches.append({"url": url, "title": title, "source": dom,
                         "is_high_risk": is_hr, "is_free": is_fr})
        if dom and dom not in seen_domains:
            seen_domains.add(dom)
            top_domains.append(dom)
        if is_hr and not is_fr and best_risk_weight < 1.0:
            has_high_risk = True
            best_risk_weight = 1.0
            best_rights_holder = dom

    # sites (사이트별 요약)
    for site in raw.get("sites", []):
        url   = site.get("link", "")
        title = site.get("title", "")
        if not url:
            continue
        dom   = _domain(url)
        is_hr = dom in _HIGH_RISK_DOMAINS
        is_fr = dom in _FREE_DOMAINS
        if dom and dom not in seen_domains:
            seen_domains.add(dom)
            top_domains.append(dom)
            matches.append({"url": url, "title": title, "source": dom,
                             "is_high_risk": is_hr, "is_free": is_fr})
        if is_hr and not is_fr and best_risk_weight < 0.9:
            has_high_risk = True
            best_risk_weight = 0.9
            best_rights_holder = dom

    logger.debug("yandex_parse_done", ts=timestamp_ms,
                 total=len(matches), high_risk=has_high_risk,
                 domains=top_domains[:5])

    return {
        "matches":       matches,
        "top_domains":   top_domains[:10],
        "has_high_risk": has_high_risk,
        "rights_holder": best_rights_holder,
        "error":         None,
    }


def _empty_result(error: str = "") -> Dict:
    return {
        "matches":       [],
        "top_domains":   [],
        "has_high_risk": False,
        "rights_holder": "",
        "error":         error or None,
    }
