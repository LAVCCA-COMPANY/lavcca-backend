"""
pdf_report_generator.py — NATAM 스타일 PDF 리포트 생성기
=========================================================
이미지 디자인 기준 3페이지 구성:
  Page 1: 콘텐츠 리스크 분석 요약 (메타 + 핵심요약 + 리스크 레벨)
  Page 2: 주요 리스크 분석 (NATAM A축 · B축 항목별 단계)
  Page 3: 권장 수정 및 대응 가이드 (대응 액션 + 유사 사례)

사용법:
  from pdf_report_generator import generate_pdf_report
  pdf_path = generate_pdf_report(report, output_dir="reports")
"""

import os
import re
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus.flowables import Flowable


# ════════════════════════════════════════════════════════
# 🎨 디자인 토큰
# ════════════════════════════════════════════════════════

W, H = A4  # 595.28 x 841.89 pt
PAD  = 20 * mm

# 컬러 팔레트 (이미지 디자인 기준)
C = {
    "bg":          colors.HexColor("#F7F7F5"),   # 페이지 배경 (아이보리)
    "card":        colors.white,
    "card_border": colors.HexColor("#E8E8E4"),
    "primary":     colors.HexColor("#1A1A1A"),   # 메인 텍스트
    "secondary":   colors.HexColor("#6B6B6B"),   # 서브 텍스트
    "accent":      colors.HexColor("#E85D26"),   # 오렌지 포인트
    "blue":        colors.HexColor("#2563EB"),
    "green":       colors.HexColor("#16A34A"),
    "yellow":      colors.HexColor("#D97706"),

    # 리스크 단계별
    "SAFE":     colors.HexColor("#16A34A"),
    "CARE":     colors.HexColor("#2563EB"),
    "ALERT":    colors.HexColor("#D97706"),
    "DANGER":   colors.HexColor("#EA580C"),
    "CRITICAL": colors.HexColor("#DC2626"),

    # 배지 배경
    "SAFE_bg":     colors.HexColor("#DCFCE7"),
    "CARE_bg":     colors.HexColor("#DBEAFE"),
    "ALERT_bg":    colors.HexColor("#FEF3C7"),
    "DANGER_bg":   colors.HexColor("#FFEDD5"),
    "CRITICAL_bg": colors.HexColor("#FEE2E2"),

    "divider": colors.HexColor("#E5E5E0"),
    "header_bg": colors.HexColor("#1A1A1A"),
}

LEVEL_LABEL = {
    "SAFE": "낮음", "CARE": "주의", "ALERT": "중간",
    "DANGER": "높음", "CRITICAL": "심각",
}
LEVEL_ORDER = ["SAFE", "CARE", "ALERT", "DANGER", "CRITICAL"]


# ════════════════════════════════════════════════════════
# 🔤 폰트 등록
# ════════════════════════════════════════════════════════

# 한국어 폰트 후보 경로 (Windows 맑은 고딕 우선, Linux Nanum 대체)
# 한국어 폰트 후보: (regular_path, bold_path, ttc_index_or_None)
_FONT_CANDIDATES = [
    # Windows 맑은 고딕
    ("C:/Windows/Fonts/malgun.ttf",   "C:/Windows/Fonts/malgunbd.ttf",  None),
    # Linux Nanum
    ("/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
     "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",         None),
    # macOS Apple SD Gothic (TTC, subfont index 0)
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc",
     "/System/Library/Fonts/AppleSDGothicNeo.ttc",                       0),
]

def _register_fonts():
    from reportlab.pdfbase.ttfonts import TTFont
    for reg_path, bold_path, ttc_idx in _FONT_CANDIDATES:
        if not (os.path.exists(reg_path) and os.path.exists(bold_path)):
            continue
        try:
            if ttc_idx is not None:
                pdfmetrics.registerFont(TTFont("KorR", reg_path,  subfontIndex=ttc_idx))
                pdfmetrics.registerFont(TTFont("KorB", bold_path, subfontIndex=ttc_idx))
            else:
                pdfmetrics.registerFont(TTFont("KorR", reg_path))
                pdfmetrics.registerFont(TTFont("KorB", bold_path))
            return "KorR", "KorB"
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold"

FONT_R, FONT_B = _register_fonts()


# ════════════════════════════════════════════════════════
# 🧩 커스텀 Flowable — 헤더 배너
# ════════════════════════════════════════════════════════

class HeaderBanner(Flowable):
    """각 페이지 상단 NATAM 브랜드 헤더"""
    def __init__(self, page_num: int, total: int = 3, subtitle: str = "콘텐츠 업로드 전 리스크 체크",
                 label: str = None):
        super().__init__()
        self.page_num = page_num
        self.total    = total
        self.subtitle = subtitle
        # label 지정 시 우측 "N/total" 대신 이 텍스트 표기 (상세/부록 페이지용)
        self.label    = label
        self.width    = W - 2 * PAD
        self.height   = 14 * mm

    def draw(self):
        c = self.canv
        w, h = self.width, self.height

        # 배경
        c.setFillColor(C["header_bg"])
        c.roundRect(0, 0, w, h, 4, fill=1, stroke=0)

        # NATAM 로고
        c.setFillColor(colors.white)
        c.setFont(FONT_B, 11)
        c.drawString(10, h / 2 - 4, "NATAM")

        # 서브타이틀
        c.setFont(FONT_R, 8)
        c.setFillColor(colors.HexColor("#AAAAAA"))
        c.drawString(10, h / 2 - 14, self.subtitle)

        # 페이지 번호 (또는 커스텀 라벨)
        pg_text = self.label if self.label else f"{self.page_num}/{self.total}"
        c.setFont(FONT_R, 9)
        c.setFillColor(colors.white)
        c.drawRightString(w - 10, h / 2 - 4, pg_text)


class SectionTitle(Flowable):
    """아이콘 + 대제목 + 부제목 조합 섹션 타이틀"""
    def __init__(self, icon: str, title: str, subtitle: str, icon_color=None):
        super().__init__()
        self.icon       = icon
        self.title      = title
        self.subtitle   = subtitle
        self.icon_color = icon_color or C["accent"]
        self.width      = W - 2 * PAD
        self.height     = 28 * mm

    def draw(self):
        c = self.canv
        h = self.height

        # 아이콘 원
        c.setFillColor(self.icon_color)
        c.circle(15, h - 16, 10, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(FONT_B, 13)
        c.drawCentredString(15, h - 21, self.icon)

        # 제목
        c.setFillColor(C["primary"])
        c.setFont(FONT_B, 18)
        c.drawString(32, h - 14, self.title)

        # 부제목
        c.setFont(FONT_R, 9)
        c.setFillColor(C["secondary"])
        c.drawString(32, h - 26, self.subtitle)

        # 구분선
        c.setStrokeColor(C["divider"])
        c.setLineWidth(0.5)
        c.line(0, 0, self.width, 0)


class RiskBadge(Flowable):
    """단계 배지 (SAFE / CARE / ALERT / DANGER / CRITICAL)"""
    def __init__(self, level: str, width: float = 45, height: float = 14):
        super().__init__()
        self.level  = level
        self.width  = width
        self.height = height

    def draw(self):
        c   = self.canv
        lv  = self.level
        bg  = C.get(f"{lv}_bg",  colors.HexColor("#F3F4F6"))
        fg  = C.get(lv,          colors.HexColor("#6B7280"))
        txt = LEVEL_LABEL.get(lv, lv)

        c.setFillColor(bg)
        c.roundRect(0, 0, self.width, self.height, 5, fill=1, stroke=0)
        c.setFillColor(fg)
        c.setFont(FONT_B, 8)
        c.drawCentredString(self.width / 2, 3.5, txt)


# ════════════════════════════════════════════════════════
# 🛠️  스타일 팩토리
# ════════════════════════════════════════════════════════

def _ps(name, font=None, size=9, color=None, leading=None, space_before=0, space_after=2, **kw):
    return ParagraphStyle(
        name,
        fontName     = font or FONT_R,
        fontSize     = size,
        textColor    = color or C["primary"],
        leading      = leading or (size * 1.45),
        spaceBefore  = space_before,
        spaceAfter   = space_after,
        **kw,
    )

ST = {
    "body":       _ps("body"),
    "body_sm":    _ps("body_sm",  size=8,  color=C["secondary"]),
    "bold":       _ps("bold",     font=FONT_B, size=9),
    "bold_sm":    _ps("bold_sm",  font=FONT_B, size=8),
    "label":      _ps("label",    font=FONT_B, size=7,  color=C["secondary"]),
    "card_title": _ps("card_t",   font=FONT_B, size=11),
    "bullet":     _ps("bullet",   size=8,  leftIndent=10, bulletIndent=0),
}


# ════════════════════════════════════════════════════════
# 🃏 카드 빌더
# ════════════════════════════════════════════════════════

def _card_table(content_rows, col_widths=None):
    """흰색 라운드 카드처럼 보이는 단일 셀 테이블"""
    col_w = col_widths or [W - 2 * PAD]
    t = Table([[content_rows]], colWidths=col_w)
    t.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), C["card"]),
        ("ROUNDEDCORNERS", [6]),
        ("BOX",         (0, 0), (-1, -1), 0.5, C["card_border"]),
        ("TOPPADDING",  (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0,0), (-1,-1),  10),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING",(0, 0), (-1, -1), 12),
    ]))
    return t


