"""
tools/train_font_classifier.py - 폰트 분류 CNN 학습
사용법:
  python tools/train_font_classifier.py                 # 시스템 폰트 + fonts_train/ 학습
  python tools/train_font_classifier.py --samples 400 --epochs 10

학습 데이터:
  1. 시스템 내장 폰트 (아래 SYSTEM_FONTS — HY 상업 폰트 + 무료/번들 폰트)
  2. fonts_train/ 폴더의 .ttf/.otf — manifest.json 으로 상업 여부 지정
     (구매한 상업 폰트 파일을 넣고 재학습하면 감지 범위 확장)

fonts_train/manifest.json 형식:
  [{"file": "격동고딕.otf", "name": "격동고딕", "commercial": true,
    "foundry": "Sandoll", "risk": 0.88}]
  manifest에 없는 파일은 파일명을 클래스명으로, commercial=true 로 간주.

설계: 상업 폰트만 학습하면 모든 자막이 상업 폰트로 강제 분류되므로
무료/시스템 폰트를 거절(negative) 클래스로 반드시 포함한다.
"""
import os
import sys
import re
import json
import time
import random
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 렌더 실패 등 debug/info 로그 억제 (진행 상황은 print로만) → 출력 스팸 방지
import logging as _logging
import structlog as _structlog
_structlog.configure(
    wrapper_class=_structlog.make_filtering_bound_logger(_logging.WARNING)
)

import numpy as np

from utils.font_classifier import (
    render_text_sample, build_model, MODEL_DIR, MODEL_PATH, META_PATH,
    INPUT_H, INPUT_W,
)

WIN_FONTS = r"C:\Windows\Fonts"
USER_FONTS = os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts")

# ─────────────────────────────────────────────
# 시스템 폰트 매니페스트
# (file, index) 목록을 같은 클래스로 묶음 — 여러 굵기 = 한 폰트
# commercial: 상업 폰트 여부 (True만 finding 대상)
# 굴림/바탕 등 Windows 번들은 사실상 전 사용자 라이선스 보유 → negative
# ─────────────────────────────────────────────
SYSTEM_FONTS = [
    # ── HY 상업 폰트 (한양정보통신 — Office/HWP 번들이지만 상업 파운드리) ──
    {"name": "HY헤드라인M",  "files": [("H2HDRM.TTF", 0)], "commercial": True,  "foundry": "HY", "risk": 0.82},
    {"name": "HY중고딕",     "files": [("H2GTRM.TTF", 0), ("H2GTRE.TTF", 0)], "commercial": True, "foundry": "HY", "risk": 0.82},
    {"name": "HY신명조",     "files": [("H2MJRE.TTF", 0), ("H2MJSM.TTF", 0)], "commercial": True, "foundry": "HY", "risk": 0.82},
    {"name": "HY그래픽",     "files": [("H2GPRM.TTF", 0)], "commercial": True,  "foundry": "HY", "risk": 0.77},
    {"name": "HY엽서",       "files": [("H2PORL.TTF", 0), ("H2PORM.TTF", 0)], "commercial": True, "foundry": "HY", "risk": 0.77},
    {"name": "HY샘물M",      "files": [("H2SA1M.TTF", 0)], "commercial": True,  "foundry": "HY", "risk": 0.72},
    {"name": "HY궁서B",      "files": [("H2GSRB.TTF", 0)], "commercial": True,  "foundry": "HY", "risk": 0.72},
    {"name": "HY목각파임B",  "files": [("H2MKPB.TTF", 0)], "commercial": True,  "foundry": "HY", "risk": 0.82},

    # ── Windows 번들 (negative — 전 사용자 사실상 보유) ──
    {"name": "굴림",     "files": [("gulim.ttc", 0), ("gulim.ttc", 1)],  "commercial": False, "foundry": "HY/Windows"},
    {"name": "돋움",     "files": [("gulim.ttc", 2), ("gulim.ttc", 3)],  "commercial": False, "foundry": "HY/Windows"},
    {"name": "바탕",     "files": [("batang.ttc", 0), ("batang.ttc", 1)], "commercial": False, "foundry": "HY/Windows"},
    {"name": "궁서",     "files": [("batang.ttc", 2), ("batang.ttc", 3)], "commercial": False, "foundry": "HY/Windows"},
    {"name": "새굴림",   "files": [("NGULIM.TTF", 0)], "commercial": False, "foundry": "HY/Windows"},
    {"name": "맑은고딕", "files": [("malgun.ttf", 0), ("malgunbd.ttf", 0), ("malgunsl.ttf", 0)],
     "commercial": False, "foundry": "Microsoft"},

    # ── 무료 폰트 (negative) ──
    {"name": "본고딕(Noto Sans KR)",  "files": [("NotoSansKR-VF.ttf", 0)],  "commercial": False, "foundry": "Google"},
    {"name": "본명조(Noto Serif KR)", "files": [("NotoSerifKR-VF.ttf", 0)], "commercial": False, "foundry": "Google"},
    {"name": "배민주아체",  "files": [("BMJUA_otf.otf", 0)],     "commercial": False, "foundry": "Woowa Bros"},
    {"name": "배민한나체",  "files": [("BMHANNAProOTF.otf", 0)], "commercial": False, "foundry": "Woowa Bros"},
    {"name": "KIMM체",     "files": [("KIMM_bold.ttf", 0), ("KIMM_Light.ttf", 0)], "commercial": False, "foundry": "한국기계연구원"},
    {"name": "에이투지체", "files": [("에이투지체-4Regular.ttf", 0), ("에이투지체-7Bold.ttf", 0), ("에이투지체-9Black.ttf", 0)],
     "commercial": False, "foundry": "에이투지"},
]


