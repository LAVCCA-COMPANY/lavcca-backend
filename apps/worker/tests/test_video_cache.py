"""
DatabaseManager.check_video_cached() 회귀 테스트 (Ponytail Audit #2, 원인 B).

같은 영상을 --force 로 재분석하면 같은 해시의 COMPLETED 작업이 여러 개 생긴다.
이때 오류(MultipleResultsFound) 없이 '최신 완료 + 리포트 있음' 결과를 돌려줘야 한다.
메모리 SQLite 만 사용 → 실제 copyright_db.sqlite 는 건드리지 않는다.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "copyright_detector"))

from sqlalchemy import update  # noqa: E402

from database.db_manager import DatabaseManager  # noqa: E402
from database.models import AnalysisJob, AnalysisStatus  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def db():
    return DatabaseManager("sqlite://")


def _job(db, job_id, created_at, status=AnalysisStatus.COMPLETED, report=True,
         video_hash="HASH_X"):
    db.create_job(job_id=job_id, video_path=f"/v/{job_id}.mp4", video_hash=video_hash,
                  video_duration=10.0)
    db.update_job_status(job_id, status)
    with db.get_session() as s:
        s.execute(update(AnalysisJob).where(AnalysisJob.job_id == job_id)
                  .values(created_at=created_at))
        s.commit()
    if report:
        db.save_report(job_id, {"job_id": job_id, "summary": {}})


def test_duplicate_completed_returns_newest(db):
    _job(db, "OLD", T0)
    _job(db, "NEW", T0 + timedelta(minutes=5))      # --force 재분석
    assert db.check_video_cached("HASH_X")["job_id"] == "NEW"


def test_ignores_newer_failed_or_processing(db):
    _job(db, "DONE", T0)
    _job(db, "FAILED", T0 + timedelta(minutes=1), AnalysisStatus.FAILED)
    _job(db, "RUNNING", T0 + timedelta(minutes=2), AnalysisStatus.PROCESSING, report=False)
    assert db.check_video_cached("HASH_X")["job_id"] == "DONE"


def test_skips_newest_completed_without_report(db):
    # 상태는 COMPLETED 로 바뀌었지만 save_report 전에 죽은 작업
    _job(db, "WITH_REPORT", T0)
    _job(db, "NO_REPORT", T0 + timedelta(minutes=1), report=False)
    assert db.check_video_cached("HASH_X")["job_id"] == "WITH_REPORT"


def test_same_timestamp_prefers_later_insert(db):
    _job(db, "FIRST", T0)
    _job(db, "SECOND", T0)
    assert db.check_video_cached("HASH_X")["job_id"] == "SECOND"


def test_other_hash_or_nothing_completed_is_miss(db):
    _job(db, "OTHER", T0, video_hash="HASH_Y")
    _job(db, "FAILED", T0, AnalysisStatus.FAILED)
    assert db.check_video_cached("HASH_X") is None
