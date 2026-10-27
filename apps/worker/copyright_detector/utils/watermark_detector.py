"""
utils/watermark_detector.py - 향상된 워터마크 / 방송 오버레이 감지
비용: 무료 (OpenCV + NumPy만 사용)

제공 기능:
  1. detect_tiled_watermark_fft  - FFT 기반 반복 패턴 워터마크 (Getty 대각선 텍스트 등)
  2. enhance_frame_for_ocr       - CLAHE + 양방향 필터 + 감마 보정 (흐린 워터마크 강조)
  3. detect_letterbox_precise    - 적응형 임계값 레터박스/필러박스 감지
  4. score_broadcast_lower_third - 방송 하단 자막바(Lower Third) 정밀 스코어링
  5. detect_corner_logo_precise  - 코너 로고 엣지 + 색상 복합 감지
  6. detect_cinematic_aspect     - 시네마스코프/극장 화면비 감지 (영화 클립 식별)
  7. detect_scene_color_type     - 색상 분포 기반 장면 유형 힌트 (스포츠/콘서트/뉴스)
"""
import numpy as np
import cv2
from typing import Dict, Tuple


# ─────────────────────────────────────────────
# 1. FFT 기반 반복 패턴 워터마크 감지
# ─────────────────────────────────────────────
def detect_tiled_watermark_fft(frame: np.ndarray) -> Tuple[bool, float]:
    """
    FFT 기반 반복 패턴 워터마크 감지.
    Getty Images, Shutterstock 등의 반투명 대각선 반복 텍스트를
    주파수 영역에서 감지한다 (일반 OCR로는 잡기 어려운 패턴).

    Returns:
        (detected: bool, confidence: float 0.0~1.0)

    알려진 한계: 상위 0.5% 피크는 512×512 에서 늘 ~1300개이고 실수 이미지의 스펙트럼은 원래
    중심 대칭이라, 워터마크가 없는 일반 프레임도 거의 항상 (True, 1.0)이 나온다.
    강화 전처리 여부 정도로만 쓰고, 비싼 작업(OCR 예산 우회 등)의 근거로 쓰지 말 것.
    """
    if frame is None or frame.size == 0:
        return False, 0.0

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    small = cv2.resize(gray, (512, 512)).astype(np.float32)

    # FFT → 주파수 영역 변환
    fshift = np.fft.fftshift(np.fft.fft2(small))
    magnitude = np.log(np.abs(fshift) + 1.0)

    # DC 컴포넌트 제거 (중심 20×20)
    h, w = magnitude.shape
    magnitude[h // 2 - 10:h // 2 + 10, w // 2 - 10:w // 2 + 10] = 0.0

    # 99.5 퍼센타일 이상의 강한 피크 감지
    threshold = np.percentile(magnitude, 99.5)
    binary_peaks = (magnitude > threshold).astype(np.uint8)
    peaks = int(np.sum(binary_peaks))

    if peaks == 0:
        return False, 0.0

    # 피크의 대칭성 체크 (워터마크 = 중심 대칭 패턴)
    h_sym = int(np.sum(
        binary_peaks[:h // 2, :].astype(bool) &
        np.flipud(binary_peaks[h // 2:, :]).astype(bool)
    ))
    v_sym = int(np.sum(
        binary_peaks[:, :w // 2].astype(bool) &
        np.fliplr(binary_peaks[:, w // 2:]).astype(bool)
    ))
    symmetry_ratio = (h_sym + v_sym) / max(peaks * 2, 1)

    confidence = min(float(peaks) / 30.0 * symmetry_ratio * 2, 1.0)
    return confidence > 0.25, round(confidence, 3)


# ─────────────────────────────────────────────
# 2. 향상된 OCR 전처리 (흐린 워터마크용)
# ─────────────────────────────────────────────
def enhance_frame_for_ocr(frame: np.ndarray) -> np.ndarray:
    """
    반투명/흐린 워터마크 텍스트를 강조하는 전처리 파이프라인.
    EasyOCR에 넘기기 전 적용하면 감지율이 크게 높아진다.

    Pipeline:
        CLAHE → Bilateral filter → 감마 보정(1.5) → Unsharp masking

    Returns:
        전처리된 BGR 이미지 (EasyOCR 3채널 입력 호환)
    """
    if frame is None or frame.size == 0:
        return frame

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame.copy()

    # 1. CLAHE: 적응형 대비 향상
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # 2. Bilateral filter: 노이즈 제거 + 텍스트 엣지 보존
    denoised = cv2.bilateralFilter(enhanced, 9, 75, 75)

    # 3. 감마 보정 γ=1.5 → 전체적으로 밝게 (어두운 텍스트 강조)
    gamma_table = np.array(
        [((i / 255.0) ** (1.0 / 1.5)) * 255 for i in range(256)],
        dtype=np.uint8,
    )
    brightened = cv2.LUT(denoised, gamma_table)

    # 4. Unsharp masking: 텍스트 선명도 강화
    blur = cv2.GaussianBlur(brightened, (0, 0), 3)
    sharpened = cv2.addWeighted(brightened, 1.5, blur, -0.5, 0)

    return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)


# ─────────────────────────────────────────────
# 3. 정밀 레터박스 / 필러박스 감지
# ─────────────────────────────────────────────
def detect_letterbox_precise(frame: np.ndarray) -> Dict:
    """
    적응형 임계값을 사용한 정밀 레터박스/필러박스 감지.
    기존 고정 임계값(18) 대비 더 넓은 소스 영상을 잡아낸다.

    Returns:
        {
          has_letterbox: bool,   # 상하 검은 띠
          has_pillarbox: bool,   # 좌우 검은 띠
          bar_ratio: float,      # 띠 비율 (0~1)
          confidence: float      # 감지 신뢰도 (0~1)
        }
    """
    result = {"has_letterbox": False, "has_pillarbox": False, "bar_ratio": 0.0, "confidence": 0.0}
    if frame is None or frame.size == 0:
        return result

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    h, w = gray.shape
    if h < 10 or w < 10:
        return result

    frame_mean = float(np.mean(gray))
    # 적응형 임계값: 전체 평균의 12% 또는 최대 22 (밝은 영상/어두운 영상 모두 대응)
    dark_thresh = min(max(frame_mean * 0.12, 8.0), 22.0)

    row_means = np.mean(gray, axis=1)   # (h,)
    col_means = np.mean(gray, axis=0)   # (w,)

    # 레터박스: 상단 1/3 + 하단 1/3 에서 어두운 행 수
    top_dark  = int(np.sum(row_means[:h // 3] < dark_thresh))
    bot_dark  = int(np.sum(row_means[2 * h // 3:] < dark_thresh))
    left_dark = int(np.sum(col_means[:w // 3] < dark_thresh))
    right_dark = int(np.sum(col_means[2 * w // 3:] < dark_thresh))

    if top_dark > h * 0.04 and bot_dark > h * 0.04:
        bar_ratio = (top_dark + bot_dark) / h
        result["has_letterbox"] = True
        result["bar_ratio"] = round(bar_ratio, 3)
        result["confidence"] = round(min(bar_ratio * 3.5, 0.97), 3)

    if left_dark > w * 0.03 and right_dark > w * 0.03:
        bar_ratio = (left_dark + right_dark) / w
        result["has_pillarbox"] = True
        result["bar_ratio"] = round(max(result["bar_ratio"], bar_ratio), 3)
        result["confidence"] = round(max(result["confidence"], min(bar_ratio * 3.0, 0.93)), 3)

    return result


# ─────────────────────────────────────────────
# 4. 방송 하단 자막바 (Lower Third) 정밀 스코어링
# ─────────────────────────────────────────────
def score_broadcast_lower_third(frame: np.ndarray) -> Dict:
    """
    방송 하단 자막바 정밀 감지.
    기존 단순 임계값 방식보다 높은 정확도:
    - 채도 (뉴스 채널 자막바는 대체로 선명한 단색 배경)
    - 엣지 밀도 (텍스트 글자 윤곽)
    - 색상 균일성 (단색 배경이면 std 낮음)
    - 복합 가중 점수

    Returns:
        {detected: bool, score: float, region_type: str|None}
    """
    result = {"detected": False, "score": 0.0, "region_type": None}
    if frame is None or frame.size == 0:
        return result

    h, w = frame.shape[:2]
    # 하단 25~88% 구간 (Lower Third 위치)
    y1, y2 = int(h * 0.63), int(h * 0.88)
    x1, x2 = int(w * 0.03), int(w * 0.97)
    lower = frame[y1:y2, x1:x2]

    if lower.size == 0:
        return result

    # 채도 분석 (HSV)
    hsv = cv2.cvtColor(lower, cv2.COLOR_BGR2HSV) if len(lower.shape) == 3 else None
    high_sat_ratio = 0.0
    if hsv is not None:
        high_sat_ratio = float(np.sum(hsv[:, :, 1] > 60)) / hsv[:, :, 1].size

    # 엣지 밀도
    gray = cv2.cvtColor(lower, cv2.COLOR_BGR2GRAY) if len(lower.shape) == 3 else lower
    edges = cv2.Canny(gray, 50, 150)
    edge_density = float(np.sum(edges > 0)) / max(edges.size, 1)

    # 색상 균일성 (낮을수록 단색 배경 가능성 높음)
    if len(lower.shape) == 3:
        std_val = float(np.std(lower.reshape(-1, 3), axis=0).mean())
    else:
        std_val = float(np.std(lower))
    uniformity_score = max(0.0, 1.0 - std_val / 80.0)

    # 복합 점수
    score = (high_sat_ratio * 0.35
             + min(edge_density * 8, 1.0) * 0.35
             + uniformity_score * 0.30)
    score = round(min(score, 1.0), 3)

    if score > 0.35:
        result["detected"] = True
        result["score"] = score
        result["region_type"] = "news_lower_third" if high_sat_ratio > 0.20 else "text_overlay"

    return result


# ─────────────────────────────────────────────
# 5. 코너 로고 정밀 감지
# ─────────────────────────────────────────────
def detect_corner_logo_precise(frame: np.ndarray) -> Dict:
    """
    코너 로고 정밀 감지 (엣지 밀도 + 채도 복합).
    단순 엣지 임계값보다 False Positive 감소.

    Returns:
        {detected: bool, position: str|None, confidence: float}
    """
    result = {"detected": False, "position": None, "confidence": 0.0}
    if frame is None or frame.size == 0:
        return result

    h, w = frame.shape[:2]
    ch, cw = max(int(h * 0.14), 20), max(int(w * 0.18), 20)

    corners = {
        "top_left":     frame[:ch, :cw],
        "top_right":    frame[:ch, w - cw:],
        "bottom_left":  frame[h - ch:, :cw],
        "bottom_right": frame[h - ch:, w - cw:],
    }

    best_pos, best_conf = None, 0.0
    for pos, corner in corners.items():
        if corner.size == 0:
            continue

        gray = cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY) if len(corner.shape) == 3 else corner
        edges = cv2.Canny(gray, 40, 120)
        edge_density = float(np.sum(edges > 0)) / max(edges.size, 1)

        # 채도 (로고는 보통 색상이 있음)
        if len(corner.shape) == 3:
            hsv = cv2.cvtColor(corner, cv2.COLOR_BGR2HSV)
            sat_ratio = float(np.sum(hsv[:, :, 1] > 50)) / max(hsv[:, :, 1].size, 1)
        else:
            sat_ratio = 0.0

        # 복합 점수: 엣지 밀도 ≥ 0.07 AND 채도 ≥ 0.15 를 함께 요구
        conf = 0.0
        if edge_density > 0.06 and sat_ratio > 0.10:
            conf = min((edge_density * 5 + sat_ratio * 2) / 3, 1.0)
        elif edge_density > 0.10:  # 흑백 로고
            conf = min(edge_density * 4, 0.75)

        if conf > best_conf:
            best_conf = conf
            best_pos = pos

    if best_conf > 0.35:
        result["detected"] = True
        result["position"] = best_pos
        result["confidence"] = round(best_conf, 3)

    return result


# ─────────────────────────────────────────────
# 6. 시네마스코프 / 극장 화면비 감지
# ─────────────────────────────────────────────
def detect_cinematic_aspect(frame: np.ndarray) -> Dict:
    """
    시네마스코프(2.39:1) / 극장 화면비 감지 → 영화 클립 고신뢰 식별.

    영화관 스크린 포맷:
    - 2.39:1 (Anamorphic/CinemaScope) : 가장 보편적인 극장 영화 포맷
    - 2.35:1, 2.40:1               : Anamorphic 변형
    - 1.85:1 (Flat Widescreen)     : 일반 극장 영화 (American widescreen)
    - 2.76:1 (Ultra Panavision)    : 특수 촬영 (Hateful Eight 등)

    동작 원리:
    프레임 상하에 검은 레터박스 띠가 있으면 그 안의 콘텐츠 화면비를 계산.
    16:9 (1.78:1) TV/OTT는 해당 없음 → 확실히 영화 포맷인 경우만 반환.

    Returns:
        {
          is_cinematic: bool,
          ratio        : float,    # 실제 콘텐츠 화면비
          format       : str|None, # "anamorphic"|"flat_wide"|"ultra_panavision"
          confidence   : float,    # 감지 신뢰도 0~1
          bar_ratio    : float,    # 레터박스 띠 비율 (0~1)
        }
    """
    result = {
        "is_cinematic": False, "ratio": 0.0,
        "format": None, "confidence": 0.0, "bar_ratio": 0.0,
    }
    if frame is None or frame.size == 0:
        return result

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    h, w = gray.shape

    # 전체 프레임 밝기에 적응하는 어두운 띠 임계값
    frame_mean = float(np.mean(gray))
    dark_thresh = min(max(frame_mean * 0.12, 6.0), 25.0)

    row_means = np.mean(gray, axis=1)   # (h,) 행별 평균

    # 상단에서 어두운 행 수 측정
    top_dark = 0
    for i in range(h):
        if row_means[i] < dark_thresh:
            top_dark += 1
        else:
            break

    # 하단에서 어두운 행 수 측정
    bot_dark = 0
    for i in range(h - 1, -1, -1):
        if row_means[i] < dark_thresh:
            bot_dark += 1
        else:
            break

    content_height = h - top_dark - bot_dark
    if content_height <= 0 or w <= 0:
        return result

    ratio = round(w / content_height, 3)
    bar_ratio = round((top_dark + bot_dark) / h, 3)
    result["ratio"] = ratio
    result["bar_ratio"] = bar_ratio

    # 화면비별 포맷 판별 + 신뢰도
    # 상하 레터박스 띠가 충분해야 의미 있음 (최소 3% 이상)
    if bar_ratio < 0.03:
        return result   # 거의 전체 화면 → 영화 포맷 아님

    if 2.25 <= ratio <= 2.60:            # 2.39:1 ±0.15
        fmt = "anamorphic"
        conf = min(bar_ratio * 4.5, 0.95)
    elif 1.75 <= ratio <= 2.00:          # 1.85:1 ±0.10
        fmt = "flat_wide"
        conf = min(bar_ratio * 3.5, 0.82)
    elif 2.60 <= ratio <= 3.00:          # 2.76:1 Ultra Panavision
        fmt = "ultra_panavision"
        conf = min(bar_ratio * 5.0, 0.97)
    else:
        return result   # 일반 TV/OTT 화면비

    result["is_cinematic"] = True
    result["format"] = fmt
    result["confidence"] = round(conf, 3)
    return result


# ─────────────────────────────────────────────
# 7. 색상 분포 기반 장면 유형 힌트
# ─────────────────────────────────────────────
def detect_scene_color_type(frame: np.ndarray) -> Dict:
    """
    HSV 색상 분포 분석으로 장면 유형 힌트 제공.
    CLIP의 텍스트 분류와 직교(orthogonal)하므로 보완 신호로 활용.

    감지 유형:
    - "sports_field" : 녹색 필드 지배 → 야구/축구/스포츠 중계 가능성
    - "concert"      : 고채도 + 다양한 색 → 콘서트/뮤직비디오 가능성
    - "news_studio"  : 청색 계열 + 균일 조명 → 뉴스 스튜디오 가능성
    - None           : 해당 없음

    Returns:
        {"type": str|None, "confidence": float, "detail": dict}
    """
    result = {"type": None, "confidence": 0.0, "detail": {}}
    if frame is None or frame.size == 0:
        return result

    # 작은 크기로 리사이즈 (속도 최적화)
    small = cv2.resize(frame, (128, 72)) if len(frame.shape) == 3 else frame
    if len(small.shape) == 2:
        # 그레이스케일은 색상 분석 불가
        return result

    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h_ch = hsv[:, :, 0].astype(float)   # Hue   0-179
    s_ch = hsv[:, :, 1].astype(float)   # Sat   0-255
    v_ch = hsv[:, :, 2].astype(float)   # Value 0-255
    total_px = h_ch.size

    # ── 스포츠 필드: 녹색 지배 (Hue 35~90, 채도 > 50) ──
    green_mask = ((h_ch >= 35) & (h_ch <= 90) & (s_ch > 50) & (v_ch > 40))
    green_ratio = float(np.sum(green_mask)) / total_px

    if green_ratio > 0.30:
        conf = min(green_ratio * 2.2, 0.90)
        result["type"] = "sports_field"
        result["confidence"] = round(conf, 3)
        result["detail"] = {"green_ratio": round(green_ratio, 3)}
        return result

    # ── 콘서트/음악: 고채도(>140) + 채도 분산 높음 ──
    high_sat = float(np.sum(s_ch > 140)) / total_px
    sat_std  = float(np.std(s_ch))

    if high_sat > 0.45 and sat_std > 55:
        conf = min(high_sat * 1.3 + sat_std / 300, 0.82)
        result["type"] = "concert"
        result["confidence"] = round(conf, 3)
        result["detail"] = {"high_sat": round(high_sat, 3), "sat_std": round(sat_std, 1)}
        return result

    # ── 뉴스 스튜디오: 청색 계열(Hue 100~135) + 낮은 밝기 분산 ──
    blue_mask = ((h_ch >= 100) & (h_ch <= 135) & (s_ch > 35))
    blue_ratio = float(np.sum(blue_mask)) / total_px
    v_std      = float(np.std(v_ch))

    if blue_ratio > 0.22 and v_std < 55:
        conf = min(blue_ratio * 2.5, 0.72)
        result["type"] = "news_studio"
        result["confidence"] = round(conf, 3)
        result["detail"] = {"blue_ratio": round(blue_ratio, 3), "v_std": round(v_std, 1)}
        return result

    return result
