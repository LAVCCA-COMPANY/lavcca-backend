"""
utils/clip_engine.py - AI 기반 저작권 콘텐츠 분류기
비용: 무료 (오프라인 추론)

동작 우선순위:
  1. CLIP (openai/clip-vit-base-patch32) — 최고 정확도
     첫 실행 시 ~340MB 모델 자동 다운로드 (~/.cache/huggingface)
     pip install transformers 필요
  2. EfficientNet-B0 (torchvision) — transformers 없을 때 자동 fallback
     ImageNet 사전학습 가중치 (~20MB), 정확도는 CLIP보다 낮음
  3. Disabled — torch 없을 때 조용히 비활성화

지원 분류 카테고리 (저작권 위험도 관련):
  movie_tv, animation, music_video, news_broadcast,
  sports_broadcast, stock_photo, watermarked, user_content
"""
import threading
import numpy as np
import cv2
import structlog
from typing import Dict, List, Optional

from utils.model_load import MODEL_LOAD_LOCK

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# 카테고리 정의
# ─────────────────────────────────────────────
# (CLIP 텍스트 프롬프트, 기본 저작권 위험도)
CLIP_CATEGORIES: Dict[str, tuple] = {
    "movie_tv": (
        "a dramatic scene from a movie or television drama with professional actors "
        "cinematic lighting film production",
        0.72,
    ),
    "animation": (
        "an animated cartoon or anime scene with colorful illustrated characters 2D 3D animation",
        0.70,
    ),
    "music_video": (
        "a music video or live music stage performance with pop idol singers dancers "
        "choreography stage lighting concert fancam direct cam",
        0.68,
    ),
    "variety_show": (
        "a Korean television variety entertainment show with multiple cast members "
        "colorful caption subtitle graphics studio set reality program comedy talk show",
        0.68,
    ),
    "advertisement": (
        "a television commercial or product advertisement with brand logo polished "
        "product close-up shots promotional marketing text and slogan",
        0.70,
    ),
    "poster_art": (
        "a movie poster album cover or promotional key visual with title typography "
        "dramatic character artwork and promotional graphic design",
        0.75,
    ),
    "news_broadcast": (
        "a television news broadcast with news anchor desk lower third graphics chyron ticker",
        0.58,
    ),
    "sports_broadcast": (
        "a live sports game broadcast with scoreboard stadium crowd athletes competing",
        0.65,
    ),
    "stock_photo": (
        "a professional commercial stock photograph lifestyle photography clean background",
        0.78,
    ),
    "watermarked": (
        "a photograph with visible semi-transparent watermark copyright text overlay branding",
        0.88,
    ),
    "game_footage": (
        "a video game screenshot gameplay footage with HUD heads-up display health bar minimap "
        "inventory UI interface first person shooter RPG battle scene pixel art",
        0.68,
    ),
    "manga_comic": (
        "a manga comic book panel page with illustrated characters speech bubbles Japanese anime "
        "art style black and white ink drawing webtoon manhwa sequential art",
        0.65,
    ),
    "user_content": (
        "an amateur casual home video selfie user generated content low production quality",
        0.08,
    ),
    # ── 거절 클래스: 창작자 자작 화면 흡수 (트리거 안 함, user_content와 동일 역할) ──
    "title_card": (
        "a simple title card or text intro slide with large typography on a plain "
        "or gradient solid color background made by a content creator vlog thumbnail",
        0.06,
    ),
}

# CLIP 분류 → 역검색(Vision/Yandex) 트리거 임계값 (두 분석기 공유)
#
# CLIP은 finding을 직접 만들지 않고 역검색 트리거 역할만 하므로,
# 오탐 통제는 역검색 증거 계층(자기 채널 필터·무료 출처 필터·가중치 임계값)이
# 담당한다. 따라서 이 임계값은 재현율(recall) 위주로 낮게 잡는다.
# 트리거 과다는 역검색 대상 상한(_select_reverse_targets)이 막아준다.
VISION_TRIGGER_THRESHOLDS: Dict[str, float] = {
    "stock_photo":      0.40,
    "watermarked":      0.38,
    "movie_tv":         0.50,
    "animation":        0.50,
    "music_video":      0.46,
    "variety_show":     0.50,   # 예능: 자막 그래픽 특징 뚜렷, 한국 유튜브 최대 공백
    "advertisement":    0.50,   # 광고/CF
    "poster_art":       0.46,   # 포스터·앨범아트: 특정 작품 홍보물 → 역검색 정확
    "news_broadcast":   0.52,
    "sports_broadcast": 0.50,
    "game_footage":     0.46,
    "manga_comic":      0.44,
    # user_content / title_card: 거절 클래스 → 트리거 안 함 (기본값 999)
}

