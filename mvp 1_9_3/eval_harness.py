# -*- coding: utf-8 -*-
"""
eval_harness.py  —  골든셋 평가 + 카테고리별 재현율 + 비회귀 게이트 [3순위]

왜 필요한가:
  rules.yaml 수정 / 모델 재학습 / trend_updater 머지 때마다 성능이 좋아졌는지
  나빠졌는지 모른 채 날아가지 않으려면, 라벨링된 골든셋에 대한 P/R 추적이 필수.
  리스크 툴은 false negative(놓친 캔슬 위험)의 비용이 false positive보다 크므로
  '고심각도 카테고리 재현율'을 따로 본다.

사용:
  1) 골든셋 작성 (golden_set.json):
     [
       {"text": "문장 또는 자막 한 줄", "labels": ["L03"]},
       {"text": "...", "labels": ["L12"]}          # L12 = 해당 없음
     ]
  2) 평가:        python eval_harness.py golden_set.json
  3) 기준 저장:   python eval_harness.py golden_set.json --save-baseline
  4) 비회귀 게이트(머지 전 CI):  python eval_harness.py golden_set.json --gate

게이트는 고심각도 카테고리 재현율이 baseline 대비 떨어지면 exit code 1 → trend 머지 차단.
"""

import sys
import json
from pathlib import Path
from collections import defaultdict

BASE_DIR      = Path(__file__).parent
BASELINE_PATH = BASE_DIR / "eval_baseline.json"

# mvp 엔진과 동일 기준의 고심각도 라벨 (mvp_ver_1_9_2.HIGH_SEVERITY_LABELS 와 일치시킬 것)
HIGH_SEVERITY_LABELS = {"L03", "L06", "L09", "L11"}
RECALL_REGRESSION_TOL = 0.02   # 고심각도 재현율 허용 하락폭


def _load_predict_fn():
    """
    학습된 ML 모델(controversy_model.joblib)로 멀티라벨 예측 함수를 만든다.
    threshold 이상인 라벨 집합을 반환. (Tier1 트리거 기준과 동일 사상)
    """
    import joblib
    import numpy as np
    from classifier_model import normalize_text   # 동일 전처리 재사용

    bundle = joblib.load(BASE_DIR / "controversy_model.joblib")
    model, mlb = bundle["model"], bundle["mlb"]
    classes = list(mlb.classes_)

    def predict(text, threshold=0.3):
        probs = model.predict_proba([normalize_text(text)])[0]
        labels = {classes[i] for i, p in enumerate(probs) if p >= threshold}
        return labels or {"L12"}

    return predict


def evaluate(golden_path: str) -> dict:
    predict = _load_predict_fn()
    with open(golden_path, "r", encoding="utf-8-sig") as f:
        golden = json.load(f)
    # 라벨을 아직 채우지 않은(검수 보류) 항목은 평가에서 제외
    golden = [g for g in golden if g.get("labels")]

    # 카테고리별 TP/FP/FN 집계
    tp = defaultdict(int); fp = defaultdict(int); fn = defaultdict(int)
    for item in golden:
        gold = set(item["labels"])
        pred = predict(item["text"])
        for lbl in pred & gold: tp[lbl] += 1
        for lbl in pred - gold: fp[lbl] += 1
        for lbl in gold - pred: fn[lbl] += 1

    per_cat = {}
    all_labels = set(tp) | set(fp) | set(fn)
    for lbl in sorted(all_labels):
        p_den = tp[lbl] + fp[lbl]
        r_den = tp[lbl] + fn[lbl]
        precision = tp[lbl] / p_den if p_den else 0.0
        recall    = tp[lbl] / r_den if r_den else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_cat[lbl] = {"precision": round(precision, 4),
                        "recall": round(recall, 4),
                        "f1": round(f1, 4),
                        "support": r_den}

    # 고심각도 매크로 재현율 (게이트 지표)
    hs = [per_cat[l]["recall"] for l in HIGH_SEVERITY_LABELS if l in per_cat]
    high_sev_recall = round(sum(hs) / len(hs), 4) if hs else None

    return {"n": len(golden), "per_category": per_cat,
            "high_severity_macro_recall": high_sev_recall}


def _print_report(scores: dict):
    print(f"\n📊 골든셋 평가 (n={scores['n']})")
    print(f"{'라벨':<6}{'정밀도':>9}{'재현율':>9}{'F1':>9}{'지원':>7}")
    print("─" * 42)
    for lbl, m in scores["per_category"].items():
        flag = " ⚠️HS" if lbl in HIGH_SEVERITY_LABELS else ""
        print(f"{lbl:<6}{m['precision']:>9.3f}{m['recall']:>9.3f}"
              f"{m['f1']:>9.3f}{m['support']:>7}{flag}")
    print("─" * 42)
    print(f"🔴 고심각도 매크로 재현율: {scores['high_severity_macro_recall']}")


def gate(scores: dict) -> bool:
    """baseline 대비 고심각도 재현율 비회귀 검사. 통과=True."""
    if not BASELINE_PATH.exists():
        print("⚠️ baseline 없음 → 게이트 스킵 (먼저 --save-baseline 실행).")
        return True
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    cur  = scores["high_severity_macro_recall"] or 0.0
    prev = base.get("high_severity_macro_recall") or 0.0
    if cur + RECALL_REGRESSION_TOL < prev:
        print(f"❌ 비회귀 게이트 실패: 고심각도 재현율 {prev:.3f} → {cur:.3f} (하락)")
        return False
    print(f"✅ 비회귀 게이트 통과: 고심각도 재현율 {prev:.3f} → {cur:.3f}")
    return True


def main():
    if len(sys.argv) < 2:
        print("사용법: python eval_harness.py golden_set.json [--save-baseline|--gate]")
        sys.exit(2)

    golden_path = sys.argv[1]
    scores = evaluate(golden_path)
    _print_report(scores)

    if "--save-baseline" in sys.argv:
        BASELINE_PATH.write_text(
            json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 baseline 저장 → {BASELINE_PATH.name}")

    if "--gate" in sys.argv:
        sys.exit(0 if gate(scores) else 1)


if __name__ == "__main__":
    main()
