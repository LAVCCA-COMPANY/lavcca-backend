"""
database/models.py - DB 모델 정의 (SQLAlchemy ORM)
학습 데이터 누적 + 분석 결과 저장
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    Text, ForeignKey, JSON, Enum, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────
class CopyrightType(str, enum.Enum):
    MUSIC = "music"
    VIDEO_CLIP = "video_clip"
    IMAGE = "image"
    LOGO = "logo"
    FONT = "font"
    MEME = "meme"

class RiskLevel(str, enum.Enum):
    HIGH = "HIGH"       # 🔴 75%+
    MEDIUM = "MEDIUM"   # 🟡 45-74%
    LOW = "LOW"         # 🟢 20-44%
    SAFE = "SAFE"       # ✅ 20% 미만

class AnalysisStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ─────────────────────────────────────────────
# 분석 작업
# ─────────────────────────────────────────────
class AnalysisJob(Base):
    """영상 분석 작업"""
    __tablename__ = "analysis_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), unique=True, nullable=False, index=True)
    video_path = Column(Text, nullable=False)
    video_filename = Column(String(255))
    video_duration = Column(Float)          # seconds
    video_hash = Column(String(64), index=True)  # SHA256 for dedup
    s3_key = Column(String(512))            # AWS S3 key

    status = Column(Enum(AnalysisStatus), default=AnalysisStatus.PENDING)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    processing_time_sec = Column(Float)

    overall_risk_score = Column(Float)      # 0.0 ~ 1.0
    overall_risk_level = Column(Enum(RiskLevel))
    total_issues_found = Column(Integer, default=0)

    error_message = Column(Text)
    extra_metadata = Column(JSON)                 # 추가 메타데이터

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations
    findings = relationship("CopyrightFinding", back_populates="job", cascade="all, delete-orphan")
    report = relationship("AnalysisReport", back_populates="job", uselist=False)


# ─────────────────────────────────────────────
# 저작권 발견 사항
# ─────────────────────────────────────────────
class CopyrightFinding(Base):
    """개별 저작권 발견 사항"""
    __tablename__ = "copyright_findings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), ForeignKey("analysis_jobs.job_id"), nullable=False)
    finding_type = Column(Enum(CopyrightType), nullable=False)

    # 시간 정보
    timestamp_start = Column(Float, nullable=False)   # seconds
    timestamp_end = Column(Float)
    timestamp_display = Column(String(20))            # "00:01:23"

    # 저작권 정보
    title = Column(String(512))
    author = Column(String(256))
    rights_holder = Column(String(256))
    source = Column(String(256))          # "ACRCloud", "YouTube", "Google Vision" 등
    external_id = Column(String(256))     # 외부 ID (ISRC, YouTube video ID 등)
    reference_url = Column(Text)

    # 위험도
    confidence_score = Column(Float)      # 0.0 ~ 1.0
    risk_score = Column(Float)            # 최종 위험도 (가중치 적용)
    risk_level = Column(Enum(RiskLevel))

    # 상세 정보
    description = Column(Text)
    raw_response = Column(JSON)           # API 원본 응답 저장 (학습용)

    # 플래그
    is_confirmed = Column(Boolean, default=False)   # 사람이 확인함
    is_false_positive = Column(Boolean, default=False)  # 오탐으로 표시됨
    human_verified_by = Column(String(128))
    human_verified_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relations
    job = relationship("AnalysisJob", back_populates="findings")

    __table_args__ = (
        Index("idx_findings_job_type", "job_id", "finding_type"),
        Index("idx_findings_risk", "risk_level"),
        Index("idx_findings_timestamp", "job_id", "timestamp_start"),
    )


# ─────────────────────────────────────────────
# 누적 학습 데이터베이스
# ─────────────────────────────────────────────
class KnownCopyrightedMusic(Base):
    """알려진 저작권 음악 DB (누적 학습)"""
    __tablename__ = "known_music"

    id = Column(Integer, primary_key=True, autoincrement=True)
    isrc = Column(String(20), index=True)           # 국제 표준 음원 코드
    title = Column(String(512), nullable=False)
    artist = Column(String(256))
    album = Column(String(256))
    rights_holder = Column(String(256))
    release_year = Column(Integer)
    duration_sec = Column(Float)

    # 음악 핑거프린트 (자체 학습)
    audio_fingerprint = Column(Text)        # base64 encoded fingerprint
    fingerprint_hash = Column(String(64), index=True)

    # 소스
    source = Column(String(64))             # "acrcloud", "audd", "manual"
    acrcloud_id = Column(String(128))
    youtube_ids = Column(JSON)              # 관련 YouTube 영상들

    # 통계
    detection_count = Column(Integer, default=1)    # 발견된 횟수
    last_detected_at = Column(DateTime)
    confirmed_copyright = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("isrc", name="uq_music_isrc"),
        Index("idx_music_fingerprint", "fingerprint_hash"),
    )


class KnownLogo(Base):
    """알려진 로고/상표 DB (누적 학습)"""
    __tablename__ = "known_logos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand_name = Column(String(256), nullable=False)
    trademark_owner = Column(String(256))
    category = Column(String(128))          # "tech", "food", "sports" 등

    # 이미지 해시 (자체 학습)
    phash = Column(String(64), index=True)  # perceptual hash
    dhash = Column(String(64), index=True)
    image_embedding = Column(Text)          # 벡터 임베딩 (base64)

    # 소스
    source = Column(String(64))
    google_vision_label = Column(String(256))
    rekognition_label = Column(String(256))

    detection_count = Column(Integer, default=1)
    last_detected_at = Column(DateTime)
    risk_level_default = Column(Enum(RiskLevel), default=RiskLevel.HIGH)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class KnownFont(Base):
    """알려진 상업용 폰트 DB (누적 학습)"""
    __tablename__ = "known_fonts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    font_name = Column(String(256), nullable=False, unique=True)
    foundry = Column(String(256))
    license_type = Column(String(128))      # "commercial", "free", "open_source"
    requires_license = Column(Boolean, default=True)

    detection_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)


class KnownVideoClip(Base):
    """알려진 저작권 영상 클립 DB"""
    __tablename__ = "known_video_clips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512))
    rights_holder = Column(String(256))
    youtube_id = Column(String(32), index=True)
    source_platform = Column(String(64))    # "youtube", "vimeo" 등

    # 비디오 핑거프린트
    frame_hashes = Column(JSON)             # 대표 프레임 해시들
    phash_sequence = Column(Text)           # 순차 phash

    detection_count = Column(Integer, default=1)
    last_detected_at = Column(DateTime)
    confirmed_copyright = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class KnownMeme(Base):
    """알려진 밈 템플릿 DB (누적 학습)"""
    __tablename__ = "known_memes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512), nullable=False)       # 밈 이름
    original_source = Column(String(256))             # 원본 출처 (영화명, 드라마명 등)
    rights_holder = Column(String(256))               # 권리자
    source_type = Column(String(64))                  # "movie", "tv_show", "photo", "comic" 등

    # 이미지 해시 (자체 학습)
    phash = Column(String(64), index=True)
    dhash = Column(String(64))

    # 감지 메타데이터
    detection_count = Column(Integer, default=1)
    last_detected_at = Column(DateTime)
    risk_score = Column(Float, default=0.70)

    # 출처 URL
    reference_url = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_meme_phash", "phash"),
    )


class KnownContentEmbedding(Base):
    """
    출처가 확인된 저작권 콘텐츠의 CLIP 임베딩 DB (누적 학습)

    Vision/Yandex 역검색으로 출처가 확인된 프레임의 CLIP 임베딩을 저장.
    같은 콘텐츠가 다시 등장하면 (리사이즈/크롭/재인코딩 변형 포함)
    API 호출 없이 코사인 유사도로 즉시 감지 → 비용 절감 + 쿼터 소진 시 백업.
    (pHash는 픽셀 단위 변형에 약하지만 CLIP 임베딩은 시각 의미 기반이라 강건)
    """
    __tablename__ = "known_content_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512))
    rights_holder = Column(String(256))
    source = Column(String(64))             # "google_vision", "yandex", "manual"
    reference_url = Column(Text)
    risk_score = Column(Float, default=0.70)

    phash = Column(String(128), index=True)  # 정확 중복 판단용
    embedding = Column(Text)                 # base64 float32 벡터 (CLIP 512차원)

    # 출처 추적 (사용자가 잘못된 학습을 식별·삭제할 수 있도록)
    job_id = Column(String(64))              # 어느 분석 작업에서 학습됐는지
    source_timestamp = Column(Float)         # 해당 영상의 몇 초 프레임이었는지

    detection_count = Column(Integer, default=1)
    last_detected_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# 분석 리포트
# ─────────────────────────────────────────────
class AnalysisReport(Base):
    """분석 결과 리포트"""
    __tablename__ = "analysis_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), ForeignKey("analysis_jobs.job_id"), unique=True)
    report_json = Column(JSON)              # 전체 리포트 JSON
    report_s3_key = Column(String(512))     # S3에 저장된 HTML 리포트
    summary = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("AnalysisJob", back_populates="report")