# Finding을 생성할 카테고리 → (최소 신뢰도, risk_level, 한글명)
FINDING_THRESHOLDS: Dict[str, tuple] = {
    "movie_tv":         (0.45, "MEDIUM", "영화/드라마 장면"),
    "animation":        (0.45, "MEDIUM", "애니메이션 콘텐츠"),
    "music_video":      (0.43, "MEDIUM", "뮤직비디오/무대"),
    "variety_show":     (0.45, "MEDIUM", "TV 예능/버라이어티"),
    "advertisement":    (0.45, "MEDIUM", "광고/CF"),
    "poster_art":       (0.43, "HIGH",   "포스터/앨범아트"),
    "news_broadcast":   (0.50, "LOW",    "방송 뉴스"),
    "sports_broadcast": (0.48, "MEDIUM", "스포츠 중계"),
    "stock_photo":      (0.40, "HIGH",   "스톡 사진"),
    "watermarked":      (0.35, "HIGH",   "워터마크 이미지"),
    "game_footage":     (0.42, "MEDIUM", "게임 영상/스크린샷"),
    "manga_comic":      (0.40, "MEDIUM", "만화/웹툰/애니 장면"),
}


# ─────────────────────────────────────────────
# EfficientNet ImageNet 클래스 매핑 (fallback용)
# ─────────────────────────────────────────────
_EFFICIENTNET_CLASS_MAP = {
    "movie_tv":         [563, 664, 782, 851],   # monitor, television, screen
    "music_video":      [541, 612, 819, 820],   # stage, spotlight, theater
    "sports_broadcast": [805, 806, 812, 674],   # sports-related classes
}


