"""
utils/progress.py - 분석 단계별 실시간 진행률 추적 + 콘솔 게이지바

여러 분석기가 스레드에서 동시에 돌 때, 각자 진행률을 보고하면
콘솔에 단계별 게이지바가 실시간 갱신된다. 병목(어느 단계가 오래 걸리는지)을
눈으로 확인할 수 있다.

사용:
    tracker = ProgressTracker()
    tracker.add("music", "🎵 음악"); tracker.add("image", "🖼️ 이미지")
    # 분석기 안에서 (스레드 안전):
    tracker.set_total("image", 320)
    tracker.advance("image")            # 1 증가
    tracker.done("image")               # 완료 표시
    # 렌더링 (asyncio):
    await tracker.render_loop()          # 모든 단계 done 될 때까지 게이지 갱신
"""
import sys
import time
import threading
from typing import Dict, Optional


class _Stage:
    __slots__ = ("key", "label", "total", "current", "status", "t_start", "t_end", "note")

    def __init__(self, key: str, label: str):
        self.key = key
        self.label = label
        self.total = 0
        self.current = 0
        self.status = "대기"     # 대기 | 진행 | 완료 | 실패
        self.t_start: Optional[float] = None
        self.t_end: Optional[float] = None
        self.note = ""

    @property
    def pct(self) -> float:
        if self.status == "완료":
            return 100.0
        if self.total <= 0:
            return 0.0
        return min(100.0, self.current / self.total * 100.0)

    @property
    def elapsed(self) -> float:
        if self.t_start is None:
            return 0.0
        return (self.t_end or time.time()) - self.t_start


class ProgressTracker:
    """스레드 안전 진행률 추적기 + 콘솔 게이지 렌더러."""

    def __init__(self, enabled: bool = True):
        self._lock = threading.Lock()
        self._stages: Dict[str, _Stage] = {}
        self._order = []
        self.enabled = enabled and sys.stdout.isatty()
        self._t0 = time.time()
        self._rendered_lines = 0

    # ── 단계 등록/업데이트 (스레드에서 호출 가능) ──
    def add(self, key: str, label: str):
        with self._lock:
            if key not in self._stages:
                self._stages[key] = _Stage(key, label)
                self._order.append(key)

    def set_total(self, key: str, total: int, note: str = ""):
        with self._lock:
            s = self._stages.get(key)
            if s:
                s.total = total
                if s.t_start is None:
                    s.t_start = time.time()
                s.status = "진행"
                if note:
                    s.note = note

    def advance(self, key: str, n: int = 1, note: str = ""):
        with self._lock:
            s = self._stages.get(key)
            if s:
                if s.t_start is None:
                    s.t_start = time.time()
                    s.status = "진행"
                s.current += n
                if note:
                    s.note = note

    def note(self, key: str, note: str):
        with self._lock:
            s = self._stages.get(key)
            if s:
                s.note = note

    def done(self, key: str, note: str = ""):
        with self._lock:
            s = self._stages.get(key)
            if s:
                s.status = "완료"
                s.t_end = time.time()
                if s.total <= 0:
                    s.total = max(s.current, 1)
                s.current = s.total
                if note:
                    s.note = note

    def fail(self, key: str, note: str = ""):
        with self._lock:
            s = self._stages.get(key)
            if s:
                s.status = "실패"
                s.t_end = time.time()
                if note:
                    s.note = note

    def _all_finished(self) -> bool:
        with self._lock:
            return all(s.status in ("완료", "실패") for s in self._stages.values()) \
                and bool(self._stages)

    # ── 렌더링 ──
    def _bar(self, s: _Stage, width: int = 28) -> str:
        icons = {"대기": "⏳", "진행": "▶️", "완료": "✅", "실패": "❌"}
        filled = int(round(s.pct / 100 * width))
        bar = "█" * filled + "░" * (width - filled)
        note = f"  {s.note}" if s.note else ""
        return (f"  {icons.get(s.status,'')} {s.label:<12} "
                f"[{bar}] {s.pct:5.1f}%  {s.elapsed:5.1f}s{note}")

    def _draw(self):
        if not self.enabled:
            return
        with self._lock:
            lines = [self._bar(self._stages[k]) for k in self._order]
        total_line = f"  ⏱️  전체 경과: {time.time()-self._t0:5.1f}s"
        out = "\n".join(lines + [total_line])
        # 이전 출력 지우고 다시 그리기 (ANSI: 커서 위로 이동)
        if self._rendered_lines:
            sys.stdout.write(f"\033[{self._rendered_lines}A")
        sys.stdout.write("\033[J")   # 커서 아래 전부 지우기
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
        self._rendered_lines = len(lines) + 1

    async def render_loop(self, interval: float = 0.4):
        """모든 단계가 끝날 때까지 게이지를 주기적으로 다시 그린다."""
        import asyncio
        if not self.enabled:
            return
        while not self._all_finished():
            self._draw()
            await asyncio.sleep(interval)
        self._draw()   # 최종 상태

    # ── 최종 요약 (게이지 비활성/로그용) ──
    def summary(self) -> str:
        with self._lock:
            rows = [f"{self._stages[k].label}: {self._stages[k].elapsed:.1f}s "
                    f"({self._stages[k].status})" for k in self._order]
        return " | ".join(rows)
