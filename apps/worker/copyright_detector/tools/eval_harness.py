"""
tools/eval_harness.py — copyright_detector 정확도 평가 하네스

목적:
  정답(라벨)이 있는 영상들로 precision / recall / F1 과 위험도 등급 정확도를 측정한다.
  임계값(WEAK_VISUAL_DEMOTE_CONF, FRAME_MAX_COUNT, *_confidence_threshold 등)을
  '감'이 아니라 '데이터'로 튜닝하기 위한 도구.

라벨 파일(JSON) 형식 — tools/eval_labels.sample.json 참고:
  [
    {
      "video": "downloads/xxxx.mp4",     # 로컬 경로 또는 https URL
      "expected_risk": "HIGH",           # HIGH|MEDIUM|LOW|SAFE (선택)
      "expected_holders": ["SBS", "IU"], # 감지돼야 할 권리자/출처 키워드 (선택)
      "notes": "설명"                    # 메모 (선택)
    }
  ]

사용법:
  1) 분석 실행(API 호출·느림) → 결과 캐시 저장:
       python tools/eval_harness.py labels.json --analyze
  2) 채점(캐시 사용·즉시) — config/임계값 바꾸고 반복해서 효과 확인:
       python tools/eval_harness.py labels.json
  3) 기준선 저장 / 비회귀 게이트:
       python tools/eval_harness.py labels.json --save-baseline
       python tools/eval_harness.py labels.json --gate   # recall 하락 시 exit 1

캐시 덕분에 (1)은 한 번만 하면 되고, 이후 임계값 튜닝은 (2)만 반복(무료·즉시)하면 된다.
채점은 순수 함수(evaluate_one/aggregate)라 영상 없이도 단위 검증 가능하다.
"""
import os
import sys
import json
import argparse
import hashlib
from pathlib import Path

# 파이프/리다이렉트 시 cp949 인코딩 에러 방지 (대화형 콘솔은 건드리지 않음)
for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _ROOT / "eval_cache"
BASELINE_PATH = _ROOT / "eval_baseline.json"