def _two_col_table(left_items, right_items, left_w=None, right_w=None):
    """2컬럼 나란히 테이블"""
    avail = W - 2 * PAD - 6
    lw = left_w  or avail * 0.5
    rw = right_w or avail * 0.5
    t = Table([[left_items, right_items]], colWidths=[lw, rw])
    t.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ("INNERGRID",    (0, 0), (-1, -1), 0, colors.transparent),
        ("BOX",          (0, 0), (-1, -1), 0, colors.transparent),
    ]))
    return t


# ════════════════════════════════════════════════════════
# 📊 헬퍼 — 데이터 추출
# ════════════════════════════════════════════════════════

def _safe(val, default="—"):
    if val in (None, "", [], {}): return default
    return str(val).strip() or default

def _overall_level(natam: dict) -> str:
    """A·B축 전체에서 가장 높은 단계 반환"""
    all_levels = []
    for axis in ("A", "B"):
        for v in natam.get(axis, {}).values():
            lv = v.get("level", "SAFE")
            if lv in LEVEL_ORDER:
                all_levels.append(LEVEL_ORDER.index(lv))
    if not all_levels:
        return "SAFE"
    return LEVEL_ORDER[max(all_levels)]

def _overall_label(natam: dict) -> str:
    lv = _overall_level(natam)
    return LEVEL_LABEL.get(lv, lv)

def _count_alerts(natam: dict) -> int:
    count = 0
    for axis in ("A", "B"):
        for v in natam.get(axis, {}).values():
            if v.get("level", "SAFE") not in ("SAFE", "CARE"):
                count += 1
    return count

NATAM_A_NAMES = {
    "A-01": "정치·이념 리스크",        "A-02": "특정 인물·집단 낙인 리스크",
    "A-03": "사생활·윤리 리스크",      "A-04": "이미지 역반전 리스크",
    "A-05": "맥락 절단·클립화 리스크", "A-06": "갈등 증폭 리스크",
    "A-07": "밈화·조롱 소비 리스크",   "A-08": "브랜드 세이프티 리스크",
    "A-09": "감정 선동 리스크",        "A-10": "기존 논란 결합 리스크",
}
NATAM_B_NAMES = {
    "B-01": "괴롭힘·혐오·차별 표현 리스크", "B-02": "폭력·위협·불법행위 리스크",
    "B-03": "저작권 리스크",              "B-04": "성적 표현·대상화 리스크",
    "B-05": "광고친화성 정책 리스크",
}

ICON_BY_NAME = {
    "정치·이념 리스크":         "⚑",
    "특정 인물·집단 낙인 리스크": "👤",
    "사생활·윤리 리스크":       "🔒",
    "이미지 역반전 리스크":     "↩",
    "맥락 절단·클립화 리스크":  "✂",
    "갈등 증폭 리스크":         "⚡",
    "밈화·조롱 소비 리스크":    "😂",
    "브랜드 세이프티 리스크":   "🏷",
    "감정 선동 리스크":         "🔥",
    "기존 논란 결합 리스크":    "🔗",
    "괴롭힘·혐오·차별 표현 리스크": "⚠",
    "폭력·위협·불법행위 리스크": "🚨",
    "저작권 리스크":           "©",
    "성적 표현·대상화 리스크":  "🔞",
    "광고친화성 정책 리스크":   "💰",
}


# ════════════════════════════════════════════════════════
# 📄 Page 1 — 콘텐츠 리스크 분석 요약
# ════════════════════════════════════════════════════════

def _build_page1(report: dict) -> list:
    meta   = report.get("meta", {})
    natam  = report.get("natam_risk", {})
    cls    = report.get("classification", {})
    spread = report.get("spread_stage", {})
    yt     = report.get("youtube_meta", {})

    overall_lv    = _overall_level(natam)
    overall_label = _overall_label(natam)
    alert_count   = _count_alerts(natam)

    story = []

    # ── 헤더 ──
    story.append(HeaderBanner(1, label="요약 1 / 3"))
    story.append(Spacer(1, 6 * mm))

    # ── 섹션 타이틀 ──
    story.append(SectionTitle("🛡", "콘텐츠 리스크 분석 요약", "업로드 전 잠재 리스크를 미리 확인하세요"))
    story.append(Spacer(1, 5 * mm))

    # ── 분석 대상 영상 카드 ──
    title     = _safe(meta.get("incident_title") or meta.get("video_filename") or yt.get("title"), "영상 제목 없음")
    channel   = _safe(yt.get("channel") or meta.get("youtube_channel"), "로컬 파일")
    platform  = "YouTube" if yt.get("url") else "로컬 파일"
    dur       = _safe(meta.get("duration_sec"), "—")
    analyzed  = _safe(meta.get("analyzed_at"), datetime.now().strftime("%Y-%m-%d %H:%M"))
    upload_dt = _safe(yt.get("upload_date"), "—")
    if upload_dt != "—" and len(upload_dt) == 8:
        upload_dt = f"{upload_dt[:4]}-{upload_dt[4:6]}-{upload_dt[6:]}"

    # 영상 정보 2컬럼 그리드
    info_data = [
        [Paragraph("분석 대상 영상", ST["label"]),  ""],
        [Paragraph(f"<b>{title[:45]}{'…' if len(title)>45 else ''}</b>",
                   _ps("t2", font=FONT_B, size=10)), ""],
        [Paragraph(f"{channel}", ST["body_sm"]),    ""],
        ["", ""],
        [Paragraph("📺  업로드 플랫폼", ST["body_sm"]),
         Paragraph(f"📅  업로드 예정일 {upload_dt}", ST["body_sm"])],
        [Paragraph(f"<font color='#E85D26'><b>{platform}</b></font>",
                   _ps("pl", font=FONT_B, size=9, color=C["accent"])),
         Paragraph(f"⏱  분석 일시 {analyzed[:16]}", ST["body_sm"])],
    ]
    info_t = Table(info_data, colWidths=[(W - 2*PAD - 24) * 0.6, (W - 2*PAD - 24) * 0.4])
    info_t.setStyle(TableStyle([
        ("SPAN",         (0, 0), (1, 0)),
        ("SPAN",         (0, 1), (1, 1)),
        ("SPAN",         (0, 2), (1, 2)),
        ("SPAN",         (0, 3), (1, 3)),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW",    (0, 3), (1, 3), 0.5, C["divider"]),
    ]))

    card_content = [info_t]
    story.append(_wrap_in_card(card_content))
    story.append(Spacer(1, 4 * mm))

    # ── 핵심 요약 카드 ──
    summary_text = _safe(
        meta.get("incident_summary") or meta.get("input_query"),
        "자막 분석 기반으로 생성된 위기 관리 분석 보고서입니다."
    )
    if len(summary_text) > 200:
        summary_text = summary_text[:200] + "…"

    summary_items = [
        Paragraph("핵심 요약", ST["label"]),
        Spacer(1, 3),
        Paragraph(summary_text, ST["body"]),
        Spacer(1, 5 * mm),
        # 4개 지표 박스
        _build_metric_row(overall_label, overall_lv, alert_count, spread.get("stage","—")),
    ]
    story.append(_wrap_in_card(summary_items))
    story.append(Spacer(1, 4 * mm))

    # ── 리스크 수준 안내 카드 ──
    guide_items = [
        Paragraph("리스크 수준 안내", ST["label"]),
        Spacer(1, 4),
        _build_level_guide(),
    ]
    story.append(_wrap_in_card(guide_items))

    return story


