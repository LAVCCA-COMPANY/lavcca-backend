# -*- coding: utf-8 -*-
"""
run_logging.py — mvp193 통합 로깅.

목적: 파이프라인 전체(래퍼·엔진·spread_signals·pdf_from_md + 저작권 서브프로세스)의
      콘솔 출력을 '한 개의 타임스탬프 로그 파일'로 통합 기록한다. 콘솔 출력은 그대로 유지.

설계
  · sys.stdout / sys.stderr 를 Tee 로 감싼다 → 콘솔(원본) + 로그파일(줄마다 [HH:MM:SS]) 동시 기록.
  · 엔진이 백그라운드 스레드에서도 print 하므로 파일 기록은 Lock 으로 보호(줄 섞임 방지).
  · 콘솔 인코딩 오류(cp949 등)에도 죽지 않도록 방어(파일은 항상 utf-8 전체 기록).
  · '서브프로세스'(저작권 main.py)는 fd 상속이라 Tee 로 안 잡히므로, stream_subprocess()가
    출력을 라인 단위로 받아 print()로 되흘려 통합 로그에 포함시킨다.
  · logging 모듈로 나오는 라이브러리 경고(WARNING+)도 같은 스트림으로 모은다.

진입점
  · setup(log_dir="logs")   — 로깅 시작. 로그 파일 경로 반환.
  · stream_subprocess(cmd)  — 서브프로세스를 실행하며 출력을 통합 로그로 흘림. returncode 반환.
  · teardown()              — 복원(원래 stdout/stderr) + 파일 닫기. atexit 로 자동 호출.
"""
import os
import sys
import atexit
import logging
import threading
import subprocess
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

_state = {
    "file": None, "path": None,
    "orig_out": None, "orig_err": None,
    "lock": threading.Lock(),
    "buf": {"O": "", "E": ""},
    "log_handler": None,
}


class _Tee:
    """콘솔 스트림과 로그파일에 동시 기록. 파일 쪽은 줄 단위로 [HH:MM:SS] 접두."""

    def __init__(self, console, tag):
        self._c = console          # 원본 콘솔 스트림
        self._tag = tag            # 'O'(stdout) / 'E'(stderr) — 버퍼 구분

    def write(self, s):
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        # 1) 콘솔에 원본 그대로 (인코딩 오류에도 죽지 않게 방어)
        try:
            self._c.write(s)
        except Exception:
            try:
                enc = getattr(self._c, "encoding", None) or "utf-8"
                self._c.write(s.encode(enc, "replace").decode(enc, "replace"))
            except Exception:
                pass
        # 2) 로그파일에 줄 단위 타임스탬프 기록
        f = _state["file"]
        if f is not None:
            with _state["lock"]:
                b = _state["buf"].get(self._tag, "") + s
                while "\n" in b:
                    line, b = b.split("\n", 1)
                    line = line.split("\r")[-1]   # 진행바(\r 덮어쓰기)는 마지막 값만
                    ts = datetime.now().strftime("%H:%M:%S")
                    try:
                        f.write(f"[{ts}] {line}\n")
                    except Exception:
                        pass
                _state["buf"][self._tag] = b
                try:
                    f.flush()
                except Exception:
                    pass
        return len(s)

    def flush(self):
        try:
            self._c.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._c.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._c.fileno()

    @property
    def encoding(self):
        return getattr(self._c, "encoding", "utf-8")


def setup(log_dir: str = "logs", prefix: str = "mvp193") -> str | None:
    """통합 로깅 시작. 이미 설정돼 있으면 기존 로그 경로 반환."""
    if _state["file"] is not None:
        return _state["path"]
    try:
        d = SCRIPT_DIR / log_dir
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(d / f"{prefix}_{ts}.log")
        f = open(path, "w", encoding="utf-8")
    except Exception as e:
        print(f"⚠️ 통합 로깅 파일 생성 실패(로깅 없이 계속): {e}")
        return None

    _state.update(file=f, path=path, orig_out=sys.stdout, orig_err=sys.stderr,
                  buf={"O": "", "E": ""})
    sys.stdout = _Tee(_state["orig_out"], "O")
    sys.stderr = _Tee(_state["orig_err"], "E")

    # 라이브러리 logging(WARNING+)도 같은 스트림(→콘솔+파일)으로 모음
    try:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(logging.WARNING)
        h.setFormatter(logging.Formatter("[LOG:%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(h)
        _state["log_handler"] = h
    except Exception:
        pass

    f.write("=" * 60 + "\n")
    f.write(f"  mvp193 통합 실행 로그\n")
    f.write(f"  생성: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    f.write(f"  경로: {path}\n")
    f.write("=" * 60 + "\n\n")
    f.flush()

    atexit.register(teardown)
    return path


def teardown():
    """원래 stdout/stderr 복원 + 남은 버퍼 flush + 파일 닫기."""
    f = _state["file"]
    if f is None:
        return
    # logging 핸들러 제거
    if _state.get("log_handler") is not None:
        try:
            logging.getLogger().removeHandler(_state["log_handler"])
        except Exception:
            pass
        _state["log_handler"] = None
    with _state["lock"]:
        for tag, b in list(_state["buf"].items()):
            if b:
                ts = datetime.now().strftime("%H:%M:%S")
                try:
                    f.write(f"[{ts}] {b}\n")
                except Exception:
                    pass
        _state["buf"] = {"O": "", "E": ""}
        try:
            f.write(f"\n종료: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.flush()
            f.close()
        except Exception:
            pass
    if _state["orig_out"] is not None:
        sys.stdout = _state["orig_out"]
    if _state["orig_err"] is not None:
        sys.stderr = _state["orig_err"]
    _state["file"] = None


def stream_subprocess(cmd, cwd=None, env=None, prefix: str = "") -> int:
    """
    서브프로세스를 실행하며 출력(stdout+stderr)을 라인 단위로 받아 print()로 되흘린다.
      → Tee 를 거쳐 콘솔 + 통합 로그파일에 함께 기록된다. 자식 프로세스의 returncode 반환.
    """
    e = dict(os.environ)
    if env:
        e.update(env)
    e["PYTHONUNBUFFERED"] = "1"          # 자식 파이썬 라인버퍼 → 실시간 로그
    e.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.Popen(
            cmd, cwd=cwd, env=e,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, encoding="utf-8", errors="replace")
    except Exception as ex:
        print(f"⚠️  서브프로세스 실행 실패: {ex}")
        return -1
    try:
        for line in p.stdout:
            print(prefix + line.rstrip("\n"))     # → Tee → 콘솔 + 파일
    finally:
        p.wait()
    return p.returncode
