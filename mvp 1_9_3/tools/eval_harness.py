# -*- coding: utf-8 -*-
"""
eval_harness.py  —  골든셋 평가 + 카테고리별 재현율 + 비회귀 게이트 [3순위]

왜 필요한가:
  rules.yaml 수정 / 모델 재학습 / trend_updater 머지 때마다 성능이 좋아졌는지
  나빠졌는지 모른 채 날아가지 않으려면, 라벨링된 골든셋에 대한 P/R 추적이 필수.
  리스크 툴은 false negative(놓친 캔슬 위험)의 비용이 false positive보다 크므로
  '고심각도 카테고리 재현율'을 따로 본다.

사용:
  1) 골든셋 작성 (data/golden_set.json, tools/label_golden.py 로 라벨링):
     [
       {"text": "문장 또는 자막 한 줄", "labels": ["L03"]},
       {"text": "...", "labels": ["L12"]}          # L12 = 해당 없음
     ]
  2) 평가:        python tools/eval_harness.py data/golden_set.json
  3) 기준 저장:   python tools/eval_harness.py data/golden_set.json --save-baseline
  4) 비회귀 게이트(머지 전 CI):  python tools/eval_harness.py data/golden_set.json --gate

게이트는 고심각도 카테고리 재현율이 baseline 대비 떨어지면 exit code 1 → trend 머지 차단.
"""

import sys
import json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # mvp 1_9_3/ (paths·엔진 모듈)
import paths

BASELINE_PATH = paths.EVAL_BASELINE

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

    bundle = joblib.load(paths.CONTROVERSY_MODEL)
    model, mlb = bundle["model"], bundle["mlb"]
    classes = list(mlb.classes_)

    def predict(text, threshold=0.3):
        probs = model.predict_proba([normalize_text(text)])[0]
        labels = {classes[i] for i, p in enumerate(probs) if p >= threshold}
        return labels or {"L12"}

    return predict


def _reference_labels(item: dict):
    """사람 라벨이 있으면 gold. 비어 있고 tier2가 있으면 silver(회귀 추적용)."""
    labels = [l for l in (item.get("labels") or []) if l]
    if labels:
        return set(labels), "gold"
    tier2 = item.get("tier2") or {}
    if not tier2:
        return None, None
    if tier2.get("is_controversy") is False:
        return {"L12"}, "silver"
    category = tier2.get("category")
    if category:
        return {category}, "silver"
    return None, None


def evaluate(golden_path: str) -> dict:
    predict = _load_predict_fn()
    with open(golden_path, "r", encoding="utf-8-sig") as f:
        golden = json.load(f)
    # 사람 라벨이 없으면 저장된 tier2 판정을 silver 기준으로 쓴다. 파일은 고치지 않는다.
    # 이 수치는 라이브 Tier2 재호출이 아니라, SVM이 그때의 Tier2와 얼마나 맞는지다.
    usable = []
    for item in golden:
        ref, kind = _reference_labels(item)
        if ref is None:
            continue
        usable.append({**item, "_ref": ref, "_kind": kind})
    golden = usable

    # 카테고리별 TP/FP/FN 집계 (표시 임계값 0.3)
    tp = defaultdict(int); fp = defaultdict(int); fn = defaultdict(int)
    trig_hit = trig_total = 0
    for item in golden:
        gold = set(item["_ref"])
        pred = predict(item["text"])
        for lbl in pred & gold: tp[lbl] += 1
        for lbl in pred - gold: fp[lbl] += 1
        for lbl in gold - pred: fn[lbl] += 1
        risky = gold - {"L12"}
        if risky:
            trig_total += 1
            if predict(item["text"], threshold=0.15) & risky:
                trig_hit += 1

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
    hs = [per_cat[l]["recall"] for l in HIGH_SEVERITY_LABELS
          if l in per_cat and per_cat[l]["support"] > 0]
    high_sev_recall = round(sum(hs) / len(hs), 4) if hs else None

    n_gold = sum(1 for g in golden if g["_kind"] == "gold")
    trigger_recall = round(trig_hit / trig_total, 4) if trig_total else None
    return {
        "n": len(golden),
        "n_gold": n_gold,
        "n_silver": len(golden) - n_gold,
        "per_category": per_cat,
        "high_severity_macro_recall": high_sev_recall,
        "tier1_trigger_recall": trigger_recall,
        "reference": "gold" if n_gold and n_gold == len(golden) else "silver" if not n_gold else "mixed",
    }


def _print_report(scores: dict):
    print(f"\n📊 평가 n={scores['n']} (사람 라벨 {scores.get('n_gold', 0)}, "
          f"Tier2 silver {scores.get('n_silver', 0)}, 기준={scores.get('reference')})")
    print(f"{'라벨':<6}{'정밀도':>9}{'재현율':>9}{'F1':>9}{'지원':>7}")
    print("─" * 42)
    for lbl, m in scores["per_category"].items():
        flag = " ⚠️HS" if lbl in HIGH_SEVERITY_LABELS else ""
        print(f"{lbl:<6}{m['precision']:>9.3f}{m['recall']:>9.3f}"
              f"{m['f1']:>9.3f}{m['support']:>7}{flag}")
    print("─" * 42)
    print(f"🔴 고심각도 매크로 재현율: {scores['high_severity_macro_recall']}")
    print(f"🎯 Tier1 트리거 재현율(임계값 0.15, 위험 라벨 교집합): {scores.get('tier1_trigger_recall')}")


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
        print("사용법: python tools/eval_harness.py data/golden_set.json [--save-baseline|--gate]")
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
