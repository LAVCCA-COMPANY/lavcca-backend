from flask import Flask, request, jsonify, render_template, send_file
import threading, os, uuid, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

import mvp_ver_1_9_3 as v193

mvp = v193.mvp  # v1.9.2 엔진 (v193 내부에서 재사용 중인 모듈)

# 최종 MD 리포트를 웹에서 그대로 보여주기 위한 MD→HTML 변환기
try:
    import markdown as _md
except Exception:
    _md = None


def _report_html(md_path: str) -> str:
    """생성된 최종 MD 리포트를 HTML로 변환(표/코드블록 포함). 실패 시 원문을 <pre>로."""
    try:
        text = Path(md_path).read_text(encoding='utf-8')
    except Exception as e:
        return f'<pre>리포트 파일을 읽을 수 없습니다: {e}</pre>'
    if _md is None:
        import html as _h
        return '<pre>' + _h.escape(text) + '</pre>'
    return _md.markdown(text, extensions=['tables', 'fenced_code', 'sane_lists'])

app = Flask(__name__, template_folder=str(SCRIPT_DIR.parent / "templates"))

# 시스템 초기화 (서버 시작 시 한 번만)
system = mvp.CrisisConsultantSystem(
    db_path            = 'case_db.json',
    rules_yaml         = 'rules.yaml',
    labels_yaml        = 'controversy_labels.yaml',
    worst_actions_yaml = 'worst_actions_map.yaml',
    template_path      = 'report template.md',
    report_dir         = 'reports',
)

# 공유 system 객체(비용 계측·캐시 등 분석 1건 단위 상태 포함)는 동시 분석에 안전하지 않으므로
# NATAM 분석은 한 번에 하나씩 실행한다. 저작권 서브프로세스는 이 락 밖에서 병렬로 돈다.
_analysis_lock = threading.Lock()

# 분석 작업 상태 저장 (job_id → 상태/결과). 여러 요청 스레드가 동시에
# 접근하므로 락으로 보호하고, 오래된 작업은 새 작업 생성 시 정리한다.
JOB_TTL_SECONDS = 3600
_jobs_lock = threading.Lock()
jobs = {}


def _prune_old_jobs():
    # 아직 실행 중인(queued/running) 작업은 오래 걸릴 수 있으므로 건드리지 않고,
    # 끝난(done/error) 작업만 TTL이 지나면 정리한다.
    cutoff = time.time() - JOB_TTL_SECONDS
    stale = [
        jid for jid, j in jobs.items()
        if j.get('status') in ('done', 'error') and j.get('created_at', 0) < cutoff
    ]
    for jid in stale:
        jobs.pop(jid, None)


def _update_job(job_id, **fields):
    with _jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(fields)


