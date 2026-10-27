"""
utils/video_utils.py - 영상에서 오디오/프레임 추출 유틸리티
"""
import os
import subprocess
import hashlib
import asyncio
from pathlib import Path
from typing import List, Tuple, Optional, Generator
import structlog

import cv2
import numpy as np

from config import config

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# 영상 해시 (중복 감지)
# ─────────────────────────────────────────────
def compute_video_hash(video_path: str, chunk_size: int = 65536) -> str:
    """SHA256 해시 - 동일 영상 재분석 방지"""
    sha256 = hashlib.sha256()
    with open(video_path, "rb") as f:
        while chunk := f.read(chunk_size):
            sha256.update(chunk)
    return sha256.hexdigest()


def get_video_info(video_path: str) -> dict:
    """ffprobe로 영상 메타데이터 추출"""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", video_path
    ]
    try:
        import json
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        data = json.loads(result.stdout)

        duration = float(data.get("format", {}).get("duration", 0))
        width, height = 0, 0
        fps = 0.0

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = stream.get("width", 0)
                height = stream.get("height", 0)
                r_frame_rate = stream.get("r_frame_rate", "0/1")
                num, den = r_frame_rate.split("/")
                fps = float(num) / float(den) if float(den) > 0 else 0

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "fps": fps,
            "format": data.get("format", {}).get("format_name", ""),
            "size_bytes": int(data.get("format", {}).get("size", 0)),
        }
    except Exception as e:
        logger.error("video_info_failed", error=str(e))
        return {"duration": 0, "width": 0, "height": 0, "fps": 0, "format": "", "size_bytes": 0}


# ─────────────────────────────────────────────
# 오디오 추출
# ─────────────────────────────────────────────
def extract_audio(video_path: str, output_path: str = None,
                  sample_rate: int = 16000) -> str:
    """
    ffmpeg으로 오디오 추출 (WAV, mono, 16kHz)
    ACRCloud/AudD가 원하는 포맷
    """
    if not output_path:
        output_path = str(config.TEMP_DIR / f"{Path(video_path).stem}_audio.wav")

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn",                    # 비디오 제거
        "-acodec", "pcm_s16le",  # WAV PCM 16bit
        "-ac", "1",              # Mono
        "-ar", str(sample_rate), # Sample rate
        "-y",                    # Overwrite
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {result.stderr.decode()}")

    logger.info("audio_extracted", output=output_path)
    return output_path


def load_wav_mono(path: str) -> Tuple[np.ndarray, int]:
    """
    16bit PCM WAV → (int16 ndarray, sample_rate).
    extract_audio()가 만든 mono 16kHz wav를 메모리에 한 번만 로드해
    청크 추출 시 ffmpeg 프로세스 기동(청크당 1회)을 전부 제거한다.
    """
    import wave
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n_ch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype=np.int16)
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1).astype(np.int16)
    return data, sr


def write_wav_mono(path: str, data: np.ndarray, sample_rate: int) -> str:
    """int16 ndarray → 16bit PCM mono WAV 파일 (ACRCloud/AudD 업로드용)"""
    import wave
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(np.ascontiguousarray(data, dtype=np.int16).tobytes())
    return path


