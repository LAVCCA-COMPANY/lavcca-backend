"""
analyzers/video_clip_analyzer.py - 영상 클립 저작권 분석

감지 방법 (증거 기반 — 구조 신호는 트리거로만 사용):
  1. 자체 phash DB 비교
  2. 정지 이미지 구간 감지 → Vision/Yandex 역검색 대상으로 수집 (직접 finding 없음)
  3. OCR 워터마크/오버레이 → 스톡 워터마크·방송 채널명 텍스트 (직접 증거)
  4. CLIP 배치 분류        → Vision 역검색 트리거 (직접 finding 없음)
  5. Google Vision + Yandex + YouTube 역검색 → 인덱싱된 출처 확인 후 finding 생성

설계 원칙: CLIP/정지구간 같은 추정 신호만으로는 finding을 만들지 않는다.
finding은 OCR 텍스트(직접 증거) 또는 역검색 결과(인덱싱 증거)가 있을 때만 생성.
"""
import asyncio
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
import structlog
import numpy as np
import requests
import cv2

from config import config
from database.db_manager import get_db_manager
from utils.video_utils import compute_phash
from utils.ocr_engine import safe_ocr
from utils.clip_engine import classify_frames_batch, VISION_TRIGGER_THRESHOLDS, embed_frame
from utils.google_vision_searcher import search_image_for_copyright, clear_vision_cache
from analyzers.yandex_searcher import yandex_reverse_search, clear_yandex_cache

logger = structlog.get_logger()

# 스톡 영상/이미지 서비스 워터마크 키워드
STOCK_WATERMARK_KEYWORDS = {
    "getty images", "gettyimages", "getty",
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
    "videoblocks", "audioblocks", "storyblocks",
    # 뉴스 에이전시
    "ap photo", "associated press",
    "reuters",
    "afp", "agence france",
    "yonhap", "연합뉴스",
    "newsis", "뉴시스",
    "뉴스1", "news1",
}

# 방송사 채널 키워드 (로고/워터마크에 나타날 수 있는)
BROADCAST_KEYWORDS = {
    "kbs", "mbc", "sbs", "jtbc", "tvn", "ocn", "mnet",
    "채널a", "channel a", "tv조선",
    "ytn", "연합뉴스tv",
    "cnn", "bbc", "abc", "nbc", "fox", "cbs",
    "espn", "nfl", "nba", "fifa",
    "hbo", "netflix", "disney+", "apple tv",
}


# ─────────────────────────────────────────────
# YouTube Data API 클라이언트
# ─────────────────────────────────────────────
class YouTubeClient:
    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self):
        self.api_key = config.api.youtube_api_key

    def search_by_title(self, title: str, max_results: int = 5) -> List[Dict]:
        if not self.api_key:
            return []
        try:
            params = {
                "part": "snippet", "q": title,
                "type": "video", "maxResults": max_results,
            }
            # 키는 헤더로 보낸다: URL 에 있으면 연결 오류 메시지(=로그)에 그대로 찍힌다
            response = requests.get(
                f"{self.BASE_URL}/search", params=params,
                headers={"x-goog-api-key": self.api_key},
                timeout=config.pipeline.api_timeout_seconds
            )
            data = response.json()
            results = []
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                results.append({
                    "youtube_id": item["id"].get("videoId"),
                    "title": snippet.get("title", ""),
                    "channel": snippet.get("channelTitle", ""),
                    "published_at": snippet.get("publishedAt", ""),
                })
            return results
        except Exception as e:
            logger.error("youtube_search_error", error=str(e))
            return []


