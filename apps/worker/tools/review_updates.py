# -*- coding: utf-8 -*-
"""
review_updates.py  —  pending_updates.json 검토/반영 CLI

trend_updater.py 가 쌓아둔 '검토 대기' 항목을 사람이 하나씩 확인하고,
승인한 것만 실제 rules.yaml / controversy_samples.json 에 머지한다.
(case_db.json 사례 머지는 비활성 — CASES_ENABLED 참고)

조작:
    y  = 승인(반영)   |   n = 거절(폐기)   |   s = 보류(다음에 다시)   |   q = 종료
"""

import json
import sys
from pathlib import Path
from datetime import datetime

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # apps/worker/ (paths·엔진 모듈)
import paths

RULES_PATH   = paths.RULES_YAML
CASE_DB_PATH = paths.CASE_DB_JSON
# case_db.json 은 1_9_3 에서 폐기됨(_deprecated/) — 유사 사례는 similar_case_engine + casedb/ 가 담당.
# 예전 pending 에 남아 있는 사례는 건드리지 않고 건너뛴다(검토 완료 판정에서도 제외).
CASES_ENABLED = False
SAMPLES_PATH = paths.SAMPLES_JSON      # ML 학습 문장
PENDING_PATH = paths.PENDING_UPDATES

SEVERITY_ACTION = {   # 키워드 반영 시 카테고리 기본 액션 매핑(신규 카테고리 대비)
    "CRITICAL": "BAN_USER",
    "HIGH":     "BLOCK_COMMENT",
    "MEDIUM":   "MARK_FLAG",
}

#  L05(기만)·L10(저작권)은 별도 Copyright Detector 담당 → 문장 분류 제외
VALID_LABELS = {f"L{i:02d}" for i in range(1, 13)} - {"L05", "L10"}


def _ask(prompt: str) -> str:
    return input(prompt).strip().lower()


def _parse_labels(raw: str):
    """'l01,l11' 또는 'L01 L11' 같은 입력을 유효 라벨 리스트로 파싱. 유효한 게 없으면 None."""
    parts = [p.strip().upper() for p in raw.replace(" ", ",").split(",") if p.strip()]
    labels = [p for p in parts if p in VALID_LABELS]
    return labels or None


