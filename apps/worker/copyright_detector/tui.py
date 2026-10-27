import sys
import os
import asyncio
import uuid
import time
import json
import argparse
from pathlib import Path
from typing import List, Optional, Dict

# ── 프로젝트 루트를 Python 경로에 추가 ──
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

# ── Rich 임포트 (없으면 plain 모드) ──
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from rich.text import Text
    from rich.rule import Rule
    from rich.status import Status
    from rich.columns import Columns
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore

# ─────────────────────────────────────────────
# 색상/이모지 상수
# ─────────────────────────────────────────────
RISK_COLORS  = {"HIGH": "red",    "MEDIUM": "yellow", "LOW": "blue",  "SAFE": "green"}
RISK_EMOJIS  = {"HIGH": "🔴",     "MEDIUM": "🟡",     "LOW": "🔵",   "SAFE": "✅"}
RISK_LABELS  = {"HIGH": "위험",   "MEDIUM": "주의",   "LOW": "낮음", "SAFE": "안전"}
TYPE_EMOJIS  = {
    "music":      "🎵",
    "video_clip": "🎬",
    "image":      "🖼️ ",
    "logo":       "🏷️ ",
    "font":       "🔤",
}
TYPE_LABELS  = {
    "music":      "음악",
    "video_clip": "영상 클립",
    "image":      "이미지/사진",
    "logo":       "로고/상표",
    "font":       "폰트",
}


# ─────────────────────────────────────────────
# 배너
# ─────────────────────────────────────────────
def show_banner():
    if _RICH:
        banner = (
            "[bold cyan]  ██████╗ ██████╗ ██████╗ ██╗   ██╗██████╗ ██╗ ██████╗ ██╗  ██╗████████╗[/bold cyan]\n"
            "[bold cyan]  ██╔════╝██╔═══██╗██╔══██╗╚██╗ ██╔╝██╔══██╗██║██╔════╝ ██║  ██║╚══██╔══╝[/bold cyan]\n"
            "[bold cyan]  ██║     ██║   ██║██████╔╝ ╚████╔╝ ██████╔╝██║██║  ███╗███████║   ██║   [/bold cyan]\n"
            "[bold cyan]  ██║     ██║   ██║██╔═══╝   ╚██╔╝  ██╔══██╗██║██║   ██║██╔══██║   ██║   [/bold cyan]\n"
            "[bold cyan]  ╚██████╗╚██████╔╝██║        ██║   ██║  ██║██║╚██████╔╝██║  ██║   ██║   [/bold cyan]\n"
            "[bold cyan]   ╚═════╝ ╚═════╝ ╚═╝        ╚═╝   ╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   [/bold cyan]\n"
            "\n"
            "[dim]         🔍 저작권 자동 감지 시스템 v2.0  |  영상·이미지·음악·로고·폰트 통합 분석[/dim]"
        )
        console.print(Panel(banner, border_style="cyan", padding=(0, 2)))
    else:
        print("=" * 60)
        print("  🔍 저작권 감지기 v2.0")
        print("  영상 · 이미지 · 음악 · 로고 · 폰트 통합 분석")
        print("=" * 60)


# ─────────────────────────────────────────────
# 입력 수집
# ─────────────────────────────────────────────
def get_target_input() -> str:
    """파일 경로 또는 URL 입력"""
    if _RICH:
        console.print("\n[bold]분석 대상을 입력하세요:[/bold]")
        console.print("  [dim]• 로컬 파일: C:\\Users\\...\\video.mp4[/dim]")
        console.print("  [dim]• YouTube:   https://youtu.be/xxxx[/dim]")
        console.print("  [dim]• 기타 URL:  https://...[/dim]\n")
        target = console.input("[cyan]▶ 경로/URL[/cyan]: ").strip().strip('"').strip("'")
    else:
        print("\n분석 대상을 입력하세요 (파일 경로 또는 URL):")
        target = input("▶ 경로/URL: ").strip().strip('"').strip("'")
    return target