def extract_audio_chunk(video_path: str, start_sec: float, duration_sec: float,
                        output_path: str) -> str:
    """특정 구간 오디오 추출 (ACRCloud API 호출용)"""
    cmd = [
        "ffmpeg", "-ss", str(start_sec), "-i", video_path,
        "-t", str(duration_sec),
        "-vn", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
        "-y", output_path
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Chunk extraction failed")
    return output_path


def get_audio_chunks(video_path: str, chunk_duration: int = 12,
                     overlap: int = 2) -> Generator[Tuple[float, str], None, None]:
    """
    영상을 chunk_duration초 단위로 분할 (overlap 포함)
    Yields: (start_time_sec, chunk_wav_path)
    """
    info = get_video_info(video_path)
    total_duration = info["duration"]

    start = 0.0
    chunk_idx = 0

    while start < total_duration:
        end = min(start + chunk_duration, total_duration)
        if end - start < 3:  # 3초 미만은 건너뜀
            break

        chunk_path = str(config.TEMP_DIR / f"chunk_{chunk_idx:04d}.wav")
        try:
            extract_audio_chunk(video_path, start, end - start, chunk_path)
            yield start, chunk_path
        except Exception as e:
            logger.warning("chunk_extraction_failed", start=start, error=str(e))

        start += chunk_duration - overlap
        chunk_idx += 1


# ─────────────────────────────────────────────
# 프레임 추출
# ─────────────────────────────────────────────
def compute_dynamic_fps(duration: float) -> float:
    """
    영상 길이에 따른 동적 target FPS
    짧은 영상 → 촘촘히, 긴 영상 → 듬성듬성

    [참고] extract_frames_smart가 max_frames 안에 전체 길이가 들어가도록
    step을 자동 확대하므로, 여기서는 상한 걱정 없이 촘촘하게 잡아도 된다.
    """
    if duration <= 600:    # 10분 이하
        return 1.0
    elif duration <= 1800: # 30분 이하
        return 0.5
    else:                  # 30분 초과
        return 0.3


def extract_frames_smart(video_path: str,
                         target_fps: float = 1.0,
                         phash_threshold: int = 12,
                         max_frames: int = 100) -> List[Tuple[float, np.ndarray]]:
    """
    스마트 프레임 추출 (프레임 스킵 + 인라인 pHash 중복 제거)

    전략:
    - 소스 FPS 기반 정수 스킵: 60fps 영상, target=1fps → 60프레임마다 1개 읽음
    - 연속 유사 프레임 인라인 제거 (pHash threshold=12 / 64비트 기준)
      → 정지 화면·자막만 변하는 구간 효율적 스킵
    - 영상 길이별 target FPS 자동 조정

    Returns: [(timestamp_sec, frame_ndarray), ...]
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    source_fps    = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration      = total_frames / source_fps if source_fps > 0 else 0

    if duration <= 0:
        cap.release()
        return []

    # 영상 길이에 따른 동적 target FPS
    effective_target = min(target_fps, compute_dynamic_fps(duration))

    # 소스 FPS 기반 정수 스킵 간격
    # 예: 60fps, target=1fps → step=60 (60프레임마다 1개)
    # 예: 30fps, target=0.5fps → step=60
    frame_step = max(1, int(round(source_fps / effective_target)))

    # 시작/끝 0.5초 여유 (인트로/아웃트로 제목 로고 등)
    start_frame = max(0, min(int(source_fps * 0.5), int(total_frames * 0.01)))
    end_frame   = max(start_frame + 1,
                      total_frames - max(0, min(int(source_fps * 0.5),
                                                int(total_frames * 0.01))))

    # ── 전체 길이 커버 보장 ──
    # 기존: 앞에서부터 max_frames개를 채우면 중단 → 긴 영상은 앞부분만 분석되고
    #       후반부는 어떤 분석기도 보지 못함 (미탐의 최대 원인)
    # 수정: max_frames 안에 영상 전체가 들어가도록 step을 늘려 균등 샘플링
    span = end_frame - start_frame
    if span > frame_step * max_frames:
        frame_step = int(np.ceil(span / max_frames))
        logger.info("frame_step_stretched",
                    new_step_sec=f"{frame_step / source_fps:.1f}",
                    reason="full_duration_coverage")

    logger.info("smart_extraction_start",
                duration=f"{duration:.0f}s",
                source_fps=f"{source_fps:.1f}",
                effective_target_fps=f"{effective_target:.2f}",
                frame_step=frame_step,
                estimated=min((end_frame - start_frame) // frame_step, max_frames))

    frames         = []
    prev_hash      = None
    prev_idx       = None
    skipped_dup    = 0
    frame_idx      = start_frame
    cut_candidates = []   # (이전 샘플 idx, 현재 샘플 idx, 이전 pHash int) — 장면 전환 의심 구간
    _CUT_THRESHOLD = 48   # 256비트 pHash 기준: 같은 장면 <30, 컷 전환 60+

    while frame_idx < end_frame and len(frames) < max_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, float(frame_idx))
        ret, frame = cap.read()
        if not ret:
            frame_idx += frame_step
            continue

        timestamp = frame_idx / source_fps

        # 크기 표준화 (640×360 이하, 이미 작으면 그대로)
        if frame.shape[1] > 640:
            frame = cv2.resize(frame, (640, 360))

        # ── 인라인 pHash 중복 검사 ──
        try:
            curr_hash = compute_phash(frame)
            if prev_hash is not None:
                distance = bin(int(curr_hash, 16) ^ int(prev_hash, 16)).count("1")
                if distance <= phash_threshold:
                    skipped_dup += 1
                    frame_idx += frame_step
                    continue          # 유사(정지) 프레임 스킵
                # 샘플 간 변화가 매우 큼 + 간격 2초 초과 → 사이에 장면 전환이 숨어 있음
                # 삽입된 클립의 실제 시작 프레임을 놓치지 않도록 경계 정밀화 후보 등록
                if distance >= _CUT_THRESHOLD and prev_idx is not None \
                        and frame_idx - prev_idx > source_fps * 2:
                    cut_candidates.append((prev_idx, frame_idx, int(prev_hash, 16)))
            frames.append((timestamp, frame))
            prev_hash = curr_hash
            prev_idx = frame_idx
        except Exception:
            frames.append((timestamp, frame))  # 해시 실패 시 그냥 추가

        frame_idx += frame_step

    # ── 컷 경계 정밀화: 전환 직후 첫 프레임 추가 (장면 기반 샘플링 보강) ──
    boundary_frames = _refine_cut_boundaries(
        cap, cut_candidates, source_fps,
        cut_threshold=_CUT_THRESHOLD, max_extra=30,
    )
    if boundary_frames:
        frames.extend(boundary_frames)
        frames.sort(key=lambda x: x[0])
        logger.info("cut_boundaries_added", count=len(boundary_frames))

    cap.release()
    actual_fps = len(frames) / duration if duration > 0 else 0
    logger.info("smart_extraction_done",
                extracted=len(frames),
                skipped_dup=skipped_dup,
                actual_fps=f"{actual_fps:.3f}",
                duration=f"{duration:.0f}s")
    return frames


def _refine_cut_boundaries(cap, candidates, source_fps: float,
                            cut_threshold: int = 48,
                            max_extra: int = 30):
    """
    샘플 간격 사이에 숨은 장면 전환 지점을 이진 탐색으로 찾아
    전환 직후 첫 프레임을 반환 (PySceneDetect 없이 컷 경계 포착).

    격자 샘플링은 컷 중간 프레임을 뽑지만, 역검색(Vision)에는
    전환 직후의 깨끗한 첫 프레임이 훨씬 유리하다.
    후보당 최대 5회 시킹 (~0.2초 해상도), 전체 max_extra개 상한.
    """
    out = []
    if not candidates:
        return out

    # 후보 과다 시 시간축 등간격 샘플링
    if len(candidates) > max_extra:
        idxs = np.linspace(0, len(candidates) - 1, max_extra).astype(int)
        candidates = [candidates[i] for i in sorted(set(idxs.tolist()))]

    for lo, orig_hi, prev_hash_int in candidates:
        try:
            hi = orig_hi
            for _ in range(5):
                if hi - lo <= max(1, int(source_fps * 0.2)):
                    break
                mid = (lo + hi) // 2
                cap.set(cv2.CAP_PROP_POS_FRAMES, float(mid))
                ret, f = cap.read()
                if not ret:
                    break
                if f.shape[1] > 640:
                    f = cv2.resize(f, (640, 360))
                d = bin(int(compute_phash(f), 16) ^ prev_hash_int).count("1")
                if d >= cut_threshold:
                    hi = mid    # 전환은 mid 이전 → 좌측 탐색
                else:
                    lo = mid    # 아직 이전 장면 → 우측 탐색

            # 탐색이 전혀 좁혀지지 않음 = 전환이 기존 샘플 직전 → 중복이므로 스킵
            if hi >= orig_hi:
                continue

            cap.set(cv2.CAP_PROP_POS_FRAMES, float(hi))
            ret, f = cap.read()
            if not ret:
                continue
            if f.shape[1] > 640:
                f = cv2.resize(f, (640, 360))
            out.append((hi / source_fps, f))
        except Exception:
            continue

    return out


# 하위 호환성 별칭
def extract_frames_for_scene(video_path: str, fps: float = 1.0,
                              scene_threshold: float = 30.0,
                              max_frames: int = 100) -> List[Tuple[float, np.ndarray]]:
    """extract_frames_smart 의 호환 래퍼"""
    return extract_frames_smart(video_path,
                                target_fps=fps,
                                phash_threshold=12,
                                max_frames=max_frames)


def deduplicate_frames(frames: List[Tuple[float, np.ndarray]],
                       phash_threshold: int = 12) -> List[Tuple[float, np.ndarray]]:
    """
    pHash 기반 중복 프레임 제거 (후처리용 호환 함수)
    extract_frames_smart 사용 시 이미 인라인 제거되므로 보통 불필요.
    """
    if not frames:
        return frames

    deduped   = [frames[0]]
    prev_hash = compute_phash(frames[0][1])
    removed   = 0

    for timestamp, frame in frames[1:]:
        try:
            curr_hash = compute_phash(frame)
            distance  = bin(int(curr_hash, 16) ^ int(prev_hash, 16)).count("1")
            if distance > phash_threshold:
                deduped.append((timestamp, frame))
                prev_hash = curr_hash
            else:
                removed += 1
        except Exception:
            deduped.append((timestamp, frame))

    logger.info("frame_dedup", original=len(frames), after=len(deduped), removed=removed)
    return deduped


def frame_to_bytes(frame: np.ndarray, quality: int = 85) -> bytes:
    """프레임 → JPEG bytes"""
    _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buffer.tobytes()


def compute_phash(frame: np.ndarray, hash_size: int = 16) -> str:
    """Perceptual Hash 계산 (영상 중복 감지용)"""
    try:
        from PIL import Image
        import imagehash
        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        return str(imagehash.phash(pil_img, hash_size=hash_size))
    except ImportError:
        # Fallback: 단순 평균 해시
        small = cv2.resize(frame, (hash_size, hash_size))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        mean = np.mean(gray)
        bits = (gray > mean).flatten()
        return format(int("".join("1" if b else "0" for b in bits), 2), "x")


def cleanup_temp_files(prefix: str = "chunk_"):
    """임시 파일 정리"""
    for f in config.TEMP_DIR.glob(f"{prefix}*"):
        try:
            f.unlink()
        except Exception:
            pass
