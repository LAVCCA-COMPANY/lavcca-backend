# -*- coding: utf-8 -*-
"""
build_golden_candidates.py  —  골든셋 후보 추출 (라벨은 사람이 확정)

reports/report_*.json 의 transcript_analysis(교정 자막)에서 문장을 모아
학습 데이터(controversy_samples.json)와 겹치는 문장을 제외하고,
검수용 후보 파일 golden_set_candidates.json 을 만든다.

  - 모델이 L12 외 라벨을 의심한 문장(prob ≥ 0.15) 또는 Tier2 가 논란으로 판정한 문장은 전부 포함
  - 나머지(정상으로 보이는 문장)는 같은 수만큼 무작위 샘플(seed 고정) → 오탐 측정용
  - 각 후보에 model_suggested / tier2 판정을 '참고용'으로 붙이고, labels 는 비워 둔다

검수 방법:
  1) golden_set_candidates.json 을 열어 각 항목의 "labels" 를 채운다 (예: ["L03"], 해당 없음은 ["L12"])
     특히 고심각도 L03/L06/L09/L11 은 꼼꼼히. 애매하면 labels 를 비워 두면 평가에서 제외된다.
  2) data/golden_set.json 으로 저장(이름 변경) → python tools/eval_harness.py data/golden_set.json --save-baseline

사용:
  python tools/build_golden_candidates.py            # 기본 출력 data/golden_set_candidates.json
  python tools/build_golden_candidates.py --max 300  # 후보 상한
"""

import argparse
import glob
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # apps/worker/ (paths·엔진 모듈)
import paths

SAMPLES_PATH = paths.SAMPLES_JSON
OUT_PATH     = paths.GOLDEN_CANDIDATES

SUSPECT_PROB = 0.15   # Tier1 트리거 임계값과 동일
MIN_CHARS    = 6      # 너무 짧은 감탄사·추임새 제외
SEED         = 42


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def _video_id(path: str) -> str:
    m = re.search(r"_([\w-]{11})\.json$", path)
    return m.group(1) if m else Path(path).stem


def collect(report_glob: str):
    with open(SAMPLES_PATH, "r", encoding="utf-8-sig") as f:
        train_texts = {_norm(s.get("text", "")) for s in json.load(f)}

    seen, rows = set(), []
    for path in sorted(glob.glob(report_glob)):
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
        tier2 = {}
        for a in (rep.get("two_tier") or {}).get("adjudicated") or []:
            if "Tier2 실패" in (a.get("rationale") or ""):
                continue   # 429 등으로 Tier2 가 실제 판정하지 못한 항목은 참고 정보에서 제외
            tier2[a.get("segment_idx")] = {
                "is_controversy": a.get("is_controversy"),
                "category":       a.get("category"),
                "severity":       a.get("severity"),
            }
        vid = _video_id(path)
        for i, seg in enumerate(rep.get("transcript_analysis") or []):
            text = (seg.get("corrected_text") or "").strip()
            key = _norm(text)
            if len(key) < MIN_CHARS or key in seen or key in train_texts:
                continue
            seen.add(key)
            probs = {l["id"]: round(l["prob"], 3) for l in seg.get("labels") or [] if "id" in l}
            t2 = tier2.get(seg.get("segment_id", i))
            rows.append({
                "text":            text,
                "labels":          [],          # ← 사람이 채움
                "model_suggested": probs,
                "tier2":           t2,
                "source":          {"video_id": vid, "segment_id": seg.get("segment_id", i),
                                    "timestamp": seg.get("timestamp")},
            })
    return rows


def _is_suspect(r) -> bool:
    if any(l != "L12" and p >= SUSPECT_PROB for l, p in r["model_suggested"].items()):
        return True
    return bool(r["tier2"] and r["tier2"].get("is_controversy"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", default=str(paths.REPORTS_DIR / "report_*.json"))
    ap.add_argument("--max", type=int, default=250)
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    rows = collect(args.reports)
    suspect = [r for r in rows if _is_suspect(r)]
    normal  = [r for r in rows if not _is_suspect(r)]
    rnd = random.Random(SEED)
    rnd.shuffle(suspect)
    rnd.shuffle(normal)

    n_sus = min(len(suspect), args.max * 2 // 3)
    n_nor = min(len(normal), args.max - n_sus)
    picked = suspect[:n_sus] + normal[:n_nor]
    picked.sort(key=lambda r: (r["source"]["video_id"], r["source"]["segment_id"] or 0))

    Path(args.out).write_text(json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8")

    by_label = {}
    for r in picked:
        for l, p in r["model_suggested"].items():
            if l != "L12" and p >= SUSPECT_PROB:
                by_label[l] = by_label.get(l, 0) + 1
    print(f"📄 전체 후보 문장 {len(rows)}개 (학습 데이터 중복·짧은 문장 제외)")
    print(f"   의심 {len(suspect)}개 중 {n_sus}개 + 정상 {len(normal)}개 중 {n_nor}개 → {len(picked)}개")
    print(f"   모델 의심 라벨 분포: {dict(sorted(by_label.items()))}")
    print(f"💾 저장 → {args.out}  (labels 를 채운 뒤 data/golden_set.json 으로 저장)")


if __name__ == "__main__":
    main()