def _wrap_in_card(items: list) -> Table:
    """items 리스트를 단일 셀 흰색 카드로 감싸기"""
    inner = Table([[items]], colWidths=[W - 2 * PAD - 2])
    inner.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), C["card"]),
        ("BOX",          (0, 0), (-1, -1), 0.5, C["card_border"]),
        ("TOPPADDING",   (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
        ("LEFTPADDING",  (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
    ]))
    return inner


def _build_metric_row(overall_label: str, overall_lv: str, alert_count: int, stage: str) -> Table:
    """4개 지표 나란히"""
    def _metric_cell(label, value, value_color=None):
        vc = value_color or C["primary"]
        return [
            Paragraph(label, _ps("ml", size=7, color=C["secondary"])),
            Paragraph(f"<b>{value}</b>", _ps("mv", font=FONT_B, size=14, color=vc)),
        ]

    lv_color = C.get(overall_lv, C["primary"])

    cells = [
        _metric_cell("종합 리스크 수준", overall_label, lv_color),
        _metric_cell("예상 영향도", "보통"),
        _metric_cell("권장 조치 항목", f"{alert_count}건"),
        _metric_cell("확산 단계", stage),
    ]

    row_data  = [[Table([[c]], colWidths=[(W - 2*PAD - 28) / 4]) for c in cells]]
    t = Table(row_data, colWidths=[(W - 2*PAD - 28) / 4] * 4)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), colors.HexColor("#F9F9F7")),
        ("BOX",          (0, 0), (-1, -1), 0.5, C["divider"]),
        ("INNERGRID",    (0, 0), (-1, -1), 0.5, C["divider"]),
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _build_level_guide() -> Table:
    rows = [
        (C["SAFE"],    "🟢", "낮음", "플랫폼 정책 위반 가능성이 낮고, 이슈 발생 가능성이 낮은 상태"),
        (C["ALERT"],   "🟡", "중간", "일부 주의가 필요한 요소가 있으며, 수정/보완을 권장하는 상태"),
        (C["CRITICAL"],"🔴", "높음", "정책 위반 또는 논란 발생 가능성이 높아, 수정 및 주의가 필요한 상태"),
    ]
    data = []
    for color, emoji, label, desc in rows:
        data.append([
            Paragraph(f"<font color='{color.hexval()}'>{emoji}</font>",
                      _ps("gi", size=10)),
            Paragraph(f"<b>{label}</b>: {desc}",
                      _ps("gd", size=8, color=C["secondary"])),
        ])
    t = Table(data, colWidths=[14, W - 2*PAD - 28 - 14])
    t.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


# ════════════════════════════════════════════════════════
# 📄 Page 2 — 주요 리스크 분석
# ════════════════════════════════════════════════════════

def _build_page2(report: dict) -> list:
    natam = report.get("natam_risk", {})
    story = [PageBreak()]

    story.append(HeaderBanner(2, label="요약 2 / 3"))
    story.append(Spacer(1, 6 * mm))
    story.append(SectionTitle("⚠", "주요 리스크 분석",
                              "5가지 핵심 영역별 리스크 수준과 세부 내용을 확인하세요.",
                              icon_color=C["ALERT"]))
    story.append(Spacer(1, 5 * mm))

    # ALERT 이상 항목을 앞으로, 나머지는 뒤에 배치
    all_items = []
    for key, name in NATAM_A_NAMES.items():
        item = natam.get("A", {}).get(key, {})
        all_items.append((key, name, item.get("level","SAFE"), item.get("reason","—")))
    for key, name in NATAM_B_NAMES.items():
        item = natam.get("B", {}).get(key, {})
        all_items.append((key, name, item.get("level","SAFE"), item.get("reason","—")))

    # ALERT 이상 먼저, 그 다음 SAFE/CARE
    priority   = [i for i in all_items if i[2] not in ("SAFE", "CARE")]
    rest       = [i for i in all_items if i[2] in ("SAFE", "CARE")]
    show_items = priority[:6]  # ALERT 이상 항목만, 최대 6개 (SAFE/CARE 제외로 중요도 집중)

    for key, name, level, reason in show_items:
        story.append(_build_risk_item_card(key, name, level, reason))
        story.append(Spacer(1, 3 * mm))

    return story


def _build_risk_item_card(key: str, name: str, level: str, reason: str) -> Table:
    lv_color = C.get(level, C["secondary"])
    bg_color = C.get(f"{level}_bg", colors.HexColor("#F9F9F7"))
    label    = LEVEL_LABEL.get(level, level)
    icon     = ICON_BY_NAME.get(name, "•")

    # 배지
    badge_data = [[
        Paragraph(f"<font color='{lv_color.hexval()}'><b>{label}</b></font>",
                  _ps("bdg", font=FONT_B, size=8, color=lv_color)),
    ]]
    badge_t = Table(badge_data, colWidths=[30])
    badge_t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), bg_color),
        ("ROUNDEDCORNERS",[5]),
        ("TOPPADDING",   (0,0),(-1,-1), 2),
        ("BOTTOMPADDING",(0,0),(-1,-1), 2),
        ("LEFTPADDING",  (0,0),(-1,-1), 5),
        ("RIGHTPADDING", (0,0),(-1,-1), 5),
    ]))

    # 제목 행
    header_row = Table([[
        Paragraph(f"<b>{name}</b>", _ps("rn", font=FONT_B, size=10)),
        badge_t,
    ]], colWidths=[W - 2*PAD - 28 - 40, 40])
    header_row.setStyle(TableStyle([
        ("VALIGN",      (0,0),(-1,-1),"MIDDLE"),
        ("LEFTPADDING", (0,0),(-1,-1), 0),
        ("RIGHTPADDING",(0,0),(-1,-1), 0),
        ("TOPPADDING",  (0,0),(-1,-1), 0),
        ("BOTTOMPADDING",(0,0),(-1,-1), 0),
    ]))

    # 이유 텍스트
    reason_short = reason[:120] + "…" if len(reason) > 120 else reason

    # 주요 포인트 (reason에서 첫 문장 추출)
    first_sentence = reason_short.split(".")[0].strip()

    card_inner = [
        header_row,
        Spacer(1, 4),
        Paragraph(reason_short, ST["body_sm"]),
        Spacer(1, 4),
        Paragraph("주요 포인트", ST["label"]),
        Paragraph(f"• {first_sentence}", ST["bullet"]),
    ]

    return _wrap_in_card(card_inner)


# ════════════════════════════════════════════════════════
# 📄 Page 3 — 권장 수정 및 대응 가이드
# ════════════════════════════════════════════════════════

