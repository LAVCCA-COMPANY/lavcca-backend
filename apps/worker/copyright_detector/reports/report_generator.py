"""
reports/report_generator.py - HTML/JSON 리포트 생성
타임라인 시각화 포함
"""
import re
from typing import Dict, List
from pathlib import Path
from datetime import datetime
from config import config


def safe_filename_part(name: str, max_len: int = 60) -> str:
    """
    영상 제목 등을 파일명에 안전하게 쓰도록 정리.
    Windows 금지문자(\\ / : * ? " < > |)와 제어문자를 _로, 공백은 정리.
    """
    if not name:
        return ""
    name = str(name).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)  # 금지·제어문자 → _
    name = re.sub(r"\s+", " ", name)                     # 공백 정리
    name = re.sub(r"_{2,}", "_", name)                   # 연속 밑줄 → 하나
    name = name.strip(" _.")                              # 양끝 밑줄/점/공백 제거
    return name[:max_len].strip(" _.")

RISK_COLORS = {
    "HIGH": "#ef4444",
    "MEDIUM": "#f59e0b",
    "LOW": "#3b82f6",
    "SAFE": "#10b981",
}

RISK_EMOJIS = {
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "SAFE": "✅",
}

TYPE_LABELS_KO = {
    "music": "🎵 음악",
    "video_clip": "🎬 영상 클립",
    "image": "🖼️ 이미지/사진",
    "logo": "🏷️ 로고/상표",
    "font": "🔤 폰트",
}


