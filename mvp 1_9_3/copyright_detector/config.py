"""
config.py - 전체 설정 관리
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = Path(__file__).parent

# GOOGLE_APPLICATION_CREDENTIALS may be a relative path in .env (portable
# across machines); google-cloud client libraries read this env var directly
# and need an absolute path regardless of the process's working directory.
# 단, 파일이 실제로 존재할 때만 export한다. 없는 경로를 남겨두면 google-cloud
# 라이브러리가 'file not found'로 실패한다. (Vision은 GOOGLE_API_KEY REST 사용이라
# 서비스계정 JSON이 없어도 정상 동작 — 인증 경로를 API 키로 일원화)
_cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
if _cred_path:
    _abs_cred = Path(_cred_path) if os.path.isabs(_cred_path) else (_BASE_DIR / _cred_path)
    if _abs_cred.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_abs_cred)
    else:
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

# ─────────────────────────────────────────────
# API Keys
# ─────────────────────────────────────────────
@dataclass
class APIConfig:
    # ACRCloud (음악 인식) - https://www.acrcloud.com
    acrcloud_host: str = os.getenv("ACRCLOUD_HOST", "")
    acrcloud_access_key: str = os.getenv("ACRCLOUD_ACCESS_KEY", "")
    acrcloud_access_secret: str = os.getenv("ACRCLOUD_ACCESS_SECRET", "")

    # AudD (음악 인식 백업) - https://audd.io
    audd_api_token: str = os.getenv("AUDD_API_TOKEN", "")

    # Google Cloud Vision (이미지/로고 분석)
    google_credentials_path: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")

    # YouTube Data API (영상 Content ID)
    youtube_api_key: str = os.getenv("YOUTUBE_API_KEY", "")

    # AWS
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_region: str = os.getenv("AWS_REGION", "ap-northeast-2")  # 서울 리전

    # SerpAPI (밈 역이미지 검색) - https://serpapi.com
    serpapi_key: str = os.getenv("SERPAPI_KEY", "")


@dataclass
class OwnContentConfig:
    """
    본인 소유 콘텐츠 등록 (오탐 방지).
    Vision이 본인 유튜브/사이트에서 자기 콘텐츠를 찾아 저작권 위반으로
    오탐하는 것을 막는다. .env에 쉼표로 구분해 입력.
      OWN_CHANNELS=MyChannel,@myhandle
      OWN_DOMAINS=myblog.com,myname.tistory.com
    """
    channels: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("OWN_CHANNELS", "").split(",") if c.strip()
    ])
    domains: list = field(default_factory=lambda: [
        d.strip() for d in os.getenv("OWN_DOMAINS", "").split(",") if d.strip()
    ])


# ─────────────────────────────────────────────
# Database Config
# ─────────────────────────────────────────────
@dataclass
class DatabaseConfig:
    # 로컬/프로덕션 DB (PostgreSQL on RDS)
    database_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///./copyright_db.sqlite"  # 로컬 개발용 SQLite
    )
    # AWS RDS (프로덕션)
    rds_host: str = os.getenv("RDS_HOST", "")
    rds_port: int = int(os.getenv("RDS_PORT", "5432"))
    rds_db: str = os.getenv("RDS_DB", "copyright_detector")
    rds_user: str = os.getenv("RDS_USER", "")
    rds_password: str = os.getenv("RDS_PASSWORD", "")

    @property
    def production_url(self) -> str:
        if self.rds_host:
            return f"postgresql://{self.rds_user}:{self.rds_password}@{self.rds_host}:{self.rds_port}/{self.rds_db}"
        return self.database_url


# ─────────────────────────────────────────────
# Pipeline Config
# ─────────────────────────────────────────────
@dataclass
class PipelineConfig:
    # 오디오 추출 설정
    audio_sample_rate: int = 16000
    audio_chunk_duration: int = 10        # seconds per chunk for music recognition
    audio_chunk_overlap: int = 2          # overlap between chunks
    audio_fingerprint_duration: int = 12  # ACRCloud fingerprint length

    # 음악 인식 비용 절감 (2단계 프로브)
    #   stride=2: 1단계에서 청크 2개당 1개만 API 조회 → 히트 주변만 2단계 정밀 조회.
    #   미조회 구간의 미커버 오디오는 최대 ~8초 (fingerprint 12s가 양옆을 덮음)
    #   → ACRCloud가 안정적으로 매칭하려면 5~10초가 필요하므로 실질 정확도 손실 없음.
    #   1 = 전수 조사 (기존 방식)
    music_probe_stride: int = int(os.getenv("MUSIC_PROBE_STRIDE", "2"))
    #   무음 게이트: 정규화 RMS가 이 값 미만인 청크는 API 호출 스킵 (인트로/아웃트로 무음)
    music_silence_rms: float = float(os.getenv("MUSIC_SILENCE_RMS", "0.003"))

    # 역검색 API 작업당 호출 상한 (비용 상한선 — 캐시 히트는 카운트 제외)
    #   Google Vision Web Detection: $3.5/1000건 → 40건 = 작업당 최대 $0.14
    #   SerpAPI(Yandex): 플랜에 따라 검색당 ~$0.01+ → 15건 상한
    vision_max_calls_per_job: int = int(os.getenv("VISION_MAX_CALLS_PER_JOB", "40"))
    yandex_max_calls_per_job: int = int(os.getenv("YANDEX_MAX_CALLS_PER_JOB", "15"))
    #   Yandex 역검색은 프레임을 공개 임시 호스트(0x0.st/tmpfiles.org)에 올려야 해서
    #   기본 비활성. 영상 프레임 외부 공개에 동의한 경우에만 "true"로 켠다.
    yandex_public_upload_enabled: bool = os.getenv("YANDEX_PUBLIC_UPLOAD_ENABLED", "false").lower() == "true"

    # 다운로드 화질 상한 (px, 세로 해상도)
    #   분석 다운스트림이 모두 축소해서 사용하므로 원본 HD가 불필요:
    #     CLIP → 224px, Google Vision 역검색 → 800px, Yandex → JPEG 재인코딩
    #   유일하게 해상도가 의미 있는 곳은 워터마크/방송자막 OCR인데 720p면 충분.
    #   1080p→720p로 다운로드 용량 2~4배, 프레임 디코딩 시간 절감.
    #   0 = 무제한(원본 최고화질). 작은 워터마크 정확도가 아쉬우면 1080으로.
    download_max_height: int = int(os.getenv("DOWNLOAD_MAX_HEIGHT", "720"))

    # 영상 프레임 추출 (extract_frames_smart 파라미터)
    frame_extraction_fps: float = 1.0     # target FPS (길이에 따라 자동 축소)
    frame_phash_threshold: int = 12       # 유사 프레임 스킵 threshold (64비트 중)
    frame_scene_threshold: float = 30.0   # scene change detection threshold
    # 최대 추출 프레임 수 (30분 영상 전체 커버: ~5.6초 간격). 클수록 미탐↓ 정확도↑,
    # 대신 시각 분석이 느려짐. 너무 느리면 .env 에서 FRAME_MAX_COUNT 를 낮춘다(예: 160).
    frame_max_count: int = int(os.getenv("FRAME_MAX_COUNT", "320"))

    # 폰트 분석 보류 (2026-07)
    # 시각 기반 폰트 분류가 신뢰 불가로 판명(평범한 산세리프 구분 불가, 오탐 다수)
    # → 폰트 분석 전체 비활성. 재개하려면 True로 변경 + 검증된 모델 필요.
    enable_font_analysis: bool = False

    # 자체 학습 DB — 읽기(탐지 반영)와 쓰기(학습 저장)를 독립적으로 제어 (2026-07)
    #
    # ① use_learned_db (읽기/반영): 자체 학습된 임베딩·로고 등을 탐지 결과에 반영할지.
    #    오학습 항목이 정확도를 떨어뜨려 기본 False. False면 분석은 순수 API 결과만 사용.
    #    .env: USE_LEARNED_DB=true 로 켤 수 있음.
    use_learned_db: bool = os.getenv("USE_LEARNED_DB", "false").lower() == "true"
    #
    # ② learn_to_db (쓰기/학습): 분석 중 확인된 콘텐츠를 자체 DB에 계속 누적 학습할지.
    #    기본 True — 탐지 반영(①)은 꺼도 데이터는 계속 쌓아두어, 나중에 ①을 켜면
    #    그동안 학습된 걸 바로 활용할 수 있다. .env: LEARN_TO_DB=false 로 끌 수 있음.
    learn_to_db: bool = os.getenv("LEARN_TO_DB", "true").lower() == "true"

    # 병렬 처리
    max_workers: int = 8                  # 병렬 워커 수
    batch_size: int = 10                  # 배치 처리 크기

    # 타임아웃 (AWS Lambda 고려)
    total_timeout_minutes: int = 10       # 전체 10분 목표
    api_timeout_seconds: int = 30

    # 신뢰도 임계값
    music_confidence_threshold: float = 0.6
    image_confidence_threshold: float = 0.7
    logo_confidence_threshold: float = 0.75

    # 정확도 튜닝 (2026-07)
    # ① 약한 시각 finding 강등: 교차검증(두 엔진 일치) 없이 단일 신호로 잡힌
    #    image/video_clip/logo 중 confidence 가 이 값 미만이면 위험도를 LOW(참고)로
    #    하향한다. 오탐 1건이 영상 전체를 HIGH로 부풀리는 것을 막음. (music은 제외)
    #    0 으로 두면 강등 안 함. .env: WEAK_VISUAL_DEMOTE_CONF
    weak_visual_demote_conf: float = float(os.getenv("WEAK_VISUAL_DEMOTE_CONF", "0.70"))

    # ② 유튜브 스튜디오 기준 점수 정렬 (하이브리드)
    #    risk_score를 '법적 침해 심각도'가 아니라 '유튜브가 실제로 조치할 가능성'에 맞춰 재조정.
    #    - 음악/영상클립: Content ID 자동조치 대상 → 유지~강화
    #    - 정지 이미지:   유튜브 자동조치 낮음 → 하향(단 법적 리스크 바닥값 유지, MEDIUM 상한)
    #    - 로고:          상표권 → 유튜브 저작권 시스템 밖 → LOW
    #    원래(법적) 점수는 finding['legal_risk_score']에 보존한다.
    #    False면 예전 법적 점수 그대로. .env: YOUTUBE_ALIGNED_SCORING
    youtube_aligned_scoring: bool = os.getenv("YOUTUBE_ALIGNED_SCORING", "true").lower() == "true"
    #    정지 이미지 위험도 상한 (유튜브 자동조치 낮음 → MEDIUM 이하로 캡)
    image_risk_cap: float = float(os.getenv("IMAGE_RISK_CAP", "0.55"))


# ─────────────────────────────────────────────
# AWS Config
# ─────────────────────────────────────────────
@dataclass
class AWSConfig:
    region: str = os.getenv("AWS_REGION", "ap-northeast-2")

    # S3
    s3_bucket: str = os.getenv("S3_BUCKET", "copyright-detector-videos")
    s3_results_bucket: str = os.getenv("S3_RESULTS_BUCKET", "copyright-detector-results")

    # ECS Fargate (메인 실행 환경)
    ecs_cluster: str = os.getenv("ECS_CLUSTER", "copyright-detector-cluster")
    ecs_task_definition: str = os.getenv("ECS_TASK_DEFINITION", "copyright-detector-task")

    # Lambda (경량 태스크용)
    lambda_function: str = os.getenv("LAMBDA_FUNCTION", "copyright-detector")

    # Rekognition (AWS 이미지 분석)
    use_rekognition: bool = os.getenv("USE_REKOGNITION", "true").lower() == "true"

    # SQS (비동기 처리)
    sqs_queue_url: str = os.getenv("SQS_QUEUE_URL", "")

    # ElastiCache Redis (캐시)
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))


# ─────────────────────────────────────────────
# 저작권 위험도 기준
# ─────────────────────────────────────────────
@dataclass
class RiskConfig:
    # 위험도 등급 (0.0 ~ 1.0)
    HIGH_THRESHOLD: float = 0.75      # 🔴 높음
    MEDIUM_THRESHOLD: float = 0.45    # 🟡 중간
    LOW_THRESHOLD: float = 0.20       # 🟢 낮음
    # 이하 = SAFE

    # 분류별 기본 가중치 (합산 시 사용)
    WEIGHTS = {
        "music": 0.35,      # 음악이 가장 위험
        "video_clip": 0.28, # 영상 클립
        "image": 0.22,      # 이미지/사진
        "logo": 0.09,       # 로고/상표
        "font": 0.06,       # 폰트
    }


# ─────────────────────────────────────────────
# 통합 설정
# ─────────────────────────────────────────────
class Config:
    api = APIConfig()
    db = DatabaseConfig()
    pipeline = PipelineConfig()
    aws = AWSConfig()
    risk = RiskConfig()
    own = OwnContentConfig()

    BASE_DIR = Path(__file__).parent
    TEMP_DIR = BASE_DIR / "temp"
    REPORTS_DIR = BASE_DIR / "results"

    @classmethod
    def ensure_dirs(cls):
        cls.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        cls.REPORTS_DIR.mkdir(parents=True, exist_ok=True)


config = Config()
config.ensure_dirs()