def get_own_channels_input() -> List[str]:
    """자기 채널 입력 (오탐 방지)"""
    if _RICH:
        console.print("\n[dim]본인 YouTube 채널명을 입력하면 자신의 영상이 저작권 위반으로 오탐되는 것을 방지합니다.[/dim]")
        raw = console.input(
            "[dim]본인 채널명 (없으면 Enter, 여러 개면 쉼표 구분): [/dim]"
        ).strip()
    else:
        print("\n본인 YouTube 채널명 (없으면 Enter, 여러 개면 쉼표 구분):")
        raw = input("채널명: ").strip()

    if not raw:
        return []
    channels = [c.strip().lstrip('@') for c in raw.split(',') if c.strip()]
    return channels


def is_url(target: str) -> bool:
    return target.startswith("http://") or target.startswith("https://")


def _channels_from_meta(info: Dict) -> List[str]:
    """
    yt-dlp 메타에서 본인채널 필터에 쓸 식별자들을 추출(중복 제거, @ 제거).
    이름/핸들/채널ID를 모두 등록해 URL·엔티티 어느 쪽으로 나오든 매칭되게 한다.
    """
    seen, out = set(), []
    for v in (info.get("channel"), info.get("uploader"),
              info.get("uploader_id"), info.get("channel_id")):
        v = (v or "").strip().lstrip("@")
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


# ─────────────────────────────────────────────
# 다운로드
# ─────────────────────────────────────────────
def download_video_with_progress(url: str):
    """
    URL에서 영상 다운로드 (진행 표시 포함).
    반환: (파일경로, 메타) — 메타 = {"title": 제목, "url": 링크, "channels": [본인채널 후보]}
    """
    from utils.downloader import get_video_info_from_url, download_video

    meta = {"title": "", "url": url, "channels": []}
    if _RICH:
        console.print(f"\n[cyan]🔗 URL 메타데이터 확인 중...[/cyan] {url[:70]}")
        try:
            info = get_video_info_from_url(url)
            meta["title"]    = info.get("title", "")
            meta["url"]      = info.get("webpage_url") or url
            meta["channels"] = _channels_from_meta(info)
            dur  = info.get("duration", 0)
            console.print(
                f"  [green]✓[/green] 제목: [bold]{info.get('title', '?')[:60]}[/bold]\n"
                f"       채널: [dim]{info.get('uploader', '?')}[/dim]  "
                f"길이: [dim]{dur//60}분 {dur%60}초[/dim]"
            )
        except Exception:
            pass

        console.print("\n[cyan]⬇️  영상 다운로드 중...[/cyan]")
    else:
        try:
            info = get_video_info_from_url(url)
            meta["title"]    = info.get("title", "")
            meta["url"]      = info.get("webpage_url") or url
            meta["channels"] = _channels_from_meta(info)
        except Exception:
            pass
        print(f"\n[다운로드 중] {url}")

    download_dir = str(_ROOT / "temp" / "downloads")
    path = download_video(url, output_dir=download_dir)

    if _RICH:
        console.print(f"  [green]✓[/green] 저장 완료: [dim]{path}[/dim]")
    else:
        print(f"저장 완료: {path}")

    return path, meta


# ─────────────────────────────────────────────
# 분석 파이프라인
# ─────────────────────────────────────────────
def _build_summary(findings: List[Dict]) -> Dict:
    if not findings:
        return {
            "overall_risk_level": "SAFE",
            "overall_risk_score": 0.0,
            "total_issues_found": 0,
            "by_type": {},
            "high_count":   0,
            "medium_count": 0,
            "low_count":    0,
        }

    level_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "SAFE": 0}
    best = max(findings, key=lambda x: level_order.get(x.get("risk_level", "SAFE"), 0))

    by_type: Dict[str, int] = {}
    for f in findings:
        t = f.get("finding_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "overall_risk_level": best.get("risk_level", "SAFE"),
        "overall_risk_score": max(f.get("risk_score", 0.0) for f in findings),
        "total_issues_found": len(findings),
        "by_type":      by_type,
        "high_count":   sum(1 for f in findings if f.get("risk_level") == "HIGH"),
        "medium_count": sum(1 for f in findings if f.get("risk_level") == "MEDIUM"),
        "low_count":    sum(1 for f in findings if f.get("risk_level") == "LOW"),
    }


