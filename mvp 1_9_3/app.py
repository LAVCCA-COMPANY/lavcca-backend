from flask import Flask, request, jsonify, render_template, send_file
import threading, os, uuid, time, traceback, re
import html as _h
from html.parser import HTMLParser
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
    except Exception:
        traceback.print_exc()  # 상세(경로 포함)는 서버 로그에만
        return '<pre>리포트 파일을 읽을 수 없습니다.</pre>'
    if _md is None:
        return '<pre>' + _h.escape(text) + '</pre>'
    # 리포트에는 영상 제목·자막·기사 등 외부 텍스트가 섞이므로, 브라우저(innerHTML)로
    # 보내기 전에 마크다운 서식 태그만 남기고 나머지는 모두 무력화한다.
    return _sanitize_html(_md.markdown(text, extensions=['tables', 'fenced_code', 'sane_lists']))


# 마크다운(tables / fenced_code / sane_lists)이 만드는 서식 태그만 허용
_ALLOWED_TAGS = {
    'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
    'strong', 'em', 'b', 'i', 'code', 'pre', 'blockquote', 'del', 'sup', 'sub',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'span',
}
_DROP_CONTENT_TAGS = {'script', 'style', 'iframe', 'object', 'embed', 'template',
                      'noscript', 'textarea', 'title', 'svg', 'math'}
_SAFE_HREF = re.compile(r'^(https?://|mailto:|#)', re.I)
_SAFE_ALIGN = re.compile(r'^text-align:\s*(left|right|center);?$', re.I)
_SAFE_CODE_CLASS = re.compile(r'^language-[\w+#.-]+$')


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.drop = [], 0

    def _attrs(self, tag, attrs):
        kept = []
        for k, v in attrs:
            v = v or ''
            if ((tag == 'a' and k == 'href' and _SAFE_HREF.match(v.strip()))
                    or (tag in ('th', 'td') and k == 'style' and _SAFE_ALIGN.match(v.strip()))
                    or (tag == 'th' and k == 'align' and v in ('left', 'right', 'center'))
                    or (tag == 'code' and k == 'class' and _SAFE_CODE_CLASS.match(v))):
                kept.append(f' {k}="{_h.escape(v.strip(), quote=True)}"')
        if tag == 'a':
            kept.append(' rel="noopener noreferrer"')
        return ''.join(kept)

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT_TAGS:
            self.drop += 1
        elif not self.drop and tag in _ALLOWED_TAGS:
            self.out.append(f'<{tag}{self._attrs(tag, attrs)}>')

    def handle_startendtag(self, tag, attrs):
        if not self.drop and tag in _ALLOWED_TAGS:
            self.out.append(f'<{tag}{self._attrs(tag, attrs)}>')

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT_TAGS:
            self.drop = max(0, self.drop - 1)
        elif not self.drop and tag in _ALLOWED_TAGS:
            self.out.append(f'</{tag}>')

    def handle_data(self, data):
        if not self.drop:
            self.out.append(_h.escape(data, quote=False))


def _sanitize_html(html_text: str) -> str:
    """허용 목록 밖의 태그·속성(on*, style, javascript: 링크 등)을 제거한 HTML."""
    s = _Sanitizer()
    s.feed(html_text)
    s.close()
    return ''.join(s.out)

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

