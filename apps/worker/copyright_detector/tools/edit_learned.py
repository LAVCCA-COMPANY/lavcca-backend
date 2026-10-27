"""
tools/edit_learned.py — 터미널 대화형 학습 데이터 편집기

실행:
    python tools/edit_learned.py

번호만 고르면 되는 간단한 편집기.
  - 목록에서 번호 입력  → 그 항목 수정 (필드별로 새 값 입력, 그냥 Enter = 유지)
  - d + 번호 (예: d3)   → 삭제 (확인 후, 복구 불가)
  - r                   → 목록 새로고침
  - q                   → 종료

의존성 없음. db_manager 의 검증된 함수(update/delete)만 사용하므로
id·embedding 같은 핵심 필드는 건드리지 않는다 (화이트리스트 보호 그대로).
"""
import os
import sys

# 인코딩 처리:
#  - 실제 터미널(대화형)에서는 Windows 콘솔이 한글/이모지·엔터를 알아서 처리하므로
#    건드리지 않는다. (건드리면 입력이 한 칸씩 밀리거나 엔터 스킵이 깨짐)
#  - 파이프/리다이렉트로 돌릴 때만 cp949 인코딩 에러를 피하려고 UTF-8로 바꾼다.
for _stream in (sys.stdout, sys.stdin):
    try:
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

from database.db_manager import get_db_manager

DB = get_db_manager()

# 종류별 한글 이름
KIND_LABEL = {
    "emb": "🧠 콘텐츠 임베딩", "logo": "🏷️ 로고", "music": "🎵 음악",
    "font": "🔤 폰트", "meme": "🖼️ 밈", "clip": "🎬 클립",
}

# 실제 DB 컬럼명 → 화면에 보여줄 한글 이름 (수정 프롬프트용)
COL_LABEL = {
    "title": "제목", "rights_holder": "권리자", "source": "출처",
    "risk_score": "위험도(0~1)", "reference_url": "참고 URL",
    "brand_name": "브랜드명", "trademark_owner": "상표권자", "category": "분류",
    "artist": "아티스트", "font_name": "폰트명", "foundry": "제작사",
    "license_type": "라이선스", "requires_license": "라이선스 필요(y/n)",
    "source_type": "출처종류", "source_platform": "플랫폼",
}


def _parse_index(s):
    """'3', '3.', '3.0', ' 3 ' 등을 정수 3으로. 실패하면 None."""
    s = (s or "").strip().rstrip(".")
    if s == "":
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != int(f):          # 3.5 처럼 정수가 아니면 거절
        return None
    return int(f)


def _flat_list():
    """모든 학습 항목을 (전역번호, kind, id, 요약) 로 펼쳐서 반환."""
    learned = DB.list_learned_data()
    items = []
    for kind in KIND_LABEL:
        for row in learned.get(kind, []):
            items.append((kind, row["id"], row))
    return items


def _print_list(items):
    print("\n" + "=" * 64)
    print(" 학습 데이터 편집기  —  번호=수정 · d번호=삭제 · r=새로고침 · q=종료")
    print("=" * 64)
    if not items:
        print("  (학습된 항목이 없습니다)")
        return
    cur_kind = None
    for i, (kind, eid, row) in enumerate(items, 1):
        if kind != cur_kind:
            print(f"\n  {KIND_LABEL[kind]}")
            cur_kind = kind
        title = row.get("title") or "(제목 없음)"
        holder = row.get("rights_holder") or "-"
        risk = row.get("risk_score")
        risk_str = f" · 위험도 {risk}" if risk is not None else ""
        print(f"   [{i:>2}] {title}  · 권리자: {holder}{risk_str}   (id={eid})")
    print()


def _edit(kind, eid):
    """항목 하나를 필드별로 수정."""
    model, allowed = DB._LEARNED_MODELS[kind]
    # 현재 값을 실제 컬럼명으로 정확히 읽어온다
    with DB.get_session() as s:
        obj = s.get(model, eid)
        if obj is None:
            print("  ⚠️  항목을 찾을 수 없습니다.")
            return
        current = {col: getattr(obj, col) for col in allowed}

    print(f"\n  ── {KIND_LABEL[kind]}  (id={eid}) 수정 ──")
    print("  (바꾸지 않을 값은 그냥 Enter · 그만두려면 아무 칸에 q 입력 → 그대로 둠)")
    updates = {}
    for col in allowed:
        label = COL_LABEL.get(col, col)
        now = current[col]
        now_str = "" if now is None else str(now)
        new = input(f"   {label} [{now_str}]: ").strip()
        if new.lower() in ("q", "취소", "cancel"):
            print("  ↩️  수정 취소 — 그대로 둡니다.")
            return
        if new != "":
            updates[col] = new

    if not updates:
        print("  변경 없음 — 그대로 둡니다.")
        return
    ok = DB.update_learned_entry(kind, eid, updates)
    print("  ✅ 저장 완료" if ok else "  ⚠️  저장되지 않음 (허용되지 않은 값?)")


def _delete(kind, eid):
    yn = input(f"  정말 삭제할까요? {kind} id={eid} (복구 불가) [y/N]: ").strip().lower()
    if yn == "y":
        ok = DB.delete_learned_entry(kind, eid)
        print("  🗑️  삭제 완료" if ok else "  ⚠️  삭제 실패")
    else:
        print("  취소됨.")


VERSION = "v3-input-fix"


def main():
    # 실행 중인 파일이 '수정된 그 파일'인지 눈으로 확인할 수 있게 표시
    print(f"\n[버전 {VERSION}] 실행 파일: {os.path.abspath(__file__)}")
    while True:
        items = _flat_list()
        _print_list(items)
        cmd = input("  입력 > ").strip().lower()

        if cmd in ("q", "quit", "exit"):
            print("종료.")
            return
        if cmd in ("r", ""):
            continue

        delete = cmd.startswith("d")
        num_str = cmd[1:].strip() if delete else cmd
        idx = _parse_index(num_str)
        if idx is None:
            print("  ⚠️  번호를 입력하세요. (예: 3  또는 삭제는 d3)")
            continue
        if not (1 <= idx <= len(items)):
            print("  ⚠️  목록에 없는 번호입니다.")
            continue

        kind, eid, _ = items[idx - 1]
        if delete:
            _delete(kind, eid)
        else:
            _edit(kind, eid)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n종료.")
