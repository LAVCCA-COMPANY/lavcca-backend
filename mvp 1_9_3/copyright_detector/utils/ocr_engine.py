"""
utils/ocr_engine.py - 공유 OCR 싱글톤 + 프레임 결과 캐시

최적화 포인트:
  1. EasyOCR 모델 1회만 로드 (3-5초, 500MB RAM 절약)
  2. 동일 프레임 재요청 시 캐시에서 즉시 반환
     → video_clip / image / font 분석기가 같은 프레임 OCR 중복 방지
  3. 전역 lock으로 스레드 안전 보장
"""
import threading
import hashlib
import structlog
import numpy as np

from utils.model_load import MODEL_LOAD_LOCK

logger = structlog.get_logger()

# ─── OCR 싱글톤 ───
_lock   = threading.Lock()
_engine = None
_failed = False

# ─── 프레임 결과 캐시 ───
# key: MD5(64×36 썸네일 bytes)  → value: readtext 원본 결과 리스트
_cache      : dict = {}
_cache_lock = threading.Lock()
_CACHE_MAX  = 500          # 최대 보관 항목 수 (500 × ~200B ≈ 100KB)


# ─────────────────────────────────────────────
# 캐시 키 생성
# ─────────────────────────────────────────────
def _cache_key(image: np.ndarray) -> str:
    """64×36 썸네일 MD5 → 빠른 동일성 판단"""
    try:
        import cv2
        small = cv2.resize(image, (64, 36))
        return hashlib.md5(small.tobytes()).hexdigest()
    except Exception:
        return ""


# ─────────────────────────────────────────────
# 싱글톤 관리
# ─────────────────────────────────────────────
def get_shared_ocr():
    """
    EasyOCR 싱글톤 반환 (최초 1회만 로드)
    Thread-safe double-checked locking
    """
    global _engine, _failed
    if _engine is not None:
        return _engine
    if _failed:
        return None
    with _lock:
        if _engine is None and not _failed:
            try:
                import easyocr
                # GPU 자동 감지: CUDA 가능 시 OCR 5~10배 가속 (없으면 CPU 폴백)
                try:
                    import torch
                    _gpu = bool(torch.cuda.is_available())
                except Exception:
                    _gpu = False
                with MODEL_LOAD_LOCK:   # CLIP 로드와 겹치면 모델이 meta 장치에 생긴다
                    _engine = easyocr.Reader(["ko", "en"], gpu=_gpu, verbose=False)
                logger.info("shared_ocr_loaded", gpu=_gpu)
            except ImportError:
                logger.warning("easyocr_not_installed")
                _failed = True
            except Exception as e:
                logger.warning("ocr_load_failed", error=str(e))
                _failed = True
    return _engine


# ─────────────────────────────────────────────
# OCR 호출
# ─────────────────────────────────────────────
def safe_ocr(image: np.ndarray, detail: int = 1, paragraph: bool = False,
             min_confidence: float = 0.4) -> list:
    """
    Thread-safe OCR 호출 (캐시 우선)

    Returns (detail=1): [(bbox, text, confidence), ...]
    Returns (detail=0): [text, ...]
    """
    if image is None or image.size == 0:
        return []

    # ── 캐시 조회 (detail=1 만 캐싱) ──
    ck = ""
    if detail == 1:
        ck = _cache_key(image)
        if ck:
            with _cache_lock:
                cached = _cache.get(ck)
            if cached is not None:
                # 신뢰도 필터만 다시 적용
                return [(bbox, text, conf) for bbox, text, conf in cached
                        if conf >= min_confidence]

    ocr = get_shared_ocr()
    if ocr is None:
        return []

    try:
        with _lock:
            results = ocr.readtext(image, detail=detail, paragraph=paragraph)

        # ── 캐시 저장 ──
        if detail == 1 and ck:
            with _cache_lock:
                if len(_cache) < _CACHE_MAX:
                    _cache[ck] = list(results)  # 원본 저장 (필터 없이)

        if detail == 1:
            return [(bbox, text, conf) for bbox, text, conf in results
                    if conf >= min_confidence]
        return results

    except Exception as e:
        logger.debug("ocr_call_failed", error=str(e))
        return []


def extract_text_only(image: np.ndarray, min_confidence: float = 0.4) -> str:
    """프레임에서 텍스트만 추출 (단일 문자열)"""
    results = safe_ocr(image, detail=1, min_confidence=min_confidence)
    return " ".join(text for _, text, _ in results).strip()


def clear_ocr_cache():
    """OCR 캐시 초기화 (새 작업 시작 시 호출 가능)"""
    with _cache_lock:
        _cache.clear()
    logger.info("ocr_cache_cleared")