def _resolve(fname: str) -> str:
    for d in (WIN_FONTS, USER_FONTS):
        p = os.path.join(d, fname)
        if os.path.exists(p):
            return p
    return ""


# 굵기/스타일 토큰 (패밀리 그룹핑 시 제거) — 굵기 변형은 한 폰트로 묶는다
_WEIGHT_TOKENS = re.compile(
    r"(?i)(thin|extralight|ultralight|hairline|light|regular|normal|medium|"
    r"semibold|demibold|extrabold|ultrabold|bold|black|heavy|air|book|"
    r"_ttf|_otf|-ttf|-otf|ttf|otf|vf|variable|ver[0-9.]+|v[0-9.]+|"
    r"\d{6,}|ligature|otf\b)"
)


def _family_of(fname: str) -> str:
    """파일명 → 폰트 패밀리명 (굵기 변형 제거, 유료 접두사 제거)."""
    name = os.path.splitext(fname)[0]
    name = re.sub(r"^\s*유료\s*", "", name)          # 유료 접두사 제거
    name = re.sub(r"[-_ ]+", " ", name)
    name = _WEIGHT_TOKENS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" -_")
    return name or os.path.splitext(fname)[0]


def collect_classes(fonts_dir: str, focus: bool = True) -> list:
    """
    학습 클래스 목록 구성.

    fonts_train/ 에 폰트가 있으면 그것만 사용 (모델이 아는 세계 = 사용자 폰트).
      - 굵기 변형(Bold/Light/...)은 한 폰트 패밀리로 묶음
      - 파일명이 "유료"로 시작 → commercial=True (저작권 대상)
      - 나머지 → commercial=False (무료, 거절/식별용)
      - manifest.json 이 있으면 파일별 override 가능

    focus=True (기본): 무료 폰트를 개별 클래스 대신 하나의 '기타(무료/미학습)'
      클래스로 묶는다. 유료 폰트는 소수(2/237)라 개별 클래스로 두고 나머지를
      거대 배경(reject) 클래스로 통합 → 유료 탐지 신뢰도·분리도 대폭 향상.
      237-way 분류는 자막 크기에서 신뢰도가 얇게 퍼져 실패했음(실측 검증).

    fonts_train/ 이 비어 있으면 시스템 내장 폰트로 폴백.
    """
    from collections import OrderedDict

    manifest = {}
    if os.path.isdir(fonts_dir):
        mpath = os.path.join(fonts_dir, "manifest.json")
        if os.path.exists(mpath):
            try:
                with open(mpath, encoding="utf-8") as f:
                    manifest = {m["file"]: m for m in json.load(f)}
            except Exception:
                manifest = {}

    font_files = []
    if os.path.isdir(fonts_dir):
        font_files = [f for f in sorted(os.listdir(fonts_dir))
                      if f.lower().endswith((".ttf", ".otf", ".ttc"))]

    # ── fonts_train 폴더 우선 ──
    if font_files:
        fams: "OrderedDict[str, dict]" = OrderedDict()
        for fname in font_files:
            m = manifest.get(fname, {})
            fam = m.get("name") or _family_of(fname)
            is_paid = fname.strip().startswith("유료")
            commercial = bool(m.get("commercial", is_paid))
            path = os.path.join(fonts_dir, fname)
            idx = int(m.get("index", 0))
            if fam not in fams:
                fams[fam] = {
                    "name": fam,
                    "files": [],
                    "commercial": commercial,
                    "foundry": m.get("foundry", ""),
                    "risk": float(m.get("risk", 0.85 if commercial else 0.05)),
                }
            fams[fam]["files"].append((path, idx))
            # 패밀리 내 한 파일이라도 유료면 유료로 승격
            if commercial:
                fams[fam]["commercial"] = True
                fams[fam]["risk"] = max(fams[fam]["risk"], float(m.get("risk", 0.85)))
        classes = list(fams.values())
        n_paid = sum(1 for c in classes if c["commercial"])
        print(f"  fonts_train: {len(font_files)}파일 → {len(classes)}패밀리 "
              f"(유료 {n_paid} / 무료 {len(classes) - n_paid})")

        if focus and n_paid >= 1:
            paid_classes = [c for c in classes if c["commercial"]]
            free_files = [f for c in classes if not c["commercial"] for f in c["files"]]
            if free_files:
                other = {
                    "name": "기타 (무료/미학습)",
                    "files": free_files,
                    "commercial": False,
                    "foundry": "",
                    "risk": 0.0,
                    # 거대 배경 클래스 → 더 많은 샘플로 다양성 커버
                    "sample_mult": min(6, max(2, len(free_files) // 40)),
                }
                classes = paid_classes + [other]
                print(f"  [집중 모드] 유료 {len(paid_classes)}클래스 + "
                      f"'기타' 1클래스(무료 {len(free_files)}파일 통합, "
                      f"샘플 {other['sample_mult']}배) = {len(classes)}클래스")
        return classes

    # ── 폴백: 시스템 내장 폰트 ──
    print("  fonts_train 비어 있음 → 시스템 내장 폰트 사용")
    classes = []
    for entry in SYSTEM_FONTS:
        files = [(_resolve(f), i) for f, i in entry["files"]]
        files = [(p, i) for p, i in files if p]
        if files:
            classes.append({**entry, "files": files})
    return classes


def _renderable_files(files, rng):
    """
    렌더 가능한 (path, idx)만 남긴다.
    일부 폰트는 FreeType 'too many function definitions' 등으로 렌더 불가 →
    학습 전에 1회 프로브해서 제외 (재시도 낭비·데드 클래스 방지).
    """
    ok = []
    for path, idx in files:
        arr = render_text_sample(path, idx, text="가나다ABC", rng=rng, augment=False)
        if arr is not None:
            ok.append((path, idx))
    return ok


def prevalidate(classes: list, seed: int = 3) -> list:
    """렌더 불가 파일/패밀리를 사전 제거 → 유효 클래스만 반환."""
    rng = random.Random(seed)
    valid, dropped = [], []
    for cls in classes:
        good = _renderable_files(cls["files"], rng)
        if good:
            if len(good) < len(cls["files"]):
                dropped.append(f"{cls['name']}({len(cls['files'])-len(good)}파일 렌더불가)")
            valid.append({**cls, "files": good})
        else:
            dropped.append(f"{cls['name']}(전체 렌더불가→클래스 제외)")
    if dropped:
        print(f"  렌더 불가 정리: {len(dropped)}건")
        for d in dropped[:20]:
            print(f"    - {d}")
        if len(dropped) > 20:
            print(f"    ... 외 {len(dropped)-20}건")
    return valid


def build_dataset(classes: list, n_per_class: int, seed: int = 7):
    rng = random.Random(seed)
    X, y = [], []
    for ci, cls in enumerate(classes):
        target = int(n_per_class * cls.get("sample_mult", 1))
        made = 0
        attempts = 0
        consecutive_fail = 0
        # 사전 검증을 통과한 파일들이므로 재시도 상한을 낮게 (여전히 증강 변형 중 일부 실패 가능)
        while made < target and attempts < target * 2:
            attempts += 1
            path, idx = rng.choice(cls["files"])
            arr = render_text_sample(path, idx, rng=rng, augment=True)
            if arr is None:
                consecutive_fail += 1
                if consecutive_fail >= 25:   # 이 클래스는 사실상 렌더 불가 → 조기 중단
                    break
                continue
            consecutive_fail = 0
            X.append(arr)
            y.append(ci)
            made += 1
        print(f"  [{ci:3d}/{len(classes)}] {cls['name']:<22} {made}샘플"
              + (" (유료)" if cls["commercial"] else ""))
    return np.stack(X), np.array(y, dtype=np.int64)


def train(args):
    import torch
    import torch.nn as nn

    print("── 클래스 수집 ──")
    classes = collect_classes(args.fonts_dir, focus=not args.all_classes)
    print(f"  수집 {len(classes)}클래스 → 렌더 가능 검증 중...")
    classes = prevalidate(classes)
    n_comm = sum(1 for c in classes if c["commercial"])
    print(f"총 {len(classes)}개 유효 클래스 (유료 {n_comm} / 무료 {len(classes) - n_comm})\n")
    if len(classes) < 2:
        print("⚠ 유효 클래스가 2개 미만 — 학습 불가")
        return

    print(f"── 합성 샘플 렌더링 ({args.samples}/클래스) ──")
    t0 = time.time()
    X, y = build_dataset(classes, args.samples)
    print(f"렌더링 완료: {len(X)}샘플, {time.time()-t0:.0f}초\n")

    # train/val 분할
    rng = np.random.RandomState(0)
    idx = rng.permutation(len(X))
    n_val = max(int(len(X) * 0.12), len(classes))
    vi, ti = idx[:n_val], idx[n_val:]
    Xt, yt, Xv, yv = X[ti], y[ti], X[vi], y[vi]

    model = build_model(len(classes))
    opt = torch.optim.Adam(model.parameters(), lr=1.5e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)

    # ── 클래스 가중 손실 (불균형 보정) ──
    # 기타 클래스가 샘플이 훨씬 많아(다양성 확보용) 손실을 지배 → 모델이 전부
    # '기타'로 찍는 붕괴 방지. 가중치 = 역빈도 정규화 → 각 클래스 손실 기여 균등.
    counts = np.bincount(yt, minlength=len(classes)).astype(np.float32)
    counts = np.clip(counts, 1, None)
    cls_w = (counts.sum() / (len(classes) * counts))
    weight_t = torch.tensor(cls_w, dtype=torch.float32)
    lossf = nn.CrossEntropyLoss(weight=weight_t)
    print(f"  클래스 가중치: "
          + ", ".join(f"{c['name'][:12]}={w:.2f}" for c, w in zip(classes, cls_w)))

    def to_tensor(a):
        t = torch.from_numpy(a)[:, None, :, :]
        return (t - 0.5) / 0.5

    print(f"── 학습 ({args.epochs} epochs, train {len(Xt)} / val {len(Xv)}) ──")
    best_score = -1.0
    best_acc = 0.0
    for ep in range(args.epochs):
        model.train()
        perm = np.random.permutation(len(Xt))
        tot_loss = 0.0
        for s in range(0, len(perm), args.batch):
            b = perm[s:s + args.batch]
            xb, yb = to_tensor(Xt[b]), torch.from_numpy(yt[b])
            opt.zero_grad()
            loss = lossf(model(xb), yb)
            loss.backward()
            opt.step()
            tot_loss += loss.item() * len(b)
        sched.step()

        # 검증 — 전체 정확도 + 클래스별(macro) 정확도 (불균형이라 macro가 진짜 지표)
        model.eval()
        preds = np.empty(len(Xv), dtype=np.int64)
        with torch.no_grad():
            for s in range(0, len(Xv), 256):
                preds[s:s + 256] = model(to_tensor(Xv[s:s + 256])).argmax(dim=-1).numpy()
        acc = float((preds == yv).mean())
        per_cls = []
        for c in range(len(classes)):
            m = yv == c
            per_cls.append(float((preds[m] == c).mean()) if m.any() else 0.0)
        macro = float(np.mean(per_cls))
        # 유료 클래스 평균 정확도 (핵심 지표)
        paid_idx = [i for i, cl in enumerate(classes) if cl["commercial"]]
        paid_acc = float(np.mean([per_cls[i] for i in paid_idx])) if paid_idx else 0.0
        print(f"  epoch {ep+1:2d}/{args.epochs}  loss={tot_loss/len(Xt):.4f}  "
              f"acc={acc:.3f}  macro={macro:.3f}  유료acc={paid_acc:.3f}")

        # 유료 정확도 우선 저장 (전체 acc는 기타 편향이라 신뢰 못함)
        score = paid_acc + 0.1 * macro
        if score > best_score:
            best_score = score
            best_acc = acc
            os.makedirs(MODEL_DIR, exist_ok=True)
            torch.save(model.state_dict(), MODEL_PATH)

    # 메타 저장
    meta = {
        "input": {"h": INPUT_H, "w": INPUT_W},
        "val_accuracy": round(best_acc, 4),
        "classes": [
            {"name": c["name"], "commercial": c["commercial"],
             "foundry": c.get("foundry", ""), "risk": c.get("risk", 0.5)}
            for c in classes
        ],
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 저장: {MODEL_PATH} (best val_acc={best_acc:.3f})")
    print(f"   메타: {META_PATH}")

    # 상업 폰트별 검증 정확도 리포트
    import collections
    model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
    model.eval()
    per = collections.defaultdict(lambda: [0, 0])
    with torch.no_grad():
        for s in range(0, len(Xv), 256):
            pred = model(to_tensor(Xv[s:s + 256])).argmax(dim=-1).numpy()
            for p_, t_ in zip(pred, yv[s:s + 256]):
                per[int(t_)][1] += 1
                if p_ == t_:
                    per[int(t_)][0] += 1
    print("\n── 클래스별 검증 정확도 ──")
    for ci, (c_, n_) in sorted(per.items()):
        tag = "상업" if classes[ci]["commercial"] else "무료"
        print(f"  [{tag}] {classes[ci]['name']:<22} {c_}/{n_} ({c_/max(n_,1):.0%})")

    # ── 실제로 중요한 지표: 상업 vs 무료 이진 정확도 + finding 게이트 적용 시 ──
    # (사용자 목적 = "상업 폰트인가 아닌가" + conf≥0.80·margin≥0.25 게이트)
    comm_flag = np.array([1 if c["commercial"] else 0 for c in classes])
    bin_correct = bin_total = 0
    gated_fp = gated_tp = gated_total_comm = 0
    with torch.no_grad():
        for s in range(0, len(Xv), 256):
            logits = model(to_tensor(Xv[s:s + 256]))
            probs = torch.softmax(logits, dim=-1).numpy()
            for row, t_ in zip(probs, yv[s:s + 256]):
                order = np.argsort(row)[::-1]
                p1 = int(order[0])
                conf = float(row[p1]); margin = conf - float(row[int(order[1])])
                # 이진: 예측 클래스의 상업여부 == 정답 클래스의 상업여부
                bin_total += 1
                if comm_flag[p1] == comm_flag[int(t_)]:
                    bin_correct += 1
                # 게이트 통과 시 상업 판정의 정/오탐 (3클래스용 완화 게이트)
                _GC, _GM = 0.50, 0.20
                fires = comm_flag[p1] == 1 and conf >= _GC and margin >= _GM
                if comm_flag[int(t_)] == 1:
                    gated_total_comm += 1
                    if fires:
                        gated_tp += 1
                elif fires:
                    gated_fp += 1   # 무료인데 상업으로 발화 = 오탐
    print("\n── 상업/무료 이진 정확도 (실사용 핵심 지표) ──")
    print(f"  이진 정확도: {bin_correct}/{bin_total} ({bin_correct/max(bin_total,1):.1%})")
    print(f"  게이트(conf≥{_GC},margin≥{_GM}) 적용 시:")
    print(f"    유료 폰트 탐지율(recall): {gated_tp}/{gated_total_comm} "
          f"({gated_tp/max(gated_total_comm,1):.1%})")
    print(f"    무료 폰트 오탐(false positive): {gated_fp}건 "
          f"(낮을수록 좋음 — 0이 이상적)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=300, help="클래스당 샘플 수")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--fonts-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts_train"))
    ap.add_argument("--all-classes", action="store_true",
                    help="집중 모드 끄기 — 무료 폰트를 개별 클래스로 (비추천, 237-way 실패했음)")
    train(ap.parse_args())