def generate_html_report(results: Dict) -> str:
    """전체 HTML 리포트 생성"""

    summary = results.get("summary", {})
    risk_level = summary.get("overall_risk_level", "SAFE")
    risk_score = summary.get("overall_risk_score", 0)
    risk_color = RISK_COLORS.get(risk_level, "#10b981")
    risk_emoji = RISK_EMOJIS.get(risk_level, "✅")
    duration = results.get("video_duration", 0)
    duration_str = f"{int(duration//60)}분 {int(duration%60)}초"
    processing_time = results.get("processing_time_sec", 0)
    total_issues = summary.get("total_issues_found", 0)

    # 영상 제목·링크 (있을 때만 표기). 제목 없으면 파일명 사용.
    import html as _html
    video_title = results.get("video_title") or results.get("video_filename", "")
    video_url = results.get("video_url") or ""
    _title_disp = _html.escape(video_title)
    if video_url:
        _url_esc = _html.escape(video_url, quote=True)
        video_link_html = (
            f'&nbsp;|&nbsp; 🔗 <a href="{_url_esc}" target="_blank" '
            f'style="color:#3b82f6;text-decoration:none">영상 링크</a>'
        )
    else:
        video_link_html = ""

    # 타임라인 HTML
    timeline_html = _build_timeline_html(results.get("timeline", []), duration)

    # 타입별 섹션 HTML
    sections_html = _build_sections_html(results.get("findings_by_type", {}))

    # 타입별 통계 바
    by_type = summary.get("by_type", {})
    type_stats_html = "".join([
        f"""<div class="type-stat">
            <span>{TYPE_LABELS_KO.get(t, t)}</span>
            <span class="badge {'badge-red' if c > 0 else 'badge-green'}">{c}건</span>
        </div>"""
        for t, c in by_type.items()
    ])

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>저작권 분석 리포트 — {_title_disp}</title>
<style>
  :root {{
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
    --text: #f1f5f9; --muted: #94a3b8;
    --red: #ef4444; --yellow: #f59e0b; --blue: #3b82f6; --green: #10b981;
    --border: rgba(148,163,184,0.15);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, 'Noto Sans KR', sans-serif; background: var(--bg); color: var(--text); }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px; }}

  /* Header */
  .header {{ background: var(--surface); border-radius: 16px; padding: 28px 32px; margin-bottom: 24px;
             border: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; }}
  .header-title {{ font-size: 22px; font-weight: 700; }}
  .header-sub {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}

  /* Risk Badge */
  .risk-badge {{ padding: 10px 24px; border-radius: 50px; font-size: 18px; font-weight: 700;
                 color: white; background: {risk_color}; box-shadow: 0 0 20px {risk_color}55; }}

  /* Stats Grid */
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .stat-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
               padding: 20px 24px; }}
  .stat-value {{ font-size: 32px; font-weight: 800; }}
  .stat-label {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}

  /* Type Stats */
  .type-stats {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
                padding: 20px 24px; margin-bottom: 24px; }}
  .type-stat {{ display: flex; justify-content: space-between; align-items: center;
               padding: 8px 0; border-bottom: 1px solid var(--border); }}
  .type-stat:last-child {{ border-bottom: none; }}
  .badge {{ padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
  .badge-red {{ background: rgba(239,68,68,0.2); color: var(--red); }}
  .badge-green {{ background: rgba(16,185,129,0.2); color: var(--green); }}
  .badge-learned {{ background: rgba(168,85,247,0.2); color: #c084fc; padding: 2px 8px;
                    border-radius: 20px; font-size: 11px; font-weight: 600; white-space: nowrap; }}

  /* Timeline */
  .timeline-section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
                       padding: 24px; margin-bottom: 24px; }}
  .section-title {{ font-size: 16px; font-weight: 700; margin-bottom: 16px; }}
  .timeline-bar {{ position: relative; height: 48px; background: var(--surface2); border-radius: 8px; overflow: hidden; margin-bottom: 16px; }}
  .timeline-marker {{ position: absolute; height: 100%; min-width: 4px; border-radius: 2px; opacity: 0.85; }}
  .timeline-tooltip {{ display: none; position: absolute; bottom: 120%; left: 50%; transform: translateX(-50%);
                        background: #0f172a; border: 1px solid var(--border); border-radius: 8px;
                        padding: 8px 12px; font-size: 12px; white-space: nowrap; z-index: 10; }}
  .timeline-marker:hover .timeline-tooltip {{ display: block; }}
  .timeline-marker:hover {{ opacity: 1; cursor: pointer; }}

  /* Findings Table */
  .findings-section {{ margin-bottom: 24px; }}
  .findings-header {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px 12px 0 0;
                      padding: 16px 24px; font-weight: 700; font-size: 15px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 0 0 12px 12px;
           overflow: hidden; border: 1px solid var(--border); border-top: none; }}
  th {{ padding: 10px 16px; text-align: left; font-size: 12px; color: var(--muted); text-transform: uppercase;
        background: rgba(51,65,85,0.5); border-bottom: 1px solid var(--border); }}
  td {{ padding: 12px 16px; font-size: 13px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover {{ background: rgba(148,163,184,0.05); }}
  .ts {{ font-family: monospace; font-size: 13px; color: var(--blue); font-weight: 600; }}
  .risk-high {{ color: var(--red); font-weight: 700; }}
  .risk-medium {{ color: var(--yellow); font-weight: 700; }}
  .risk-low {{ color: var(--blue); font-weight: 600; }}
  .risk-safe {{ color: var(--green); }}
  .score-bar {{ width: 60px; height: 6px; background: var(--surface2); border-radius: 3px; display: inline-block; vertical-align: middle; margin-right: 6px; }}
  .score-fill {{ height: 100%; border-radius: 3px; }}
  .footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 32px; }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <div>
      <div class="header-title">🔍 저작권 분석 리포트</div>
      <div class="header-sub">
        🎬 {_title_disp}{video_link_html}
      </div>
      <div class="header-sub">
        ⏱️ {duration_str} &nbsp;|&nbsp;
        🕐 분석 시간: {processing_time:.0f}초 &nbsp;|&nbsp;
        🗂️ Job ID: {results.get('job_id', '')}
      </div>
    </div>
    <div class="risk-badge">{risk_emoji} {risk_level} — {risk_score:.1f}%</div>
  </div>

  <!-- YouTube Studio 관점 예측 -->
  {_build_youtube_panel(summary.get('youtube'))}

  <!-- Stats -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-value" style="color:{risk_color}">{risk_score:.1f}%</div>
      <div class="stat-label">전체 저작권 위험도</div>
    </div>
    <div class="stat-card">
      <div class="stat-value" style="color:var(--red)">{summary.get('high_risk_count', 0)}</div>
      <div class="stat-label">🔴 HIGH 위험 항목</div>
    </div>
    <div class="stat-card">
      <div class="stat-value" style="color:var(--yellow)">{summary.get('medium_risk_count', 0)}</div>
      <div class="stat-label">🟡 MEDIUM 위험 항목</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{total_issues}</div>
      <div class="stat-label">총 발견 항목</div>
    </div>
  </div>

  <!-- Type Stats -->
  <div class="type-stats">
    <div class="section-title">📊 분류별 현황</div>
    {type_stats_html if type_stats_html else '<div style="color:var(--muted)">발견된 저작권 항목 없음 ✅</div>'}
  </div>

  <!-- Timeline -->
  <div class="timeline-section">
    <div class="section-title">⏱️ 타임라인</div>
    {timeline_html}
  </div>

  <!-- Findings by Type -->
  {sections_html}

  <div class="footer">
    <span class="badge-learned">🧠 자체학습</span> 표시 항목은 과거 분석에서 학습된 자체 DB 매칭입니다
    (이번 분석에서 외부 API로 재확인된 것이 아님).<br>
    잘못 학습된 항목은 <code>python main.py --learned</code> 로 확인 후
    <code>python main.py --forget 종류:ID</code> 로 삭제하세요.<br><br>
    생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp;
    Copyright Detector v1.0
  </div>
</div>
</body>
</html>"""

    return html


def _build_youtube_panel(yt: Dict) -> str:
    """유튜브 스튜디오 관점 예측 패널 (수익화 영향/클레임 확률/차단·경고 위험)."""
    if not yt:
        return ""
    monet = yt.get("monetization_impact", "없음")
    monet_color = {"높음": "#ef4444", "중간": "#f59e0b",
                   "낮음": "#3b82f6", "없음": "#10b981"}.get(monet, "#10b981")
    claim = yt.get("claim_probability", 0)

    def _risk_pill(label, val):
        c = {"있음": "#ef4444", "낮음": "#f59e0b", "없음": "#10b981"}.get(val, "#10b981")
        return (f'<div style="flex:1;text-align:center;padding:10px;background:var(--surface2);'
                f'border-radius:8px"><div style="font-size:12px;color:var(--muted)">{label}</div>'
                f'<div style="font-size:16px;font-weight:700;color:{c};margin-top:2px">{val}</div></div>')

    return f"""
  <div style="background:var(--surface);border:1px solid var(--border);border-left:4px solid {monet_color};
              border-radius:12px;padding:24px;margin-bottom:24px">
    <div class="section-title">📺 유튜브 스튜디오 예측 <span style="font-size:12px;color:var(--muted);font-weight:400">(추정치 — 실제 조치는 권리자 정책에 따라 다름)</span></div>
    <div style="font-size:17px;font-weight:700;margin:8px 0 16px">{yt.get('headline', '')}</div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px">
      <div style="flex:1;text-align:center;padding:10px;background:var(--surface2);border-radius:8px">
        <div style="font-size:12px;color:var(--muted)">수익화 영향(노란 딱지)</div>
        <div style="font-size:16px;font-weight:700;color:{monet_color};margin-top:2px">{monet}</div>
      </div>
      <div style="flex:1;text-align:center;padding:10px;background:var(--surface2);border-radius:8px">
        <div style="font-size:12px;color:var(--muted)">Content ID 클레임 확률</div>
        <div style="font-size:16px;font-weight:700;margin-top:2px">{claim}%</div>
      </div>
      {_risk_pill("차단 위험", yt.get("block_risk", "없음"))}
      {_risk_pill("저작권 경고(Strike)", yt.get("strike_risk", "없음"))}
    </div>
    <div style="font-size:13px;color:var(--muted)">💡 {yt.get('advice', '')}</div>
  </div>"""


def _build_timeline_html(timeline: List[Dict], duration: float) -> str:
    if not timeline or duration <= 0:
        return '<div style="color:var(--muted);text-align:center;padding:20px">발견된 항목 없음</div>'

    markers_html = ""
    for item in timeline:
        # findings 딕셔너리 필드명에 맞게 수정
        start_sec = float(item.get("timestamp_start", item.get("timestamp_start_sec", 0)) or 0)
        end_sec   = float(item.get("timestamp_end",   item.get("timestamp_end_sec",   start_sec + 5)) or start_sec + 5)
        risk = item.get("risk_level", "SAFE")
        color = RISK_COLORS.get(risk, "#10b981")
        left_pct = min((start_sec / duration) * 100, 99)
        width_pct = max(1.0, min((end_sec - start_sec) / duration * 100, 5))
        title = item.get("title", "")[:30]
        ts = item.get("timestamp_display", item.get("timestamp", "00:00"))
        type_label = TYPE_LABELS_KO.get(item.get("finding_type", item.get("type", "")), "")

        markers_html += f"""
        <div class="timeline-marker" style="left:{left_pct:.1f}%;width:{width_pct:.1f}%;background:{color}">
          <div class="timeline-tooltip">{ts} | {type_label} | {title}</div>
        </div>"""

    # 시간 눈금
    ticks = ""
    for pct in [0, 25, 50, 75, 100]:
        sec = duration * pct / 100
        label = f"{int(sec//60):02d}:{int(sec%60):02d}"
        ticks += f'<span style="position:absolute;left:{pct}%;font-size:11px;color:var(--muted);transform:translateX(-50%)">{label}</span>'

    return f"""
    <div class="timeline-bar">{markers_html}</div>
    <div style="position:relative;height:20px">{ticks}</div>
    <div style="margin-top:12px;font-size:12px;color:var(--muted)">
      <span style="margin-right:16px">🔴 HIGH</span>
      <span style="margin-right:16px">🟡 MEDIUM</span>
      <span style="margin-right:16px">🔵 LOW</span>
    </div>"""


def _build_sections_html(findings_by_type: Dict) -> str:
    html = ""
    for ftype, findings in findings_by_type.items():
        if not findings:
            continue

        label = TYPE_LABELS_KO.get(ftype, ftype)
        rows = ""
        for f in findings:
            risk = f.get("risk_level", "SAFE")
            risk_class = f"risk-{risk.lower()}"
            risk_emoji = RISK_EMOJIS.get(risk, "")
            # risk_score 는 0~1 범위 float — % 표시용으로 100 곱해서 사용
            raw_score = f.get("risk_score", 0)
            try:
                if isinstance(raw_score, str):
                    raw_score = float(raw_score.replace("%", ""))
                else:
                    raw_score = float(raw_score)
                score_pct = round(raw_score * 100, 1) if raw_score <= 1.0 else round(raw_score, 1)
            except (ValueError, TypeError):
                score_pct = 0.0
            color = RISK_COLORS.get(risk, "#10b981")
            # 타임스탬프 필드명 통일
            ts_display = f.get("timestamp_display", f.get("timestamp", "00:00"))
            rights = f.get("rights_holder", "") or f.get("author", "")

            # 자체 학습 DB에서 나온 finding은 배지로 명확히 구분
            # (외부 API 확인 결과가 아니므로 사용자가 검증 후 오학습이면 --forget으로 삭제)
            src = f.get("source", "")
            if "internal" in src:
                src_html = (f'<span class="badge-learned">🧠 자체학습</span>'
                            f'<div style="font-size:10px;color:var(--muted);margin-top:2px">{src}</div>')
            else:
                src_html = src

            yt_label = f.get("yt_outcome_label", "")
            yt_emoji = f.get("yt_emoji", "")
            yt_claim = f.get("yt_claim_prob", "")
            yt_html = (f'<span style="font-size:12px">{yt_emoji} {yt_label}</span>'
                       f'<div style="font-size:11px;color:var(--muted);margin-top:2px">클레임 {yt_claim}</div>'
                       if yt_label else "")

            rows += f"""<tr>
              <td><span class="ts">{ts_display}</span></td>
              <td>{f.get('title', '')}</td>
              <td style="color:var(--muted);font-size:12px">{rights}</td>
              <td>
                <div class="score-bar"><div class="score-fill" style="width:{score_pct}%;background:{color}"></div></div>
                <span class="{risk_class}">{risk_emoji} {score_pct:.0f}%</span>
              </td>
              <td>{yt_html}</td>
              <td style="color:var(--muted);font-size:12px">{src_html}</td>
              <td style="font-size:12px;color:var(--muted)">{f.get('description', '')[:120]}</td>
            </tr>"""

        html += f"""
        <div class="findings-section">
          <div class="findings-header">{label} <span style="font-size:13px;color:var(--muted);font-weight:400">— {len(findings)}건 발견</span></div>
          <table>
            <thead><tr>
              <th>시간</th><th>제목</th><th>권리자</th>
              <th>위험도</th><th>유튜브 조치(예측)</th><th>소스</th><th>설명</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    if not html:
        html = '<div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:32px;text-align:center;color:var(--green);font-size:18px">✅ 저작권 이슈 없음</div>'

    return html


def save_report(results: Dict, output_dir: Path = None) -> str:
    """HTML 리포트 파일 저장"""
    output_dir = output_dir or config.REPORTS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    job_id = results.get("job_id", "unknown")
    # 파일명에 영상 제목 우선 사용(있으면), 없으면 파일명 → job_id 순 폴백
    name_part = safe_filename_part(
        results.get("video_title")
        or Path(results.get("video_filename", "")).stem
        or job_id
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"copyright_report_{name_part}_{ts}.html" if name_part \
        else f"copyright_report_{job_id}_{ts}.html"
    filepath = output_dir / filename

    html = generate_html_report(results)
    filepath.write_text(html, encoding="utf-8")

    return str(filepath)
