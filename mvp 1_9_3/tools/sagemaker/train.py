# -*- coding: utf-8 -*-
"""
train.py — 논란 분류기(ML① Tier1) 학습 스크립트. 로컬과 SageMaker script mode 에서 똑같이 돈다.

학습 로직은 classifier_model.prepare_training_data / build_pipeline 을 그대로 쓴다(중복 구현 없음).
AWS 호출 없음. 사용법·환경변수 규칙은 같은 폴더의 README.md.

  python tools/sagemaker/train.py            # 홀드아웃 평가만, out/metrics.json
  python tools/sagemaker/train.py --final    # + 전체 데이터로 out/controversy_model.joblib
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]   # mvp 1_9_3/ (SageMaker 에선 /opt/ml/code)
sys.path.insert(0, str(APP_DIR))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows cp949 콘솔
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import joblib
import numpy as np
import scipy
import sklearn
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MultiLabelBinarizer

from classifier_model import (EXCLUDED_LABELS, LABEL_NAMES, build_pipeline,
                              normalize_text, prepare_training_data)

DATA_FILE = "controversy_samples.json"
MODEL_FILE = "controversy_model.joblib"
SERVICE_MODEL = APP_DIR / "models" / MODEL_FILE   # 서비스 모델. 절대 덮어쓰지 않는다.


def str2bool(v):
    # SageMaker 는 하이퍼파라미터를 "--final True" 처럼 문자열로 넘긴다.
    if isinstance(v, bool):
        return v
    if v.lower() in ("1", "true", "yes", "y"):
        return True
    if v.lower() in ("0", "false", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"bool 값이 아님: {v}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--holdout", type=float, default=0.2, help="검증 비율 (0 이면 평가 생략)")
    p.add_argument("--threshold", type=float, default=0.15, help="Tier1 트리거 임계값")
    p.add_argument("--display-threshold", type=float, default=0.3,
                   help="라벨별 P/R/F1 판정 임계값 (eval_harness 와 같은 0.3)")
    p.add_argument("--final", type=str2bool, nargs="?", const=True, default=False,
                   help="전체 데이터로 최종 모델도 저장")
    p.add_argument("--ngram-min", type=int, default=2)
    p.add_argument("--ngram-max", type=int, default=4)
    p.add_argument("--C", type=float, default=1.0)
    p.add_argument("--data-dir", default=os.environ.get("SM_CHANNEL_TRAIN") or str(APP_DIR / "data"))
    p.add_argument("--model-dir", default=os.environ.get("SM_MODEL_DIR") or str(APP_DIR / "out"))
    args, unknown = p.parse_known_args()
    if unknown:
        print(f"⚠️ 무시한 인자: {unknown}")
    return args


def load_samples(data_dir):
    path = Path(data_dir) / DATA_FILE
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    return data, path, hashlib.sha256(raw).hexdigest()


def fit(raw_data, args):
    texts, labels, cv_folds = prepare_training_data(raw_data)
    mlb = MultiLabelBinarizer()
    y = mlb.fit_transform(labels)
    pipeline = build_pipeline(cv_folds, ngram_range=(args.ngram_min, args.ngram_max), C=args.C)
    print(f"🚀 학습 (n={len(texts)}, CV Folds: {cv_folds})...")
    pipeline.fit(texts, y)
    return pipeline, mlb, cv_folds


def evaluate(pipeline, mlb, val_items, args):
    """검증 문장의 실제 라벨(제외 라벨만 뺀 것) 기준. 정의는 tools/eval_harness.py 와 같다."""
    classes = list(mlb.classes_)
    probs = pipeline.predict_proba([normalize_text(it["text"]) for it in val_items])

    tp, fp, fn = {}, {}, {}
    trig_hit = trig_total = 0
    for item, p in zip(val_items, probs):
        gold = set(item["labels"])
        pred = {classes[i] for i, v in enumerate(p) if v >= args.display_threshold} or {"L12"}
        for l in pred & gold: tp[l] = tp.get(l, 0) + 1
        for l in pred - gold: fp[l] = fp.get(l, 0) + 1
        for l in gold - pred: fn[l] = fn.get(l, 0) + 1
        risky = gold - {"L12"}
        if risky:
            trig_total += 1
            if {classes[i] for i, v in enumerate(p) if v >= args.threshold} & risky:
                trig_hit += 1

    per_label = {}
    for l in sorted((set(tp) | set(fp) | set(fn)) - {"L12"}):
        t, f_p, f_n = tp.get(l, 0), fp.get(l, 0), fn.get(l, 0)
        prec = t / (t + f_p) if t + f_p else 0.0
        rec = t / (t + f_n) if t + f_n else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per_label[l] = {"precision": round(prec, 4), "recall": round(rec, 4),
                        "f1": round(f1, 4), "support": t + f_n}

    supported = [m["f1"] for m in per_label.values() if m["support"] > 0]
    return {
        "n_val": len(val_items),
        "per_label": per_label,
        # 검증 세트에 실제로 나온 위험 라벨만 평균 (지원 0 라벨은 제외)
        "macro_f1": round(sum(supported) / len(supported), 4) if supported else None,
        "trigger_recall": round(trig_hit / trig_total, 4) if trig_total else None,
        "trigger_total": trig_total,
        "not_in_train": sorted({l for it in val_items for l in it["labels"]} - set(classes) - {"L12"}),
    }


def main():
    args = parse_args()
    model_dir = Path(args.model_dir).resolve()
    if model_dir == SERVICE_MODEL.parent.resolve():
        sys.exit(f"❌ 서비스 모델 폴더({model_dir})에는 저장하지 않습니다. --model-dir 를 바꾸세요.")
    model_dir.mkdir(parents=True, exist_ok=True)

    raw_data, data_path, data_sha = load_samples(args.data_dir)
    print(f"📦 데이터: {data_path} ({len(raw_data)}개)")

    metrics = {
        "params": {k: v for k, v in vars(args).items() if k not in ("data_dir", "model_dir")},
        "data": {"file": DATA_FILE, "n_raw": len(raw_data), "sha256": data_sha},
        "versions": {"python": sys.version.split()[0], "scikit-learn": sklearn.__version__,
                     "numpy": np.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    if args.holdout > 0:
        # 학습과 같은 기준으로 제외 라벨만 빼고, 라벨이 빈 샘플은 버린다.
        usable = []
        for it in raw_data:
            labs = [l for l in it.get("labels", []) if l not in EXCLUDED_LABELS]
            if labs:
                usable.append({**it, "labels": labs})
        train_items, val_items = train_test_split(usable, test_size=args.holdout, random_state=args.seed)
        pipeline, mlb, cv_folds = fit(train_items, args)
        val = evaluate(pipeline, mlb, val_items, args)
        metrics["holdout"] = {"n_train": len(train_items), "cv_folds": cv_folds,
                              "classes": list(mlb.classes_), **val}

        print(f"\n{'라벨':<6}{'정밀도':>9}{'재현율':>9}{'F1':>9}{'지원':>7}")
        for l, m in val["per_label"].items():
            print(f"{l:<6}{m['precision']:>9.3f}{m['recall']:>9.3f}{m['f1']:>9.3f}{m['support']:>7}")
        if val["not_in_train"]:
            print(f"⚠️ 학습 분할에 없어 예측 불가한 라벨: {val['not_in_train']}")
        # SageMaker metric_definitions 정규식용. 한 줄에 하나, 형식 바꾸지 말 것.
        print(f"val_macro_f1={val['macro_f1'] if val['macro_f1'] is not None else float('nan'):.4f}")
        print(f"val_trigger_recall={val['trigger_recall'] if val['trigger_recall'] is not None else float('nan'):.4f}")

    if args.final:
        pipeline, mlb, cv_folds = fit(raw_data, args)
        save_path = model_dir / MODEL_FILE
        joblib.dump({"model": pipeline, "mlb": mlb, "label_names": dict(LABEL_NAMES)}, save_path)
        metrics["final"] = {"cv_folds": cv_folds, "classes": list(mlb.classes_), "path": MODEL_FILE}
        print(f"✅ 최종 모델 저장: {save_path}")

    metrics["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (model_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 지표 저장: {model_dir / 'metrics.json'}")


if __name__ == "__main__":
    main()