LEVEL_ORDER = {"SAFE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
ALARM_MIN = LEVEL_ORDER["MEDIUM"]          # MEDIUM 이상만 '알람'으로 간주
RECALL_REGRESSION_TOL = 0.02               # 게이트: recall 허용 하락폭


# ─────────────────────────────────────────────
# 순수 채점 로직 (영상 불필요 — 단위 검증 가능)
# ─────────────────────────────────────────────
def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _holder_match(text: str, expected: str) -> bool:
    """detected 텍스트 안에 expected 키워드가 (양방향 부분일치) 있으면 True."""
    d, e = _norm(text), _norm(expected)
    if not d or not e:
        return False
    return e in d or d in e


def _iter_findings(result: dict):
    """파이프라인(findings_by_type)·TUI(findings/timeline) 두 결과 형식 모두 지원."""
    fbt = result.get("findings_by_type")
    if isinstance(fbt, dict) and fbt:
        for lst in fbt.values():
            for f in (lst or []):
                yield f
        return
    for f in (result.get("findings") or result.get("timeline") or []):
        yield f


def _finding_text(f: dict) -> str:
    return " ".join(str(f.get(k, "")) for k in ("rights_holder", "title", "description"))


def evaluate_one(result: dict, label: dict) -> dict:
    """단일 영상 결과 vs 라벨 채점. alarm(MEDIUM+) 기준으로 TP/FP/FN 산출."""
    expected = label.get("expected_holders") or []

    alarms = [f for f in _iter_findings(result)
              if LEVEL_ORDER.get(f.get("risk_level", "SAFE"), 0) >= ALARM_MIN]
    alarm_texts = [_finding_text(f) for f in alarms]

    # recall: expected 중 알람에 잡힌 것
    matched = [e for e in expected if any(_holder_match(t, e) for t in alarm_texts)]
    tp = len(matched)
    fn = len(expected) - tp

    # precision: expected 어디에도 안 맞는 알람 = 오탐. 권리자 텍스트 기준 중복 제거.
    fp_keys = set()
    for t in alarm_texts:
        if not any(_holder_match(t, e) for e in expected):
            fp_keys.add(_norm(t)[:60])
    fp = len(fp_keys)

    pred_level = (result.get("summary") or {}).get("overall_risk_level", "SAFE")
    exp_level = label.get("expected_risk")
    level_ok = (exp_level is None) or (pred_level == exp_level)

    return {
        "video": label.get("video", "?"),
        "tp": tp, "fp": fp, "fn": fn,
        "matched": matched,
        "missed": [e for e in expected if e not in matched],
        "pred_level": pred_level, "exp_level": exp_level, "level_ok": level_ok,
        "n_alarms": len(alarms),
    }


def aggregate(per: list) -> dict:
    TP = sum(p["tp"] for p in per)
    FP = sum(p["fp"] for p in per)
    FN = sum(p["fn"] for p in per)
    prec = TP / (TP + FP) if (TP + FP) else 0.0
    rec = TP / (TP + FN) if (TP + FN) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    lvl_scored = [p for p in per if p["exp_level"] is not None]
    lvl_acc = (sum(1 for p in lvl_scored if p["level_ok"]) / len(lvl_scored)
               if lvl_scored else None)
    return {"precision": prec, "recall": rec, "f1": f1,
            "TP": TP, "FP": FP, "FN": FN,
            "level_acc": lvl_acc, "n": len(per)}


# ─────────────────────────────────────────────
# 캐시 + 분석 실행
# ─────────────────────────────────────────────
def _cache_path(video: str) -> Path:
    h = hashlib.md5(video.encode("utf-8")).hexdigest()[:12]
    stem = "".join(c for c in Path(video).stem if c.isalnum())[:30] or "vid"
    return CACHE_DIR / f"{stem}_{h}.json"


def _analyze_video(video: str) -> dict:
    """영상(로컬/URL) 분석 → 결과 dict. URL이면 먼저 다운로드."""
    from pipeline import analyze_video_sync
    path = video
    if video.startswith(("http://", "https://")):
        from utils.downloader import download_video
        path = download_video(video, output_dir=str(_ROOT / "temp" / "downloads"))
    return analyze_video_sync(path)


def _get_result(video: str, do_analyze: bool) -> dict:
    cp = _cache_path(video)
    if do_analyze or not cp.exists():
        if not do_analyze and not cp.exists():
            raise FileNotFoundError(
                f"캐시 없음: {video}\n  → 먼저 '--analyze' 로 분석을 실행하세요.")
        print(f"  ⏳ 분석 중: {video}")
        result = _analyze_video(video)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8")
        return result
    return json.loads(cp.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────
# 리포트
# ─────────────────────────────────────────────
def _print_report(per: list, agg: dict):
    print("\n" + "=" * 70)
    print(" 정확도 평가 결과")
    print("=" * 70)
    print(f"{'영상':28} {'등급(예측/정답)':16} {'TP':>3} {'FP':>3} {'FN':>3}  놓친항목")
    print("-" * 70)
    for p in per:
        lvl = f"{p['pred_level']}/{p['exp_level'] or '-'}"
        mark = "" if p["level_ok"] else " ✗"
        missed = ", ".join(p["missed"])[:24]
        vid = Path(p["video"]).name[:26]
        print(f"{vid:28} {lvl:16}{mark:2} {p['tp']:>3} {p['fp']:>3} {p['fn']:>3}  {missed}")
    print("-" * 70)
    la = f"{agg['level_acc']*100:.0f}%" if agg["level_acc"] is not None else "N/A"
    print(f" 영상 {agg['n']}개 | TP={agg['TP']} FP={agg['FP']} FN={agg['FN']}")
    print(f" Precision(정밀도, 오탐↓) : {agg['precision']*100:5.1f}%")
    print(f" Recall   (재현율, 미탐↓) : {agg['recall']*100:5.1f}%")
    print(f" F1                       : {agg['f1']*100:5.1f}%")
    print(f" 위험등급 정확도          : {la}")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description="copyright_detector 정확도 평가")
    ap.add_argument("labels", help="라벨 JSON 파일 경로")
    ap.add_argument("--analyze", action="store_true",
                    help="영상을 실제로 분석해 캐시 생성/갱신 (API 호출·느림)")
    ap.add_argument("--save-baseline", action="store_true", help="현재 결과를 기준선으로 저장")
    ap.add_argument("--gate", action="store_true",
                    help="기준선 대비 recall 하락 시 exit 1 (CI용)")
    args = ap.parse_args()

    labels = json.loads(Path(args.labels).read_text(encoding="utf-8-sig"))
    if not isinstance(labels, list) or not labels:
        print("❌ 라벨 파일이 비었거나 리스트가 아닙니다."); sys.exit(1)

    per = []
    for label in labels:
        video = label.get("video")
        if not video:
            print("⚠️  'video' 없는 항목 건너뜀"); continue
        try:
            result = _get_result(video, args.analyze)
        except Exception as e:
            print(f"  ⚠️  실패: {video} — {e}"); continue
        per.append(evaluate_one(result, label))

    if not per:
        print("❌ 채점할 결과가 없습니다."); sys.exit(1)

    agg = aggregate(per)
    _print_report(per, agg)

    if args.save_baseline:
        BASELINE_PATH.write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"💾 기준선 저장: {BASELINE_PATH}")

    if args.gate:
        if not BASELINE_PATH.exists():
            print("⚠️  기준선 없음 — 먼저 --save-baseline"); sys.exit(0)
        base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        drop = base.get("recall", 0) - agg["recall"]
        if drop > RECALL_REGRESSION_TOL:
            print(f"❌ 게이트 실패: recall {base['recall']*100:.1f}% → "
                  f"{agg['recall']*100:.1f}% ({drop*100:.1f}%p 하락)")
            sys.exit(1)
        print(f"✅ 게이트 통과 (recall {agg['recall']*100:.1f}%)")


if __name__ == "__main__":
    main()