def _build_page3(report: dict) -> list:
    meta   = report.get("meta", {})
    spread = report.get("spread_stage", {})
    cases  = report.get("similar_cases", [])
    wa     = report.get("worst_actions", [])

    stage = spread.get("stage", "Early")
    actions = {
        "Early": {
            "immediate": ["문제 구간 내부 검토 및 증거 보전", "커뮤니티·SNS 언급량 모니터링 시작"],
            "short":     ["법률·PR 전문가와 대응 방향 사전 협의", "공식 입장문 초안 작성"],
        },
        "Mid": {
            "immediate": ["문제 콘텐츠 접근 제한 또는 수정본 업로드 검토", "공식 입장문 즉시 작성 및 채널 공지"],
            "short":     ["커뮤니티 댓글·DM 모니터링 및 대응 방침 수립", "미디어 문의 대응 창구 일원화"],
        },
        "Late": {
            "immediate": ["추가 악화 요인 차단 (불필요한 발언·라이브 자제)", "기존 공개 입장문 일관성 유지"],
            "short":     ["장기적 이미지 회복 전략 수립", "법적 리스크 최종 점검"],
        },
    }.get(stage, {
        "immediate": ["확산 지표 데이터 수집 우선 진행", "내부 상황 파악 및 관계자 보고"],
        "short":     ["전문가 자문 의뢰", "모니터링 체계 구축"],
    })

    story = [PageBreak()]

    story.append(HeaderBanner(3, label="요약 3 / 3"))
    story.append(Spacer(1, 6 * mm))
    story.append(SectionTitle("✅", "권장 수정 및 대응 가이드",
                              "아래 권장 사항을 반영하면 리스크를 줄이고 콘텐츠 안정성을 높일 수 있습니다.",
                              icon_color=C["green"]))
    story.append(Spacer(1, 5 * mm))

    # ── 대응 가이드 섹션 (action 번호별) ──
    natam  = report.get("natam_risk", {})
    all_items = []
    for key, name in NATAM_A_NAMES.items():
        item = natam.get("A", {}).get(key, {})
        all_items.append((name, item.get("level","SAFE"), item.get("reason","—")))
    for key, name in NATAM_B_NAMES.items():
        item = natam.get("B", {}).get(key, {})
        all_items.append((name, item.get("level","SAFE"), item.get("reason","—")))

    alert_items = [i for i in all_items if i[1] not in ("SAFE", "CARE")][:4]

    for idx, (name, level, reason) in enumerate(alert_items, 1):
        lv_color = C.get(level, C["secondary"])
        label    = LEVEL_LABEL.get(level, level)

        badge = Table([[
            Paragraph(f"<b>{label}</b>", _ps("ab", font=FONT_B, size=7, color=lv_color)),
        ]], colWidths=[28])
        badge.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(-1,-1), C.get(f"{level}_bg", colors.white)),
            ("TOPPADDING",   (0,0),(-1,-1), 2),
            ("BOTTOMPADDING",(0,0),(-1,-1), 2),
            ("LEFTPADDING",  (0,0),(-1,-1), 4),
            ("RIGHTPADDING", (0,0),(-1,-1), 4),
        ]))

        title_row = Table([[
            Paragraph(f"<b>{idx}. {name}</b>", _ps("an", font=FONT_B, size=10)),
            badge,
        ]], colWidths=[W - 2*PAD - 28 - 38, 38])
        title_row.setStyle(TableStyle([
            ("VALIGN",       (0,0),(-1,-1),"MIDDLE"),
            ("LEFTPADDING",  (0,0),(-1,-1), 0),
            ("RIGHTPADDING", (0,0),(-1,-1), 0),
            ("TOPPADDING",   (0,0),(-1,-1), 0),
            ("BOTTOMPADDING",(0,0),(-1,-1), 0),
        ]))

        # 권장 bullet
        rec_text = _get_recommendation(name, reason)
        bullets  = [Paragraph(f"• {b}", ST["bullet"]) for b in rec_text[:2]]

        card_inner = [title_row, Spacer(1, 4)] + bullets
        story.append(_wrap_in_card(card_inner))
        story.append(Spacer(1, 3 * mm))

    # 권장 항목이 부족하면 즉시 대응 행동 카드 추가
    if len(alert_items) < 2:
        imm_bullets = [Paragraph(f"• {a}", ST["bullet"]) for a in actions["immediate"]]
        sht_bullets = [Paragraph(f"• {a}", ST["bullet"]) for a in actions["short"]]
        card_inner = [
            Paragraph("즉시 대응 (24시간 이내)", ST["bold"]),
            Spacer(1, 4),
        ] + imm_bullets + [
            Spacer(1, 4),
            Paragraph("단기 대응 (3일 이내)", ST["bold"]),
            Spacer(1, 4),
        ] + sht_bullets
        story.append(_wrap_in_card(card_inner))
        story.append(Spacer(1, 3 * mm))

    # ── 추가 참고 사항 ──
    note_inner = [
        Paragraph("추가 참고 사항", ST["bold"]),
        Spacer(1, 4),
        Paragraph("• 위 분석은 업로드 전 사전 검토 기준이며, 플랫폼 정책 변경에 따라 리스크 수준이 달라질 수 있습니다.",
                  ST["body_sm"]),
        Paragraph("• 중요한 이슈가 발생할 경우 즉시 콘텐츠 수정 또는 비공개 전환을 권장합니다.",
                  ST["body_sm"]),
    ]
    story.append(_wrap_in_card(note_inner))
    story.append(Spacer(1, 4 * mm))

    # ── CTA 배너 ──
    story.append(_build_cta_banner())

    return story


def _get_recommendation(name: str, reason: str) -> list:
    """항목명 + reason으로 권장 수정 사항 생성 (간단 규칙 기반)"""
    recs = {
        "정치·이념 리스크":         ["정치적 단정 표현을 완화하거나 복수 시각을 제시해 주세요.",
                                     "진영 프레이밍 표현은 삭제하거나 중립 표현으로 대체해 주세요."],
        "특정 인물·집단 낙인 리스크": ["실명 언급 시 사실 확인된 내용만 포함해 주세요.",
                                     "부정적 프레이밍의 반복 표현을 제거해 주세요."],
        "사생활·윤리 리스크":        ["개인 정보·가족사 관련 언급은 당사자 동의 여부를 확인해 주세요.",
                                     "사적 정보 공개가 불필요한 부분은 편집해 주세요."],
        "이미지 역반전 리스크":       ["기존 콘텐츠 방향성과 상충되는 표현을 재검토해 주세요.",
                                     "위선 프레임으로 소비될 수 있는 장면을 삭제하거나 맥락 설명을 추가해 주세요."],
        "맥락 절단·클립화 리스크":   ["자극적인 문구나 과장된 표현은 오해를 유발할 수 있습니다.",
                                     "실제 내용과 일치하는 썸네일/제목으로 신뢰도를 높여주세요."],
        "갈등 증폭 리스크":           ["편가르기 구조의 표현을 중립적으로 수정해 주세요.",
                                     "논쟁 유도 표현 대신 정보 전달 중심으로 구성해 주세요."],
        "밈화·조롱 소비 리스크":      ["어색한 표현이나 극단적 리액션은 편집하거나 자막으로 보완해 주세요.",
                                     "의도치 않게 클립 소비될 수 있는 구간을 재검토해 주세요."],
        "브랜드 세이프티 리스크":     ["광고주 비친화적 표현(과도한 욕설, 자극적 분위기)을 완화해 주세요.",
                                     "브랜드 이미지와 충돌하는 요소를 제거하거나 수위를 조절해 주세요."],
        "감정 선동 리스크":           ["분노 유도 구조의 표현을 완화하거나 팩트 중심으로 재구성해 주세요.",
                                     "과격한 단정 표현 대신 '~한 경향이 있다' 식의 완화 표현을 사용해 주세요."],
        "기존 논란 결합 리스크":      ["과거 논란과 연결될 수 있는 소재나 표현을 삭제해 주세요.",
                                     "'원래 저랬다' 소비 구조를 방지하기 위해 맥락 설명을 추가해 주세요."],
        "괴롭힘·혐오·차별 표현 리스크": ["인신공격·집단 비하 표현을 삭제하거나 중립 표현으로 교체해 주세요.",
                                     "지속적 비하 발언이 포함된 구간은 편집해 주세요."],
        "폭력·위협·불법행위 리스크":  ["폭력 묘사나 위협 발언이 포함된 구간을 삭제해 주세요.",
                                     "위험행동을 조장하는 표현은 반드시 편집하거나 경고 문구를 추가해 주세요."],
        "저작권 리스크":             ["음악·영상·이미지·폰트·로고의 사용 권한과 출처를 확인해 주세요.",
                                     "권리 확인이 안 된 구간은 교체하거나 삭제해 주세요."],
        "성적 표현·대상화 리스크":    ["성적 암시나 신체 대상화 표현은 삭제하거나 수위를 낮춰 주세요.",
                                     "선정성이 높은 구간은 편집하거나 연령 제한 설정을 검토해 주세요."],
        "광고친화성 정책 리스크":     ["광고 협찬 표기 여부를 반드시 확인해 주세요.",
                                     "과도한 욕설이나 브랜드 세이프티 충돌 요소를 완화해 주세요."],
    }
    return recs.get(name, [
        "해당 리스크 요소를 재검토하고 필요 시 수정해 주세요.",
        reason[:60] + ("…" if len(reason) > 60 else ""),
    ])


def _build_cta_banner() -> Table:
    inner = [
        Paragraph("<b>더 안전한 콘텐츠 운영을 위한</b>", _ps("ct1", font=FONT_B, size=12, color=C["primary"])),
        Paragraph("<b>리스크 체크가 필요하신가요?</b>", _ps("ct2", font=FONT_B, size=12, color=C["primary"])),
        Spacer(1, 4),
        Paragraph("NATAM은 업로드 전 잠재 리스크를 미리 분석하여",  ST["body_sm"]),
        Paragraph("콘텐츠가 더 안전하게 사랑받을 수 있도록 도와드립니다.", ST["body_sm"]),
        Spacer(1, 4),
        Paragraph("natam.ai", _ps("url", font=FONT_B, size=9, color=C["blue"])),
    ]
    t = Table([[inner]], colWidths=[W - 2*PAD - 2])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), colors.HexColor("#F0F4FF")),
        ("BOX",          (0,0),(-1,-1), 0.5, colors.HexColor("#BFCFEF")),
        ("TOPPADDING",   (0,0),(-1,-1), 12),
        ("BOTTOMPADDING",(0,0),(-1,-1), 12),
        ("LEFTPADDING",  (0,0),(-1,-1), 14),
        ("RIGHTPADDING", (0,0),(-1,-1), 14),
        ("VALIGN",       (0,0),(-1,-1), "TOP"),
    ]))
    return t


# ════════════════════════════════════════════════════════
# 🦶 푸터 콜백
# ════════════════════════════════════════════════════════

def _footer_callback(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT_R, 7)
    canvas.setFillColor(C["secondary"])
    footer_text = "본 분석은 자동화와 전문 리뷰를 기반으로 제공되며, 최종 판단과 책임은 콘텐츠 운영자에게 있습니다."
    canvas.drawCentredString(W / 2, 12 * mm, footer_text)
    canvas.restoreState()