# ─────────────────────────────────────────────
# 영상 클립 분석기
# ─────────────────────────────────────────────
class VideoClipAnalyzer:
    """
    영상 클립 저작권 분석
    외부 API 없이도 동작하는 로컬 지표 우선
    """

    def __init__(self):
        self.youtube = YouTubeClient()
        self.db = get_db_manager()
        self.executor = ThreadPoolExecutor(max_workers=4)

    async def analyze(self, frames: List[Tuple[float, np.ndarray]],
                      job_id: str, progress=None) -> List[Dict]:
        logger.info("video_clip_analysis_start", frames=len(frames), job_id=job_id)

        # 새 분석 작업 시작 시 역검색 캐시 초기화
        # → 이전 작업의 캐시가 다음 작업에 영향을 주지 않도록
        clear_vision_cache()
        clear_yandex_cache()
        if progress:
            # 단계: 내부DB → OCR검사 → CLIP분류+역검색 (어느 단계가 병목인지 표시)
            progress.set_total("video_clip", 3, note="내부 DB 스캔")

        findings = []

        # 1. 자체 DB phash 비교 (로고)
        db_findings = await self._check_internal_db(frames, job_id)
        findings.extend(db_findings)

        # 1-b. 자체 학습 콘텐츠 DB 전체 프레임 스캔 (pHash, API 비용 0)
        #   → 과거 확인된 저작물은 CLIP 트리거 여부와 무관하게 영상 어디서든 감지
        #   → SerpAPI 소진·Vision 쿼터 한계와 무관하게 작동 (미탐↓ + 무료)
        internal_findings, matched_ts = self._scan_internal_phash_db(frames, job_id)
        findings.extend(internal_findings)
        if internal_findings:
            logger.info("internal_phash_hits", count=len(internal_findings))

        # 2. 정지 이미지 구간 감지 → Vision/Yandex 분석 대상 수집 (finding 직접 생성 안 함)
        static_frames = self._detect_static_image_inserts(frames, job_id)
        if static_frames:
            logger.info("static_frames_queued", count=len(static_frames))

        loop = asyncio.get_event_loop()
        if progress:
            progress.advance("video_clip", note="OCR 워터마크 검사 중")

        # 3. 스톡 워터마크 / 방송 오버레이 OCR 검사 (대표 프레임만)
        ocr_findings = await self._ocr_scan_key_frames(frames, job_id)
        findings.extend(ocr_findings)

        if progress:
            progress.advance("video_clip", note="CLIP 분류 + 역검색 중")

        # 4. CLIP 배치 분류 + Vision/Yandex 역검색 (정지 이미지 프레임 포함)
        #    이미 자체 DB로 확정된 프레임(matched_ts)은 외부 API 호출에서 제외
        clip_findings = await loop.run_in_executor(
            self.executor,
            self._run_clip_classification,
            frames, job_id, static_frames, matched_ts,
        )
        findings.extend(clip_findings)
        logger.info("clip_classification_done", count=len(clip_findings))

        findings = self._deduplicate_findings(findings)
        findings.sort(key=lambda x: x.get("timestamp_start", 0))
        logger.info("video_clip_analysis_done", findings=len(findings), job_id=job_id)
        if progress:
            progress.done("video_clip", note=f"{len(findings)}건 발견")
        return findings

    # ─────────────────────────────────────────────
    # 자체 DB phash 비교
    # ─────────────────────────────────────────────
    async def _check_internal_db(self, frames: List[Tuple[float, np.ndarray]],
                                   job_id: str) -> List[Dict]:
        sampled = []
        last_t = -10
        for ts, frame in frames:
            if ts - last_t >= 10:
                sampled.append((ts, frame))
                last_t = ts

        findings = []
        for ts, frame in sampled:
            try:
                phash = compute_phash(frame)
                result = self.db.lookup_logo_by_phash(phash, threshold=8)
                if result and result.get("found"):
                    findings.append({
                        "job_id": job_id,
                        "finding_type": "video_clip",
                        "timestamp_start": ts,
                        "timestamp_end": ts,
                        "timestamp_display": self._format_time(ts),
                        "title": result.get("brand_name", "알 수 없는 클립"),
                        "rights_holder": result.get("trademark_owner", ""),
                        "source": "internal_db",
                        "confidence_score": result.get("similarity", 0.8),
                        "risk_score": min(result.get("similarity", 0.8) * 0.9, 0.95),
                        "risk_level": "HIGH",
                        "description": f"내부 DB 클립 매칭: {result.get('brand_name', '')}",
                    })
            except Exception:
                pass
        return findings

    # ─────────────────────────────────────────────
    # 자체 학습 콘텐츠 DB 전체 프레임 스캔 (pHash, 무료)
    # ─────────────────────────────────────────────
    def _scan_internal_phash_db(self, frames: List[Tuple[float, np.ndarray]],
                                 job_id: str) -> Tuple[List[Dict], set]:
        """
        모든 프레임의 pHash를 자체 학습 콘텐츠 DB와 대조 → API 없이 즉시 감지.

        외부 역검색(Vision/Yandex)은 CLIP이 트리거한 ~20프레임에만 도는 반면,
        이 조회는 비용이 0이라 전체 프레임에 돌릴 수 있다.
        → 한 번 확인된 저작물은 CLIP이 못 알아채도 영상 어디서든 잡힌다.

        Returns: (findings, matched_timestamps)
        """
        findings: List[Dict] = []
        matched_ts: set = set()
        for ts, frame in frames:
            try:
                ph = compute_phash(frame)
            except Exception:
                continue
            hit = self.db.lookup_content_by_phash(ph)
            if not hit:
                continue
            matched_ts.add(ts)
            _lid = hit.get("learned_id")
            risk = hit.get("risk_score", 0.70)
            findings.append({
                "job_id":           job_id,
                "finding_type":     "video_clip",
                "timestamp_start":  ts,
                "timestamp_end":    ts,
                "timestamp_display": self._format_time(ts),
                "title":            f"[자체학습] {hit.get('title') or hit.get('rights_holder', '')}",
                "rights_holder":    "",
                "source":           "internal_phash_db",
                "confidence_score": hit.get("similarity", 0.95),
                "risk_score":       round(risk, 3),
                "risk_level":       self._get_risk_level(risk),
                "description": (
                    f"자체 학습 DB pHash 일치 (유사도 {hit.get('similarity', 0):.0%}, "
                    f"누적 {hit.get('detection_count', 1)}회, 학습ID emb:{_lid} — "
                    f"오학습이면 python main.py --forget emb:{_lid}): "
                    f"{hit.get('title', '')}"
                ),
                "reference_url":    hit.get("reference_url"),
            })
        return findings, matched_ts

    # ─────────────────────────────────────────────
    # 정지 이미지 구간 감지
    # ─────────────────────────────────────────────
    def _detect_static_image_inserts(self, frames: List[Tuple[float, np.ndarray]],
                                      job_id: str,
                                      min_static_seconds: float = 2.5,
                                      max_duration: float = 90.0,
                                      ) -> List[Tuple[float, np.ndarray]]:
        """
        타임스탬프 간격 기반 정지 이미지 구간 감지.

        extract_frames_smart는 pHash 유사 프레임을 제거하므로
        연속 프레임 간격이 기본 샘플링 간격보다 크게 벌어진 구간 = 정지 화면.

        기본 샘플링 간격은 영상 길이에 따라 달라진다 (1fps~0.2fps).
        고정 임계값(2.5초)만 쓰면 10분 이상 영상(간격 3.3초+)에서
        모든 인접 프레임이 정지 구간으로 오인 → Vision 호출 폭증.
        → 실측 최소 간격(=샘플링 스텝)의 1.9배를 함께 요구해 해결.

        [설계] finding을 직접 생성하지 않고 정지 구간 시작 프레임을 반환.
        → _run_clip_classification 에서 Vision/Yandex로 실제 저작권 확인 후 finding 생성.
        → 크리에이터 자체 제작 타이틀 카드·전환 슬라이드 등 오탐 방지.
        """
        if len(frames) < 3:
            return []

        gaps = [frames[i][0] - frames[i - 1][0] for i in range(1, len(frames))]
        base_step = min(gaps)   # 샘플링 스텝 추정 (dedup 안 된 인접 쌍 간격)
        if base_step <= 0:
            return []
        threshold = max(min_static_seconds, base_step * 1.9)

        result: List[Tuple[float, np.ndarray]] = []
        for i in range(1, len(frames)):
            gap = frames[i][0] - frames[i - 1][0]
            if threshold < gap <= max_duration:
                result.append((frames[i - 1][0], frames[i - 1][1]))
        return result

    # ─────────────────────────────────────────────
    # OCR 스캔 (스톡 워터마크 + 방송 오버레이)
    # ─────────────────────────────────────────────
    async def _ocr_scan_key_frames(self, frames: List[Tuple[float, np.ndarray]],
                                    job_id: str) -> List[Dict]:
        """
        대표 프레임에서 OCR로 스톡 워터마크 / 방송 채널명 감지
        - 20초 간격 샘플링 (image_analyzer의 OCR 허용 간격과 정렬 →
          동일 프레임은 OCR 결과 캐시를 공유해 실제 비용 증가 없음)
        - 빠른 사전 필터: 균일 프레임 스킵
        """
        key_frames = []
        last_t = -20
        for ts, frame in frames:
            if ts - last_t >= 20:
                key_frames.append((ts, frame))
                last_t = ts

        if not key_frames:
            return []

        loop = asyncio.get_event_loop()
        results = await asyncio.gather(
            *[
                loop.run_in_executor(
                    self.executor,
                    self._ocr_single_frame,
                    ts, frame, job_id
                )
                for ts, frame in key_frames
            ],
            return_exceptions=True
        )

        findings = []
        seen = set()
        for result in results:
            if isinstance(result, Exception) or not result:
                continue
            for f in result:
                key = f"{f.get('title', '')}_{int(f.get('timestamp_start', 0) // 30)}"
                if key not in seen:
                    seen.add(key)
                    findings.append(f)
        return findings

    def _ocr_single_frame(self, timestamp: float, frame: np.ndarray,
                           job_id: str) -> List[Dict]:
        """
        단일 프레임 OCR 분석 — 스톡 워터마크 텍스트 / 방송 채널명 감지

        텍스트가 읽힌 뒤에만 스톡·방송 키워드를 맞춘다.
        FFT 로 중앙 강화 OCR 을 고르던 분기는 뺐다. 그 함수가 일반 프레임에서도
        거의 항상 참이라, 20초마다 EasyOCR 이 한 번 더 돌았다.
        """
        h, w = frame.shape[:2]

        # ── 빠른 사전 필터: 균일 프레임 스킵 ──
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if float(gray.std()) < 10.0:
                return []
        except Exception:
            pass

        findings = []

        # 전체 프레임 OCR. 스톡 키워드는 이 결과에서 찾는다.
        ocr_results = safe_ocr(frame, detail=1, min_confidence=0.35)
        if not ocr_results:
            return findings

        # ── bbox 중심점 기반 영역별 텍스트 분리 (추가 OCR 없음) ──
        def region_text(y_min_r: float, y_max_r: float,
                        x_min_r: float = 0.0, x_max_r: float = 1.0) -> str:
            y0, y1 = h * y_min_r, h * y_max_r
            x0, x1 = w * x_min_r, w * x_max_r
            return " ".join(
                text for (bbox, text, _) in ocr_results
                if x0 <= (bbox[0][0] + bbox[2][0]) / 2 <= x1
                and y0 <= (bbox[0][1] + bbox[2][1]) / 2 <= y1
            ).lower()

        full_text  = " ".join(t for _, t, _ in ocr_results).lower()
        lower_text = region_text(0.70, 1.00)
        tl_text    = region_text(0.00, 0.18, 0.00, 0.22)
        tr_text    = region_text(0.00, 0.18, 0.78, 1.00)
        bl_text    = region_text(0.82, 1.00, 0.00, 0.22)
        br_text    = region_text(0.82, 1.00, 0.78, 1.00)

        # ── 스톡 워터마크 (전체 프레임 OCR) ──
        stock_hit = self._check_stock_keywords(full_text)
        if stock_hit:
            findings.append({
                "job_id": job_id,
                "finding_type": "video_clip",
                "timestamp_start": timestamp,
                "timestamp_end": timestamp,
                "timestamp_display": self._format_time(timestamp),
                "title": f"스톡 영상: {stock_hit}",
                "rights_holder": stock_hit,
                "source": "stock_watermark_ocr",
                "confidence_score": 0.90,
                "risk_score": 0.88,
                "risk_level": "HIGH",
                "description": f"스톡 미디어 워터마크 감지: '{stock_hit}'",
            })
            return findings  # 스톡 확인 시 추가 검사 불필요

        # ── 방송 하단 오버레이 ──
        broadcast_hit = self._check_broadcast_keywords(lower_text)
        if broadcast_hit:
            findings.append({
                "job_id": job_id,
                "finding_type": "video_clip",
                "timestamp_start": timestamp,
                "timestamp_end": timestamp,
                "timestamp_display": self._format_time(timestamp),
                "title": f"방송 클립: {broadcast_hit}",
                "rights_holder": broadcast_hit,
                "source": "broadcast_overlay_ocr",
                "confidence_score": 0.80,
                "risk_score": 0.75,
                "risk_level": "HIGH",
                "description": f"방송 채널 하단 오버레이 감지: '{broadcast_hit}'",
            })
            # YouTube API로 원본 영상 검색 (하단 자막 텍스트 활용)
            if lower_text.strip():
                yt_result = self._search_youtube_by_text(lower_text)
                if yt_result:
                    findings.append({
                        "job_id": job_id,
                        "finding_type": "video_clip",
                        "timestamp_start": timestamp,
                        "timestamp_end": timestamp,
                        "timestamp_display": self._format_time(timestamp),
                        "title": f"YouTube 원본 영상 의심: {yt_result['title'][:40]}",
                        "rights_holder": yt_result.get("channel", broadcast_hit),
                        "source": "youtube_api_text_search",
                        "confidence_score": 0.72,
                        "risk_score": 0.78,
                        "risk_level": "HIGH",
                        "description": (
                            f"YouTube 검색 매칭: '{yt_result['title']}' "
                            f"— 채널: {yt_result.get('channel', '불명')}"
                        ),
                        "reference_url": (
                            f"https://www.youtube.com/watch?v={yt_result['youtube_id']}"
                            if yt_result.get("youtube_id") else None
                        ),
                    })

        # ── 코너 로고 (4개 코너를 bbox로 분리, 추가 OCR 없음) ──
        corner_map = [("좌상", tl_text), ("우상", tr_text),
                      ("좌하", bl_text), ("우하", br_text)]
        for pos_name, corner_text in corner_map:
            if not corner_text:
                continue
            bcast = self._check_broadcast_keywords(corner_text)
            if bcast:
                findings.append({
                    "job_id": job_id,
                    "finding_type": "video_clip",
                    "timestamp_start": timestamp,
                    "timestamp_end": timestamp,
                    "timestamp_display": self._format_time(timestamp),
                    "title": f"방송 클립: {bcast}",
                    "rights_holder": bcast,
                    "source": "corner_logo_ocr",
                    "confidence_score": 0.78,
                    "risk_score": 0.72,
                    "risk_level": "HIGH",
                    "description": f"코너 방송 로고 감지 ({pos_name}): '{bcast}'",
                })
                break

        return findings

    def _check_stock_keywords(self, text: str) -> Optional[str]:
        for kw in STOCK_WATERMARK_KEYWORDS:
            if kw in text:
                return kw
        return None

    def _check_broadcast_keywords(self, text: str) -> Optional[str]:
        for kw in BROADCAST_KEYWORDS:
            if kw in text:
                return kw
        return None

    # ─────────────────────────────────────────────
    # CLIP 배치 분류
    # ─────────────────────────────────────────────
    def _run_clip_classification(
        self,
        frames: list,
        job_id: str,
        extra_vision_frames: Optional[List[Tuple[float, np.ndarray]]] = None,
        skip_ts: Optional[set] = None,
    ) -> list:
        """
        전체 추출 프레임을 CLIP으로 배치 분류 → Vision/Yandex/YouTube 역검색.

        [설계] CLIP은 Vision 트리거 역할만 담당 — finding을 직접 생성하지 않음.
        CLIP 단독 finding은 오탐률이 높으므로 제거.
        finding은 Vision/Yandex/OCR 결과가 있을 때만 생성.

        [재현율] CLIP은 로컬 추론이라 비용이 없으므로 샘플링 없이 전 프레임 분류.
        30초 간격 샘플링은 짧게 삽입된 클립(5~20초)을 통째로 놓치는 원인이었음.
        역검색 비용은 _select_reverse_targets의 상한이 통제한다.

        extra_vision_frames: 정지 이미지 구간 프레임 (_detect_static_image_inserts 결과)
        → CLIP 트리거 프레임과 합산하여 Vision/Yandex 분석.
        """
        try:
            if not frames and not extra_vision_frames:
                return []

            # ── CLIP 배치 분류: 전 프레임 (로컬 추론, 320프레임 ≈ 20~40초) ──
            batch_results = classify_frames_batch([f for _, f in frames]) if frames else []

            # ── 역검색 후보 수집: (ts, frame, priority) ──
            # 이미 자체 DB로 확정된 프레임(skip_ts)은 외부 API 낭비 방지 위해 제외
            skip_ts = skip_ts or set()
            candidates: list = []
            for i, (ts, frame) in enumerate(frames):
                if ts in skip_ts:
                    continue
                r = batch_results[i] if i < len(batch_results) else None
                if not r:
                    continue
                cat  = r.get("category", "")
                conf = r.get("confidence", 0.0)
                thr = VISION_TRIGGER_THRESHOLDS.get(cat, 999.0)
                if conf >= thr:
                    candidates.append((ts, frame, conf))

            # 정지 이미지 구간 프레임: 삽입 이미지일 확률이 높아 우선순위 부여
            for ts, frame in (extra_vision_frames or []):
                if ts not in skip_ts:
                    candidates.append((ts, frame, 0.60))

            targets = self._select_reverse_targets(candidates, cap=20)

            if targets:
                logger.info("reverse_search_targets",
                            candidates=len(candidates),
                            selected=len(targets),
                            static=len(extra_vision_frames or []))

            # ── Google Vision + Yandex + YouTube 역검색 (프레임 병렬 처리) ──
            # 프레임당 Vision + 업로드 + SerpAPI가 직렬로 돌면 수 분 소요
            # → 별도 풀에서 4개씩 병렬 처리 (20프레임 기준 ~2분)
            vision_raw_findings: list = []
            if targets:
                from concurrent.futures import ThreadPoolExecutor as _Pool
                with _Pool(max_workers=4) as pool:
                    for frame_findings in pool.map(
                        lambda tf: self._reverse_search_frame(tf[0], tf[1], job_id),
                        targets,
                    ):
                        vision_raw_findings.extend(frame_findings)

            # Temporal Voting: 같은 출처가 여러 프레임에서 반복 감지 → 신뢰도 상향
            return self._temporal_vote_findings(vision_raw_findings)

        except Exception as e:
            logger.warning("clip_classification_failed", error=str(e))
            return []

    @staticmethod
    def _select_reverse_targets(candidates: List[Tuple[float, np.ndarray, float]],
                                 cap: int = 20,
                                 bucket_sec: float = 60.0,
                                 ) -> List[Tuple[float, np.ndarray]]:
        """
        역검색 대상 선정 — 시간 커버리지 우선 + 신뢰도 보충.

        1) 동일 타임스탬프 중복 제거 (높은 priority 유지)
        2) 60초 버킷마다 최고 priority 1개 → 영상 전체를 고르게 커버
           (등간격 샘플링과 달리 의심도가 높은 프레임이 버킷 대표가 됨)
        3) cap 미달 시 남은 후보를 priority 내림차순으로 보충
        """
        if not candidates:
            return []

        # 타임스탬프 중복 제거 (max priority)
        by_ts: Dict[float, Tuple[float, np.ndarray, float]] = {}
        for c in candidates:
            if c[0] not in by_ts or c[2] > by_ts[c[0]][2]:
                by_ts[c[0]] = c
        uniq = list(by_ts.values())

        # 버킷별 최고 priority 후보 선택
        by_bucket: Dict[int, Tuple[float, np.ndarray, float]] = {}
        for c in uniq:
            b = int(c[0] // bucket_sec)
            if b not in by_bucket or c[2] > by_bucket[b][2]:
                by_bucket[b] = c
        picked = sorted(by_bucket.values(), key=lambda c: -c[2])[:cap]

        # cap 미달 시 priority 순 보충
        if len(picked) < cap:
            picked_ts = {c[0] for c in picked}
            rest = sorted((c for c in uniq if c[0] not in picked_ts),
                          key=lambda c: -c[2])
            picked.extend(rest[:cap - len(picked)])

        picked.sort(key=lambda c: c[0])
        return [(ts, frame) for ts, frame, _ in picked]

    # ─────────────────────────────────────────────
    # 프레임 단위 역검색 (Vision → Yandex → YouTube)
    # ─────────────────────────────────────────────
    def _reverse_search_frame(self, ts: float, frame: np.ndarray,
                               job_id: str) -> List[Dict]:
        """
        단일 프레임에 대해 Google Vision / Yandex / YouTube 역검색 수행.
        병렬 실행되므로 공유 상태 없이 이 프레임의 finding 목록만 반환.
        교차 프레임 중복은 _temporal_vote_findings가 출처별로 병합한다.
        """
        frame_findings: List[Dict] = []
        try:
            # ── 0. 자체 임베딩 DB 조회: 과거 확인된 콘텐츠면 API 호출 없이 즉시 감지 ──
            # Vision/SerpAPI 쿼터 소진 시에도 작동하는 무료 백업 경로
            emb = embed_frame(frame)
            known = self.db.lookup_content_by_embedding(emb) if emb is not None else None
            if known:
                _lid = known.get("learned_id")
                frame_findings.append({
                    "job_id":           job_id,
                    "finding_type":     "video_clip",
                    "timestamp_start":  ts,
                    "timestamp_end":    ts,
                    "timestamp_display": self._format_time(ts),
                    "title":            f"[자체학습] {known.get('title') or known.get('rights_holder', '')}",
                    "rights_holder":    "",
                    "source":           "internal_embedding_db",
                    "confidence_score": known.get("similarity", 0.92),
                    "risk_score":       round(known.get("risk_score", 0.70), 3),
                    "risk_level":       self._get_risk_level(known.get("risk_score", 0.70)),
                    "description": (
                        f"자체 학습 DB 일치 (유사도 {known.get('similarity', 0):.0%}, "
                        f"누적 {known.get('detection_count', 1)}회, "
                        f"학습ID emb:{_lid} — 오학습이면 "
                        f"python main.py --forget emb:{_lid}): "
                        f"{known.get('title', '')}"
                    ),
                    "reference_url":    known.get("reference_url"),
                })
                logger.info("embedding_db_hit",
                            title=known.get("title", "")[:40],
                            sim=known.get("similarity"), ts=ts)
                return frame_findings   # API 호출 절약

            # ── Google Vision ──
            vr = search_image_for_copyright(frame)
            if vr is not None and not vr.get("is_free"):
                _holder  = vr.get("rights_holder")
                _entity  = vr.get("detected_entity")
                _rsc     = vr.get("risk_score", 0.0)
                _rlv     = vr.get("risk_level", "MEDIUM")
                _n_full  = vr.get("full_matches", 0)
                _ocr_cr  = vr.get("ocr_copyright")
                _src_url = vr.get("source_url")
                _pages   = vr.get("pages", [])
                if _holder or _n_full > 0:
                    _sources_str = self._extract_domains(
                        ([_src_url] if _src_url else []) + (_pages or [])
                    ) or _holder or "알 수 없음"
                    _title = (
                        f"인터넷 영상: {_entity}"
                        if _entity else f"인터넷 영상 발견: {_sources_str}"
                    )
                    _desc = (
                        f"Google 역이미지: {_sources_str}에서 발견됨"
                        + (f" (완전 일치 {_n_full}건)" if _n_full else "")
                    )
                    if _ocr_cr:
                        _desc += f" [이미지 내 텍스트: {_ocr_cr}]"
                    frame_findings.append({
                        "job_id":           job_id,
                        "finding_type":     "video_clip",
                        "timestamp_start":  ts,
                        "timestamp_end":    ts,
                        "timestamp_display": self._format_time(ts),
                        "title":            _title,
                        "rights_holder":    "",
                        "source":           "google_vision_web_search+clip",
                        "confidence_score": round(min(0.68 + _n_full * 0.04, 0.95), 3),
                        "risk_score":       round(_rsc, 3),
                        "risk_level":       _rlv,
                        "description":      _desc,
                        "reference_url":    _src_url,
                    })
                    logger.info("vision_clip_hit",
                                sources=_sources_str, entity=_entity, ts=ts)

                    # ── 자체 임베딩 DB 학습: 출처가 확실한 경우만 (오염 방지) ──
                    # 다음 분석부터 같은 콘텐츠는 API 없이 위 0번 경로로 감지
                    if emb is not None and (_holder or _n_full >= 2):
                        self.db.learn_content_embedding({
                            "title":            _title,
                            "rights_holder":    _holder or _sources_str,
                            "source":           "google_vision",
                            "reference_url":    _src_url,
                            "risk_score":       _rsc,
                            "phash":            compute_phash(frame),
                            "embedding":        emb,
                            "job_id":           job_id,
                            "source_timestamp": ts,
                        })

            # ── Yandex 역이미지 (Vision 결과와 무관하게 항상 실행) ──
            _yr = yandex_reverse_search(frame, ts)
            if _yr and not _yr.get("error") and _yr.get("has_high_risk"):
                _y_holder  = _yr["rights_holder"]
                _y_domains = set(_yr["top_domains"])
                _gv_holder = (vr or {}).get("rights_holder", "") if vr is not None else ""
                _common = bool(_gv_holder) and any(
                    _gv_holder.lower().find(d) >= 0 or d.find(_gv_holder.lower()) >= 0
                    for d in _y_domains if d
                )
                _y_domains_str = ", ".join(sorted(_y_domains)[:3]) or _y_holder or "알 수 없음"
                # 동일 프레임에서 Vision이 이미 같은 도메인을 보고했으면 중복 생략
                _already_y = bool(_y_holder) and any(
                    _y_holder in (r.get("title", "") + r.get("description", ""))
                    for r in frame_findings
                )
                if not _already_y and (_y_holder or _yr.get("matches")):
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
                        _y_desc = f"Yandex 역이미지: {_y_domains_str}에서 발견됨"
                    _y_ref = _yr["matches"][0]["url"] if _yr.get("matches") else None
                    frame_findings.append({
                        "job_id":           job_id,
                        "finding_type":     "video_clip",
                        "timestamp_start":  ts,
                        "timestamp_end":    ts,
                        "timestamp_display": self._format_time(ts),
                        "title":            f"인터넷 영상 발견: {_y_domains_str}",
                        "rights_holder":    "",
                        "source":           _y_src,
                        "confidence_score": _y_conf,
                        "risk_score":       _y_risk,
                        "risk_level":       _y_level,
                        "description":      _y_desc,
                        "reference_url":    _y_ref,
                    })
                    logger.info("yandex_clip_hit",
                                domains=_y_domains_str, cross=_common, ts=ts)

            # ── YouTube API: Vision 엔티티(작품명/아티스트명)로 원본 영상 검색 ──
            if vr is not None and vr.get("detected_entity"):
                _yt_query  = vr["detected_entity"]
                _yt_result = self._search_youtube_by_text(_yt_query)
                if _yt_result:
                    frame_findings.append({
                        "job_id":           job_id,
                        "finding_type":     "video_clip",
                        "timestamp_start":  ts,
                        "timestamp_end":    ts,
                        "timestamp_display": self._format_time(ts),
                        "title":            f"YouTube 원본 의심: {_yt_result['title'][:40]}",
                        "rights_holder":    "",
                        "source":           "youtube_api_entity_search",
                        "confidence_score": 0.70,
                        "risk_score":       0.65,
                        "risk_level":       "MEDIUM",
                        "description": (
                            f"Vision 엔티티 '{_yt_query}' YouTube 검색: "
                            f"'{_yt_result['title']}'"
                            f" — 채널: {_yt_result.get('channel', '불명')}"
                        ),
                        "reference_url": (
                            f"https://www.youtube.com/watch?v={_yt_result['youtube_id']}"
                            if _yt_result.get("youtube_id") else None
                        ),
                    })
                    logger.info("youtube_entity_hit",
                                entity=_yt_query,
                                title=_yt_result.get("title", "")[:40],
                                ts=ts)

        except Exception as e:
            logger.warning("reverse_search_frame_failed", ts=ts, error=str(e)[:80])

        return frame_findings

    @staticmethod
    def _extract_domains(urls: List[str], limit: int = 3) -> str:
        """URL 목록에서 고유 도메인 추출 → 'a.com, b.com' 형태 문자열"""
        from urllib.parse import urlparse
        domains: List[str] = []
        for u in urls:
            if not u:
                continue
            try:
                d = urlparse(u).netloc.lower()
                if d.startswith("www."):
                    d = d[4:]
                if d and d not in domains:
                    domains.append(d)
            except Exception:
                pass
        return ", ".join(domains[:limit])

    # ─────────────────────────────────────────────
    # YouTube API 텍스트 검색
    # ─────────────────────────────────────────────
    def _search_youtube_by_text(self, text: str, max_words: int = 8) -> Optional[Dict]:
        """
        OCR로 감지된 텍스트로 YouTube 검색 → 원본 영상 찾기.
        너무 짧거나 일반적인 단어는 스킵.

        Returns:
            {"title": ..., "channel": ..., "youtube_id": ...} or None
        """
        if not self.youtube.api_key:
            return None
        # 의미있는 단어만 추출 (불용어 제거)
        _STOPWORDS = {"a", "the", "is", "and", "or", "in", "at", "to", "of",
                      "이", "가", "은", "는", "을", "를", "에", "의", "와", "과"}
        words = [w for w in text.split() if len(w) >= 2 and w not in _STOPWORDS]
        if len(words) < 2:
            return None
        query = " ".join(words[:max_words])
        try:
            results = self.youtube.search_by_title(query, max_results=1)
            if results:
                r = results[0]
                # ── 자기 채널 필터: 본인 채널 영상이면 오탐 방지 ──
                from utils.google_vision_searcher import _OWN_CHANNELS
                if _OWN_CHANNELS:
                    ch_lower = r.get("channel", "").lower()
                    if any(own in ch_lower for own in _OWN_CHANNELS):
                        logger.info("youtube_own_channel_skip",
                                    channel=r.get("channel"), own=list(_OWN_CHANNELS)[:2])
                        return None
                # 검색어와 결과 제목의 단어 겹침 확인 (관련성 필터)
                result_words = set(r.get("title", "").lower().split())
                query_words  = set(query.lower().split())
                overlap = len(result_words & query_words)
                if overlap >= 1:  # 최소 1단어 겹치면 관련 있음
                    return r
        except Exception as e:
            logger.debug("youtube_search_failed", error=str(e)[:60])
        return None

    # ─────────────────────────────────────────────
    # Temporal Voting: 반복 감지된 저작권자 신뢰도 상향
    # ─────────────────────────────────────────────
    def _temporal_vote_findings(self, findings: List[Dict]) -> List[Dict]:
        """
        같은 rights_holder가 여러 프레임에서 반복 감지된 경우 신뢰도 상향.

        논리:
        - 독립된 두 프레임에서 동일 권리자 → 우연의 일치 아님 → confidence ↑
        - 단독 감지 1건: 원래 confidence 유지
        - 2건 감지: confidence × 1.15, risk_level LOW→MEDIUM 상향 가능
        - 3건 이상: confidence × 1.25, risk_level MEDIUM→HIGH 상향 가능
        - 단, 이미 HIGH이면 변경 없음

        Returns:
            통합된 finding 목록 (중복 제거 + 신뢰도 조정)
        """
        if not findings:
            return []

        # 그룹화 키: rights_holder → title의 첫 번째 도메인/작품명 → source 순 폴백
        def _vote_key(f: Dict) -> str:
            holder = f.get("rights_holder", "")
            if holder:
                return holder
            title = f.get("title", "")
            # "인터넷 영상 발견: gettyimages.com, ..." → "gettyimages.com"
            # "YouTube 원본 의심: 제목..."          → "제목..."
            if ": " in title:
                candidate = title.split(": ", 1)[-1].split(",")[0].strip()
                if candidate:
                    return candidate
            return f.get("source", "__unknown__")

        groups: Dict[str, List[Dict]] = {}
        for f in findings:
            key = _vote_key(f)
            if key not in groups:
                groups[key] = []
            groups[key].append(f)

        result = []
        for holder, group in groups.items():
            count = len(group)
            if count == 1:
                result.append(group[0])
                continue

            # 가장 높은 신뢰도의 finding을 기준으로 통합
            best = max(group, key=lambda x: x.get("confidence_score", 0))
            base_conf  = best.get("confidence_score", 0.7)
            base_risk  = best.get("risk_score", 0.5)
            base_level = best.get("risk_level", "MEDIUM")

            # 신뢰도 보정
            if count >= 3:
                new_conf  = min(base_conf * 1.25, 0.97)
                new_risk  = min(base_risk * 1.15, 0.96)
                extra_msg = f" [동일 출처 {count}회 반복 감지 → 신뢰도 상향]"
                # MEDIUM → HIGH 상향
                if base_level == "MEDIUM":
                    base_level = "HIGH"
            else:
                new_conf  = min(base_conf * 1.15, 0.95)
                new_risk  = min(base_risk * 1.08, 0.94)
                extra_msg = f" [동일 출처 {count}회 감지]"

            updated = dict(best)
            updated["confidence_score"] = round(new_conf, 3)
            updated["risk_score"]       = round(new_risk, 3)
            updated["risk_level"]       = base_level
            updated["description"]      = best.get("description", "") + extra_msg
            # 구간 정보 보존: 같은 출처가 감지된 첫~마지막 시점을 span으로
            _t0 = min(f.get("timestamp_start", 0) for f in group)
            _t1 = max(f.get("timestamp_end") or f.get("timestamp_start", 0)
                      for f in group)
            updated["timestamp_start"] = _t0
            updated["timestamp_end"]   = _t1
            if _t1 > _t0:
                updated["timestamp_display"] = (
                    f"{self._format_time(_t0)}~{self._format_time(_t1)}"
                )
            result.append(updated)

        return result

    # ─────────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────────
    def _deduplicate_findings(self, findings: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for f in findings:
            key = f"{f.get('title', '')}_{int(f.get('timestamp_start', 0) // 15)}"
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique

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
