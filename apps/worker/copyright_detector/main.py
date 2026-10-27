"""
main.py - CLI 진입점
사용법: python main.py video.mp4 [--output ./results]
"""
import argparse
import asyncio
import json
import sys
import os
from pathlib import Path
from datetime import datetime
import structlog

# Windows 콘솔 UTF-8 출력 설정
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 로깅 설정
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ]
)
logger = structlog.get_logger()


def print_banner():
    print("""
╔══════════════════════════════════════════════════════════╗
║         🔍  Copyright Detector v1.0                      ║
║         저작권 침해 자동 감지 시스템                        ║
╚══════════════════════════════════════════════════════════╝
    """)


def print_summary(results: dict):
    """콘솔 결과 출력"""
    summary = results.get("summary", {})
    risk = summary.get("overall_risk_level", "SAFE")
    score = summary.get("overall_risk_score", 0)
    total = summary.get("total_issues_found", 0)

    risk_colors = {
        "HIGH": "\033[91m",    # 빨강
        "MEDIUM": "\033[93m",  # 노랑
        "LOW": "\033[94m",     # 파랑
        "SAFE": "\033[92m",    # 초록
    }
    risk_emojis = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵", "SAFE": "✅"}
    RESET = "\033[0m"
    BOLD = "\033[1m"

    color = risk_colors.get(risk, "")
    emoji = risk_emojis.get(risk, "")

    print(f"\n{'─'*60}")
    print(f"{BOLD}📊 분석 결과 요약{RESET}")
    print(f"{'─'*60}")
    print(f"  영상:      {results.get('video_filename', '')}")
    print(f"  길이:      {results.get('video_duration', 0):.0f}초")
    print(f"  분석시간:  {results.get('processing_time_sec', 0):.1f}초")
    print(f"\n  {BOLD}전체 위험도:{RESET} {color}{BOLD}{emoji} {risk} — {score:.1f}%{RESET}")
    print(f"  발견 항목:  {total}건")
    print(f"    🔴 HIGH:   {summary.get('high_risk_count', 0)}건")
    print(f"    🟡 MEDIUM: {summary.get('medium_risk_count', 0)}건")

    # ── 유튜브 스튜디오 관점 예측 ──
    yt = summary.get("youtube")
    if yt:
        monet_color = {"높음": "\033[91m", "중간": "\033[93m",
                       "낮음": "\033[94m", "없음": "\033[92m"}.get(
                           yt.get("monetization_impact"), "")
        print(f"\n  {BOLD}📺 유튜브 스튜디오 예측(추정):{RESET}")
        print(f"    {yt.get('headline', '')}")
        print(f"    수익화 영향(노란 딱지): {monet_color}{BOLD}{yt.get('monetization_impact')}{RESET}"
              f"   |  Content ID 클레임 확률: {BOLD}{yt.get('claim_probability')}%{RESET}")
        print(f"    차단 위험: {yt.get('block_risk')}   |  저작권 경고(Strike) 위험: {yt.get('strike_risk')}")
        if yt.get("advice"):
            print(f"    💡 {yt.get('advice')}")
    print(f"\n  분류별:")
    for t, c in summary.get("by_type", {}).items():
        type_labels = {
            "music": "🎵 음악",
            "video_clip": "🎬 영상 클립",
            "image": "🖼️ 이미지",
            "logo": "🏷️ 로고",
            "font": "🔤 폰트",
        }
        label = type_labels.get(t, t)
        print(f"    {label}: {c}건")

    print(f"\n{'─'*60}")
    print(f"{BOLD}⏱️ 타임라인 (위험도 높은 순){RESET}")
    print(f"{'─'*60}")

    timeline = results.get("timeline", [])
    high_items = [f for f in timeline if f.get("risk_level") in ("HIGH", "MEDIUM")]
    high_items.sort(key=lambda x: -float(x.get("risk_score", "0").replace("%", "")))

    if high_items:
        for item in high_items[:15]:  # 상위 15개
            ts = item.get("timestamp", "00:00")
            t = item.get("type", "")
            title = item.get("title", "")[:40]
            risk_s = item.get("risk_score", "0%")
            rl = item.get("risk_level", "")
            color = risk_colors.get(rl, "")
            emoji = risk_emojis.get(rl, "")
            type_labels = {
                "music": "🎵", "video_clip": "🎬",
                "image": "🖼️", "logo": "🏷️", "font": "🔤",
            }
            t_emoji = type_labels.get(t, "📌")
            yt_label = item.get("yt_outcome_label", "")
            yt_e = item.get("yt_emoji", "")
            print(f"  [{ts}] {t_emoji} {color}{emoji} {risk_s}{RESET}  {title}")
            if yt_label:
                print(f"          └─ {yt_e} {yt_label} (클레임 {item.get('yt_claim_prob', '?')})")
    else:
        green = "\033[92m"
        print(f"  {green}✅ 높은 위험도 항목 없음{RESET}")

    print(f"{'─'*60}\n")