# ────────── rules.yaml 머지 ──────────
def apply_keyword(item: dict):
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
    rules.setdefault("risk_policies", {})
    cat = item["category"]

    if cat in rules["risk_policies"]:
        kws = rules["risk_policies"][cat].setdefault("keywords", [])
        if item["keyword"] not in kws:
            kws.append(item["keyword"])
    else:
        rules["risk_policies"][cat] = {
            "keywords": [item["keyword"]],
            "action":   SEVERITY_ACTION.get(item.get("severity", "MEDIUM"), "MARK_FLAG"),
            "severity": item.get("severity", "MEDIUM"),
        }

    RULES_PATH.write_text(
        yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


# ────────── case_db.json 머지 ──────────
def _next_case_id(cases: list) -> str:
    nums = [int(c["id"].split("_")[-1]) for c in cases if c.get("id", "").startswith("case_")]
    return f"case_{(max(nums) + 1 if nums else 1):03d}"


def apply_case(item: dict):
    cases = json.loads(CASE_DB_PATH.read_text(encoding="utf-8"))
    cases.append({
        "id":               _next_case_id(cases),
        "title":            item["title"],
        "controversy_type": item["controversy_type"],
        "summary":          item.get("summary", ""),
        "spread_stage":     item.get("spread_stage", "early"),
        "response_pattern": item.get("response_pattern", []),
        "keyframes":        item.get("keyframes", []),
    })
    CASE_DB_PATH.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ────────── controversy_samples.json 머지 (ML 학습 문장) ──────────
def apply_sample(item: dict):
    samples = json.loads(SAMPLES_PATH.read_text(encoding="utf-8-sig"))
    new_id  = max((s.get("id", 0) for s in samples), default=0) + 1
    samples.append({
        "id":     new_id,
        "text":   item["text"],
        "labels": item["labels"],
    })
    SAMPLES_PATH.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ────────── 메인 루프 ──────────
def main():
    if not PENDING_PATH.exists():
        print("📭 pending_updates.json 이 없습니다. 먼저 tools/trend_updater.py 를 실행하세요.")
        return

    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    runs = [r for r in pending.get("runs", []) if r.get("status") == "pending"]
    if not runs:
        print("✅ 검토할 대기 항목이 없습니다.")
        return

    approved_kw = approved_case = approved_sample = rejected = 0

    for run in runs:
        print("\n" + "=" * 60)
        print(f"🗓  수집 시각: {run.get('collected_at')}")
        print("=" * 60)

        # 키워드 검토
        kept_kw = []
        for k in run.get("keywords", []):
            print(f"\n[키워드] '{k.get('keyword')}'")
            print(f"   카테고리: {k.get('category')} / 위험도: {k.get('severity')}")
            print(f"   근거: {k.get('rationale')}")
            ch = _ask("   반영? (y/n/s/q) > ")
            if ch == "q":
                _finalize(pending, runs, kept_kw, k, run); return
            if ch == "y":
                apply_keyword(k); approved_kw += 1
            elif ch == "s":
                kept_kw.append(k)
            else:
                rejected += 1
        run["keywords"] = kept_kw

        # 사례 검토
        kept_case = []
        for c in (run.get("cases", []) if CASES_ENABLED else []):
            print(f"\n[사례] {c.get('title')}")
            print(f"   유형: {c.get('controversy_type')} / 확산: {c.get('spread_stage')}")
            print(f"   요약: {c.get('summary')}")
            ch = _ask("   반영? (y/n/s/q) > ")
            if ch == "q":
                run["cases"] = kept_case + run["cases"][run["cases"].index(c):]
                _save(pending); _report(approved_kw, approved_case, approved_sample, rejected); return
            if ch == "y":
                apply_case(c); approved_case += 1
            elif ch == "s":
                kept_case.append(c)
            else:
                rejected += 1
        if CASES_ENABLED:
            run["cases"] = kept_case

        # 학습 문장 검토 (controversy_samples.json)
        #   y = 제안 라벨 그대로 반영 | 라벨 직접입력(예: L01,L11) = 고쳐서 반영
        #   n = 거절 | s = 보류 | q = 종료
        kept_sample = []
        for s in run.get("samples", []):
            print(f"\n[학습문장] {s.get('text')}")
            print(f"   제안 라벨: {', '.join(s.get('labels', []))}")
            ch = _ask("   반영? (y=그대로 / 라벨직접입력 예:L01,L11 / n / s / q) > ")
            if ch == "q":
                run["samples"] = kept_sample + run["samples"][run["samples"].index(s):]
                _save(pending); _report(approved_kw, approved_case, approved_sample, rejected); return
            if ch == "y":
                apply_sample(s); approved_sample += 1
            elif ch in ("n", "s"):
                if ch == "s":
                    kept_sample.append(s)
                else:
                    rejected += 1
            else:
                # 라벨 직접 입력 → 검증 후 고쳐서 반영
                new_labels = _parse_labels(ch)
                if new_labels:
                    apply_sample({"text": s["text"], "labels": new_labels})
                    approved_sample += 1
                    print(f"   ✏️ 라벨 수정 반영: {', '.join(new_labels)}")
                else:
                    kept_sample.append(s)   # 인식 불가 입력 → 보류로 안전 처리
                    print("   ⚠️ 라벨 인식 실패 → 보류 처리 (유효: L01~L12)")
        run["samples"] = kept_sample

        # 남은 게 없으면 처리 완료로 표시
        leftover = run["keywords"] or run["samples"] or (CASES_ENABLED and run.get("cases"))
        run["status"] = "pending" if leftover else "done"
        run["reviewed_at"] = datetime.now().isoformat(timespec="seconds")

    _save(pending)
    _report(approved_kw, approved_case, approved_sample, rejected)


def _finalize(pending, runs, kept_kw, current, run):
    """키워드 검토 중 q 종료 시 현재 항목 보존."""
    run["keywords"] = kept_kw + run["keywords"][run["keywords"].index(current):]
    _save(pending)


def _save(pending):
    PENDING_PATH.write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _report(kw, case, sample, rej):
    print("\n" + "─" * 60)
    print(f"✅ 반영: 키워드 {kw}개 · 사례 {case}개 · 학습문장 {sample}개 / 🗑 거절 {rej}개")
    print("   rules.yaml / controversy_samples.json 업데이트 완료.")
    if sample:
        print("   ⚠️ 학습문장이 추가됐습니다 → python classifier_model.py 로 재학습해야 모델에 반영됩니다.")


if __name__ == "__main__":
    main()
