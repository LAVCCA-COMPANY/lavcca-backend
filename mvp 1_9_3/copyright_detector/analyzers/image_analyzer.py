"""
analyzers/image_analyzer.py - 이미지/사진/로고 저작권 분석
우선순위: 자체 DB → 스톡 워터마크(OCR) → Google Vision/Yandex 역검색 → OCR 브랜드 → Rekognition

설계 원칙: 구조 신호(FFT 패턴·시네마 화면비·CLIP 분류)는 역검색 트리거로만 사용.
finding은 OCR 텍스트(직접 증거) 또는 역검색 결과(인덱싱 증거)가 있을 때만 생성.
"""
import asyncio
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
import structlog
import numpy as np
import cv2

from config import config
from database.db_manager import get_db_manager
from utils.video_utils import compute_phash, frame_to_bytes
from utils.ocr_engine import safe_ocr, extract_text_only
from utils.clip_engine import classify_frames_batch, VISION_TRIGGER_THRESHOLDS, embed_frame
from utils.watermark_detector import (
    detect_tiled_watermark_fft,
    enhance_frame_for_ocr,
    detect_cinematic_aspect,
)
from utils.google_vision_searcher import (
    search_image_for_copyright, clear_vision_cache, get_searcher,
)
from analyzers.yandex_searcher import yandex_reverse_search, clear_yandex_cache

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# 알려진 브랜드 키워드 (확장판)
# ─────────────────────────────────────────────
KNOWN_BRAND_KEYWORDS = {
    # 글로벌 빅테크
    "google", "apple", "microsoft", "amazon", "meta", "facebook",
    "instagram", "youtube", "twitter", "x corp", "netflix", "disney",
    "samsung", "lg", "sony", "nike", "adidas", "coca-cola", "pepsi",
    "mcdonalds", "starbucks", "tesla", "spotify", "tiktok", "snapchat",
    "linkedin", "pinterest", "reddit", "twitch", "discord", "slack",
    "zoom", "airbnb", "uber", "paypal", "visa", "mastercard",
    # 미디어/엔터
    "hbo", "paramount", "universal", "warner", "columbia", "marvel",
    "dc comics", "pixar", "dreamworks", "fox", "nbc", "cbs", "abc",
    "espn", "cnn", "bbc", "nfl", "nba", "mlb", "fifa", "ioc",
    "oscars", "grammy", "billboard", "vevo",
    # 게임
    "nintendo", "playstation", "xbox", "steam", "riot games",
    "blizzard", "ea sports", "activision", "ubisoft", "valve",
    "epic games", "rockstar", "square enix",
    # 한국 브랜드 (영문)
    "kakao", "naver", "coupang", "hyundai", "kia", "sk", "kt",
    "lotte", "cj", "amorepacific", "posco", "hanwha", "doosan",
    "shinsegae", "hanjin", "nexon", "netmarble", "ncsoft",
    # 한국 브랜드 (한글)
    "카카오", "네이버", "쿠팡", "현대", "기아", "롯데", "삼성",
    "엘지", "에스케이", "케이티", "포스코", "한화", "두산",
    "신세계", "넥슨", "넷마블", "엔씨소프트",
    # 방송사
    "kbs", "mbc", "sbs", "jtbc", "tvn", "mnet", "ocn",
    "채널a", "tv조선", "ytn",
    # 패션/명품
    "gucci", "louis vuitton", "chanel", "prada", "hermes", "rolex",
    "burberry", "versace", "armani", "dior", "balenciaga",
    # 자동차
    "bmw", "mercedes", "audi", "volkswagen", "toyota", "honda",
    "ford", "chevrolet", "porsche", "ferrari", "lamborghini",
}

# 스톡 이미지/영상 서비스 워터마크
STOCK_WATERMARK_KEYWORDS = {
    "getty images", "gettyimages",
    "shutterstock",
    "istock", "istockphoto",
    "adobe stock", "adobestock",
    "123rf",
    "depositphotos",
    "dreamstime",
    "alamy",
    "corbis",
    "bigstock",
    "pond5",
    "storyblocks",
    "ap photo", "associated press",
    "reuters",
    "afp",
    "yonhap", "연합뉴스",
    "newsis", "뉴시스",
    "뉴스1",
}

