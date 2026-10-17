# -*- coding: utf-8 -*-
"""
trend_signals.py — 뉴스 트렌드 가중치 (준비 단계, 2026-10-17)

계획 문서: LAVCCA_뉴스트렌드_가중치_계획_2026-10-17 (팀 공유본)
  데이터 분석 팀원이 Perplexity 로 모은 기사(raw)를 받아
  → 트렌드 열기 표(P2) → 영상 평가 때 NATAM A/B 축·문장 가중치 제안(P3, shadow)

지금 들어 있는 것
  · TREND_MODE 스위치 (off 기본 | shadow). off 면 report 를 전혀 건드리지 않는다.
  · raw 데이터 계약 검사 (validate_row / load_raw) — 팀원 산출물 확인용
      python trend_signals.py --check data/trends/sample_raw.jsonl
      python trend_signals.py --check          (data/trends/raw/*.jsonl 전체)
  · enrich(report) 연결 자리 — shadow 여도 열기 표가 아직 없어 "not_ready" 만 기록

아직 없는 것 (계획 P2~P5): 열기 표 생성, 임베딩 매칭, 가중치 규칙, on 모드.

raw 계약 (한 줄 = 기사 1건, JSONL, 하루 1파일 trends/raw/YYYY-MM-DD.jsonl)
  필수: url, title, published_at(ISO 8601·시간대 포함, 모르면 null), collected_at
  선택: snippet, source, query, axis_hint, creator, topic …
  같은 파일 안 URL 중복 금지, 기사 본문 저장 금지.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import paths

REQUIRED = ("url", "title", "published_at", "collected_at")
MODES = ("off", "shadow")


def mode() -> str:
    """TREND_MODE 를 호출 때마다 읽는다(테스트·워커에서 환경변수로 바꿀 수 있게)."""
    m = os.getenv("TREND_MODE", "off").strip().lower()
    if m not in MODES:
        print(f"⚠️  TREND_MODE={m!r} 은 아직 지원 안 함 → off")
        return "off"
    return m


def _iso_with_tz(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def validate_row(row) -> list[str]:
    """raw 한 줄의 계약 위반 목록. 빈 리스트면 통과."""
    if not isinstance(row, dict):
        return ["JSON 객체가 아님"]
    errs = [f"필수 필드 없음: {k}" for k in REQUIRED if k not in row]
    url = row.get("url")
    if "url" in row and not (isinstance(url, str) and url.startswith(("http://", "https://"))):
        errs.append("url 이 http(s) 주소가 아님")
    if "title" in row and not (isinstance(row["title"], str) and row["title"].strip()):
        errs.append("title 이 비어 있음")
    if "published_at" in row and row["published_at"] is not None and not _iso_with_tz(row["published_at"]):
        errs.append("published_at 이 시간대 포함 ISO 8601 이 아님(모르면 null)")
    if "collected_at" in row and not _iso_with_tz(row["collected_at"]):
        errs.append("collected_at 이 시간대 포함 ISO 8601 이 아님")
    return errs


def load_raw(files) -> tuple[list[dict], list[str]]:
    """JSONL 파일들을 읽어 (통과한 행, 오류 메시지) 를 돌려준다. 파일 안 URL 중복도 오류."""
    rows, errors = [], []
    for f in map(Path, files):
        seen = set()
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            where = f"{f.name}:{n}"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"{where} JSON 파싱 실패: {e.msg}")
                continue
            errs = validate_row(row)
            if not errs and row["url"] in seen:
                errs = ["같은 파일 안 URL 중복"]
            if errs:
                errors.extend(f"{where} {e}" for e in errs)
                continue
            seen.add(row["url"])
            rows.append(row)
    return rows, errors


def enrich(report: dict) -> dict | None:
    """리포트 저장 직전에 호출. off 면 아무것도 안 하고 None.
    shadow 는 등급을 바꾸지 않고 report['trend_shadow'] 에 기록만 한다(JSON 전용)."""
    if mode() == "off":
        return None
    res = {"status": "not_ready", "mode": "shadow",
           "note": "트렌드 열기 표 미구현(계획 P2) — 기록만, 등급 변경 없음"}
    report["trend_shadow"] = res
    return res


def _main(argv: list[str]) -> int:
    if not argv or argv[0] != "--check":
        print("사용법: python trend_signals.py --check [raw.jsonl ...]")
        return 2
    files = argv[1:] or sorted(paths.TREND_RAW_DIR.glob("*.jsonl"))
    if not files:
        print(f"검사할 파일 없음: {paths.TREND_RAW_DIR}")
        return 2
    rows, errors = load_raw(files)
    for e in errors:
        print(f"❌ {e}")
    print(f"통과 {len(rows)}건 · 오류 {len(errors)}건")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
