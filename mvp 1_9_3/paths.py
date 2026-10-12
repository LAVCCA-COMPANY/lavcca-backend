"""
paths.py — mvp 1_9_3 파일 위치 모음. 파일을 옮길 때는 여기만 고친다.

  config/  업무 규칙·라벨·템플릿 (검토된 정책, 이미지에 포함)
  models/  학습된 분류 모델 (이미지에 포함, 나중에 S3 로 이동 예정)
  data/    학습·평가·검토 데이터 (오프라인 작업용, 이미지 제외)
  tools/   오프라인 스크립트 (학습 데이터 갱신·골든셋·평가·사례 동기화, 이미지 제외)
"""
from pathlib import Path

APP_DIR    = Path(__file__).resolve().parent
CONFIG_DIR = APP_DIR / "config"
MODELS_DIR = APP_DIR / "models"
DATA_DIR   = APP_DIR / "data"

RULES_YAML          = CONFIG_DIR / "rules.yaml"
LABELS_YAML         = CONFIG_DIR / "controversy_labels.yaml"
WORST_ACTIONS_YAML  = CONFIG_DIR / "worst_actions_map.yaml"
REPORT_TEMPLATE_V193 = CONFIG_DIR / "report_template_v1_9_3.md"

CONTROVERSY_MODEL   = MODELS_DIR / "controversy_model.joblib"

SAMPLES_JSON        = DATA_DIR / "controversy_samples.json"
GOLDEN_CANDIDATES   = DATA_DIR / "golden_set_candidates.json"
GOLDEN_SET          = DATA_DIR / "golden_set.json"
EVAL_BASELINE       = DATA_DIR / "eval_baseline.json"
PENDING_UPDATES     = DATA_DIR / "pending_updates.json"
TREND_SEEN          = DATA_DIR / ".trend_seen.json"

# 실행 산출물·로컬 상태 (커밋 안 함)
REPORTS_DIR = APP_DIR / "reports"
CASEDB_DIR  = APP_DIR / "casedb"
CASE_DB_JSON = APP_DIR / "case_db.json"   # 폐기된 옛 사례 DB(_deprecated/). 경로만 유지