# ════════════════════════════════════════════════════════
# 📑  상세 리포트 페이지 (MD 리포트 전체 정보 반영)
# ════════════════════════════════════════════════════════

# 논란 라벨 정의 (MD 부록 A)
_LABEL_DEFS = {
    "L01": ("직접적 욕설", "명시적 비속어·욕설 포함 발언"),
    "L02": ("인신공격/비하", "특정인·집단을 대상으로 한 비하 표현"),
    "L03": ("혐오 표현", "성별·인종·종교 등 기반 혐오 발언"),
    "L04": ("허위 정보", "사실과 다른 정보의 의도적·비의도적 유포"),
    "L05": ("기만 행위", "광고 미표기, 뒷광고 등 시청자 기만"),
    "L06": ("위험/자해 행동", "신체적 위험을 초래하거나 조장하는 콘텐츠"),
    "L07": ("정치적 편향", "특정 정치 성향의 일방적 주장 또는 선동"),
    "L08": ("사생활 침해", "동의 없는 개인정보·사생활 노출"),
    "L09": ("성적 불쾌감", "성적 발언·표현으로 인한 불쾌감 유발"),
    "L10": ("저작권 위반", "허가 없는 타인 저작물 사용"),
    "L11": ("피해자 조롱", "사건·사고 피해자를 대상으로 한 조롱"),
    "L12": ("해당 없음", "위 유형에 해당하지 않음"),
}

# 확산 단계별 위기 전망 (MD §3-3, 정적)
_STAGE_OUTLOOK = [
    ("Early (현재)", "소수 시청자 인지, 제한적 공유", "조회수 완만한 상승, 댓글 소수"),
    ("Mid", "커뮤니티 확산, 미디어 관심", "조회수 급등, 외부 링크 유입 증가"),
    ("Late", "대중 인지, 추가 확산 정체", "조회수 정체, 기존 구독자 이탈"),
]


def _detail_title(text: str, color=None) -> Paragraph:
    """상세 섹션 소제목"""
    return Paragraph(f"<b>{text}</b>",
                     _ps("dt", font=FONT_B, size=12,
                         color=color or C["primary"], space_before=4, space_after=4))


def _detail_sub(text: str) -> Paragraph:
    return Paragraph(f"<b>{text}</b>",
                     _ps("ds", font=FONT_B, size=9.5, color=C["accent"],
                         space_before=6, space_after=3))


def _para_cell(text, style=None, bold=False):
    st = style or (ST["bold_sm"] if bold else ST["body_sm"])
    return Paragraph(_safe(text, "—"), st)


def _data_table(headers: list, rows: list, col_ratios: list = None,
                header_bg=None) -> Table:
    """
    헤더 + 여러 행의 데이터 테이블 (자동 줄바꿈 Paragraph 셀).
    col_ratios: 각 열 폭 비율 (합=1). 미지정 시 균등 분할.
    """
    avail = W - 2 * PAD - 4
    n = len(headers)
    ratios = col_ratios or [1.0 / n] * n
    col_w = [avail * r for r in ratios]

    head_style = _ps("th", font=FONT_B, size=8, color=colors.white)
    data = [[Paragraph(f"<b>{h}</b>", head_style) for h in headers]]
    for row in rows:
        data.append([c if hasattr(c, "wrap") else _para_cell(c) for c in row])

    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), header_bg or C["header_bg"]),
        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#F7F7F5")]),
        ("BOX",          (0, 0), (-1, -1), 0.5, C["card_border"]),
        ("INNERGRID",    (0, 0), (-1, -1), 0.4, C["divider"]),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _kv_table(pairs: list, key_ratio: float = 0.32) -> Table:
    """key-value 2열 테이블 (기본 정보/파라미터용)"""
    avail = W - 2 * PAD - 4
    kw, vw = avail * key_ratio, avail * (1 - key_ratio)
    data = [[Paragraph(f"<b>{k}</b>", ST["bold_sm"]), _para_cell(v)]
            for k, v in pairs]
    t = Table(data, colWidths=[kw, vw])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (0, -1), colors.HexColor("#F7F7F5")),
        ("BOX",          (0, 0), (-1, -1), 0.5, C["card_border"]),
        ("INNERGRID",    (0, 0), (-1, -1), 0.4, C["divider"]),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _detail_header(page_label: str, icon: str, title: str, subtitle: str,
                   icon_color=None) -> list:
    return [
        PageBreak(),
        HeaderBanner(0, subtitle="콘텐츠 업로드 전 리스크 체크", label=page_label),
        Spacer(1, 6 * mm),
        SectionTitle(icon, title, subtitle, icon_color=icon_color),
        Spacer(1, 4 * mm),
    ]


def _detail_deps():
    """
    상세 렌더링에 필요한 데이터/헬퍼를 mvp_ver_1_9_2에서 지연 로드.
    (mvp가 pdf_report_generator를 import하므로 순환 방지 위해 함수 내부에서 import)
    로드 실패 시에도 PDF가 깨지지 않도록 안전한 폴백을 제공한다.
    """
    try:
        import mvp_ver_1_9_2 as _m
        return {
            "STAGE_DESC":    getattr(_m, "_SPREAD_STAGE_DESC", {}),
            "ACTIONS":       getattr(_m, "_DEFAULT_ACTIONS", {}),
            "worst_fx":      getattr(_m, "_rp_worst_fx", lambda a: "—"),
            "parse_pattern": getattr(_m, "_rp_parse_pattern", lambda t: ("—", "—", "—")),
            "priority_matrix": getattr(_m, "_build_priority_matrix", lambda r: None),
            "WHISPER_MODEL":       getattr(_m, "WHISPER_MODEL", "—"),
            "GEN_MODEL":           getattr(_m, "GEN_MODEL", "—"),
            "GEMINI_REFINE_MODEL": getattr(_m, "GEMINI_REFINE_MODEL", "—"),
            "EMBED_MODEL":         getattr(_m, "EMBED_MODEL", "—"),
        }
    except Exception:
        return {
            "STAGE_DESC": {}, "ACTIONS": {},
            "worst_fx": lambda a: "—",
            "parse_pattern": lambda t: ("—", "—", "—"),
            "priority_matrix": lambda r: None,
            "WHISPER_MODEL": "—", "GEN_MODEL": "—",
            "GEMINI_REFINE_MODEL": "—", "EMBED_MODEL": "—",
        }


