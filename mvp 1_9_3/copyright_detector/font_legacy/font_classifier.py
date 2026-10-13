"""
utils/font_classifier.py - 한글 폰트 분류 전용 CNN
비용: 무료 (오프라인 추론, ~0.5M 파라미터)

목적:
  자막/썸네일 텍스트 크롭 이미지를 보고 "어떤 폰트로 렌더링됐는지" 식별.
  기존 획 굵기 휴리스틱(명조/고딕/손글씨/장식체 4종)을 대체하는 정밀 경로.

설계 원칙 (오탐 방지):
  - 상업 폰트만 학습하면 모든 자막이 "가장 비슷한 상업 폰트"로 강제 분류됨
    → 무료/시스템 폰트를 거절(negative) 클래스로 함께 학습
  - finding은 (1) 예측 클래스가 commercial=True 이고
              (2) confidence ≥ 임계값, (3) top1-top2 margin ≥ 임계값일 때만
  - 모델 파일이 없으면 조용히 비활성 → font_analyzer가 기존 휴리스틱으로 폴백

학습: python tools/train_font_classifier.py
산출물: models/font_classifier.pt + models/font_classifier_meta.json
"""
import os
import io
import json
import math
import random
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np
import cv2
import structlog

logger = structlog.get_logger()

# 입력 규격 (학습·추론 공유)
INPUT_H = 48
INPUT_W = 256

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "font_classifier.pt")
META_PATH = os.path.join(MODEL_DIR, "font_classifier_meta.json")

# 학습 샘플 텍스트 풀 (자막에 흔한 음절 위주)
_SYLLABLES = (
    "가나다라마바사아자차카타파하"
    "고노도로모보소오조초코토포호"
    "구누두루무부수우주추쿠투푸후"
    "개내대래매배새애재채캐태패해"
    "그는를이가에서의도와로한할했"
    "안녕하세요진짜정말너무완전대박"
    "오늘영상시작합니다구독좋아요"
    "켰단었습는데지만거든요네까지"
)
_WORDS = [
    "안녕하세요", "오늘은", "진짜", "대박", "구독과 좋아요",
    "지금 시작합니다", "충격적인", "결과는?", "꿀팁 대방출",
    "여러분", "1편", "최초 공개", "리뷰", "솔직 후기",
]


# ─────────────────────────────────────────────
# 합성 학습 샘플 렌더러 (trainer와 공유)
# ─────────────────────────────────────────────
def random_text(rng: random.Random) -> str:
    if rng.random() < 0.4:
        return rng.choice(_WORDS)
    n = rng.randint(2, 10)
    return "".join(rng.choice(_SYLLABLES) for _ in range(n))