# ─────────────────────────────────────────────
# 결과 표시
# ─────────────────────────────────────────────
def display_results(results: Dict):
    summary  = results["summary"]
    level    = summary["overall_risk_level"]
    score    = summary["overall_risk_score"]
    findings = results["findings"]
    duration = results["video_duration"]
    elapsed  = results["processing_time_sec"]
    fname    = results["video_filename"]

    color = RISK_COLORS.get(level, "white")
    emoji = RISK_EMOJIS.get(level, "")

    if not _RICH:
        _display_plain(results, summary, level, score, findings, elapsed)
        return

    # ── 요약 패널 ──
    dur_str = f"{int(duration // 60)}분 {int(duration % 60)}초"
    by_type_str = "  ".join(
        f"{TYPE_EMOJIS.get(t, '📌')} {TYPE_LABELS.get(t, t)}: {c}건"
        for t, c in summary.get("by_type", {}).items()
    ) or "없음"

    own_ch = results.get("own_channels", [])
    own_str = (f"  [dim]자기 채널 필터: {', '.join(own_ch)}[/dim]\n" if own_ch else "")

    header = (
        f"[bold]{fname}[/bold]\n\n"
        f"  전체 위험도: [{color} bold]{emoji} {level}[/{color} bold]  "
        f"[dim](점수 {score:.2f})[/dim]\n"
        f"  감지 건수:  [bold]{summary['total_issues_found']}건[/bold]  "
        f"([red]🔴 HIGH {summary['high_count']}[/red]  "
        f"[yellow]🟡 MED {summary['medium_count']}[/yellow]  "
        f"[blue]🔵 LOW {summary['low_count']}[/blue])\n\n"
        f"  유형별: {by_type_str}\n"
        f"{own_str}"
        f"  [dim]분석 시간: {elapsed:.1f}초  /  영상 길이: {dur_str}[/dim]"
    )
    console.print()
    console.print(Panel(header, title="[bold cyan]📊 분석 결과[/bold cyan]",
                        border_style=color, padding=(0, 2)))

    if not findings:
        console.print(
            Panel("[green bold]✅ 저작권 문제가 감지되지 않았습니다.[/green bold]",
                  border_style="green")
        )
        return

    # ── 결과 테이블 ──
    table = Table(
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
        expand=True, show_lines=False,
    )
    table.add_column("시각",      style="dim",  width=8,  no_wrap=True)
    table.add_column("유형",      width=12, no_wrap=True)
    table.add_column("위험도",    width=9,  no_wrap=True)
    table.add_column("저작권자",  width=18, no_wrap=True)
    table.add_column("내용",      min_width=30, ratio=1)
    table.add_column("감지 방법", width=18, style="dim", no_wrap=True)

    for f in findings:
        lv   = f.get("risk_level", "SAFE")
        col  = RISK_COLORS.get(lv, "white")
        em   = RISK_EMOJIS.get(lv, "")
        ftype = f.get("finding_type", "?")
        type_str = f"{TYPE_EMOJIS.get(ftype, '📌')} {TYPE_LABELS.get(ftype, ftype)}"
        holder   = (f.get("rights_holder") or "미확인")[:18]
        title    = (f.get("title") or f.get("description") or "")[:70]
        source   = (f.get("source") or "")[:18]
        ts       = f.get("timestamp_display", "?")

        table.add_row(
            ts,
            type_str,
            f"[{col}]{em} {lv}[/{col}]",
            holder,
            title,
            source,
        )

    console.print(table)