# ─────────────────────────────────────────────
# 분류기 싱글톤
# ─────────────────────────────────────────────
class _ClipClassifier:
    """
    Thread-safe 싱글톤 분류기.
    첫 classify() 호출 시 모델을 한 번만 로드한다.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._mode: Optional[str] = None   # "clip" | "efficientnet" | "disabled"
        self._initialized = False

    # ── 초기화 ──────────────────────────────
    def _init(self):
        if self._initialized:
            return
        with self._lock, MODEL_LOAD_LOCK:   # EasyOCR 로드와 겹치지 않게(utils/model_load.py)
            if self._initialized:
                return
            if not self._try_clip():
                self._try_efficientnet()
            self._initialized = True

    def _try_clip(self) -> bool:
        try:
            from transformers import CLIPProcessor, CLIPModel
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("clip_loading", device=device)

            model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
            processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
            model.to(device).eval()

            # 텍스트 특징 사전 계산
            # transformers 5.x 호환: get_text_features()가 BaseModelOutputWithPooling 반환 가능
            texts = [v[0] for v in CLIP_CATEGORIES.values()]
            with torch.no_grad():
                txt_inputs = processor(text=texts, return_tensors="pt", padding=True).to(device)
                text_feats = self._extract_text_features(model, txt_inputs)
                text_feats = text_feats / text_feats.norm(dim=-1, keepdim=True)

            self._model = model
            self._processor = processor
            self._text_feats = text_feats
            self._device = device
            self._categories = list(CLIP_CATEGORIES.keys())
            self._mode = "clip"
            logger.info("clip_ready", device=device, categories=len(self._categories))
            return True
        except Exception as e:
            logger.info("clip_unavailable", reason=str(e)[:120])
            return False

    @staticmethod
    def _extract_text_features(model, inputs):
        """
        CLIP 텍스트 특징 추출 — 모든 transformers 버전 호환.
        text_model → pooler_output → text_projection 순서로 직접 호출.
        get_text_features() API 변경에 영향받지 않는다.
        """
        txt_out = model.text_model(**inputs)
        pooled = txt_out.pooler_output           # (N, text_hidden_size)
        if hasattr(model, "text_projection") and model.text_projection is not None:
            return model.text_projection(pooled)  # (N, projection_dim)
        return pooled

    @staticmethod
    def _extract_image_features(model, inputs):
        """
        CLIP 이미지 특징 추출 — 모든 transformers 버전 호환.
        vision_model → pooler_output → visual_projection 순서로 직접 호출.
        """
        img_out = model.vision_model(**inputs)
        pooled = img_out.pooler_output            # (N, vision_hidden_size)
        if hasattr(model, "visual_projection") and model.visual_projection is not None:
            return model.visual_projection(pooled)  # (N, projection_dim)
        return pooled

    def _try_efficientnet(self) -> bool:
        try:
            import torch
            from torchvision import models, transforms

            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("efficientnet_loading", device=device)

            net = models.efficientnet_b0(weights=models.EfficientNet_B0_Weights.DEFAULT)
            net.eval().to(device)

            tfm = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225]
                ),
            ])

            self._net = net
            self._tfm = tfm
            self._device = device
            self._mode = "efficientnet"
            logger.info("efficientnet_ready")
            return True
        except Exception as e:
            logger.warning("efficientnet_unavailable", reason=str(e)[:80])
            self._mode = "disabled"
            return False

    # ── Public API ──────────────────────────
    def classify_batch(self, frames: List[np.ndarray]) -> List[Optional[Dict]]:
        """
        여러 프레임을 배치로 분류.
        CLIP 모드에서는 한 번의 forward pass → 속도 향상.
        """
        self._init()
        if not frames:
            return []
        if self._mode == "clip":
            return self._batch_clip(frames)
        if self._mode == "efficientnet":
            return [self._single_efficientnet(f) for f in frames]
        return [None] * len(frames)

    def classify(self, frame: np.ndarray) -> Optional[Dict]:
        """단일 프레임 분류 (편의 래퍼)."""
        res = self.classify_batch([frame])
        return res[0] if res else None

    # ── CLIP 배치 추론 ──────────────────────
    def _batch_clip(self, frames: List[np.ndarray]) -> List[Optional[Dict]]:
        import torch
        from PIL import Image

        try:
            pil_imgs = []
            for f in frames:
                if f is None or f.size == 0:
                    pil_imgs.append(Image.new("RGB", (224, 224)))
                    continue
                rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB) if len(f.shape) == 3 else cv2.cvtColor(f, cv2.COLOR_GRAY2RGB)
                pil_imgs.append(Image.fromarray(rgb))

            # 배치 크기 제한 (메모리 보호)
            batch_size = 16
            all_probs = []
            for i in range(0, len(pil_imgs), batch_size):
                batch = pil_imgs[i:i + batch_size]
                inputs = self._processor(images=batch, return_tensors="pt").to(self._device)
                with torch.no_grad():
                    img_feats = self._extract_image_features(self._model, inputs)
                    img_feats = img_feats / img_feats.norm(dim=-1, keepdim=True)
                    sims = img_feats @ self._text_feats.T        # (N, C)
                    probs = sims.softmax(dim=-1).cpu().numpy()  # (N, C)
                all_probs.append(probs)

            all_probs = np.concatenate(all_probs, axis=0)  # (total, C)

            results = []
            for i in range(len(frames)):
                row = all_probs[i]
                best_idx = int(np.argmax(row))
                cat = self._categories[best_idx]
                conf = float(row[best_idx])
                base_risk = CLIP_CATEGORIES[cat][1]
                results.append({
                    "category": cat,
                    "confidence": round(conf, 4),
                    "base_risk": base_risk,
                    "risk_score": round(min(base_risk * (0.4 + conf * 0.6), 1.0), 4),
                    "all_scores": {c: round(float(row[j]), 4) for j, c in enumerate(self._categories)},
                    "mode": "clip",
                })
            return results

        except Exception as e:
            logger.error("clip_batch_failed", error=str(e))
            return [None] * len(frames)

    # ── CLIP 이미지 임베딩 추출 ─────────────
    def embed_batch(self, frames: List[np.ndarray]) -> List[Optional[np.ndarray]]:
        """
        프레임들의 정규화된 CLIP 이미지 임베딩 반환 (512차원 float32).
        자체 콘텐츠 DB의 코사인 유사도 매칭에 사용.
        CLIP 모드가 아니면 (EfficientNet fallback/비활성) None 목록 반환.
        """
        self._init()
        if self._mode != "clip" or not frames:
            return [None] * len(frames)

        import torch
        from PIL import Image

        try:
            pil_imgs = []
            for f in frames:
                if f is None or f.size == 0:
                    pil_imgs.append(Image.new("RGB", (224, 224)))
                    continue
                rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB) if len(f.shape) == 3 \
                    else cv2.cvtColor(f, cv2.COLOR_GRAY2RGB)
                pil_imgs.append(Image.fromarray(rgb))

            feats = []
            for i in range(0, len(pil_imgs), 16):
                batch = pil_imgs[i:i + 16]
                inputs = self._processor(images=batch, return_tensors="pt").to(self._device)
                with torch.no_grad():
                    f = self._extract_image_features(self._model, inputs)
                    f = f / f.norm(dim=-1, keepdim=True)
                feats.append(f.cpu().numpy().astype(np.float32))

            arr = np.concatenate(feats, axis=0)
            return [arr[i] for i in range(len(frames))]
        except Exception as e:
            logger.error("clip_embed_failed", error=str(e))
            return [None] * len(frames)

    # ── EfficientNet 단일 추론 ──────────────
    def _single_efficientnet(self, frame: np.ndarray) -> Optional[Dict]:
        import torch

        try:
            if frame is None or frame.size == 0:
                return None
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if len(frame.shape) == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
            tensor = self._tfm(rgb).unsqueeze(0).to(self._device)
            with torch.no_grad():
                probs_all = torch.softmax(self._net(tensor), dim=-1).cpu().numpy()[0]

            # 카테고리별 관련 ImageNet 클래스 합산
            cat_scores: Dict[str, float] = {}
            for cat, indices in _EFFICIENTNET_CLASS_MAP.items():
                valid = [i for i in indices if i < len(probs_all)]
                cat_scores[cat] = float(np.sum(probs_all[valid])) * 2.5 if valid else 0.0

            best_cat = max(cat_scores, key=cat_scores.get) if cat_scores else "user_content"
            best_score = cat_scores.get(best_cat, 0.0)

            if best_score < 0.06:
                return {
                    "category": "user_content",
                    "confidence": 0.5,
                    "base_risk": CLIP_CATEGORIES["user_content"][1],
                    "risk_score": 0.08,
                    "mode": "efficientnet",
                }

            base_risk = CLIP_CATEGORIES.get(best_cat, ("", 0.3))[1]
            conf = round(min(best_score, 0.82), 4)
            return {
                "category": best_cat,
                "confidence": conf,
                "base_risk": base_risk,
                "risk_score": round(min(base_risk * (0.5 + conf * 0.5), 1.0), 4),
                "mode": "efficientnet",
            }

        except Exception as e:
            logger.error("efficientnet_failed", error=str(e))
            return None


# ─────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────
_classifier = _ClipClassifier()

# ── 프레임 단위 결과 캐시 (이중 계산 제거) ──
# image_analyzer와 video_clip_analyzer가 같은 프레임에 CLIP을 각각 돌리므로,
# 프레임 해시로 분류/임베딩 결과를 캐시 → 두 번째 호출은 추론 없이 재사용.
# (30분 영상 320프레임 × 2분석기 = CLIP 640회 → 320회로 절반 절감)
_frame_cache_lock = threading.Lock()
_classify_cache: Dict[str, Optional[Dict]] = {}
_embed_cache: Dict[str, Optional[np.ndarray]] = {}
_FRAME_CACHE_MAX = 1200


def _frame_key(frame: np.ndarray) -> str:
    try:
        import hashlib
        small = cv2.resize(frame, (32, 32))
        return hashlib.md5(small.tobytes()).hexdigest()
    except Exception:
        return ""


def _cache_put(cache: dict, key: str, val):
    if not key:
        return
    with _frame_cache_lock:
        if len(cache) >= _FRAME_CACHE_MAX:
            for k in list(cache.keys())[:200]:
                del cache[k]
        cache[key] = val


def clear_clip_cache() -> None:
    """새 분석 작업 시작 시 호출 가능 (프레임 캐시 초기화)."""
    with _frame_cache_lock:
        _classify_cache.clear()
        _embed_cache.clear()


def _batched_with_cache(frames: List[np.ndarray], cache: dict, compute) -> list:
    """캐시 미스 프레임만 compute()로 배치 추론 후 결과 병합."""
    keys = [_frame_key(f) for f in frames]
    out = [None] * len(frames)
    miss_idx, miss_frames = [], []
    with _frame_cache_lock:
        for i, k in enumerate(keys):
            if k and k in cache:
                out[i] = cache[k]
            else:
                miss_idx.append(i)
                miss_frames.append(frames[i])
    if miss_frames:
        computed = compute(miss_frames)
        for j, i in enumerate(miss_idx):
            out[i] = computed[j] if j < len(computed) else None
            _cache_put(cache, keys[i], out[i])
    return out


def classify_frame(frame: np.ndarray) -> Optional[Dict]:
    """단일 프레임 저작권 콘텐츠 분류."""
    return classify_frames_batch([frame])[0]


def classify_frames_batch(frames: List[np.ndarray]) -> List[Optional[Dict]]:
    """여러 프레임 배치 분류 (프레임 캐시 → 이중 계산 방지)."""
    if not frames:
        return []
    return _batched_with_cache(frames, _classify_cache, _classifier.classify_batch)


def embed_frame(frame: np.ndarray) -> Optional[np.ndarray]:
    """단일 프레임 CLIP 임베딩 (512차원, 정규화). CLIP 미사용 환경이면 None."""
    res = embed_frames_batch([frame])
    return res[0] if res else None


def embed_frames_batch(frames: List[np.ndarray]) -> List[Optional[np.ndarray]]:
    """여러 프레임 CLIP 임베딩 배치 추출 (프레임 캐시 → 이중 계산 방지)."""
    if not frames:
        return []
    return _batched_with_cache(frames, _embed_cache, _classifier.embed_batch)


def make_finding_from_clip(
    clip_result: Optional[Dict],
    timestamp: float,
    job_id: str,
    finding_type: str = "image",
    format_time_fn=None,
) -> Optional[Dict]:
    """
    CLIP 분류 결과를 finding 딕셔너리로 변환.
    - FINDING_THRESHOLDS에 없는 카테고리 → None
    - 신뢰도가 최소 임계값 미만 → None
    """
    if not clip_result:
        return None

    cat = clip_result.get("category", "")
    conf = clip_result.get("confidence", 0.0)

    if cat not in FINDING_THRESHOLDS:
        return None

    min_conf, risk_level, kor_name = FINDING_THRESHOLDS[cat]
    if conf < min_conf:
        return None

    risk_score = clip_result.get("risk_score", clip_result.get("base_risk", 0.5) * conf)
    mode = clip_result.get("mode", "clip")
    source = "clip_zero_shot" if mode == "clip" else "efficientnet_classification"

    def _default_fmt(s: float) -> str:
        m, sec = divmod(int(s), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}" if h > 0 else f"{m:02d}:{sec:02d}"

    fmt = format_time_fn or _default_fmt

    tag = "[CLIP]" if mode == "clip" else "[EfficientNet]"
    return {
        "job_id": job_id,
        "finding_type": finding_type,
        "timestamp_start": timestamp,
        "timestamp_end": timestamp,
        "timestamp_display": fmt(timestamp),
        "title": f"저작권 콘텐츠 의심: {kor_name}",
        "rights_holder": "",
        "source": source,
        "confidence_score": round(conf, 3),
        "risk_score": round(risk_score, 3),
        "risk_level": risk_level,
        "description": (
            f"AI 콘텐츠 분석 {tag}: {kor_name} 패턴 감지 "
            f"(신뢰도 {conf:.0%})"
        ),
    }