def render_text_sample(font_path: str, font_index: int = 0,
                       text: Optional[str] = None,
                       rng: Optional[random.Random] = None,
                       augment: bool = True) -> Optional[np.ndarray]:
    """
    지정 폰트로 자막 스타일 텍스트 라인을 렌더링 → (INPUT_H, INPUT_W) float32 [0,1].

    유튜브 자막 현실 반영:
      외곽선(stroke), 임의 전경/배경색, 저해상도 재확대, 블러, JPEG 아티팩트
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    rng = rng or random.Random()
    text = text or random_text(rng)

    try:
        size = rng.randint(28, 56) if augment else 40
        font = ImageFont.truetype(font_path, size, index=font_index)

        # 전경/배경색: 휘도 차이 보장 (자막은 항상 대비가 있음)
        while True:
            fg = tuple(rng.randint(0, 255) for _ in range(3))
            bg = tuple(rng.randint(0, 255) for _ in range(3))
            luma = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
            if abs(luma(fg) - luma(bg)) > 70:
                break
        if not augment:
            fg, bg = (0, 0, 0), (255, 255, 255)

        stroke_w = rng.choice([0, 0, 1, 2, 3, 4]) if augment else 0
        stroke_fill = tuple(255 - c for c in fg)

        bbox = font.getbbox(text, stroke_width=stroke_w)
        tw = max(bbox[2] - bbox[0], 8)
        th = max(bbox[3] - bbox[1], 8)
        pad = rng.randint(4, 14) if augment else 8

        img = Image.new("RGB", (tw + pad * 2, th + pad * 2), bg)
        draw = ImageDraw.Draw(img)
        draw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=fg,
                  stroke_width=stroke_w, stroke_fill=stroke_fill)

        if augment:
            # 회전 (자막은 거의 수평 → 소폭만)
            if rng.random() < 0.3:
                img = img.rotate(rng.uniform(-2.0, 2.0), expand=True, fillcolor=bg)
            # 저해상도 시뮬레이션 (작은 영상에서 추출된 자막)
            if rng.random() < 0.5:
                scale = rng.uniform(0.4, 0.8)
                w2, h2 = max(int(img.width * scale), 8), max(int(img.height * scale), 8)
                img = img.resize((w2, h2)).resize((img.width, img.height))
            # 블러
            if rng.random() < 0.4:
                img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 1.2)))
            # JPEG 아티팩트
            if rng.random() < 0.5:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=rng.randint(40, 90))
                buf.seek(0)
                img = Image.open(buf).convert("RGB")

        arr = np.asarray(img.convert("L"), dtype=np.float32) / 255.0

        # 글리프가 비어 있으면 (한글 미지원 폰트) 무효
        if float(arr.std()) < 0.02:
            return None

        return _normalize_crop(arr)
    except Exception as e:
        logger.debug("render_sample_failed", font=os.path.basename(font_path), error=str(e)[:60])
        return None


def _normalize_crop(gray01: np.ndarray) -> np.ndarray:
    """임의 크기 그레이스케일 [0,1] → (INPUT_H, INPUT_W) 패딩 리사이즈."""
    h, w = gray01.shape[:2]
    scale = INPUT_H / max(h, 1)
    new_w = max(min(int(w * scale), INPUT_W), 8)
    resized = cv2.resize(gray01, (new_w, INPUT_H), interpolation=cv2.INTER_AREA)
    out = np.full((INPUT_H, INPUT_W), float(np.median(resized[:, :2])), dtype=np.float32)
    out[:, :new_w] = resized[:, :INPUT_W] if new_w >= INPUT_W else resized
    return out


def preprocess_bgr_crop(crop_bgr: np.ndarray) -> Optional[np.ndarray]:
    """추론용: BGR 텍스트 라인 크롭 → 모델 입력 텐서용 배열."""
    if crop_bgr is None or crop_bgr.size == 0:
        return None
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if len(crop_bgr.shape) == 3 else crop_bgr
    if min(gray.shape[:2]) < 10:
        return None
    return _normalize_crop(gray.astype(np.float32) / 255.0)


# ─────────────────────────────────────────────
# 모델 정의
# ─────────────────────────────────────────────
def build_model(num_classes: int):
    """경량 CNN (~0.5M 파라미터, CPU 추론 수 ms)."""
    import torch.nn as nn

    return nn.Sequential(
        nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
        nn.MaxPool2d(2),                              # 24×128
        nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
        nn.MaxPool2d(2),                              # 12×64
        nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
        nn.MaxPool2d(2),                              # 6×32
        nn.Conv2d(128, 192, 3, padding=1), nn.BatchNorm2d(192), nn.ReLU(),
        nn.AdaptiveAvgPool2d((1, 1)),
        nn.Flatten(),
        nn.Dropout(0.3),
        nn.Linear(192, num_classes),
    )


# ─────────────────────────────────────────────
# 추론 엔진 (싱글톤)
# ─────────────────────────────────────────────
class FontClassifierEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._loaded = False
        self._model = None
        self._classes: List[Dict] = []

    def _load(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if not (os.path.exists(MODEL_PATH) and os.path.exists(META_PATH)):
                logger.info("font_classifier_not_trained",
                            hint="python tools/train_font_classifier.py 로 학습")
                return
            try:
                import torch
                with open(META_PATH, encoding="utf-8") as f:
                    meta = json.load(f)
                self._classes = meta["classes"]
                model = build_model(len(self._classes))
                model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
                model.eval()
                self._model = model
                logger.info("font_classifier_loaded",
                            classes=len(self._classes),
                            commercial=sum(1 for c in self._classes if c.get("commercial")))
            except Exception as e:
                logger.warning("font_classifier_load_failed", error=str(e)[:100])
                self._model = None

    @property
    def available(self) -> bool:
        self._load()
        return self._model is not None

    def classify_crops(self, crops_bgr: List[np.ndarray]) -> Optional[Dict]:
        """
        텍스트 라인 크롭들을 분류 → 신뢰도 가중 합산으로 최종 폰트 판정.

        Returns:
            None — 모델 없음/입력 무효
            {font_name, confidence, margin, is_commercial, foundry, risk}
        """
        self._load()
        if self._model is None or not crops_bgr:
            return None

        import torch

        batch = []
        for c in crops_bgr:
            arr = preprocess_bgr_crop(c)
            if arr is not None:
                batch.append(arr)
        if not batch:
            return None

        x = torch.from_numpy(np.stack(batch)[:, None, :, :])  # (N,1,H,W)
        x = (x - 0.5) / 0.5
        with torch.no_grad():
            probs = torch.softmax(self._model(x), dim=-1).mean(dim=0).numpy()

        order = np.argsort(probs)[::-1]
        top1, top2 = int(order[0]), int(order[1]) if len(order) > 1 else int(order[0])
        cls = self._classes[top1]
        return {
            "font_name":     cls["name"],
            "confidence":    round(float(probs[top1]), 4),
            "margin":        round(float(probs[top1] - probs[top2]), 4),
            "is_commercial": bool(cls.get("commercial", False)),
            "foundry":       cls.get("foundry", ""),
            "risk":          float(cls.get("risk", 0.5)),
            "n_crops":       len(batch),
        }


_engine = FontClassifierEngine()


def get_font_classifier() -> FontClassifierEngine:
    return _engine