def run_analysis(job_id, user_input, fresh=False, do_copyright=True):
    """
    fresh=True        → 이 영상의 이전 전사/교정 캐시를 지우고 처음부터 재분석
    do_copyright=True → 위기 분석 + 저작권 침해 분석 / False → 위기 분석만
    """
    try:
        _update_job(job_id, status='running')

        if mvp.is_youtube_url(user_input):
            runner = v193.CopyrightRunner(force=fresh) if do_copyright else None
            with _analysis_lock:
                report, json_path = v193.analyze_only(
                    system, user_input, download_dir='downloads', fresh=fresh,
                    on_video_ready=runner.start if runner else None)
            copyright_results = runner.result() if runner else None
            md_path, pdf_path = v193.finalize_reports(report, json_path, copyright_results,
                                                      include_copyright=do_copyright)
            _update_job(job_id, md_path=md_path, pdf_path=pdf_path)

        elif mvp.is_google_drive_url(user_input):
            dl = mvp.download_google_drive_video(user_input, 'downloads')
            local_video = dl['video_path']
            runner = v193.CopyrightRunner(force=fresh) if do_copyright else None
            if runner:
                runner.start(local_video)
            with _analysis_lock:
                report, json_path = v193.analyze_only(system, local_video, fresh=fresh)
            copyright_results = runner.result() if runner else None
            md_path, pdf_path = v193.finalize_reports(report, json_path, copyright_results,
                                                      include_copyright=do_copyright)
            _update_job(job_id, md_path=md_path, pdf_path=pdf_path)

        elif os.path.exists(user_input):
            runner = v193.CopyrightRunner(force=fresh) if do_copyright else None
            if runner:
                runner.start(user_input)
            with _analysis_lock:
                report, json_path = v193.analyze_only(system, user_input, fresh=fresh)
            copyright_results = runner.result() if runner else None
            md_path, pdf_path = v193.finalize_reports(report, json_path, copyright_results,
                                                      include_copyright=do_copyright)
            _update_job(job_id, md_path=md_path, pdf_path=pdf_path)

        else:
            # 텍스트 입력 → 영상이 없어 저작권 분석은 수행하지 않고 NATAM 분석만 수행
            with _analysis_lock:
                classification = system.classify_controversy(user_input)
                natam_result = mvp.assess_natam_risk(
                    client             = system.client,
                    system_instruction = system.system_instruction,
                    transcript_text    = '',
                    summary            = user_input,
                    gen_model          = mvp.GEN_MODEL,
                )
                # 위험 신호(NATAM A/B축 + 분류) 기반 트리거로 유사 사례 검색
                cases, dists, summary = system.find_similar_cases(
                    base_text=user_input, natam_result=natam_result,
                    classification=classification, k=3, make_summary=True,
                )
            report = {
                'meta': {'input_query': user_input},
                'classification': classification,
                'natam_risk': natam_result,
                'similar_cases': [
                    {
                        'rank':          c.get('rank'),
                        'title':         c.get('제목', c.get('title', '')),
                        '리스크':         c.get('리스크', ''),
                        '세부 태그':      c.get('세부 태그', ''),
                        '기사 요약':      c.get('기사 요약', ''),
                        '리스크 포인트':  c.get('리스크 포인트', ''),
                        '관련 법 및 정책': c.get('관련 법 및 정책', ''),
                        '뉴스 링크':      c.get('뉴스 링크', ''),
                        '언론사':         c.get('언론사', ''),
                        'distance':      c.get('distance'),
                    } for c in cases
                ],
                'pattern_summary': summary,
                'copyright': None,
            }

            # 텍스트 입력도 최종 MD 리포트를 생성 → 웹에서 파일과 동일하게 표시(저작권 섹션 제외)
            ts = time.strftime('%Y%m%d_%H%M%S')
            os.makedirs('reports', exist_ok=True)
            json_path = os.path.join('reports', f'report_{ts}.json')
            mvp._save_json(report, json_path)
            md_path = v193.CrisisReportEngineV193(include_copyright=False).create_report(report)
            pdf_path = (v193.pdf_from_md.render(md_path)
                        if (md_path and v193.pdf_from_md) else None)
            _update_job(job_id, md_path=md_path, pdf_path=pdf_path)

        _update_job(job_id, status='done', report=report)

    except Exception as e:
        _update_job(job_id, status='error', error=str(e))


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json or {}
    user_input = (data.get('input') or '').strip()
    if not user_input:
        return jsonify({'error': '입력값이 없습니다'}), 400

    # 사용자가 화면에서 고른 옵션 (기본: 이어서 / 저작권 함께)
    fresh        = bool(data.get('fresh', False))          # True=처음부터(캐시 삭제)
    do_copyright = bool(data.get('do_copyright', True))    # True=저작권 분석 함께

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _prune_old_jobs()
        jobs[job_id] = {'status': 'queued', 'created_at': time.time()}

    thread = threading.Thread(target=run_analysis,
                              args=(job_id, user_input, fresh, do_copyright))
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def status(job_id):
    with _jobs_lock:
        job = dict(jobs[job_id]) if job_id in jobs else None
    if not job:
        return jsonify({'error': '없는 작업입니다'}), 404
    return jsonify(job)


@app.route('/report/<job_id>')
def report_html(job_id):
    """최종 MD 리포트를 HTML로 변환해 반환(웹 화면에 파일과 동일하게 표시)."""
    with _jobs_lock:
        job = dict(jobs.get(job_id, {}))
    path = job.get('md_path')
    if not path or not os.path.exists(path):
        return jsonify({'error': '리포트 없음'}), 404
    return jsonify({'html': _report_html(path)})


@app.route('/download/<job_id>/<file_type>')
def download(job_id, file_type):
    with _jobs_lock:
        job = dict(jobs.get(job_id, {}))
    path = job.get(f'{file_type}_path')
    if path and os.path.exists(path):
        return send_file(path, as_attachment=True)
    return jsonify({'error': '파일 없음'}), 404


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