def _display_plain(results, summary, level, score, findings, elapsed):
    """Rich 없을 때 plain 텍스트 표시"""
    print("\n" + "=" * 60)
    print(f"  분석 결과: {results['video_filename']}")
    print(f"  전체 위험도: {RISK_EMOJIS.get(level, '')} {level}  (점수 {score:.2f})")
    print(f"  감지 건수: {summary['total_issues_found']}건")
    print(f"  분석 시간: {elapsed:.1f}초")
    print("=" * 60)

    if not findings:
        print("✅ 저작권 문제가 감지되지 않았습니다.")
        return

    print(f"\n{'시각':<8}  {'유형':<12}  {'위험도':<8}  {'저작권자':<18}  내용")
    print("-" * 80)
    for f in findings:
        lv     = f.get("risk_level", "SAFE")
        ftype  = f.get("finding_type", "?")
        holder = (f.get("rights_holder") or "미확인")[:18]
        title  = (f.get("title") or f.get("description") or "")[:40]
        ts     = f.get("timestamp_display", "?")
        print(f"{ts:<8}  {ftype:<12}  {lv:<8}  {holder:<18}  {title}")


# ─────────────────────────────────────────────
# 리포트 저장
# ─────────────────────────────────────────────
def save_report_prompt(results: Dict):
    """리포트 저장 — 묻지 않고 JSON·HTML 둘 다 항상 저장"""
    if _RICH:
        console.print()
        console.rule("[dim]리포트 저장 (JSON + HTML)[/dim]")

    output_dir = _ROOT / "results"
    output_dir.mkdir(parents=True, exist_ok=True)
    # 파일명에 영상 제목 우선 사용(있으면), 없으면 파일명
    from reports.report_generator import safe_filename_part
    stem = (safe_filename_part(results.get("video_title"), max_len=40)
            or Path(results["video_filename"]).stem[:40])
    ts   = time.strftime("%Y%m%d_%H%M%S")

    # 1) JSON 항상 저장
    json_path = output_dir / f"report_{stem}_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    _print_ok(f"JSON 저장: {json_path}")

    # 2) HTML 항상 저장
    try:
        from reports.report_generator import generate_html_report
        html_path = output_dir / f"report_{stem}_{ts}.html"
        # report_generator가 원하는 형식으로 변환
        report_data = {
            **results,
            "findings_by_type": _group_by_type(results["findings"]),
            "timeline":         results["findings"],
        }
        html_content = generate_html_report(report_data)
        html_path.write_text(html_content, encoding="utf-8")
        _print_ok(f"HTML 저장: {html_path}")
    except Exception as e:
        _print_warn(f"HTML 생성 실패: {e}")


def _group_by_type(findings: List[Dict]) -> Dict[str, List[Dict]]:
    result: Dict[str, List[Dict]] = {}
    for f in findings:
        t = f.get("finding_type", "unknown")
        if t not in result:
            result[t] = []
        result[t].append(f)
    return result


def _print_ok(msg: str):
    if _RICH:
        console.print(f"  [green]✓[/green] {msg}")
    else:
        print(f"  ✓ {msg}")


def _print_warn(msg: str):
    if _RICH:
        console.print(f"  [yellow]⚠[/yellow] {msg}")
    else:
        print(f"  ⚠ {msg}")


def _print_step(msg: str):
    if _RICH:
        console.print(f"[cyan]  ⣷ {msg}[/cyan]")
    else:
        print(f"  ⣷ {msg}")


