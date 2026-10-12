# -*- coding: utf-8 -*-
"""
casedb_sync.py — NocoDB 사례집(공개 공유 뷰) → 로컬 스냅샷 동기화

101,825행 규모의 리스크 실사례/기사 데이터를 NocoDB 공개 API에서 페이지 단위로
내려받아 casedb/casedb_news.jsonl 로 저장한다. (유사 사례 엔진의 원천 데이터)

특징
  - 페이지네이션(1,000행/페이지) + 페이지별 재시도
  - 재개(resume) 지원: 중간에 끊겨도 sync_state.json 오프셋부터 이어받음
  - stdlib(urllib)만 사용 → 추가 의존성 없음

사용법
  python tools/casedb_sync.py            # 동기화(있으면 이어받기)
  python tools/casedb_sync.py --fresh    # 처음부터 새로
  python tools/casedb_sync.py --profile  # 내려받은 데이터 품질 프로파일만 출력
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

# Windows 콘솔(cp949)에서 이모지/한글 출력 시 UnicodeEncodeError 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 대상 공개 뷰 ────────────────────────────────────────────────
# https://app.nocodb.com/nc/view/5b2dbe3f-2568-401e-83f9-ce50a5f5527d
VIEW_ID  = "5b2dbe3f-2568-401e-83f9-ce50a5f5527d"
BASE_URL = f"https://app.nocodb.com/api/v2/public/shared-view/{VIEW_ID}/rows"

PAGE_SIZE = 1000

# 파일 경로(mvp 1_9_3/casedb 고정 → 실행 CWD와 무관. similar_case_engine 과 같은 폴더)
APP_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = os.path.join(APP_DIR, "casedb")
JSONL     = os.path.join(DATA_DIR, "casedb_news.jsonl")
STATE     = os.path.join(DATA_DIR, "sync_state.json")


# ════════════════════════════════════════════════════════════════
# HTTP
# ════════════════════════════════════════════════════════════════
def _fetch_page(offset: int, limit: int = PAGE_SIZE, retries: int = 4) -> dict:
    qs  = urllib.parse.urlencode({"limit": limit, "offset": offset})
    url = f"{BASE_URL}?{qs}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"accept": "application/json",
                         "user-agent": "natam-casedb-sync/1.0"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            print(f"   ⚠️  offset={offset} 실패({attempt + 1}/{retries}): {e} → {wait}s 후 재시도")
            time.sleep(wait)
    raise RuntimeError(f"offset={offset} 페이지를 가져오지 못했습니다: {last_err}")


# ════════════════════════════════════════════════════════════════
# 상태 저장/복원
# ════════════════════════════════════════════════════════════════
def _load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0, "written": 0, "total": None}


def _save_state(st: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════
# 동기화
# ════════════════════════════════════════════════════════════════
def sync(fresh: bool = False) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

    if fresh and os.path.exists(JSONL):
        os.remove(JSONL)
    if fresh and os.path.exists(STATE):
        os.remove(STATE)

    st = _load_state()
    # 재개 시 이미 기록된 줄 수로 오프셋 보정(파일과 상태 불일치 방지)
    existing = 0
    if os.path.exists(JSONL):
        with open(JSONL, "r", encoding="utf-8") as f:
            existing = sum(1 for _ in f)
    offset = min(st.get("offset", 0), existing)  # 파일 기준으로 안전하게
    if offset != existing:
        # 상태-파일 불일치 → 파일 줄 수를 신뢰
        offset = existing
    st["offset"] = offset
    st["written"] = existing

    print(f"⏳ NocoDB 사례집 동기화 시작 (offset={offset}, 기존 {existing}행)")
    t0 = time.time()

    mode = "a" if offset > 0 else "w"
    with open(JSONL, mode, encoding="utf-8") as out:
        while True:
            data = _fetch_page(offset)
            rows = data.get("list", [])
            page_info = data.get("pageInfo", {})
            total = page_info.get("totalRows")
            st["total"] = total

            if not rows:
                break

            for r in rows:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()

            offset += len(rows)
            st["offset"]  = offset
            st["written"] = offset
            _save_state(st)

            pct = f"{offset}/{total}" if total else str(offset)
            print(f"   📥 {pct}행  ({time.time() - t0:5.1f}s)")

            if page_info.get("isLastPage") or (total and offset >= total):
                break

    print(f"✅ 동기화 완료: {st['written']}행 → {JSONL}  ({time.time() - t0:.1f}s)")


# ════════════════════════════════════════════════════════════════
# 품질 프로파일
# ════════════════════════════════════════════════════════════════
def _iter_rows():
    with open(JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def profile() -> None:
    if not os.path.exists(JSONL):
        print("❌ 스냅샷이 없습니다. 먼저 `python tools/casedb_sync.py` 를 실행하세요.")
        return

    total = 0
    risk_counter    = Counter()
    tag_counter     = Counter()
    press_counter   = Counter()
    degenerate      = 0        # 핵심 문장이 제목과 같거나 너무 짧음
    dup_keys        = Counter()  # (제목, 핵심문장) 중복
    field_fill      = Counter()
    all_fields      = set()
    empty_keyword   = 0

    for r in _iter_rows():
        total += 1
        all_fields.update(r.keys())
        for k, v in r.items():
            if v not in (None, "", []):
                field_fill[k] += 1

        title = (r.get("제목") or "").strip()
        core  = (r.get("핵심 문장") or "").strip()
        risk  = (r.get("리스크") or "").strip()
        tag   = (r.get("세부 태그") or "").strip()
        press = (r.get("언론사") or "").strip()
        kw    = (r.get("키워드") or "").strip()

        risk_counter[risk or "(빈값)"] += 1
        tag_counter[tag or "(빈값)"] += 1
        press_counter[press or "(빈값)"] += 1
        dup_keys[(title, core)] += 1
        if not kw:
            empty_keyword += 1
        if core == title or len(core) < 4:
            degenerate += 1

    dups = sum(c - 1 for c in dup_keys.values() if c > 1)

    print("=" * 60)
    print(f"📊 사례집 프로파일  (총 {total:,}행)")
    print("=" * 60)
    print(f"\n▪ 필드({len(all_fields)}개): {sorted(all_fields)}")
    print("\n▪ 필드 채움률:")
    for k in sorted(field_fill, key=lambda x: -field_fill[x]):
        print(f"    {field_fill[k]:>7,} ({field_fill[k] / total * 100:5.1f}%)  {k}")

    print(f"\n▪ 리스크 카테고리 ({len(risk_counter)}종):")
    for k, c in risk_counter.most_common():
        print(f"    {c:>7,}  {k}")

    print(f"\n▪ 세부 태그 상위 20 ({len(tag_counter)}종):")
    for k, c in tag_counter.most_common(20):
        print(f"    {c:>7,}  {k}")

    print("\n▪ 품질 신호:")
    print(f"    degenerate(핵심문장=제목 또는 <4자): {degenerate:,} ({degenerate / total * 100:.1f}%)")
    print(f"    (제목,핵심문장) 중복 초과분:         {dups:,}")
    print(f"    키워드 빈값:                          {empty_keyword:,} ({empty_keyword / total * 100:.1f}%)")

    print("\n▪ 샘플 3행:")
    for i, r in enumerate(_iter_rows()):
        if i >= 3:
            break
        print(f"    [{i}] 제목={r.get('제목')!r} | 리스크={r.get('리스크')!r} | 키워드={r.get('키워드')!r}")


# ════════════════════════════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser(description="NocoDB 사례집 → 로컬 스냅샷 동기화")
    ap.add_argument("--fresh",   action="store_true", help="처음부터 새로 내려받기")
    ap.add_argument("--profile", action="store_true", help="동기화 없이 품질 프로파일만 출력")
    args = ap.parse_args()

    if args.profile:
        profile()
        return

    sync(fresh=args.fresh)
    print()
    profile()


if __name__ == "__main__":
    main()