# 분석 1건이 영상 다운로드·전사·저작권 서브프로세스를 모두 돌리므로 동시 실행 수를 제한한다.
# 한도를 넘는 요청은 대기열에 쌓지 않고 바로 429로 돌려보낸다.
MAX_CONCURRENT_JOBS = max(1, int(os.getenv('NATAM_MAX_CONCURRENT_JOBS', '2')))
_job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)


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

        else:
            # 텍스트 입력 → 영상이 없어 저작권 분석은 수행하지 않고 NATAM 분석만 수행
            # (웹 입력은 서버 파일 경로로 해석하지 않는다 — 경로처럼 보여도 텍스트로 취급.
            #  로컬 파일 분석은 CLI(mvp_ver_1_9_3.py)에서만 지원)
            with _analysis_lock:
                segments = [{"start": 0, "end": 0, "text": user_input, "corrected_text": user_input}]
                classification = system.classify_controversy(user_input)
                transcript_analysis = system.analyze_transcript_segments(segments)
                two_tier = system.two_tier_classify(segments)
                transcript_analysis = mvp._reconcile_transcript_with_tier2(
                    transcript_analysis, two_tier)
                classification = mvp._classification_from_tier2(classification, two_tier)
                confirmed = [
                    (a.get("quote") or user_input).strip()
                    for a in (two_tier.get("adjudicated") or [])
                    if a.get("is_controversy") is True
                ]
                natam_result = mvp.assess_natam_risk(
                    client             = system.client,
                    system_instruction = system.system_instruction,
                    transcript_text    = "\n".join(confirmed) if confirmed else user_input,
                    summary            = user_input,
                    gen_model          = mvp.GEN_MODEL,
                )
                cases, dists, _ = system.find_similar_cases(
                    base_text=user_input, natam_result=natam_result,
                    risk_sentences=confirmed[:8],
                    classification=classification, k=5, make_summary=False,
                )
                shown = mvp._select_similar_cases(cases, dists)
                summary = ""
                if shown:
                    summary = system._summarize_pattern(
                        "\n".join(confirmed) or user_input[:800],
                        [c for c, _, _ in shown],
                    )
            sim_manifest = system.sim_engine.manifest if system.sim_engine else {}
            report = {
                'meta': {
                    'input_query': user_input,
                    'natam_evaluation_status': natam_result.get('evaluation_status'),
                    'similar_index': {
                        'count': sim_manifest.get('count'),
                        'full_n': sim_manifest.get('full_n'),
                        'partial': bool(sim_manifest.get('partial')),
                    },
                },
                'classification': classification,
                'natam_risk': natam_result,
                'two_tier': two_tier,
                'transcript_analysis': transcript_analysis,
                'similar_cases': [
                    {
                        'rank':          rank,
                        'title':         c.get('제목', c.get('title', '')),
                        '제목':           c.get('제목', ''),
                        '리스크':         c.get('리스크', ''),
                        '세부 태그':      c.get('세부 태그', ''),
                        '기사 요약':      c.get('기사 요약', ''),
                        '리스크 포인트':  c.get('리스크 포인트', ''),
                        '관련 법 및 정책': c.get('관련 법 및 정책', ''),
                        '뉴스 링크':      c.get('뉴스 링크', ''),
                        '언론사':         c.get('언론사', ''),
                        'distance':      dist,
                        'match_quality': quality,
                        'score':         c.get('score'),
                        'dense':         c.get('dense'),
                        'keyword_score': c.get('keyword_score'),
                        'category_hit':  c.get('category_hit'),
                    } for rank, (c, dist, quality) in enumerate(shown, 1)
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

    except Exception:
        # 예외 메시지에는 서버 경로·내부 정보가 섞일 수 있으므로 상세는 서버 로그에만 남긴다.
        print(f"[run_analysis] job {job_id} 실패")
        traceback.print_exc()
        _update_job(job_id, status='error',
                    error='분석 중 오류가 발생했습니다. 입력을 확인한 뒤 다시 시도해 주세요.')
    finally:
        _job_slots.release()


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

    # 슬롯은 run_analysis 의 finally 에서 반납한다
    if not _job_slots.acquire(blocking=False):
        return jsonify({'error': '현재 진행 중인 분석이 많습니다. 잠시 후 다시 시도해 주세요.'}), 429

    job_id = str(uuid.uuid4())
    try:
        with _jobs_lock:
            _prune_old_jobs()
            jobs[job_id] = {'status': 'queued', 'created_at': time.time()}
        threading.Thread(target=run_analysis,
                         args=(job_id, user_input, fresh, do_copyright)).start()
    except BaseException:
        # 스레드가 시작되지 못했으면 run_analysis 의 finally 도 돌지 않으므로 여기서 반납
        _job_slots.release()
        _update_job(job_id, status='error', error='분석을 시작하지 못했습니다.')
        raise

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
    # 인증이 없으므로 이 PC에서만 접속 가능하게 한다. 외부 공개는 인증 도입 후에만 (README 참고).
    app.run(host='127.0.0.1', port=5000, debug=False)