# ─────────────────────────────────────────────
# 진행 상황 표시 래퍼
# ─────────────────────────────────────────────
async def run_with_status(video_path: str, job_id: str, own_channels: List[str],
                          video_meta: Dict = None) -> Dict:
    """분석을 단계별로 실행하며 상태 메시지 출력"""
    video_meta = video_meta or {}
    import logging
    logging.disable(logging.CRITICAL)

    from utils.video_utils           import extract_frames_smart, get_video_info
    from analyzers.video_clip_analyzer import VideoClipAnalyzer
    from analyzers.image_analyzer      import ImageAnalyzer
    from analyzers.music_analyzer      import MusicAnalyzer
    from utils.google_vision_searcher  import set_own_channels, clear_own_channels
    from config import config
    from pipeline import refine_findings, apply_hybrid_scoring

    if own_channels:
        set_own_channels(own_channels)
        if _RICH:
            console.print(
                f"  [dim]🔒 자기 채널 필터 활성화: "
                f"{', '.join('@'+c for c in own_channels)}[/dim]"
            )
    else:
        clear_own_channels()

    start = time.time()
    loop  = asyncio.get_event_loop()

    # 1. 영상 정보
    video_info = get_video_info(video_path)
    dur = video_info.get("duration", 0)
    if _RICH:
        console.print(
            f"  [dim]영상 길이: {int(dur//60)}분 {int(dur%60)}초  "
            f"│  해상도: {video_info.get('width', '?')}×{video_info.get('height', '?')}[/dim]"
        )

    # 2. 프레임 추출 (배치 파이프라인과 동일하게 config 값 사용 → 긴 영상 커버 ↑)
    _max_frames = config.pipeline.frame_max_count
    _print_step(f"프레임 추출 중... (최대 {_max_frames}개)")
    if _RICH:
        with console.status("", spinner="dots"):
            frames = await loop.run_in_executor(
                None,
                lambda: extract_frames_smart(video_path, max_frames=_max_frames)
            )
    else:
        frames = extract_frames_smart(video_path, max_frames=_max_frames)
    _print_ok(f"{len(frames)}개 프레임 추출 완료")

    # 3. 분석기 초기화
    video_analyzer = VideoClipAnalyzer()
    image_analyzer = ImageAnalyzer()
    music_analyzer = MusicAnalyzer()

    # 4. CLIP AI 분류 + 영상 클립 분석
    _print_step("영상 클립 / 이미지 AI 분석 중... (CLIP + Vision API)")
    if _RICH:
        with console.status("", spinner="dots"):
            video_findings, image_findings = await asyncio.gather(
                video_analyzer.analyze(frames, job_id),
                image_analyzer.analyze(frames, job_id),
            )
    else:
        video_findings, image_findings = await asyncio.gather(
            video_analyzer.analyze(frames, job_id),
            image_analyzer.analyze(frames, job_id),
        )
    _print_ok(
        f"영상 {len(video_findings)}건 / 이미지 {len(image_findings)}건 감지"
    )

    # 5. 음악 분석
    _print_step("음악 저작권 분석 중... (ACRCloud / AudD)")
    if _RICH:
        with console.status("", spinner="dots"):
            music_findings = await music_analyzer.analyze(video_path, job_id)
    else:
        music_findings = await music_analyzer.analyze(video_path, job_id)
    _print_ok(f"음악 {len(music_findings)}건 감지")

    logging.disable(logging.NOTSET)

    all_findings = video_findings + image_findings + music_findings
    all_findings = apply_hybrid_scoring(all_findings)  # 유튜브 기준 재점수
    all_findings = refine_findings(all_findings)       # 오탐 억제 후처리 (약한 단일신호 → LOW)
    all_findings.sort(key=lambda x: x.get("timestamp_start", 0))
    elapsed = time.time() - start

    return {
        "job_id":              job_id,
        "video_path":          video_path,
        "video_filename":      Path(video_path).name,
        # 영상 제목·링크 (URL로 받은 경우만 채워짐, 로컬 파일은 파일명이 대체)
        "video_title":         video_meta.get("title") or Path(video_path).stem,
        "video_url":           video_meta.get("url", ""),
        "video_duration":      dur,
        "processing_time_sec": elapsed,
        "own_channels":        own_channels,
        "findings":            all_findings,
        "summary":             _build_summary(all_findings),
    }


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    # ── 인수 파싱 ──
    parser = argparse.ArgumentParser(
        description="저작권 감지기 터미널 UI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python tui.py                               # 대화형 모드
  python tui.py video.mp4                     # 파일 직접 분석
  python tui.py https://youtu.be/xxxx         # URL 분석
  python tui.py video.mp4 --channel 내채널    # 자기 채널 필터
  python tui.py video.mp4 --channel "A,B,C"  # 다수 채널 필터
        """,
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="분석할 파일 경로 또는 영상 URL")
    parser.add_argument("--channel", "-c", default="",
                        help="본인 YouTube 채널명 (쉼표로 여러 개 가능)")
    args = parser.parse_args()

    show_banner()

    # ── 대상 결정 ──
    target = args.target
    if not target:
        target = get_target_input()

    if not target:
        if _RICH:
            console.print("[red]대상을 입력하지 않았습니다. 종료합니다.[/red]")
        else:
            print("대상을 입력하지 않았습니다. 종료합니다.")
        return

    # ── 자기 채널 결정 ──
    #   · --channel 로 명시하면 그걸 최우선.
    #   · URL 입력이면 아래 다운로드 단계에서 링크의 채널을 '자동 감지'해서 사용.
    #   · 로컬 파일이면 링크가 없으니 직접 입력받는다.
    manual_channels = None
    if args.channel:
        manual_channels = [c.strip().lstrip('@') for c in args.channel.split(',') if c.strip()]
    elif not is_url(target):
        manual_channels = get_own_channels_input()

    # ── URL이면 다운로드 (+ 채널 자동 감지) ──
    video_path = target
    video_meta = {"title": "", "url": "", "channels": []}
    if is_url(target):
        try:
            video_path, video_meta = download_video_with_progress(target)
        except Exception as e:
            if _RICH:
                console.print(f"[red]❌ 다운로드 실패: {e}[/red]")
            else:
                print(f"❌ 다운로드 실패: {e}")
            return

    # 본인채널 필터 확정: 수동 지정이 있으면 우선, 없으면 링크에서 감지한 채널 사용
    if manual_channels is not None:
        own_channels = manual_channels
    else:
        own_channels = video_meta.get("channels", [])
        if own_channels:
            _msg = (f"🔎 링크에서 본인 채널 자동 감지: {', '.join(own_channels[:3])} "
                    f"→ 오탐 방지 필터 적용")
            console.print(f"[dim]{_msg}[/dim]") if _RICH else print(_msg)

    # ── 파일 존재 확인 ──
    if not Path(video_path).exists():
        if _RICH:
            console.print(f"[red]❌ 파일을 찾을 수 없습니다: {video_path}[/red]")
        else:
            print(f"❌ 파일을 찾을 수 없습니다: {video_path}")
        return

    job_id = str(uuid.uuid4())[:8]

    if _RICH:
        console.print()
        console.rule(f"[bold cyan]🔍 분석 시작[/bold cyan]")
        console.print(
            f"\n  [bold]파일:[/bold] [dim]{Path(video_path).name}[/dim]  "
            f"│  [bold]Job ID:[/bold] [dim]{job_id}[/dim]\n"
        )
    else:
        print(f"\n[ 분석 시작 ] {Path(video_path).name}  |  Job: {job_id}\n")

    # ── 분석 실행 ──
    try:
        results = asyncio.run(run_with_status(video_path, job_id, own_channels, video_meta))
    except KeyboardInterrupt:
        if _RICH:
            console.print("\n[yellow]⚠ 분석이 중단되었습니다.[/yellow]")
        else:
            print("\n⚠ 분석이 중단되었습니다.")
        return
    except Exception as e:
        if _RICH:
            console.print(f"\n[red]❌ 분석 오류: {e}[/red]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
        else:
            print(f"\n❌ 분석 오류: {e}")
        return

    # ── 결과 표시 ──
    if _RICH:
        console.print()
        console.rule("[bold cyan]📊 결과[/bold cyan]")
    display_results(results)

    # ── 리포트 저장 ──
    save_report_prompt(results)

    if _RICH:
        console.print()
        console.rule()
        console.print(
            f"\n[dim]완료. Job ID: {job_id}  |  "
            f"총 소요 시간: {results['processing_time_sec']:.1f}초[/dim]\n"
        )
    else:
        print(f"\n완료. 총 소요 시간: {results['processing_time_sec']:.1f}초")


if __name__ == "__main__":
    main()
