# -*- coding: utf-8 -*-
"""
pdf_from_md.py — 최종 MD 리포트를 '참고 스타일(NATAM 네이비)' PDF로 렌더.

방식: MD → HTML(markdown) → PyMuPDF Story 로 PDF 생성.
  · MD 파이널 리포트를 그대로 렌더하므로 템플릿이 바뀌면 PDF도 자동 반영(단일 소스).
  · 참고본 스타일: 네이비 섹션 바(##), 라이트블루 표 헤더, 콜아웃(>) 박스,
    네이비 코드블록(```), 좌측 네이비 바 소제목(###), 컬러 레벨 점(●).

의존성: markdown, pymupdf(fitz). 둘 중 하나라도 없으면 render()가 None 반환(호출부는 계속 진행).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    import markdown as _markdown
except Exception:
    _markdown = None


# ── 레벨/판정 이모지 → 컬러 점(●) 치환 (PDF에서 컬러 표시) ────────────────
_DOTS = {
    "🟢": '<span class="d1">●</span>',
    "🔵": '<span class="d2">●</span>',
    "🟡": '<span class="d3">●</span>',
    "🟠": '<span class="d4">●</span>',
    "🔴": '<span class="d5">●</span>',
    "⚪": '<span class="d0">●</span>',
}

# @font-face 는 _font_css() 가 찾은 폰트로 앞에 붙인다.
_CSS = """
* { font-family: kor; }
body { font-size: 9.5px; color: #2b2b2b; line-height: 1.5; }

h1 { font-size: 20px; color: #2e3b57; font-weight: bold;
     margin: 0 0 6px 0; padding-bottom: 8px; border-bottom: 2px solid #33415c; }
h2 { font-size: 13px; color: #ffffff; font-weight: bold;
     background-color: #33415c; padding: 8px 10px; margin: 20px 0 10px 0; }
h3 { font-size: 11.5px; color: #33415c; font-weight: bold;
     border-left: 3px solid #33415c; padding-left: 8px; margin: 15px 0 6px 0; }
h4 { font-size: 10.5px; color: #2f5fae; font-weight: bold; margin: 12px 0 5px 0; }

p  { margin: 5px 0; }
strong, b { font-weight: bold; color: #1f2a44; }
a { color: #2f5fae; }

table { width: 100%; border: 1px solid #d9dee8; margin: 7px 0; }
th { background-color: #edf0f6; color: #55617a; font-weight: bold;
     padding: 6px 8px; border: 1px solid #d9dee8; text-align: left; font-size: 9px; }
td { padding: 6px 8px; border: 1px solid #d9dee8; font-size: 9px; vertical-align: top; }

blockquote { background-color: #eef2fa; color: #4a5468;
             padding: 9px 13px; margin: 8px 0; border-left: 3px solid #9fb3d6; }
blockquote p { margin: 3px 0; }

pre { background-color: #2b3550; color: #dfe4ef;
      padding: 11px 13px; margin: 8px 0; font-family: mono; font-size: 8px; }
code { font-family: mono; background-color: #eef1f5; color: #33415c; }
pre code { background-color: #2b3550; color: #dfe4ef; }

ul, ol { margin: 5px 0 5px 18px; }
li { margin: 2px 0; }

.d0 { color: #9aa0ac; } .d1 { color: #16A34A; } .d2 { color: #2563EB; }
.d3 { color: #D97706; } .d4 { color: #EA580C; } .d5 { color: #DC2626; }
"""


# (폴더, 본문, 굵게, 코드용). 코드용 폰트가 없으면 본문 폰트를 쓴다.
_FONT_CANDIDATES = [
    (r"C:\Windows\Fonts", "malgun.ttf", "malgunbd.ttf", "consola.ttf"),
    ("/usr/share/fonts/truetype/nanum",                      # Debian fonts-nanum
     "NanumBarunGothic.ttf", "NanumBarunGothicBold.ttf", "NanumGothicCoding.ttf"),
    (str(SCRIPT_DIR), "malgun.ttf", "malgunbd.ttf", "consola.ttf"),   # 번들 폰트
]


def _find_fonts():
    """한글 폰트가 있는 (폴더, 본문, 굵게, 코드용). 없으면 None — 한글 없는 폰트로 대체하지 않는다."""
    for d, reg, bold, mono in _FONT_CANDIDATES:
        if os.path.exists(os.path.join(d, reg)) and os.path.exists(os.path.join(d, bold)):
            if not os.path.exists(os.path.join(d, mono)):
                mono = reg
            return d, reg, bold, mono
    return None


def _font_css(reg: str, bold: str, mono: str) -> str:
    return (f'@font-face {{ font-family: kor;  src: url("{reg}"); }}\n'
            f'@font-face {{ font-family: kor;  font-weight: bold; src: url("{bold}"); }}\n'
            f'@font-face {{ font-family: mono; src: url("{mono}"); }}\n')


def _postprocess(html: str) -> str:
    for emoji, span in _DOTS.items():
        html = html.replace(emoji, span)
    return html


def render(md_path: str, pdf_path: str | None = None) -> str | None:
    """
    MD 파일을 참고 스타일 PDF로 변환. 성공 시 PDF 경로, 실패/의존성 없음 시 None.
    pdf_path 미지정 시 reports/<md파일명>.pdf 로 저장.
    """
    if fitz is None or _markdown is None:
        print("⚠️ PDF 생성 건너뜀 — pymupdf/markdown 미설치 "
              f"(pymupdf={fitz is not None}, markdown={_markdown is not None})")
        return None
    fonts = _find_fonts()
    if fonts is None:
        print("⚠️ PDF 생성 실패 — 한글 폰트 없음(맑은 고딕 또는 fonts-nanum 필요). "
              "한글이 깨진 PDF 를 만들지 않습니다.")
        return None
    font_dir, *font_files = fonts
    try:
        md_text = Path(md_path).read_text(encoding="utf-8")
    except Exception as e:
        print(f"⚠️ PDF 생성 실패 — MD 읽기 오류: {e}")
        return None

    if pdf_path is None:
        base = os.path.splitext(os.path.basename(md_path))[0]
        out_dir = os.path.join(str(SCRIPT_DIR), "reports")
        os.makedirs(out_dir, exist_ok=True)
        pdf_path = os.path.join(out_dir, base + ".pdf")

    try:
        import io
        body = _markdown.markdown(
            md_text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
        body = _postprocess(body)

        # ── 섹션(##) 단위로 나눠 각각 '별도 Story'로 렌더한다 ──────────────
        #   PyMuPDF Story 는 표가 '페이지 하단의 좁은 틈'에서 시작하면(헤더만 들어가고
        #   본문 행이 안 들어가는 위치) 표 행을 통째로 잃고 렌더가 중단되는 버그가 있다.
        #   섹션마다 새 Story(=새 페이지에서 시작)로 렌더하면 각 섹션의 표가 페이지
        #   상단부터 시작해 이 버그를 회피한다(제목·ID박스는 첫 섹션과 같은 페이지).
        parts    = body.split("<h2")
        preamble = parts[0]
        sections = ["<h2" + p for p in parts[1:]]
        if sections:
            chunks = [preamble + sections[0]] + sections[1:]
        else:
            chunks = [preamble]
        chunks = [c for c in chunks if c.strip()]

        css    = _font_css(*font_files) + _CSS
        arch   = fitz.Archive(font_dir)
        buf    = io.BytesIO()                 # 메모리에 먼저 렌더(임시파일 회피)
        writer = fitz.DocumentWriter(buf)
        MED    = fitz.paper_rect("a4")
        WHERE  = MED + (42, 46, -42, -50)     # 좌·상·우·하 여백
        for chunk in chunks:
            story = fitz.Story(html=f"<html><body>{chunk}</body></html>",
                               user_css=css, archive=arch)
            more, guard = 1, 0
            while more and guard < 40:        # guard: 조판 이상 시 무한 페이지 방지
                dev = writer.begin_page(MED)
                more, _ = story.place(WHERE)
                story.draw(dev)
                writer.end_page()
                guard += 1
        writer.close()

        # CJK 폰트(맑은 고딕) 전체 임베드 시 수십 MB가 되므로, 사용 글리프만
        #   서브셋 임베드해 수백 KB로 축소한 뒤 최종 경로에 바로 저장(임시파일·교체 없음).
        doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
        try:
            doc.subset_fonts()
        except Exception as e:
            print(f"   ⚠️ 폰트 서브셋 생략(용량이 클 수 있음): {e}")
        doc.save(pdf_path, garbage=4, deflate=True)
        doc.close()

        return pdf_path
    except Exception as e:
        print(f"⚠️ PDF 생성 실패 — 렌더 오류: {e}")
        return None