# 로고 위험도 맵
LOGO_RISK_MAP = {
    "youtube": 0.95, "netflix": 0.90, "disney": 0.95, "marvel": 0.95,
    "nba": 0.90, "fifa": 0.90, "nfl": 0.90, "espn": 0.88,
    "coca-cola": 0.85, "apple": 0.85, "nike": 0.80, "adidas": 0.75,
    "kbs": 0.85, "mbc": 0.85, "sbs": 0.85, "jtbc": 0.85, "tvn": 0.90,
    "카카오": 0.80, "네이버": 0.75, "삼성": 0.80,
}


# ─────────────────────────────────────────────
# Google Vision 클라이언트
# ─────────────────────────────────────────────
class GoogleVisionClient:
    """
    브랜드 로고 감지.
    인증 일원화: 서비스계정(google-credentials.json) 대신 GOOGLE_API_KEY(REST)를
    쓰는 공용 GoogleVisionSearcher.detect_logos() 에 위임한다. 별도 클라이언트·
    자격증명 파일이 필요 없어 웹 역검색과 동일한 인증 경로를 공유한다.
    """
    def detect_logos(self, image_bytes: bytes) -> List[Dict]:
        try:
            annotations = get_searcher().detect_logos(image_bytes)
        except Exception as e:
            logger.error("google_vision_logo_error", error=str(e))
            return []
        logos = []
        for a in annotations:
            score = a.get("score", 0.0)
            brand = a.get("description", "")
            base_risk = LOGO_RISK_MAP.get(brand.lower(), 0.65)
            logos.append({
                "brand_name": brand,
                "confidence": score,
                "risk_score": min(base_risk * score * 1.1, 1.0),
                "source": "google_vision",
            })
        return logos

    # (제거됨) detect_web_entities:
    #   안전장치(자기 채널 필터·무료 출처 필터·가중치 임계값) 없이 완전 일치만으로
    #   finding을 만들던 구식 경로. google_vision_searcher v2가 동일 기능을
    #   오탐 방지 장치와 함께 제공하므로 그 경로(search_image_for_copyright)만 사용.


# ─────────────────────────────────────────────
# AWS Rekognition 클라이언트
# ─────────────────────────────────────────────
class RekognitionClient:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if not self._client and config.aws.use_rekognition:
            try:
                import boto3
                self._client = boto3.client(
                    "rekognition",
                    region_name=config.aws.region,
                    # 키가 비어 있으면 None → 기본 자격 증명 체인(ECS 태스크 역할 등)을 쓴다.
                    #   빈 문자열을 넘기면 boto3 가 빈 키로 서명해 인증이 실패한다.
                    aws_access_key_id=config.api.aws_access_key_id or None,
                    aws_secret_access_key=config.api.aws_secret_access_key or None,
                )
            except Exception as e:
                logger.warning("rekognition_unavailable", error=str(e))
        return self._client

    def detect_labels(self, image_bytes: bytes) -> List[Dict]:
        client = self._get_client()
        if not client:
            return []
        try:
            response = client.detect_labels(
                Image={"Bytes": image_bytes}, MaxLabels=20, MinConfidence=70
            )
            return [
                {"name": l["Name"], "confidence": l["Confidence"] / 100.0}
                for l in response["Labels"]
            ]
        except Exception as e:
            logger.error("rekognition_label_error", error=str(e))
            return []