async def main():
    print_banner()

    parser = argparse.ArgumentParser(
        description="영상 저작권 자동 감지",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py video.mp4
  python main.py video.mp4 --output ./results --job-id MY_JOB_01
  python main.py video.mp4 --format json
  python main.py video.mp4 --stats   (DB 통계 보기)
        """
    )
    parser.add_argument("video", nargs="?", help="분석할 영상 파일 경로")
    parser.add_argument("--output", "-o", default="./results", help="결과 저장 폴더")
    parser.add_argument("--job-id", help="작업 ID (없으면 자동 생성)")
    parser.add_argument("--format", choices=["html", "json", "both"], default="both")
    parser.add_argument("--stats", action="store_true", help="DB 통계 출력")
    parser.add_argument("--force", action="store_true", help="캐시 무시하고 재분석")
    parser.add_argument("--learned", action="store_true",
                        help="자체 학습 데이터 전체 목록 출력 (오학습 검증용)")
    parser.add_argument("--forget", metavar="종류:ID",
                        help="잘못 학습된 항목 삭제 (예: --forget emb:3) "
                             "종류: emb|logo|music|font|meme|clip")
    parser.add_argument("--db-admin", action="store_true",
                        help="브라우저에서 DB 보기·수정·삭제 (로컬 관리 페이지)")

    args = parser.parse_args()

    # DB 관리 웹페이지
    if args.db_admin:
        import subprocess
        admin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tools", "db_admin.py")
        subprocess.run([sys.executable, admin])
        return

    # DB 통계
    if args.stats:
        from database.db_manager import get_db_manager
        db = get_db_manager()
        stats = db.get_stats()
        print("📊 데이터베이스 통계:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return

    # 학습 데이터 목록 (오학습 검증)
    if args.learned:
        from database.db_manager import get_db_manager
        db = get_db_manager()
        learned = db.list_learned_data()
        kind_labels = {
            "emb": "🧠 콘텐츠 임베딩", "logo": "🏷️ 로고", "music": "🎵 음악",
            "font": "🔤 폰트", "meme": "🖼️ 밈", "clip": "🎬 클립",
        }
        total = sum(len(v) for v in learned.values())
        print(f"📚 자체 학습 데이터 (총 {total}건)")
        print("   잘못된 항목 삭제: python main.py --forget 종류:ID\n")
        for kind, entries in learned.items():
            if not entries:
                continue
            print(f"── {kind_labels.get(kind, kind)} ({len(entries)}건) ──")
            for e in entries:
                line = f"  [{kind}:{e['id']}] {e.get('title', '')}"
                if e.get("rights_holder"):
                    line += f" | 권리자: {e['rights_holder']}"
                if e.get("learned_from_job"):
                    ts = e.get("video_timestamp")
                    ts_str = f" {int(ts//60):02d}:{int(ts%60):02d}" if ts is not None else ""
                    line += f" | 출처: Job {e['learned_from_job']}{ts_str}"
                line += f" | 감지 {e.get('detection_count', 1)}회 | {e.get('created_at', '')[:16]}"
                print(line)
            print()
        if total == 0:
            print("  (비어 있음 — 분석에서 출처가 확인되면 자동으로 쌓입니다)")
        return

    # 학습 항목 삭제
    if args.forget:
        from database.db_manager import get_db_manager
        try:
            kind, sid = args.forget.split(":", 1)
            entry_id = int(sid)
        except ValueError:
            print(f"❌ 형식 오류: '{args.forget}' → '종류:ID' 형식 필요 (예: emb:3)")
            sys.exit(1)
        db = get_db_manager()
        if db.delete_learned_entry(kind.strip().lower(), entry_id):
            print(f"✅ 삭제 완료: [{kind}:{entry_id}]")
        else:
            print(f"❌ 항목을 찾을 수 없음: [{kind}:{entry_id}] "
                  f"(--learned 로 ID 확인)")
        return

    if not args.video:
        parser.print_help()
        sys.exit(1)

    video_path = args.video
    if not os.path.exists(video_path):
        print(f"❌ 파일을 찾을 수 없습니다: {video_path}")
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📂 영상: {video_path}")
    print(f"📁 출력: {output_dir}")
    print(f"⏳ 분석 시작...\n")

    # 분석 실행
    from pipeline import analyze_video

    results = await analyze_video(video_path, args.job_id, force_reanalyze=args.force)

    # 결과 출력
    print_summary(results)

    # 저장
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 호출자가 --job-id 를 줬으면 결과 파일명은 반드시 그 값을 쓴다(통합 실행기가 이 이름으로 찾음).
    job_id = args.job_id or results.get("job_id", "unknown")

    saved_files = []

    if args.format in ("json", "both"):
        json_path = output_dir / f"result_{job_id}_{timestamp}.json"
        json_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        saved_files.append(str(json_path))
        print(f"💾 JSON 저장: {json_path}")

    if args.format in ("html", "both"):
        from reports.report_generator import save_report
        html_path = save_report(results, output_dir)
        saved_files.append(html_path)
        print(f"📄 HTML 리포트: {html_path}")

    print(f"\n✅ 완료! 총 {results.get('processing_time_sec', 0):.1f}초")
    return results


if __name__ == "__main__":
    asyncio.run(main())
