"""
tools/db_admin.py - 로컬 DB 관리 페이지 (브라우저에서 학습 데이터 보기·수정·삭제)

실행:
    python tools/db_admin.py            # 기본 http://127.0.0.1:8765 열림
    python tools/db_admin.py --port 9000 --no-open

의존성 없음 (파이썬 내장 http.server 만 사용). 로컬 전용(127.0.0.1)이라
외부에서 접근 불가. 잘못 학습된 항목을 표에서 바로 수정/삭제할 수 있다.
"""
import os
import sys
import json
import html
import argparse
import webbrowser
import threading
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

from database.db_manager import get_db_manager

DB = get_db_manager()

# 종류별 표시명 + 표에 보여줄 컬럼 + 수정 가능 컬럼
KINDS = {
    "emb":   {"label": "🧠 콘텐츠 임베딩", "cols": ["id", "title", "rights_holder", "source",
              "risk_score", "learned_from_job", "video_timestamp", "detection_count", "created_at"],
              "edit": ["title", "rights_holder", "source", "risk_score"]},
    "logo":  {"label": "🏷️ 로고", "cols": ["id", "title", "rights_holder", "source",
              "detection_count", "created_at"],
              "edit": ["title", "rights_holder"]},
    "music": {"label": "🎵 음악", "cols": ["id", "title", "rights_holder", "isrc", "source",
              "detection_count", "created_at"],
              "edit": ["title", "rights_holder"]},
    "font":  {"label": "🔤 폰트", "cols": ["id", "title", "rights_holder", "source",
              "detection_count", "created_at"],
              "edit": ["title", "rights_holder"]},
    "meme":  {"label": "🖼️ 밈", "cols": ["id", "title", "rights_holder", "source",
              "detection_count", "created_at"],
              "edit": ["title", "rights_holder"]},
    "clip":  {"label": "🎬 클립", "cols": ["id", "title", "rights_holder", "youtube_id",
              "source", "detection_count", "created_at"],
              "edit": ["title", "rights_holder"]},
}

# 표시 컬럼 → 실제 데이터 필드 매핑 (list_learned_data 출력 키에 맞춤)
COL_LABELS = {
    "id": "ID", "title": "제목/이름", "rights_holder": "권리자", "source": "출처",
    "risk_score": "위험도", "learned_from_job": "학습 작업", "video_timestamp": "영상시각(초)",
    "detection_count": "감지횟수", "created_at": "학습시각", "isrc": "ISRC",
    "youtube_id": "YouTube ID",
}


def _page(body: str, msg: str = "") -> bytes:
    stats = DB.get_stats()
    learned = DB.list_learned_data()
    counts = {k: len(v) for k, v in learned.items()}
    total_learned = sum(counts.values())
    msg_html = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    nav = " · ".join(
        f'<a href="#{k}">{v["label"]} ({counts.get(k,0)})</a>' for k, v in KINDS.items()
    )
    html_doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB 관리 — Copyright Detector</title>