# ─────────────────────────────────────────────
# 이미지/사진 분석기
# ─────────────────────────────────────────────
class ImageAnalyzer:

    def __init__(self):
        self.vision = GoogleVisionClient()
        self.rekognition = RekognitionClient()
        self.db = get_db_manager()
        self.executor = ThreadPoolExecutor(max_workers=6)
        # CLIP이 의심스럽다고 판단한 프레임 타임스탬프 (Vision API 검색 대상)
        # _run_clip_classification()에서 채우고, _analyze_single_frame()에서 읽음
        # CLIP이 먼저 완료된 후 프레임 분석이 시작되므로 thread-safe
        self._vision_search_ts: set = set()
        # OCR 허용 타임스탬프 (EasyOCR은 전역 직렬 자원 → 예산 관리)
        # analyze()에서 20초 간격으로 채움; FFT 감지 프레임은 예산 무관 항상 OCR
        self._ocr_ts: set = set()

    async def analyze(self, frames: List[Tuple[float, np.ndarray]],
                      job_id: str, progress=None) -> List[Dict]:
        logger.info("image_analysis_start", frames=len(frames), job_id=job_id)

        # 새 분석 작업 시작 시 역검색 캐시 초기화
        clear_vision_cache()
        clear_yandex_cache()

        selected = self._select_key_frames(frames)
        logger.info("key_frames_selected", count=len(selected))
        if progress:
            # CLIP 배치(1) + 프레임 분석(N) 단계를 합쳐 진행률 표기
            progress.set_total("image", len(selected) + 1, note="CLIP 분류")

        # OCR 예산: 20초 간격 프레임만 허용 (video_clip_analyzer의 OCR 주기와 정렬
        # → 동일 프레임은 OCR 캐시를 공유). FFT 감지 프레임은 예산과 무관하게 OCR.
        ocr_ts: set = set()
        _last_ocr = -20.0
        for ts, _ in selected:
            if ts - _last_ocr >= 20:
                ocr_ts.add(ts)
                _last_ocr = ts
        self._ocr_ts = ocr_ts

        loop = asyncio.get_event_loop()

        # ── CLIP 배치 분류 (OCR 없이 콘텐츠 유형 감지) ──
        clip_findings = await loop.run_in_executor(
            self.executor,
            self._run_clip_classification,
            selected, job_id,
        )
        logger.info("clip_classification_done", findings=len(clip_findings))
        if progress:
            progress.advance("image", note="프레임 분석")

        # ── 프레임별 심층 분석 (OCR + API) ──
        semaphore = asyncio.Semaphore(6)

        async def analyze_frame(timestamp, frame):
            async with semaphore:
                r = await loop.run_in_executor(
                    self.executor,
                    self._analyze_single_frame,
                    timestamp, frame, job_id
                )
                if progress:
                    progress.advance("image")
                return r

        results = await asyncio.gather(
            *[analyze_frame(ts, frame) for ts, frame in selected],
            return_exceptions=True
        )

        frame_findings = []
        seen_logos = set()
        for result_list in results:
            if isinstance(result_list, Exception) or not result_list:
                continue
            for result in result_list:
                key = result.get("title", "") or result.get("description", "")
                if key and key not in seen_logos:
                    seen_logos.add(key)
                    frame_findings.append(result)

        # CLIP 결과와 프레임 분석 결과 병합
        findings = clip_findings + frame_findings
        findings.sort(key=lambda x: x.get("timestamp_start", 0))
        logger.info("image_analysis_done", findings=len(findings), job_id=job_id)
        if progress:
            progress.done("image", note=f"{len(findings)}건 발견")
        return findings

    def _analyze_single_frame(self, timestamp: float, frame: np.ndarray,
                               job_id: str) -> List[Dict]:
        results = []
        image_bytes = frame_to_bytes(frame)
        phash = compute_phash(frame)

        # 1. 자체 DB 로고 조회
        cached_logo = self.db.lookup_logo_by_phash(phash)
        if cached_logo and cached_logo["found"]:
            results.append({
                "job_id": job_id,
                "finding_type": "logo",
                "timestamp_start": timestamp,
                "timestamp_end": timestamp,
                "timestamp_display": self._format_time(timestamp),
                "title": cached_logo["brand_name"],
                "rights_holder": cached_logo.get("trademark_owner", ""),
                "source": "internal_db",
                "confidence_score": cached_logo["similarity"],
                "risk_score": min(cached_logo["similarity"] * 0.95, 1.0),
                "risk_level": "HIGH" if cached_logo["similarity"] > 0.8 else "MEDIUM",
                "description": f"로고 감지: {cached_logo['brand_name']} (내부 DB)",
            })
            return results

        # 1-b. 자체 학습 콘텐츠 DB pHash 조회 (API 비용 0, 모든 프레임에 적용)
        #   과거 Vision으로 확인된 저작물이면 트리거 없이 즉시 감지 → SerpAPI/Vision
        #   쿼터와 무관하게 작동. torch 불필요(pHash)라 CLIP 꺼진 환경도 OK.
        _ph_hit = self.db.lookup_content_by_phash(phash)
        if _ph_hit:
            _lid = _ph_hit.get("learned_id")
            _risk = _ph_hit.get("risk_score", 0.70)
            results.append({
                "job_id": job_id,
                "finding_type": "image",
                "timestamp_start": timestamp,
                "timestamp_end": timestamp,
                "timestamp_display": self._format_time(timestamp),
                "title": f"[자체학습] {_ph_hit.get('title') or _ph_hit.get('rights_holder', '')}",
                "rights_holder": "",
                "source": "internal_phash_db",
                "confidence_score": _ph_hit.get("similarity", 0.95),
                "risk_score": round(_risk, 3),
                "risk_level": self._get_risk_level(_risk),
                "description": (
                    f"자체 학습 DB pHash 일치 (유사도 {_ph_hit.get('similarity', 0):.0%}, "
                    f"누적 {_ph_hit.get('detection_count', 1)}회, 학습ID emb:{_lid} — "
                    f"오학습이면 python main.py --forget emb:{_lid}): "
                    f"{_ph_hit.get('title', '')}"
                ),
                "reference_url": _ph_hit.get("reference_url"),
            })
            return results

        # 2. 워터마크 감지 (FFT 반복 패턴 + 강화 전처리 OCR 병합)
        fft_hit, fft_conf = detect_tiled_watermark_fft(frame)

        # OCR 예산 게이트: 20초 간격 허용 프레임 또는 FFT 의심 프레임만 OCR
        # (EasyOCR은 전역 직렬 자원 — 모든 프레임을 OCR하면 긴 영상에서 수 분 소요)
        _ocr_ok = fft_hit or (timestamp in self._ocr_ts)

        # FFT 감지 시 향상된 전처리 프레임으로 OCR 수행 (반투명 텍스트 강조)
        ocr_frame = enhance_frame_for_ocr(frame) if fft_hit else frame

        if _ocr_ok and (self._has_text_region(ocr_frame) or fft_hit):
            stock_hit = self._detect_stock_watermark(ocr_frame)
            # enhanced 프레임에서 못 찾으면 원본도 시도
            if not stock_hit and fft_hit:
                stock_hit = self._detect_stock_watermark(frame)

            if stock_hit:
                conf = 0.95 if fft_hit else 0.92  # FFT도 잡힌 경우 신뢰도 상승
                results.append({
                    "job_id": job_id,
                    "finding_type": "image",
                    "timestamp_start": timestamp,
                    "timestamp_end": timestamp,
                    "timestamp_display": self._format_time(timestamp),
                    "title": f"스톡 이미지: {stock_hit}",
                    "rights_holder": stock_hit,
                    "source": "stock_watermark_ocr" + ("+fft" if fft_hit else ""),
                    "confidence_score": conf,
                    "risk_score": 0.90,
                    "risk_level": "HIGH",
                    "description": f"스톡 이미지 워터마크 감지: '{stock_hit}'"
                                   + (" (FFT 반복 패턴 확인)" if fft_hit else ""),
                })
                return results  # 스톡 워터마크 확인 → 추가 분석 불필요

        # 2-a. FFT만 감지 — Vision 역검색이 불가능한 환경에서만 단독 경고 생성
        #  FFT는 직물/격자무늬 등 일반 텍스처에도 반응하는 추정 신호이므로,
        #  Vision API가 켜져 있으면 역검색 결과(2-b)로 확정하고 단독 finding은 만들지 않음
        if fft_hit and fft_conf > 0.42 and not results and not get_searcher().enabled:
            results.append({
                "job_id": job_id,
                "finding_type": "image",
                "timestamp_start": timestamp,
                "timestamp_end": timestamp,
                "timestamp_display": self._format_time(timestamp),
                "title": "반복 패턴 워터마크 의심",
                "rights_holder": "",
                "source": "fft_watermark_detection",
                "confidence_score": round(fft_conf, 3),
                "risk_score": round(min(fft_conf * 0.80, 0.78), 3),
                "risk_level": "MEDIUM",
                "description": f"FFT 분석: 반복 패턴 워터마크 감지 (신뢰도 {fft_conf:.0%})",
            })

        # 2-b. Google Vision 역이미지 검색
        #  트리거 조건 (구조 신호는 트리거로만 사용, 직접 finding 생성 안 함):
        #    - FFT 반복 패턴 감지 (스톡 워터마크 의심)
        #    - CLIP이 의심 카테고리로 분류한 프레임
        #    - 시네마 화면비 감지 (영화 클립 삽입 의심)
        #  → 역검색으로 실제 출처가 확인될 때만 finding 생성
        ca = detect_cinematic_aspect(frame)
        _cinematic = bool(ca.get("is_cinematic")) and ca.get("confidence", 0.0) > 0.55
        _vr = None  # Google Vision 결과 (Yandex 교차 검증에서 참조)
        _emb = None  # CLIP 임베딩 (자체 DB 조회·학습용)
        _use_vision = fft_hit or _cinematic or (timestamp in self._vision_search_ts)
        if _use_vision:
            # ── 0. 자체 임베딩 DB 조회: 과거 확인된 콘텐츠면 API 호출 없이 즉시 감지 ──
            _emb = embed_frame(frame)
            _known = self.db.lookup_content_by_embedding(_emb) if _emb is not None else None
            if _known:
                _lid = _known.get("learned_id")
                results.append({
                    "job_id":           job_id,
                    "finding_type":     "image",
                    "timestamp_start":  timestamp,
                    "timestamp_end":    timestamp,
                    "timestamp_display": self._format_time(timestamp),
                    "title":            f"[자체학습] {_known.get('title') or _known.get('rights_holder', '')}",
                    "rights_holder":    "",
                    "source":           "internal_embedding_db",
                    "confidence_score": _known.get("similarity", 0.92),
                    "risk_score":       round(_known.get("risk_score", 0.70), 3),
                    "risk_level":       self._get_risk_level(_known.get("risk_score", 0.70)),
                    "description": (
                        f"자체 학습 DB 일치 (유사도 {_known.get('similarity', 0):.0%}, "
                        f"누적 {_known.get('detection_count', 1)}회, "
                        f"학습ID emb:{_lid} — 오학습이면 "
                        f"python main.py --forget emb:{_lid}): "
                        f"{_known.get('title', '')}"
                    ),
                    "reference_url":    _known.get("reference_url"),
                })
                logger.info("embedding_db_hit",
                            title=_known.get("title", "")[:40],
                            sim=_known.get("similarity"), ts=timestamp)
                return results   # API 호출 절약

            _vr = search_image_for_copyright(frame)  # 결과는 _vr 에 보존 (Yandex 교차 검증용)
            if _vr is not None and _vr.get("rights_holder") and not _vr.get("is_free"):
                # 출처 식별 성공 → finding 생성
                _holder  = _vr["rights_holder"]
                _rsc     = _vr["risk_score"]
                _rlv     = _vr["risk_level"]
                _src_url = _vr.get("source_url")
                _n_full  = _vr.get("full_matches", 0)
                _entity  = _vr.get("detected_entity")   # Vision이 인식한 작품/아티스트명
                _ocr_cr  = _vr.get("ocr_copyright")     # 이미지 내 저작권 텍스트 (v2)
                _pages   = _vr.get("pages", [])
                # 발견된 출처 도메인 목록 (source_url → pages 순)
                _found_domains: list = []
                for _u in ([_src_url] if _src_url else []) + (_pages or []):
                    try:
                        from urllib.parse import urlparse as _urlp
                        _d = _urlp(_u).netloc.lower()
                        if _d.startswith("www."):
                            _d = _d[4:]
                        if _d and _d not in _found_domains:
                            _found_domains.append(_d)
                    except Exception:
                        pass
                _sources_str = ", ".join(_found_domains[:3]) or _holder or "알 수 없음"
                # 제목: 작품명이 있으면 함께 표시, 없으면 발견 출처만
                if _entity:
                    _title = f"인터넷 이미지: {_entity}"
                else:
                    _title = f"인터넷 이미지 발견: {_sources_str}"
                _desc = (
                    f"Google 역이미지: {_sources_str}에서 발견됨"
                    + (f" (완전 일치 {_n_full}건)" if _n_full else "")
                )
                if _ocr_cr:
                    _desc += f" [이미지 내 텍스트: {_ocr_cr}]"
                results.append({
                    "job_id": job_id,
                    "finding_type": "image",
                    "timestamp_start": timestamp,
                    "timestamp_end": timestamp,
                    "timestamp_display": self._format_time(timestamp),
                    "title": _title,
                    "rights_holder": "",
                    "source": "google_vision_web_search",
                    "confidence_score": round(min(0.72 + _n_full * 0.04, 0.97), 3),
                    "risk_score": round(_rsc, 3),
                    "risk_level": _rlv,
                    "description": _desc,
                    "reference_url": _src_url,
                })
                logger.info("vision_search_hit",
                            sources=_sources_str, entity=_entity, risk=_rlv, ts=timestamp)
                # ── 자체 임베딩 DB 학습: 다음 분석부터 API 없이 감지 ──
                if _emb is not None:
                    self.db.learn_content_embedding({
                        "title":            _title,
                        "rights_holder":    _holder or _sources_str,
                        "source":           "google_vision",
                        "reference_url":    _src_url,
                        "risk_score":       _rsc,
                        "phash":            phash,
                        "embedding":        _emb,
                        "job_id":           job_id,
                        "source_timestamp": timestamp,
                    })
                # 출처 식별 완료 → 이후 단계 불필요
                return results
            elif _vr is not None and _vr.get("full_matches", 0) > 0 and not _vr.get("is_free"):
                # 출처 미식별 + 완전 일치 있음 → MEDIUM finding (이미 없는 경우)
                if not any(r.get("source") == "google_vision_web_search" for r in results):
                    results.append({
                        "job_id": job_id,
                        "finding_type": "image",
                        "timestamp_start": timestamp,
                        "timestamp_end": timestamp,
                        "timestamp_display": self._format_time(timestamp),
                        "title": "인터넷 일치 이미지 (출처 미확인)",
                        "rights_holder": "",
                        "source": "google_vision_web_search",
                        "confidence_score": 0.60,
                        "risk_score": 0.48,
                        "risk_level": "MEDIUM",
                        "description": (
                            f"Google 역이미지 검색: 인터넷에 동일 이미지 "
                            f"{_vr['full_matches']}건 발견 (저작권 출처 불명)"
                        ),
                        "reference_url": _vr.get("pages", [None])[0],
                    })

        # 2-b-Y. Yandex 역이미지 역검색 (Google Vision 보완)
        #  트리거 조건: Google Vision과 동일 (_use_vision)
        #  - Google Vision HIGH → 이미 early-return 완료 → 이 코드는 실행 안 됨 (불필요)
        #  - Google Vision MEDIUM or 미감지 → Yandex로 교차 검증
        #  결과 분류:
        #    공통 출처(Google + Yandex) → HIGH (교차 검증 완료)
        #    Yandex 단독 고위험 도메인   → MEDIUM
        if _use_vision:
            _yr = yandex_reverse_search(frame, timestamp)
            if _yr and not _yr.get("error") and _yr.get("has_high_risk"):
                _y_holder  = _yr["rights_holder"]
                _y_domains = set(_yr["top_domains"])

                # 교차 검증: Google Vision 결과와 공통 출처 확인
                _gv_holder = (_vr or {}).get("rights_holder", "") if _vr is not None else ""
                _common = bool(_gv_holder) and any(
                    _gv_holder.lower().find(d) >= 0 or d.find(_gv_holder.lower()) >= 0
                    for d in _y_domains if d
                )

                # 발견된 도메인 목록 구성 (최대 3개)
                _y_domains_str = ", ".join(sorted(_y_domains)[:3]) or _y_holder or "알 수 없음"
                # 중복 체크: 동일 도메인이 이미 결과에 포함됐는지 확인
                _already = bool(_y_holder) and any(
                    _y_holder in (r.get("title", "") + r.get("description", ""))
                    for r in results
                )
                if not _already and (_y_holder or _yr.get("matches")):
                    if _common:
                        _y_risk, _y_level, _y_conf = 0.88, "HIGH", 0.92
                        _y_src  = "yandex_reverse_search+google_vision"
                        _y_desc = (
                            f"Google + Yandex 교차 검증: {_y_domains_str}에서 발견됨 "
                            f"(양 엔진 일치)"
                        )
                    else:
                        _y_risk, _y_level, _y_conf = 0.55, "MEDIUM", 0.68
                        _y_src  = "yandex_reverse_search"
                        _y_desc = (
                            f"Yandex 역이미지: {_y_domains_str}에서 발견됨"
                        )
                    _y_ref = (
                        _yr["matches"][0]["url"] if _yr.get("matches") else None
                    )
                    results.append({
                        "job_id":           job_id,
                        "finding_type":     "image",
                        "timestamp_start":  timestamp,
                        "timestamp_end":    timestamp,
                        "timestamp_display": self._format_time(timestamp),
                        "title":            f"인터넷 이미지 발견: {_y_domains_str}",
                        "rights_holder":    "",
                        "source":           _y_src,
                        "confidence_score": _y_conf,
                        "risk_score":       _y_risk,
                        "risk_level":       _y_level,
                        "description":      _y_desc,
                        "reference_url":    _y_ref,
                    })
                    logger.info("yandex_search_hit",
                                domains=_y_domains_str,
                                cross_validated=_common,
                                risk=_y_level,
                                ts=timestamp)

        # (제거됨) 시네마 화면비 / 레터박스 단독 finding:
        #   화면비·검은 띠는 크리에이터가 직접 만든 썸네일·타이틀 카드에도 흔해
        #   단독으로는 저작권 증거가 될 수 없음 → 2-b의 Vision 트리거로만 사용.

        # 3. Google Vision 로고 감지
        logos = self.vision.detect_logos(image_bytes)
        for logo in logos:
            if logo["confidence"] < config.pipeline.logo_confidence_threshold:
                continue
            risk_score = logo["risk_score"]
            risk_level = self._get_risk_level(risk_score)
            finding = {
                "job_id": job_id,
                "finding_type": "logo",
                "timestamp_start": timestamp,
                "timestamp_end": timestamp,
                "timestamp_display": self._format_time(timestamp),
                "title": logo["brand_name"],
                "source": "google_vision",
                "confidence_score": logo["confidence"],
                "risk_score": risk_score,
                "risk_level": risk_level,
                "description": f"브랜드 로고 감지: {logo['brand_name']}",
                "raw_response": logo,
            }
            results.append(finding)
            self.db.learn_from_finding("logo", {
                "brand_name": logo["brand_name"],
                "phash": phash,
                "source": "google_vision",
            })

        # (제거됨) 4. 구식 Web Detection 경로:
        #   "인터넷에 동일 이미지 존재 = 위반"으로 처리해 본인 업로드 콘텐츠까지
        #   오탐하던 경로. 자기 채널 필터·무료 출처 필터가 있는 2-b가 대체.

        # 5. OCR 브랜드 텍스트 감지 (외부 API 없을 때)
        if not results and _ocr_ok and self._has_text_region(frame):
            ocr_brands = self._detect_brands_from_ocr(frame)
            for brand in ocr_brands:
                risk_score = brand["risk_score"]
                results.append({
                    "job_id": job_id,
                    "finding_type": "logo",
                    "timestamp_start": timestamp,
                    "timestamp_end": timestamp,
                    "timestamp_display": self._format_time(timestamp),
                    "title": brand["brand_name"],
                    "source": "ocr_text_detection",
                    "confidence_score": brand["confidence"],
                    "risk_score": risk_score,
                    "risk_level": self._get_risk_level(risk_score),
                    "description": f"브랜드명 텍스트 감지: {brand['brand_name']}",
                    "raw_response": brand,
                })

        # 6. Rekognition 레이블 (최후 수단)
        if config.aws.use_rekognition and not results:
            labels = self.rekognition.detect_labels(image_bytes)
            for label in labels:
                if label["name"].lower() in KNOWN_BRAND_KEYWORDS:
                    results.append({
                        "job_id": job_id,
                        "finding_type": "logo",
                        "timestamp_start": timestamp,
                        "timestamp_end": timestamp,
                        "timestamp_display": self._format_time(timestamp),
                        "title": label["name"],
                        "source": "rekognition",
                        "confidence_score": label["confidence"],
                        "risk_score": label["confidence"] * 0.7,
                        "risk_level": self._get_risk_level(label["confidence"] * 0.7),
                        "description": f"브랜드/상표 감지: {label['name']}",
                        "raw_response": label,
                    })

        return results

    # ─────────────────────────────────────────────
    # CLIP 배치 분류
    # ─────────────────────────────────────────────
    def _run_clip_classification(
        self,
        selected_frames: list,
        job_id: str,
    ) -> list:
        """
        선택된 프레임 전체를 CLIP으로 한 번에 분류.
        - 카테고리별로 신뢰도 최고 프레임의 finding만 채택 (중복 방지)
        - stock_photo / watermarked 의심 프레임 → self._vision_search_ts에 등록
          → _analyze_single_frame()에서 Google Vision 역검색 트리거
        - finding_type: image_analyzer는 "image" 기준
        """
        try:
            just_frames = [f for _, f in selected_frames]
            batch_results = classify_frames_batch(just_frames)

            # 카테고리별 최고 신뢰도 finding만 유지
            best_per_cat: dict = {}
            vision_ts: set = set()

            for i, (ts, _) in enumerate(selected_frames):
                r = batch_results[i] if i < len(batch_results) else None
                if not r:
                    continue
                cat  = r.get("category", "")
                conf = r.get("confidence", 0.0)

                if cat not in best_per_cat or conf > best_per_cat[cat][1]:
                    best_per_cat[cat] = (ts, conf, r)

                # Google Vision 역검색 대상 수집 (카테고리별 공유 임계값)
                cat_threshold = VISION_TRIGGER_THRESHOLDS.get(cat, 999.0)
                if conf >= cat_threshold:
                    vision_ts.add(ts)

            # thread-safe: 프레임 분석이 시작되기 전에 완전히 설정됨
            self._vision_search_ts = vision_ts
            if vision_ts:
                logger.info("vision_search_targets",
                            count=len(vision_ts),
                            timestamps=sorted(vision_ts)[:5])

            # CLIP 단독 finding은 생성하지 않음 (오탐률 높음)
            # CLIP은 Vision 트리거 역할만 담당 → _analyze_single_frame에서 Vision 호출
            return []

        except Exception as e:
            logger.warning("clip_classification_failed", error=str(e))
            return []

    # ─────────────────────────────────────────────
    # 감지 유틸
    # ─────────────────────────────────────────────
    def _detect_stock_watermark(self, frame: np.ndarray) -> Optional[str]:
        """OCR로 스톡 이미지 워터마크 키워드 탐지"""
        text = extract_text_only(frame).lower()
        for kw in STOCK_WATERMARK_KEYWORDS:
            if kw in text:
                return kw
        return None

    def _detect_brands_from_ocr(self, frame: np.ndarray) -> List[Dict]:
        """EasyOCR로 프레임에서 브랜드명 매칭"""
        results_ocr = safe_ocr(frame, detail=1, min_confidence=0.5)
        found = []
        seen = set()
        for (_, text, conf) in results_ocr:
            text_lower = text.lower().strip()
            for brand in KNOWN_BRAND_KEYWORDS:
                if brand in text_lower and brand not in seen:
                    seen.add(brand)
                    base_risk = LOGO_RISK_MAP.get(brand, 0.65)
                    found.append({
                        "brand_name": text,
                        "matched_keyword": brand,
                        "confidence": float(conf),
                        "risk_score": round(min(base_risk * float(conf), 0.90), 3),
                    })
        return found

    def _has_text_region(self, frame: np.ndarray) -> bool:
        """OCR 호출 전 사전 필터: 텍스트가 있을 가능성 체크"""
        if frame is None or frame.size == 0:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        if float(gray.std()) < 12.0:
            return False
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        dark_ratio = float(np.sum(binary == 0)) / max(binary.size, 1)
        return 0.02 <= dark_ratio <= 0.65

    def _select_key_frames(self, frames: List[Tuple[float, np.ndarray]]) -> List[Tuple[float, np.ndarray]]:
        """
        초당 1프레임 선택 — 각 1초 구간의 첫 번째 프레임만 채택.
        원본 영상이 60fps라면 초당 60프레임 중 맨 앞 1개를 가져오는 것과 동일.
        시간 순서를 유지하며 중복 초(second) 제거.
        """
        seen: set = set()
        selected = []
        for ts, frame in frames:
            sec = int(ts)
            if sec not in seen:
                seen.add(sec)
                selected.append((ts, frame))
        return selected

    def _get_risk_level(self, score: float) -> str:
        if score >= config.risk.HIGH_THRESHOLD:
            return "HIGH"
        elif score >= config.risk.MEDIUM_THRESHOLD:
            return "MEDIUM"
        elif score >= config.risk.LOW_THRESHOLD:
            return "LOW"
        return "SAFE"

    def _format_time(self, seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
