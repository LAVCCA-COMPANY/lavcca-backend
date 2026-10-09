"""
database/db_manager.py - DB 연결 및 데이터 관리
자체 학습 데이터 누적 로직 포함
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
import hashlib
import json
import structlog

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool

from .models import (
    Base, AnalysisJob, CopyrightFinding, AnalysisReport,
    KnownCopyrightedMusic, KnownLogo, KnownFont, KnownVideoClip, KnownMeme,
    KnownContentEmbedding,
    CopyrightType, RiskLevel, AnalysisStatus
)
from config import config

logger = structlog.get_logger()


class DatabaseManager:
    """
    DB 매니저 - 분석 결과 저장 및 자체 학습 데이터 누적
    """

    def __init__(self, database_url: Optional[str] = None):
        url = database_url or config.db.database_url
        # AWS Lambda / ECS 환경에서는 NullPool 사용 (연결 재사용 X)
        pool_class = NullPool if "lambda" in str(url).lower() else None
        kwargs = {"poolclass": pool_class} if pool_class else {}

        self.engine = create_engine(url, echo=False, **kwargs)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Session:
        return self.SessionLocal()

    # ─────────────────────────────────────────────
    # Job 관리
    # ─────────────────────────────────────────────
    def create_job(self, job_id: str, video_path: str, video_hash: str,
                   video_duration: float, metadata: Dict = None) -> AnalysisJob:
        with self.get_session() as session:
            job = AnalysisJob(
                job_id=job_id,
                video_path=video_path,
                video_filename=video_path.split("/")[-1],
                video_hash=video_hash,
                video_duration=video_duration,
                status=AnalysisStatus.PROCESSING,
                started_at=datetime.utcnow(),
                extra_metadata=metadata or {},
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            logger.info("job_created", job_id=job_id)
            return job

    def update_job_status(self, job_id: str, status: AnalysisStatus,
                          error: str = None, risk_score: float = None,
                          risk_level: RiskLevel = None, total_issues: int = None):
        with self.get_session() as session:
            stmt = (
                update(AnalysisJob)
                .where(AnalysisJob.job_id == job_id)
                .values(
                    status=status,
                    error_message=error,
                    overall_risk_score=risk_score,
                    overall_risk_level=risk_level,
                    total_issues_found=total_issues,
                    completed_at=datetime.utcnow() if status == AnalysisStatus.COMPLETED else None,
                    updated_at=datetime.utcnow(),
                )
            )
            session.execute(stmt)
            session.commit()

    def check_video_cached(self, video_hash: str) -> Optional[Dict]:
        """동일 영상 이전 분석 결과 확인 (중복 분석 방지)"""
        with self.get_session() as session:
            job = session.execute(
                select(AnalysisJob).where(
                    AnalysisJob.video_hash == video_hash,
                    AnalysisJob.status == AnalysisStatus.COMPLETED
                ).order_by(AnalysisJob.created_at.desc())
            ).scalars().first()   # --force 재분석으로 같은 해시의 완료 작업이 여러 개일 수 있음

            if job and job.report:
                logger.info("cache_hit", video_hash=video_hash, job_id=job.job_id)
                return job.report.report_json
        return None

    # ─────────────────────────────────────────────
    # Finding 저장
    # ─────────────────────────────────────────────
    def save_findings(self, findings: List[Dict]) -> int:
        """발견 사항 일괄 저장"""
        import json

        def clean_for_json(obj):
            if isinstance(obj, dict):
                return {k: clean_for_json(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clean_for_json(i) for i in obj]
            elif isinstance(obj, bool):
                return int(obj)
            elif isinstance(obj, (int, float, str)) or obj is None:
                return obj
            else:
                return str(obj)

        cleaned = []
        for f in findings:
            f = dict(f)
            if "raw_response" in f and f["raw_response"] is not None:
                f["raw_response"] = clean_for_json(f["raw_response"])
            cleaned.append(f)

        with self.get_session() as session:
            objs = [CopyrightFinding(**f) for f in cleaned]
            session.add_all(objs)
            session.commit()
            return len(objs)

    def save_report(self, job_id: str, report_data: Dict, s3_key: str = None):
        import json as _json
        summary = report_data.get("summary", "")
        if isinstance(summary, dict):
            summary = _json.dumps(summary, ensure_ascii=False)
        with self.get_session() as session:
            report = AnalysisReport(
                job_id=job_id,
                report_json=report_data,
                report_s3_key=s3_key,
                summary=summary,
            )
            session.add(report)
            session.commit()

    # ─────────────────────────────────────────────
    # 자체 학습 DB 업데이트 (핵심!)
    # ─────────────────────────────────────────────
    def learn_from_finding(self, finding_type: CopyrightType, data: Dict):
        """
        새로운 발견을 자체 DB에 누적 학습
        - 새 항목이면 추가, 기존 항목이면 카운트 증가
        """
        if not config.pipeline.learn_to_db:
            return  # 학습 저장 비활성 (config.pipeline.learn_to_db)
        try:
            if finding_type == CopyrightType.MUSIC:
                self._learn_music(data)
            elif finding_type == CopyrightType.LOGO:
                self._learn_logo(data)
            elif finding_type == CopyrightType.FONT:
                self._learn_font(data)
            elif finding_type == CopyrightType.VIDEO_CLIP:
                self._learn_video_clip(data)
            elif finding_type == CopyrightType.MEME:
                self._learn_meme(data)
        except Exception as e:
            logger.warning("learn_failed", type=finding_type, error=str(e))

    def _learn_meme(self, data: Dict):
        with self.get_session() as session:
            phash = data.get("phash")
            title = data.get("title", "알 수 없는 밈")

            existing = session.execute(
                select(KnownMeme).where(KnownMeme.phash == phash)
            ).scalar_one_or_none() if phash else None

            if existing:
                existing.detection_count += 1
                existing.last_detected_at = datetime.utcnow()
            else:
                meme = KnownMeme(
                    title=title,
                    original_source=data.get("original_source", ""),
                    rights_holder=data.get("rights_holder", ""),
                    source_type=data.get("source_type", "unknown"),
                    phash=phash,
                    dhash=data.get("dhash"),
                    risk_score=data.get("risk_score", 0.70),
                    reference_url=data.get("reference_url"),
                    detection_count=1,
                    last_detected_at=datetime.utcnow(),
                )
                session.add(meme)
            session.commit()

    def _learn_music(self, data: Dict):
        with self.get_session() as session:
            isrc = data.get("isrc")
            if not isrc:
                return

            existing = session.execute(
                select(KnownCopyrightedMusic).where(KnownCopyrightedMusic.isrc == isrc)
            ).scalar_one_or_none()

            if existing:
                existing.detection_count += 1
                existing.last_detected_at = datetime.utcnow()
            else:
                music = KnownCopyrightedMusic(
                    isrc=isrc,
                    title=data.get("title", ""),
                    artist=data.get("artist", ""),
                    album=data.get("album", ""),
                    rights_holder=data.get("rights_holder", ""),
                    audio_fingerprint=data.get("fingerprint"),
                    fingerprint_hash=data.get("fingerprint_hash"),
                    source=data.get("source", "unknown"),
                    acrcloud_id=data.get("acrcloud_id"),
                    youtube_ids=data.get("youtube_ids", []),
                    detection_count=1,
                    last_detected_at=datetime.utcnow(),
                )
                session.add(music)
            session.commit()
            logger.debug("music_learned", isrc=isrc)

    def _learn_logo(self, data: Dict):
        with self.get_session() as session:
            phash = data.get("phash")
            brand_name = data.get("brand_name", "")
            if not brand_name:
                return

            existing = session.execute(
                select(KnownLogo).where(KnownLogo.phash == phash)
            ).scalar_one_or_none() if phash else None

            if existing:
                existing.detection_count += 1
                existing.last_detected_at = datetime.utcnow()
            else:
                logo = KnownLogo(
                    brand_name=brand_name,
                    trademark_owner=data.get("trademark_owner", ""),
                    category=data.get("category", ""),
                    phash=phash,
                    dhash=data.get("dhash"),
                    source=data.get("source", "unknown"),
                    google_vision_label=data.get("google_vision_label"),
                    rekognition_label=data.get("rekognition_label"),
                    detection_count=1,
                    last_detected_at=datetime.utcnow(),
                )
                session.add(logo)
            session.commit()

    def _learn_font(self, data: Dict):
        with self.get_session() as session:
            font_name = data.get("font_name", "")
            if not font_name:
                return

            existing = session.execute(
                select(KnownFont).where(KnownFont.font_name == font_name)
            ).scalar_one_or_none()

            if existing:
                existing.detection_count += 1
            else:
                font = KnownFont(
                    font_name=font_name,
                    foundry=data.get("foundry", ""),
                    license_type=data.get("license_type", "commercial"),
                    requires_license=data.get("requires_license", True),
                )
                session.add(font)
            session.commit()

    def _learn_video_clip(self, data: Dict):
        with self.get_session() as session:
            youtube_id = data.get("youtube_id")
            if not youtube_id:
                return

            existing = session.execute(
                select(KnownVideoClip).where(KnownVideoClip.youtube_id == youtube_id)
            ).scalar_one_or_none()

            if existing:
                existing.detection_count += 1
                existing.last_detected_at = datetime.utcnow()
            else:
                clip = KnownVideoClip(
                    title=data.get("title", ""),
                    rights_holder=data.get("rights_holder", ""),
                    youtube_id=youtube_id,
                    source_platform=data.get("platform", "youtube"),
                    frame_hashes=data.get("frame_hashes", []),
                    detection_count=1,
                    last_detected_at=datetime.utcnow(),
                )
                session.add(clip)
            session.commit()

    # ─────────────────────────────────────────────
    # 자체 DB 조회 (빠른 사전 검색)
    # ─────────────────────────────────────────────
    def lookup_music_by_fingerprint(self, fingerprint_hash: str) -> Optional[Dict]:
        """자체 DB에서 음악 핑거프린트로 빠른 조회"""
        if not config.pipeline.use_learned_db:
            return None  # 자체 학습 DB 비활성
        with self.get_session() as session:
            music = session.execute(
                select(KnownCopyrightedMusic).where(
                    KnownCopyrightedMusic.fingerprint_hash == fingerprint_hash
                )
            ).scalar_one_or_none()

            if music:
                return {
                    "found": True,
                    "title": music.title,
                    "artist": music.artist,
                    "isrc": music.isrc,
                    "rights_holder": music.rights_holder,
                    "source": "internal_db",
                    "detection_count": music.detection_count,
                }
        return None

    def lookup_logo_by_phash(self, phash: str, threshold: int = 10) -> Optional[Dict]:
        """자체 DB에서 perceptual hash로 로고 조회"""
        if not config.pipeline.use_learned_db:
            return None  # 자체 학습 DB 비활성
        with self.get_session() as session:
            logos = session.execute(
                select(KnownLogo).where(KnownLogo.phash.isnot(None))
            ).scalars().all()

            for logo in logos:
                try:
                    # Hamming distance 계산
                    distance = bin(int(phash, 16) ^ int(logo.phash, 16)).count("1")
                    if distance <= threshold:
                        return {
                            "found": True,
                            "brand_name": logo.brand_name,
                            "trademark_owner": logo.trademark_owner,
                            "source": "internal_db",
                            "similarity": 1.0 - (distance / 64.0),
                        }
                except Exception:
                    continue
        return None

    def lookup_meme_by_phash(self, phash: str, threshold: int = 10) -> Optional[Dict]:
        """자체 DB에서 perceptual hash로 밈 템플릿 조회"""
        if not config.pipeline.use_learned_db:
            return None  # 자체 학습 DB 비활성
        with self.get_session() as session:
            memes = session.execute(
                select(KnownMeme).where(KnownMeme.phash.isnot(None))
            ).scalars().all()

            for meme in memes:
                try:
                    distance = bin(int(phash, 16) ^ int(meme.phash, 16)).count("1")
                    if distance <= threshold:
                        return {
                            "found": True,
                            "title": meme.title,
                            "rights_holder": meme.rights_holder,
                            "original_source": meme.original_source,
                            "source_type": meme.source_type,
                            "risk_score": meme.risk_score,
                            "similarity": 1.0 - (distance / 64.0),
                            "source": "internal_db",
                        }
                except Exception:
                    continue
        return None

    # ─────────────────────────────────────────────
    # CLIP 임베딩 콘텐츠 DB (시각 유사도 자체 학습)
    # ─────────────────────────────────────────────
    @staticmethod
    def _encode_embedding(vec) -> str:
        import base64
        import numpy as np
        return base64.b64encode(
            np.asarray(vec, dtype=np.float32).tobytes()
        ).decode("ascii")

    @staticmethod
    def _decode_embedding(text: str):
        import base64
        import numpy as np
        return np.frombuffer(base64.b64decode(text), dtype=np.float32)

    def lookup_content_by_phash(self, phash: str, threshold: int = 6,
                                 max_rows: int = 4000) -> Optional[Dict]:
        """
        pHash 해밍거리로 기학습 콘텐츠 조회 (CLIP 임베딩보다 먼저 도는 빠른 사전검사).

        장점:
          - torch/CLIP 불필요 → CLIP 비활성 환경에서도 자체 탐지 작동
          - 임베딩 코사인 계산보다 훨씬 저렴 → 전체 프레임에 돌릴 수 있음
        threshold=6 (64비트 중): 거의 동일한 프레임만 → 오탐 매우 낮음.
        리사이즈/크롭/색보정된 변형은 이걸로 못 잡고 임베딩 조회가 담당.
        """
        if not config.pipeline.use_learned_db:
            return None  # 자체 학습 DB 비활성 → 매칭 안 함 (순수 API 결과만 사용)
        if not phash:
            return None
        try:
            ph_int = int(phash, 16)
        except (ValueError, TypeError):
            return None
        try:
            with self.get_session() as session:
                rows = session.execute(
                    select(KnownContentEmbedding).where(
                        KnownContentEmbedding.phash.isnot(None)
                    ).limit(max_rows)
                ).scalars().all()
                best, best_dist = None, threshold + 1
                for row in rows:
                    try:
                        d = bin(ph_int ^ int(row.phash, 16)).count("1")
                        if d < best_dist:
                            best, best_dist = row, d
                    except Exception:
                        continue
                if best is not None and best_dist <= threshold:
                    return {
                        "found": True,
                        "learned_id": best.id,
                        "title": best.title,
                        "rights_holder": best.rights_holder,
                        "risk_score": best.risk_score,
                        "reference_url": best.reference_url,
                        "similarity": round(1.0 - best_dist / 64.0, 4),
                        "detection_count": best.detection_count,
                        "learned_from_job": best.job_id,
                        "source": "internal_phash_db",
                    }
        except Exception as e:
            logger.warning("phash_lookup_failed", error=str(e))
        return None

    def learn_content_embedding(self, data: Dict):
        """
        Vision/Yandex로 출처가 확인된 프레임의 CLIP 임베딩 저장.
        다음 분석에서 같은 콘텐츠 재등장 시 API 호출 없이 즉시 감지된다.
        """
        if not config.pipeline.learn_to_db:
            return  # 학습 저장 비활성 (config.pipeline.learn_to_db)
        emb = data.get("embedding")
        if emb is None:
            return
        try:
            with self.get_session() as session:
                phash = data.get("phash")
                existing = session.execute(
                    select(KnownContentEmbedding).where(
                        KnownContentEmbedding.phash == phash
                    )
                ).scalar_one_or_none() if phash else None

                if existing:
                    existing.detection_count += 1
                    existing.last_detected_at = datetime.utcnow()
                else:
                    session.add(KnownContentEmbedding(
                        title=data.get("title", ""),
                        rights_holder=data.get("rights_holder", ""),
                        source=data.get("source", "unknown"),
                        reference_url=data.get("reference_url"),
                        risk_score=data.get("risk_score", 0.70),
                        phash=phash,
                        embedding=self._encode_embedding(emb),
                        job_id=data.get("job_id"),
                        source_timestamp=data.get("source_timestamp"),
                        detection_count=1,
                        last_detected_at=datetime.utcnow(),
                    ))
                session.commit()
                logger.debug("content_embedding_learned",
                             title=data.get("title", "")[:40])
        except Exception as e:
            logger.warning("embedding_learn_failed", error=str(e))

    def lookup_content_by_embedding(self, embedding,
                                     threshold: float = 0.92,
                                     max_rows: int = 2000) -> Optional[Dict]:
        """
        CLIP 임베딩 코사인 유사도로 기학습 콘텐츠 조회.
        pHash와 달리 리사이즈/크롭/색보정/재인코딩된 변형도 잡는다.
        threshold 0.92: 같은 장면의 변형은 통과, 단순히 비슷한 분위기는 차단.
        """
        if not config.pipeline.use_learned_db:
            return None  # 자체 학습 DB 비활성 → 매칭 안 함 (순수 API 결과만 사용)
        import numpy as np
        if embedding is None:
            return None
        try:
            q = np.asarray(embedding, dtype=np.float32).ravel()
            qn = float(np.linalg.norm(q))
            if qn == 0:
                return None
            q = q / qn

            with self.get_session() as session:
                rows = session.execute(
                    select(KnownContentEmbedding).where(
                        KnownContentEmbedding.embedding.isnot(None)
                    ).limit(max_rows)
                ).scalars().all()

                best, best_sim = None, threshold
                for row in rows:
                    try:
                        v = self._decode_embedding(row.embedding)
                        if v.shape != q.shape:
                            continue
                        vn = float(np.linalg.norm(v))
                        if vn == 0:
                            continue
                        sim = float(np.dot(q, v / vn))
                        if sim >= best_sim:
                            best, best_sim = row, sim
                    except Exception:
                        continue

                if best is not None:
                    return {
                        "found": True,
                        "learned_id": best.id,   # 사용자 검증·삭제용 학습 ID
                        "title": best.title,
                        "rights_holder": best.rights_holder,
                        "risk_score": best.risk_score,
                        "reference_url": best.reference_url,
                        "similarity": round(best_sim, 4),
                        "detection_count": best.detection_count,
                        "learned_from_job": best.job_id,
                        "source": "internal_embedding_db",
                    }
        except Exception as e:
            logger.warning("embedding_lookup_failed", error=str(e))
        return None

    # ─────────────────────────────────────────────
    # 학습 데이터 감사 (사용자가 옳고 그름을 직접 검증)
    # ─────────────────────────────────────────────
    def list_learned_data(self) -> Dict[str, List[Dict]]:
        """
        자체 학습 DB 전체 목록 반환 — 사용자가 잘못 학습된 항목을 찾아낼 수 있도록
        ID·내용·출처(job_id/시점)·누적 횟수를 함께 제공한다.
        삭제: delete_learned_entry(kind, id)  /  CLI: python main.py --forget emb:3
        """
        out: Dict[str, List[Dict]] = {}
        with self.get_session() as session:
            out["emb"] = [
                {
                    "id": r.id,
                    "title": r.title,
                    "rights_holder": r.rights_holder,
                    "source": r.source,
                    "risk_score": r.risk_score,
                    "learned_from_job": r.job_id,
                    "video_timestamp": r.source_timestamp,
                    "detection_count": r.detection_count,
                    "reference_url": r.reference_url,
                    "created_at": str(r.created_at or ""),
                }
                for r in session.execute(select(KnownContentEmbedding)).scalars()
            ]
            out["logo"] = [
                {
                    "id": r.id, "title": r.brand_name,
                    "rights_holder": r.trademark_owner, "source": r.source,
                    "detection_count": r.detection_count,
                    "created_at": str(r.created_at or ""),
                }
                for r in session.execute(select(KnownLogo)).scalars()
            ]
            out["music"] = [
                {
                    "id": r.id, "title": f"{r.title} — {r.artist}",
                    "rights_holder": r.rights_holder, "source": r.source,
                    "isrc": r.isrc, "detection_count": r.detection_count,
                    "created_at": str(r.created_at or ""),
                }
                for r in session.execute(select(KnownCopyrightedMusic)).scalars()
            ]
            out["font"] = [
                {
                    "id": r.id, "title": r.font_name,
                    "rights_holder": r.foundry, "source": r.license_type,
                    "detection_count": r.detection_count,
                    "created_at": str(r.created_at or ""),
                }
                for r in session.execute(select(KnownFont)).scalars()
            ]
            out["meme"] = [
                {
                    "id": r.id, "title": r.title,
                    "rights_holder": r.rights_holder, "source": r.source_type,
                    "detection_count": r.detection_count,
                    "created_at": str(r.created_at or ""),
                }
                for r in session.execute(select(KnownMeme)).scalars()
            ]
            out["clip"] = [
                {
                    "id": r.id, "title": r.title,
                    "rights_holder": r.rights_holder, "source": r.source_platform,
                    "youtube_id": r.youtube_id, "detection_count": r.detection_count,
                    "created_at": str(r.created_at or ""),
                }
                for r in session.execute(select(KnownVideoClip)).scalars()
            ]
        return out

    def delete_learned_entry(self, kind: str, entry_id: int) -> bool:
        """
        잘못 학습된 항목 삭제.
        kind: emb | logo | music | font | meme | clip
        """
        model_map = {
            "emb": KnownContentEmbedding,
            "logo": KnownLogo,
            "music": KnownCopyrightedMusic,
            "font": KnownFont,
            "meme": KnownMeme,
            "clip": KnownVideoClip,
        }
        model = model_map.get(kind)
        if model is None:
            logger.warning("delete_learned_unknown_kind", kind=kind)
            return False
        with self.get_session() as session:
            row = session.get(model, entry_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            logger.info("learned_entry_deleted", kind=kind, id=entry_id)
            return True

    # 종류별 모델 + 수정 가능 필드 화이트리스트 (관리 UI/CLI 공용)
    _LEARNED_MODELS = {
        "emb":   (KnownContentEmbedding,
                  ["title", "rights_holder", "source", "risk_score", "reference_url"]),
        "logo":  (KnownLogo, ["brand_name", "trademark_owner", "category"]),
        "music": (KnownCopyrightedMusic, ["title", "artist", "rights_holder"]),
        "font":  (KnownFont, ["font_name", "foundry", "license_type", "requires_license"]),
        "meme":  (KnownMeme, ["title", "rights_holder", "source_type", "risk_score"]),
        "clip":  (KnownVideoClip, ["title", "rights_holder", "source_platform"]),
    }

    def update_learned_entry(self, kind: str, entry_id: int, updates: Dict) -> bool:
        """
        학습 항목의 필드 수정 (관리 페이지에서 사용).
        허용된 컬럼만 반영 (_LEARNED_MODELS 화이트리스트).
        """
        entry = self._LEARNED_MODELS.get(kind)
        if entry is None:
            return False
        model, allowed = entry
        with self.get_session() as session:
            row = session.get(model, entry_id)
            if row is None:
                return False
            changed = []
            for k, v in updates.items():
                if k not in allowed:
                    continue
                # 타입 보정
                col_type = getattr(type(row), k).type.__class__.__name__.lower()
                try:
                    if "float" in col_type:
                        v = float(v)
                    elif "integer" in col_type:
                        v = int(v)
                    elif "boolean" in col_type:
                        v = str(v).strip().lower() in ("1", "true", "yes", "on", "y")
                except (ValueError, TypeError):
                    continue
                setattr(row, k, v)
                changed.append(k)
            if changed:
                session.commit()
                logger.info("learned_entry_updated", kind=kind, id=entry_id, fields=changed)
            return bool(changed)

    def get_all_known_fonts(self) -> List[Dict]:
        """상업용 폰트 목록 반환"""
        if not config.pipeline.use_learned_db:
            return []  # 자체 학습 DB 비활성
        with self.get_session() as session:
            fonts = session.execute(
                select(KnownFont).where(KnownFont.requires_license == True)
            ).scalars().all()
            return [{"name": f.font_name, "foundry": f.foundry} for f in fonts]

    def get_stats(self) -> Dict:
        """DB 통계"""
        with self.get_session() as session:
            return {
                "total_jobs": session.query(AnalysisJob).count(),
                "completed_jobs": session.query(AnalysisJob).filter(
                    AnalysisJob.status == AnalysisStatus.COMPLETED
                ).count(),
                "known_music": session.query(KnownCopyrightedMusic).count(),
                "known_logos": session.query(KnownLogo).count(),
                "known_fonts": session.query(KnownFont).count(),
                "known_clips": session.query(KnownVideoClip).count(),
                "known_embeddings": session.query(KnownContentEmbedding).count(),
                "total_findings": session.query(CopyrightFinding).count(),
            }


# 싱글톤 인스턴스
_db_manager: Optional[DatabaseManager] = None

def get_db_manager() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