<style>
 :root{{--bg:#0f172a;--surface:#1e293b;--surface2:#334155;--text:#f1f5f9;
 --muted:#94a3b8;--red:#ef4444;--green:#10b981;--blue:#3b82f6;--purple:#c084fc;
 --border:rgba(148,163,184,.15);}}
 *{{box-sizing:border-box;margin:0;padding:0}}
 body{{font-family:-apple-system,'Noto Sans KR',sans-serif;background:var(--bg);
 color:var(--text);padding:24px;line-height:1.5}}
 .wrap{{max-width:1200px;margin:0 auto}}
 h1{{font-size:22px;margin-bottom:4px}} .sub{{color:var(--muted);font-size:13px;margin-bottom:20px}}
 .nav{{background:var(--surface);border:1px solid var(--border);border-radius:10px;
 padding:12px 16px;margin-bottom:20px;font-size:13px}} .nav a{{color:var(--blue);text-decoration:none;margin-right:4px}}
 .msg{{background:rgba(16,185,129,.15);border:1px solid var(--green);color:var(--green);
 border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:14px}}
 .card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
 margin-bottom:24px;overflow:hidden}}
 .card h2{{font-size:16px;padding:14px 18px;border-bottom:1px solid var(--border)}}
 .empty{{padding:20px 18px;color:var(--muted);font-size:14px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;padding:8px 12px;color:var(--muted);font-size:11px;text-transform:uppercase;
 background:rgba(51,65,85,.4);border-bottom:1px solid var(--border)}}
 td{{padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:middle}}
 tr:hover{{background:rgba(148,163,184,.05)}}
 input[type=text]{{background:var(--surface2);border:1px solid var(--border);color:var(--text);
 border-radius:6px;padding:4px 8px;font-size:13px;width:100%;min-width:90px}}
 .btn{{border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer}}
 .btn-save{{background:var(--blue);color:#fff}} .btn-del{{background:rgba(239,68,68,.2);color:var(--red)}}
 .prov{{color:var(--muted);font-size:11px}}
 form.row{{display:contents}}
</style></head><body><div class="wrap">
<h1>🗂️ 학습 데이터 관리</h1>
<div class="sub">총 학습 {total_learned}건 · 분석작업 {stats.get('total_jobs',0)}건 · 발견 {stats.get('total_findings',0)}건
 &nbsp;|&nbsp; 표에서 값을 고치고 <b>저장</b>, 잘못된 항목은 <b>삭제</b></div>
<div class="nav">{nav}</div>
{msg_html}
{body}
<div class="sub" style="margin-top:24px">로컬 전용 (127.0.0.1) · 종료: 터미널에서 Ctrl+C</div>
</div></body></html>"""
    return html_doc.encode("utf-8")


def _table(kind: str, rows: list) -> str:
    spec = KINDS[kind]
    cols = spec["cols"]
    editable = set(spec["edit"])
    head = "".join(f"<th>{COL_LABELS.get(c, c)}</th>" for c in cols) + "<th>작업</th>"
    if not rows:
        return (f'<div class="card" id="{kind}"><h2>{spec["label"]}</h2>'
                f'<div class="empty">학습된 항목 없음</div></div>')
    body_rows = []
    for r in rows:
        cells = []
        eid = r.get("id")
        for c in cols:
            val = r.get(c, "")
            if val is None:
                val = ""
            if c in editable:
                cells.append(
                    f'<td><input type="text" name="{c}" form="f_{kind}_{eid}" '
                    f'value="{html.escape(str(val))}"></td>')
            elif c in ("learned_from_job", "video_timestamp"):
                cells.append(f'<td class="prov">{html.escape(str(val))}</td>')
            else:
                cells.append(f"<td>{html.escape(str(val))}</td>")
        action = (
            f'<td style="white-space:nowrap">'
            f'<form class="row" id="f_{kind}_{eid}" method="post" action="/update">'
            f'<input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="id" value="{eid}"></form>'
            f'<button class="btn btn-save" form="f_{kind}_{eid}">저장</button> '
            f'<form method="post" action="/delete" style="display:inline" '
            f'onsubmit="return confirm(\'삭제할까요? (복구 불가)\')">'
            f'<input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="id" value="{eid}">'
            f'<button class="btn btn-del">삭제</button></form></td>')
        body_rows.append("<tr>" + "".join(cells) + action + "</tr>")
    return (f'<div class="card" id="{kind}"><h2>{spec["label"]} — {len(rows)}건</h2>'
            f"<table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table></div>")


def _render(msg: str = "") -> bytes:
    learned = DB.list_learned_data()
    body = "".join(_table(k, learned.get(k, [])) for k in KINDS)
    return _page(body, msg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 접근 로그 억제

    def _send(self, content: bytes, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/index.html"):
            self._send(_render())
        else:
            self._send(b"Not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        get = lambda k: data.get(k, [""])[0]
        path = urlparse(self.path).path
        kind, eid = get("kind"), get("id")
        msg = ""
        try:
            eid_i = int(eid)
            if path == "/delete":
                ok = DB.delete_learned_entry(kind, eid_i)
                msg = f"삭제 {'완료' if ok else '실패'}: {kind}:{eid}"
            elif path == "/update":
                updates = {k: v[0] for k, v in data.items() if k not in ("kind", "id")}
                ok = DB.update_learned_entry(kind, eid_i, updates)
                msg = f"수정 {'완료' if ok else '(변경 없음)'}: {kind}:{eid}"
        except (ValueError, TypeError):
            msg = "요청 오류"
        # PRG 패턴: 새로고침 시 재전송 방지 위해 결과 페이지 직접 반환
        self._send(_render(msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="브라우저 자동 열기 안 함")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"🗂️  DB 관리 페이지: {url}")
    print("    (종료: Ctrl+C)")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료됨")
        server.shutdown()


if __name__ == "__main__":
    main()