def _build_detail_pages(report: dict) -> list:
    """MD 리포트의 모든 상세 정보를 PDF 페이지로 렌더링."""
    _dep = _detail_deps()
    _SPREAD_STAGE_DESC = _dep["STAGE_DESC"]
    _DEFAULT_ACTIONS   = _dep["ACTIONS"]
    _rp_worst_fx       = _dep["worst_fx"]
    _rp_parse_pattern  = _dep["parse_pattern"]
    WHISPER_MODEL       = _dep["WHISPER_MODEL"]
    GEN_MODEL           = _dep["GEN_MODEL"]
    GEMINI_REFINE_MODEL = _dep["GEMINI_REFINE_MODEL"]
    EMBED_MODEL         = _dep["EMBED_MODEL"]

    meta   = report.get("meta", {})
    spread = report.get("spread_stage", {})
    cls    = report.get("classification", {})
    rule   = report.get("rule_scan", {})
    cases  = report.get("similar_cases", [])
    ta     = report.get("transcript_analysis", [])
    claims = (report.get("claim_check", {}) or {}).get("claims", [])
    kf     = report.get("keyframe_analysis", [])
    wa     = report.get("worst_actions", [])
    tf     = report.get("transcript_files", {})
    yt     = report.get("youtube_meta", {})
    natam  = report.get("natam_risk", {})
    stage  = spread.get("stage", "Unknown")

    story = []

    # ─────────────────────────────────────────────
    # 상세 1 — 사건 요약 & 전사 분석 (MD §1)
    # ─────────────────────────────────────────────
    story += _detail_header("상세 1 · 사건 요약", "📄", "사건 요약 & 전사 분석",
                            "영상 기본 정보와 자막에서 감지된 주요 발언입니다.")

    story.append(_detail_sub("기본 정보"))
    story.append(_kv_table([
        ("사건명",           meta.get("incident_title") or meta.get("video_filename")),
        ("영상 길이",         f"{_safe(meta.get('duration_sec'))}초"),
        ("전사 세그먼트 수",   f"{_safe(meta.get('total_segments'))}개"),
        ("Gemini 교정 횟수",  f"{_safe(meta.get('total_corrected'))}개"),
        ("주요 논란 유형",     cls.get("primary")),
        ("확산 단계",         stage),
        ("분석 생성 시각",     meta.get("analyzed_at")),
        ("분석 소요 시간",     f"{_safe(meta.get('elapsed_sec'))}초"),
    ]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("사건 개요"))
    summary_text = (meta.get("incident_summary") or meta.get("input_query")
                    or "자막 전체 텍스트 기반 자동 분석 결과입니다.")
    story.append(_wrap_in_card([Paragraph(_safe(summary_text), ST["body"])]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("자막에서 감지된 주요 발언"))
    ta_rows = [s for s in ta if not str(s.get("label", "")).startswith(("L12", "L04"))]
    if ta_rows:
        rows = [[str(i), _safe(s.get("timestamp")),
                 _safe(s.get("corrected_text") or s.get("text")),
                 _safe(s.get("label")), _safe(s.get("reason"))]
                for i, s in enumerate(ta_rows, 1)]
        story.append(_data_table(
            ["#", "타임스탬프", "발언 내용", "논란 라벨", "판단 근거"],
            rows, col_ratios=[0.05, 0.13, 0.42, 0.14, 0.26]))
    else:
        story.append(Paragraph("감지된 주요 발언이 없습니다.", ST["body_sm"]))

    # ─────────────────────────────────────────────
    # 상세 2 — 위험 분석 (MD §2)
    # ─────────────────────────────────────────────
    story += _detail_header("상세 2 · 위험 분석", "⚠", "위험 분석",
                            "논란 유형·룰 엔진·키프레임·검증 필요 주장을 종합합니다.",
                            icon_color=C["ALERT"])

    story.append(_detail_sub(f"논란 유형 분류 — 주요 유형: {_safe(cls.get('primary'))}"))
    labels = cls.get("labels", [])
    if labels:
        lrows = []
        for lb in labels:
            code = str(lb).split()[0] if lb else ""
            desc = _LABEL_DEFS.get(code, ("", _safe(lb)))[1]
            lrows.append([_safe(lb), desc])
        story.append(_data_table(["감지된 라벨", "설명"], lrows, col_ratios=[0.3, 0.7]))
    story.append(Spacer(1, 2 * mm))
    story.append(_wrap_in_card([
        Paragraph("판단 근거", ST["label"]),
        Paragraph(_safe(cls.get("reason")), ST["body_sm"]),
    ]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("룰 엔진 스캔 결과"))
    rule_hit = rule.get("hit", False)
    story.append(_kv_table([
        ("상태",     "🚨 키워드 적발" if rule_hit else "✅ 즉각 위험 없음"),
        ("정책",     rule.get("policy", "해당 없음")),
        ("심각도",   rule.get("severity", "—")),
        ("적발 단어", rule.get("matched_word", "—")),
        ("권고 조치", rule.get("action", "—")),
    ]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("영상 키프레임 분석"))
    if kf:
        krows = [[f"Point {i} ({_safe(k.get('time'))})", _safe(k.get('tag'))]
                 for i, k in enumerate(kf, 1)]
        story.append(_data_table(["시점", "관찰 요소"], krows, col_ratios=[0.32, 0.68]))
        story.append(Paragraph(
            "※ 키프레임 분석은 객관적 시각 요소만 기술하며 위험도 판단을 포함하지 않습니다.",
            ST["body_sm"]))
    else:
        story.append(Paragraph("키프레임 분석 데이터가 없습니다 (텍스트/오디오 분석).", ST["body_sm"]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("종합 위험 신호"))
    has_dr = any(str(s.get("label", "")).startswith(("L01", "L02", "L03")) for s in ta)
    has_vr = any(any(kw in _safe(k.get("tag", "")) for kw in ["욕설", "폭력", "노출", "위험"])
                 for k in kf)
    story.append(_data_table(
        ["신호", "상태"],
        [["직접 발화 위험 (자막 L01~L03)", "🚨 감지됨" if has_dr else "✅ 없음"],
         ["영상 시각 위험 (키프레임)",       "🚨 감지됨" if has_vr else "✅ 없음"],
         ["키워드 룰 위험 (룰 엔진)",        "🚨 적발됨" if rule_hit else "✅ 없음"],
         ["외부 확산 신호 (외부 링크/유입)",  "✅ 없음"]],
        col_ratios=[0.6, 0.4]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub(f"검증 필요 주장 (L04 사실확인) — {len(claims)}건"))
    story.append(Paragraph(
        "⚠️ 아래는 '검증이 필요한 주장'을 식별한 것이며, 거짓으로 판정한 것이 아닙니다. "
        "진위 확인은 사람 검토 또는 별도 팩트체크가 필요합니다.", ST["body_sm"]))
    if claims:
        _cw = {"HIGH": "🔴 높음", "MEDIUM": "🟡 중간", "LOW": "🟢 낮음"}
        crows = [[str(i), _safe(c.get("timestamp")), _safe(c.get("claim")),
                  f"{_safe(c.get('domain'))} / {_cw.get(c.get('checkworthiness'), _safe(c.get('checkworthiness')))}",
                  _safe(c.get("reason"))]
                 for i, c in enumerate(claims, 1)]
        story.append(_data_table(
            ["#", "타임스탬프", "검증 대상 주장", "도메인/검증가치", "검증 필요 이유"],
            crows, col_ratios=[0.05, 0.12, 0.4, 0.18, 0.25]))
    else:
        story.append(Paragraph("검증이 필요한 사실 주장이 없습니다.", ST["body_sm"]))

    # ─────────────────────────────────────────────
    # 상세 3 — 확산 단계 & 최악의 행동 (MD §3, §4)
    # ─────────────────────────────────────────────
    story += _detail_header("상세 3 · 확산 단계", "📈", "확산 단계 & 최악의 행동",
                            "현재 확산 단계 판정 근거와 회피할 대응 패턴입니다.",
                            icon_color=C["blue"])

    story.append(_detail_sub(f"현재 단계: {stage}"))
    story.append(Paragraph(_safe(_SPREAD_STAGE_DESC.get(stage, "—")), ST["body"]))
    story.append(Spacer(1, 3 * mm))

    story.append(_detail_sub("판정 근거"))
    is_yt = spread.get("source") == "youtube_meta"
    metrics = spread.get("metrics") or {}
    if is_yt and metrics:
        cvr = metrics.get("cvr", 0.0)
        svr = metrics.get("sub_view_ratio", 0.0)
        srows = [
            ["조회수 (실측)", f"{metrics.get('view_count', 0):,}회", "—",
             f"좋아요 {metrics.get('like_count', 0):,}회 / 좋아요율 {metrics.get('like_ratio', 0):.1f}%"],
            ["댓글 수 (실측)", f"{metrics.get('comment_count', 0):,}건", "—", "—"],
            ["댓글/조회 비율(CVR)", f"{cvr:.2f}%", "≥ 5% → Mid",
             "⚠️ 집중 포화 감지" if cvr >= 5.0 else "✅ 일반 범위"],
            ["구독자 대비 조회수", f"{svr:.0f}%", "≥ 200% → Mid",
             "⚠️ 외부 유입 가능성" if svr >= 200 else "✅ 정상"],
        ]
        story.append(_data_table(["지표", "수치", "임계값", "평가"], srows,
                                  col_ratios=[0.28, 0.22, 0.2, 0.3]))
    else:
        story.append(Paragraph(
            "로컬 영상 분석 시 유튜브 실측 지표를 수집할 수 없습니다.", ST["body_sm"]))
    story.append(Spacer(1, 2 * mm))
    reasons = spread.get("reasons", ["지표 데이터 없음 → 기본값 적용"])
    story.append(_wrap_in_card(
        [Paragraph("판정 사유", ST["label"])] +
        [Paragraph(f"• {_safe(r)}", ST["bullet"]) for r in reasons]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("단계별 위기 전망"))
    story.append(_data_table(
        ["단계", "예상 시나리오", "주요 징후"],
        [[a, b, c] for a, b, c in _STAGE_OUTLOOK],
        col_ratios=[0.2, 0.42, 0.38]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("최악의 행동 리스트 (회피 권장)"))
    story.append(Paragraph(
        "⚠️ 아래는 과거 유사 사례에서 위기를 심화시킨 것으로 관찰된 대응 패턴입니다 "
        "(권고·금지가 아닌 객관적 관찰).", ST["body_sm"]))
    if wa:
        warows = [[str(i), _safe(a), _safe(_rp_worst_fx(a))]
                  for i, a in enumerate(wa[:6], 1)]
        story.append(_data_table(["순위", "금기 행동", "예상되는 역효과"], warows,
                                  col_ratios=[0.08, 0.46, 0.46]))
    else:
        story.append(Paragraph("해당 데이터가 없습니다.", ST["body_sm"]))

    # ─────────────────────────────────────────────
    # 상세 4 — 유사 사례 & NATAM 전체 (MD §5, §6)
    # ─────────────────────────────────────────────
    story += _detail_header("상세 4 · 유사 사례", "🔍", "유사 사례 & NATAM 전체 평가",
                            "검색된 유사 사례와 A·B축 전체 항목 평가입니다.")

    story.append(_detail_sub("검색된 유사 사례 Top 3"))
    if cases:
        for i, cs in enumerate(cases[:3], 1):
            _risk = cs.get("리스크") or cs.get("controversy_type") or "—"
            _tag  = cs.get("세부 태그") or ""
            _src  = f"{cs.get('언론사','')} {cs.get('기사 작성일','')}".strip() or "—"
            _rows = [
                (f"사례 {i}", _safe(cs.get("제목") or cs.get("title"))),
                ("리스크", _safe(_risk + (f" · {_tag}" if _tag else ""))),
                ("유사도 거리", f"{_safe(cs.get('distance'))} (낮을수록 유사)"),
                ("리스크 포인트", _safe(cs.get("리스크 포인트") or "—")),
                ("관련 법·정책", _safe(cs.get("관련 법 및 정책") or "—")),
                ("출처", _safe(_src)),
            ]
            _link = (cs.get("뉴스 링크") or "").strip()
            if _link:
                _rows.append(("원문", _link))
            story.append(_kv_table(_rows))
            story.append(Spacer(1, 3 * mm))
    else:
        story.append(Paragraph("검색된 유사 사례가 없습니다.", ST["body_sm"]))
    story.append(Spacer(1, 2 * mm))

    story.append(_detail_sub("공통 패턴 요약"))
    p_s, p_r, p_t = _rp_parse_pattern(report.get("pattern_summary", ""))
    story.append(_wrap_in_card([
        Paragraph("① 위기 확산의 공통 경로", ST["bold_sm"]),
        Paragraph(_safe(p_s), ST["body_sm"]), Spacer(1, 3),
        Paragraph("② 대응 방식별 여론 반응 패턴", ST["bold_sm"]),
        Paragraph(_safe(p_r), ST["body_sm"]), Spacer(1, 3),
        Paragraph("③ 위기 심화의 결정적 트리거", ST["bold_sm"]),
        Paragraph(_safe(p_t), ST["body_sm"]),
    ]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub(f"NATAM A축 — 커뮤니티 리스크 (종합: {_safe(natam.get('overall_a'))})"))
    arows = [[key, name,
              LEVEL_LABEL.get(natam.get("A", {}).get(key, {}).get("level", "SAFE"),
                              natam.get("A", {}).get(key, {}).get("level", "SAFE")),
              _safe(natam.get("A", {}).get(key, {}).get("reason"))]
             for key, name in NATAM_A_NAMES.items()]
    story.append(_data_table(["ID", "항목명", "단계", "판단 근거"], arows,
                             col_ratios=[0.08, 0.28, 0.1, 0.54]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub(f"NATAM B축 — 플랫폼 리스크 (종합: {_safe(natam.get('overall_b'))})"))
    brows = [[key, name,
              LEVEL_LABEL.get(natam.get("B", {}).get(key, {}).get("level", "SAFE"),
                              natam.get("B", {}).get(key, {}).get("level", "SAFE")),
              _safe(natam.get("B", {}).get(key, {}).get("reason"))]
             for key, name in NATAM_B_NAMES.items()]
    story.append(_data_table(["ID", "항목명", "단계", "판단 근거"], brows,
                             col_ratios=[0.08, 0.28, 0.1, 0.54]))

    # ─────────────────────────────────────────────
    # 상세 5 — 대응 & 부록 (MD §7, 부록)
    # ─────────────────────────────────────────────
    story += _detail_header("상세 5 · 대응 & 부록", "✅", "추천 대응 & 부록",
                            "단계별 대응 타임라인과 라벨 정의·분석 파라미터입니다.",
                            icon_color=C["green"])

    actions = _DEFAULT_ACTIONS.get(stage, _DEFAULT_ACTIONS.get("Unknown", {
        "immediate": ["—", "—"], "short": ["—", "—"], "mid": ["—", "—"]}))
    story.append(_detail_sub("단계별 대응 타임라인"))
    story.append(_wrap_in_card([
        Paragraph("[즉시 — 24시간 이내]", ST["bold_sm"]),
        *[Paragraph(f"□ {a}", ST["bullet"]) for a in actions.get("immediate", [])],
        Spacer(1, 3),
        Paragraph("[단기 — 3일 이내]", ST["bold_sm"]),
        *[Paragraph(f"□ {a}", ST["bullet"]) for a in actions.get("short", [])],
        Spacer(1, 3),
        Paragraph("[중기 — 1주일 이내]", ST["bold_sm"]),
        *[Paragraph(f"□ {a}", ST["bullet"]) for a in actions.get("mid", [])],
    ]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("대응 우선순위 매트릭스"))
    # [정확도 #2] 실제 감지 리스크 기반 매트릭스 (리포트별 상이)
    pm = _dep["priority_matrix"](report)
    if pm:
        story.append(_data_table(
            ["우선순위", "행동", "예상 효과", "리스크"],
            [["🔴 높음", _safe(pm["high"]["action"]), _safe(pm["high"]["effect"]), _safe(pm["high"]["risk"])],
             ["🟡 중간", _safe(pm["mid"]["action"]),  _safe(pm["mid"]["effect"]),  _safe(pm["mid"]["risk"])],
             ["🟢 낮음", _safe(pm["low"]["action"]),  _safe(pm["low"]["effect"]),  _safe(pm["low"]["risk"])]],
            col_ratios=[0.13, 0.32, 0.3, 0.25]))
    else:
        imm = actions.get("immediate", ["—"])
        story.append(_data_table(
            ["우선순위", "행동", "예상 효과", "리스크"],
            [["🔴 높음", _safe(imm[0]), "빠른 대응으로 확산 차단 가능", "섣부른 공개 대응 시 역풍 가능"]],
            col_ratios=[0.13, 0.32, 0.3, 0.25]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("논란 라벨 정의"))
    story.append(_data_table(
        ["라벨 ID", "명칭", "설명"],
        [[code, name, desc] for code, (name, desc) in _LABEL_DEFS.items()],
        col_ratios=[0.12, 0.24, 0.64]))
    story.append(Spacer(1, 4 * mm))

    story.append(_detail_sub("생성 파일 & 분석 파라미터"))
    story.append(_kv_table([
        ("Whisper 원본 전사",  tf.get("raw", "—")),
        ("정규화 전사",        tf.get("normalized", "—")),
        ("Gemini 교정 전사",   tf.get("refined", "—")),
        ("Whisper 모델",       WHISPER_MODEL),
        ("Gemini 분석 모델",    GEN_MODEL),
        ("Gemini 교정 모델",    GEMINI_REFINE_MODEL),
        ("임베딩 모델",         EMBED_MODEL),
    ], key_ratio=0.28))

    return story


# ════════════════════════════════════════════════════════
# 🏗️  메인 빌더
# ════════════════════════════════════════════════════════

_CR_TYPE_LABEL_PDF = {
    "music": "음악", "video_clip": "영상클립", "image": "이미지",
    "logo": "로고", "font": "폰트",
}
_CR_LEVEL_COLOR = {
    "HIGH": "#DC2626", "MEDIUM": "#D97706", "LOW": "#2563EB", "SAFE": "#16A34A",
}


def _cr_score_num(f: dict) -> float:
    try:
        return float(str(f.get("risk_score", "0")).replace("%", ""))
    except (ValueError, TypeError):
        return 0.0


def _build_copyright_pages(report: dict) -> list:
    """
    저작권 침해 분석(copyright_detector) 결과를 PDF 페이지로 렌더링.
    report['copyright'] 가 있을 때만 generate_pdf_report 에서 호출된다(하위호환).
    """
    cr = report.get("copyright")
    story = _detail_header("저작권", "©", "저작권 침해 분석",
                           "음악·영상클립·이미지·로고·폰트 자동 탐지",
                           icon_color=C["blue"])

    if not cr:
        story.append(Paragraph(
            "저작권 분석이 수행되지 않았거나 실패하여 표시할 결과가 없습니다. "
            "(copyright_detector 실행 로그를 확인하세요.)", ST["body"]))
        return story

    s     = cr.get("summary", {})
    level = s.get("overall_risk_level", "SAFE")
    score = s.get("overall_risk_score", 0)
    dur   = cr.get("video_duration", 0)
    dur_s = f"{dur:.0f}초" if isinstance(dur, (int, float)) else _safe(dur)
    lvcol = _CR_LEVEL_COLOR.get(level, "#1A1A1A")

    # ── 8-1 종합 결과 ──
    story.append(_detail_sub("종합 결과"))
    story.append(_kv_table([
        ("전체 저작권 위험도", f"{level} — {score}%"),
        ("발견 항목 수", f"{_safe(s.get('total_issues_found', 0))}건"),
        ("HIGH / MEDIUM",
         f"{_safe(s.get('high_risk_count', 0))}건 / {_safe(s.get('medium_risk_count', 0))}건"),
        ("분석 영상 길이", dur_s),
    ]))
    story.append(Spacer(1, 4 * mm))

    # ── 8-2 유튜브 예측 ──
    yt = s.get("youtube")
    if yt:
        story.append(_detail_sub("유튜브 스튜디오 예측 (저작권 관점)"))
        story.append(_kv_table([
            ("예측 요약", _safe(yt.get("headline"))),
            ("수익화 영향(노란 딱지)", _safe(yt.get("monetization_impact"))),
            ("Content ID 클레임 확률", f"{_safe(yt.get('claim_probability'))}%"),
            ("차단 위험 / Strike 위험",
             f"{_safe(yt.get('block_risk'))} / {_safe(yt.get('strike_risk'))}"),
            ("권고", _safe(yt.get("advice"))),
        ]))
        story.append(Spacer(1, 4 * mm))

    # ── 8-3 분류별 탐지 건수 ──
    by_type = s.get("by_type", {})
    if by_type:
        story.append(_detail_sub("분류별 탐지 건수"))
        rows = [[_CR_TYPE_LABEL_PDF.get(t, t), f"{c}건"] for t, c in by_type.items()]
        story.append(_data_table(["유형", "탐지 건수"], rows, col_ratios=[0.6, 0.4]))
        story.append(Spacer(1, 4 * mm))

    # ── 8-4 위험 타임라인 ──
    story.append(_detail_sub("위험 타임라인 (위험도 높은 순 · 상위 15건)"))
    risky = [f for f in cr.get("timeline", [])
             if f.get("risk_level") in ("HIGH", "MEDIUM")]
    risky.sort(key=lambda f: -_cr_score_num(f))
    rows = []
    for f in risky[:15]:
        lv    = f.get("risk_level", "")
        cxx   = _CR_LEVEL_COLOR.get(lv, "#1A1A1A")
        title = _safe(f.get("title") or f.get("rights_holder"))
        risk_cell = Paragraph(
            f'<font color="{cxx}"><b>{lv} {_safe(f.get("risk_score", ""))}</b></font>',
            ST["bold_sm"])
        rows.append([
            _safe(f.get("timestamp", "00:00")),
            _CR_TYPE_LABEL_PDF.get(f.get("type", ""), f.get("type", "")),
            title,
            risk_cell,
            _safe(f.get("yt_outcome_label")),
        ])
    if not rows:
        rows = [["—", "—", "위험(HIGH/MEDIUM) 등급 항목 없음", "SAFE", "—"]]
    story.append(_data_table(
        ["시점", "유형", "제목/출처", "위험도", "유튜브 예상 조치"],
        rows, col_ratios=[0.12, 0.13, 0.37, 0.18, 0.20]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "※ 음악·영상클립은 Content ID 자동 조치 가능성이 높고, 이미지·로고는 자동 조치 "
        "가능성이 낮습니다. 본 분석은 자동 탐지 결과이며 최종 판단은 권리자·법률 검토가 "
        "필요합니다.", ST["body_sm"]))
    return story


def generate_pdf_report(report: dict, output_dir: str = "reports") -> str:
    """
    report dict → PDF 파일 생성

    Parameters
    ----------
    report     : analyze_video_full() 가 반환하는 report dict
    output_dir : 저장 폴더

    Returns
    -------
    str : 생성된 PDF 파일 경로
    """
    os.makedirs(output_dir, exist_ok=True)

    meta     = report.get("meta", {})
    base     = (meta.get("video_filename") or "report").replace(".", "_").replace(" ", "_")
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"NATAM_REPORT_{base}_{ts}.pdf"
    path     = os.path.join(output_dir, filename)

    doc = SimpleDocTemplate(
        path,
        pagesize    = A4,
        leftMargin  = PAD,
        rightMargin = PAD,
        topMargin   = PAD,
        bottomMargin= 18 * mm,
    )

    story = []
    story += _build_page1(report)
    story += _build_page2(report)
    story += _build_page3(report)
    # 상세 리포트 (MD 리포트의 전체 정보 반영)
    story += _build_detail_pages(report)

    # 저작권 침해 분석 (v1.9.3+) — report에 copyright 키가 있을 때만 렌더
    if "copyright" in report:
        story += _build_copyright_pages(report)

    doc.build(story, onFirstPage=_footer_callback, onLaterPages=_footer_callback)
    print(f"✅ PDF 리포트 생성 완료 → {path}")
    return path


# ════════════════════════════════════════════════════════
# 🧪 독립 실행 테스트
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    dummy_report = {
        "meta": {
            "video_filename":  "00치킨_신메뉴_솔직_리뷰.mp4",
            "analyzed_at":     "2024-06-13 14:32:00",
            "duration_sec":    843,
            "total_segments":  72,
            "total_corrected": 8,
            "incident_title":  "00치킨 신메뉴 솔직 리뷰 영상 리스크 분석",
            "incident_summary": (
                "전반적으로 심각한 리스크는 낮지 않으나, 일부 표현과 맥락에서 오해 소지가 "
                "발생할 수 있는 요소가 확인되었습니다. 아래 주요 리스크 항목을 확인하고 "
                "권장 사항을 반영하시면 콘텐츠 안정성을 높일 수 있습니다."
            ),
        },
        "youtube_meta": {
            "title":            "[HD/60p] 00치킨 신메뉴 솔직 리뷰!",
            "channel":          "솔직리뷰TV",
            "url":              "https://youtube.com/watch?v=EXAMPLE",
            "view_count":       48200,
            "like_count":       1830,
            "comment_count":    312,
            "subscriber_count": 182000,
            "upload_date":      "20240615",
        },
        "spread_stage": {"stage": "Early", "reasons": ["초기 단계"], "metrics": None},
        "classification": {"labels": ["L05 기만 행위"], "primary": "L05", "reason": "광고 미표기 의심"},
        "similar_cases": [
            {"rank":1,"title":"대형 유튜버 뒷광고 대란","controversy_type":"advertising_issue","distance":0.312,"response_pattern":["초반 부인","증거 폭로 후 사과"]},
        ],
        "worst_actions": ["감정적으로 즉각 반박하는 영상 업로드"],
        "transcript_analysis": [],
        "keyframe_analysis":   [],
        "transcript_files":    {},
        "natam_risk": {
            "A": {
                "A-01": {"level": "SAFE",   "reason": "정치적 요소가 감지되지 않음"},
                "A-02": {"level": "CARE",   "reason": "특정 브랜드 비교 표현이 반복됨"},
                "A-03": {"level": "SAFE",   "reason": "사생활 관련 언급 없음"},
                "A-04": {"level": "ALERT",  "reason": "선행 이미지와 상충하는 상업적 표현 존재"},
                "A-05": {"level": "ALERT",  "reason": "자극적 문구가 클립으로 소비될 가능성"},
                "A-06": {"level": "CARE",   "reason": "경쟁 브랜드 언급 시 불필요한 논쟁 발생 가능성"},
                "A-07": {"level": "CARE",   "reason": "과장 표현이 밈으로 소비될 소지"},
                "A-08": {"level": "ALERT",  "reason": "협찬 표기 부재로 광고주 비친화 리스크"},
                "A-09": {"level": "SAFE",   "reason": "감정 선동 요소 감지되지 않음"},
                "A-10": {"level": "SAFE",   "reason": "기존 논란 결합 요소 없음"},
            },
            "B": {
                "B-01": {"level": "SAFE",   "reason": "모욕 표현 없음"},
                "B-02": {"level": "SAFE",   "reason": "폭력·위협 표현 없음"},
                "B-03": {"level": "SAFE",   "reason": "저작권 요소 없음"},
                "B-04": {"level": "SAFE",   "reason": "성적 표현 없음"},
                "B-05": {"level": "ALERT",  "reason": "협찬 표기가 없어 광고 정책 위반 가능성 있음"},
            },
            "overall_a": "ALERT",
            "overall_b": "ALERT",
        },
        "rule_scan": {"hit": False},
        "pattern_summary": "",
    }

    path = generate_pdf_report(dummy_report, output_dir="/mnt/user-data/outputs")
    print(f"저장: {path}")