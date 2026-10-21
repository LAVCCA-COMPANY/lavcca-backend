"""
worker.py — 영상 1개 = 작업 1개를 비대화식으로 처리하는 서비스 진입점 (컨테이너 ENTRYPOINT).

  python worker.py --job-id JOB123 --input /data/in/video.mp4 --output-dir /data/out/JOB123
  python worker.py ... --no-copyright        # 위기 분석만
  python worker.py --job-id JOB123 --input s3://lavcca-dev-customer-uploads/uploads/JOB123/video.mp4 \
                   --output-dir /tmp/out --output-s3 s3://lavcca-dev-datalake/ai-output/JOB123/

입력은 로컬 영상 파일 또는 s3:// (URL 다운로드는 워커 밖에서). 결과는 --output-dir 에 쓴다.
  report_*.json, FINAL_REPORT_*.md/.pdf   통합 리포트 3종(이름은 원본 영상 파일명에서 나온다)
  transcripts/                            전사·교정 결과(리포트가 참조)
  logs/worker_*.log                       콘솔 전체 로그
  job_result.json                         상태·산출물·단계별 소요/메모리·LLM 비용
--output-s3 를 주면 끝난 뒤 이름을 고정해 올린다(upload_layout).
  report.pdf, report.md                   고객용 — api 가 이 두 개만 다운로드 링크로 준다
  _internal/report.json, _internal/transcripts/…, _internal/job_result.json   내부용
  logs/ 는 올리지 않는다(CloudWatch 에 같은 내용). job_result.json 은 맨 마지막 = 완료 표시.

상태: ok(리포트 3종 + 저작권 결과) / partial(리포트 3종, 저작권 실패) / failed(리포트 누락·예외·업로드 실패·영구 판정 누락)
      / retry_later(Gemini 429·5xx 만 — 전사 예외, 또는 판정 누락이 전부 일시 장애로 표시된 경우).
      400·JSON 파싱 실패로 비어 있는 판정은 failed. 영구 실패가 하나라도 있으면 재시도하지 않는다.
      retry_later 는 나중에 같은 작업을 다시 돌리라는 뜻(재투입은 T5 큐 쪽 몫). 컨테이너는 WHISPER_FALLBACK=0.
종료 코드: ok·partial → 0, retry_later → 75(EX_TEMPFAIL), failed → 1.
SUPABASE_URL·SUPABASE_SERVICE_ROLE_KEY(새 sb_secret_ 키 권장, 레거시 service_role JWT 도 가능)가 있으면 jobs/job_steps 에 상태를 기록한다
(스키마: 저장소 루트 infra/supabase/migrations/0001_jobs.sql, 배포 순서: infra/README.md). 없으면 기록 생략.
비밀값은 환경변수로 받는다. 저작권 감지기는 자기 폴더의 .env 와 환경변수를 쓴다.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import resource   # Linux 전용. Windows 로컬 실행에서는 메모리 기록 생략
except ImportError:
    resource = None

# Windows 한국어 콘솔(cp949)에서 이모지/한글 출력 시 UnicodeEncodeError 방지 → UTF-8 강제
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXIT_RETRY_LATER = 75   # sysexits EX_TEMPFAIL
TRANSIENT_HTTP = {429, 500, 502, 503, 504}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _maxrss_mb(who) -> float | None:
    """최대 상주 메모리(MB). Linux ru_maxrss 단위는 KB."""
    if resource is None:
        return None
    return round(resource.getrusage(who).ru_maxrss / 1024, 1)


class Steps:
    """단계별 소요 시간과 그 시점의 최대 메모리를 기록한다(사이징 근거)."""

    def __init__(self):
        self.items = []

    def run(self, name, fn, *args, **kwargs):
        t = time.time()
        try:
            return fn(*args, **kwargs)
        finally:
            self.items.append({
                "name": name,
                "sec": round(time.time() - t, 1),
                "maxrss_mb": _maxrss_mb(resource.RUSAGE_SELF) if resource else None,
            })


# ── S3 ──────────────────────────────────────────────────────────────
def split_s3(uri: str) -> tuple[str, str]:
    """s3://bucket/key → (bucket, key). key 는 비어 있을 수 있다(접두사)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"s3:// 경로가 아님: {uri}")
    bucket, _, key = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"버킷 없음: {uri}")
    return bucket, key


def download_input(uri: str, dest_dir: Path, s3=None) -> Path:
    import boto3
    s3 = s3 or boto3.client("s3")
    bucket, key = split_s3(uri)
    # 파일명이 리포트 이름(base)이 되므로 원래 이름을 유지한다
    path = dest_dir / Path(key).name
    s3.download_file(bucket, key, str(path))
    return path


CUSTOMER_NAMES = {"pdf": "report.pdf", "md": "report.md", "json": "_internal/report.json"}


def upload_layout(out_dir: Path, outputs: dict) -> list[tuple[Path, str]]:
    """(로컬 파일, 접두사 아래 키) 목록. 리포트 3종은 고정 이름(원본 파일명이 S3 키에 안 들어가게),
    나머지는 _internal/ 아래, logs/ 는 뺀다. job_result.json 은 맨 마지막(완료 표시)."""
    fixed = {Path(outputs[k]).resolve(): name for k, name in CUSTOMER_NAMES.items() if outputs.get(k)}
    pairs = []
    for p in sorted(out_dir.rglob("*")):
        rel = p.relative_to(out_dir)
        if not p.is_file() or rel.parts[0] == "logs":
            continue
        pairs.append((p, fixed.get(p.resolve(), "_internal/" + rel.as_posix())))
    pairs.sort(key=lambda x: x[1] == "_internal/job_result.json")   # 안정 정렬 → 그것만 맨 뒤로
    return pairs


def upload_outputs(out_dir: Path, uri: str, outputs: dict, s3=None, only: set | None = None) -> int:
    """only: 이 파일 이름들만 다시 올린다(예: {"job_result.json"})."""
    import boto3
    s3 = s3 or boto3.client("s3")
    bucket, prefix = split_s3(uri)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    pairs = [(p, k) for p, k in upload_layout(out_dir, outputs) if only is None or p.name in only]
    for p, key in pairs:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix in (".md", ".log", ".json"):
            ctype = {"md": "text/markdown", "log": "text/plain",
                     "json": "application/json"}[p.suffix[1:]] + "; charset=utf-8"
        s3.upload_file(str(p), bucket, prefix + key, ExtraArgs={"ContentType": ctype})
    return len(pairs)


# ── Supabase 상태 기록 (선택) ─────────────────────────────────────────
def affected_rows(content_range: str | None) -> int | None:
    """PostgREST 쓰기 응답의 Content-Range → 바뀐 행 수. "0-0/1"·"*/0"(count=exact), "0-2/*"·"*/*"(count 없음).
    헤더가 없거나 형식을 모르면 None."""
    rng, sep, total = (content_range or "").strip().partition("/")
    if not sep:
        return None
    if total.isdigit():
        return int(total)
    if rng == "*":
        return 0
    lo, _, hi = rng.partition("-")
    return int(hi) - int(lo) + 1 if lo.isdigit() and hi.isdigit() else None


class JobStatus:
    """PostgREST 로 jobs 행을 갱신하고 job_steps 를 넣는다. 환경변수가 없으면 아무것도 안 한다.
    기록 실패는 경고만 남기고 errors 로 센다 — S3 의 job_result.json 이 결과의 원본 사본.
    로그에는 키·헤더를 넣지 않는다(메서드, 경로, HTTP 코드, 응답 본문 앞부분만)."""

    def __init__(self, url: str = "", key: str = "", session=None):
        if key.startswith("sb_publishable_"):
            print("⚠️ SUPABASE_SERVICE_ROLE_KEY 에 publishable 키가 들어옴 → 상태 기록 생략(sb_secret_ 키 필요)")
            key = ""
        self.url = url.rstrip("/")
        self.key = key
        self.session = session
        self.errors = 0

    @classmethod
    def from_env(cls):
        url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if bool(url) != bool(key):
            missing = "SUPABASE_SERVICE_ROLE_KEY" if url else "SUPABASE_URL"
            print(f"⚠️ {missing} 없음 → Supabase 상태 기록 꺼짐(SUPABASE_URL·SUPABASE_SERVICE_ROLE_KEY 둘 다 필요)")
        return cls(url, key)

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def error_count(self) -> int | None:
        """job_result.json 의 status_write_errors: 기록 꺼짐이면 None, 켜져 있으면 실패 횟수."""
        return self.errors if self.enabled else None

    def _fail(self, method, path, why):
        self.errors += 1
        print(f"⚠️ 상태 기록 실패 {method} {path}: {why}")

    def _call(self, method, path, body, prefer="return=minimal"):
        """성공하면 응답, 꺼져 있거나 실패하면 None."""
        if not self.enabled:
            return None
        try:
            if self.session is None:
                import requests
                self.session = requests.Session()
            headers = {"apikey": self.key, "Content-Type": "application/json", "Prefer": prefer}
            if not self.key.startswith("sb_"):   # 레거시 JWT 키만 Bearer 로도(새 sb_secret_ 키는 Bearer 에서 거부됨)
                headers["Authorization"] = f"Bearer {self.key}"
            r = self.session.request(
                method, f"{self.url}/rest/v1/{path}", json=body, timeout=10, headers=headers)
        except Exception as e:
            self._fail(method, path, type(e).__name__)   # 예외 문자열은 요청 정보를 담을 수 있어 이름만
            return None
        if r.status_code >= 300:
            self._fail(method, path, f"HTTP {r.status_code} {r.text[:200]}")
            return None
        return r

    def update(self, job_id: str, **fields):
        path = f"jobs?id=eq.{job_id}"
        r = self._call("PATCH", path, fields, prefer="return=minimal, count=exact")
        if r is None:
            return
        n = affected_rows(r.headers.get("Content-Range"))
        if n == 0:
            self._fail("PATCH", path, "jobs 행 없음(0행 갱신) — 작업이 API 로 만들어졌는지 확인")
        elif n is None:
            print(f"⚠️ PATCH {path}: Content-Range 없음 → 갱신 행 수 확인 불가")

    def add_steps(self, job_id: str, steps: list):
        if steps:
            self._call("POST", "job_steps", [dict(s, job_id=job_id) for s in steps])


# ── Google Vision 키 ─────────────────────────────────────────────────
def install_google_credentials(dest_dir: Path) -> str | None:
    """GOOGLE_CREDENTIALS_JSON(키 파일 내용, Secrets Manager 주입)이 있고 파일 경로가 따로 없으면
    소유자만 읽을 수 있는 파일로 써서 GOOGLE_APPLICATION_CREDENTIALS 로 넘긴다(저작권 서브프로세스가 상속)."""
    content = os.environ.pop("GOOGLE_CREDENTIALS_JSON", "")
    if not content or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return None
    json.loads(content)   # 형식 오류면 여기서 실패(내용은 출력하지 않는다)
    path = dest_dir / "gcp-credentials.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return str(path)


# ── 일시 장애 판정 ───────────────────────────────────────────────────
def transient_error(e: BaseException) -> tuple[bool, float | None]:
    """(일시 장애인가, 권장 대기 초). 엔진의 TransientAPIError 와 google-genai APIError(code 429·5xx)."""
    if hasattr(e, "retry_after"):   # mvp_ver_1_9_2.TransientAPIError (엔진 import 없이 판정)
        return True, e.retry_after
    if type(e).__module__.startswith("google.genai") and getattr(e, "code", None) in TRANSIENT_HTTP:
        return True, None
    return False, None


def quality_issues(report: dict) -> tuple[dict, list, list]:
    """(기록용 요약, retry_later 사유, failed 사유).

    unverified·NATAM failed 라도 transient 표시가 없으면 다시 돌리지 않는다.
    영구 실패가 하나라도 있으면 일시 장애 건도 재시도 목록에서 뺀다.
    """
    meta = report.get("meta") or {}
    natam = meta.get("natam_evaluation_status")
    adjudicated = (report.get("two_tier") or {}).get("adjudicated") or []
    unverified = [a for a in adjudicated if a.get("adjudication") == "unverified"]
    transient_n = sum(1 for a in unverified if a.get("transient"))
    permanent_n = len(unverified) - transient_n
    retry, fail = [], []
    if natam == "failed":
        (retry if meta.get("natam_transient") else fail).append(
            "NATAM 평가 일시 장애" if meta.get("natam_transient") else "NATAM 평가 실패(전 항목 미판정)")
    if transient_n:
        retry.append(f"Tier2 일시 장애 {transient_n}건")
    if permanent_n:
        fail.append(f"Tier2 판정 보류 {permanent_n}건")
    if fail:
        fail = retry + fail   # 영구 실패가 있으면 일시 장애 사유만 남기고 재시도는 하지 않는다
        retry = []
    return {
        "natam_evaluation_status": natam,
        "tier2_unverified": len(unverified),
        "tier2_transient": transient_n,
    }, retry, fail


# ── 실행 ────────────────────────────────────────────────────────────
REQUIRED_ENV = ("GEMINI_API_KEY",)


def require_env(names) -> None:
    """빈 시크릿(키만 있고 값이 빈 문자열)으로 시작한 태스크를 분석 전에 failed 로 끝낸다. 값은 출력하지 않는다."""
    missing = [n for n in names if not os.getenv(n, "").strip()]
    if missing:
        raise RuntimeError("필수 환경변수 비어 있음: " + ", ".join(missing))


def scope_transcribe_prefix(job_id: str) -> str:
    """AWS_TRANSCRIBE_S3(s3://버킷/접두사/)가 있으면 끝에 {job_id}/ 를 붙인다(작업별 임시 오디오 경로)."""
    base = os.getenv("AWS_TRANSCRIBE_S3", "")
    if not base:
        return ""
    scoped = base.rstrip("/") + f"/{job_id}/"
    os.environ["AWS_TRANSCRIBE_S3"] = scoped
    return scoped


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="NATAM 분석 워커 (영상 1개)")
    p.add_argument("--job-id", required=True, help="영문·숫자·_- 1~64자")
    p.add_argument("--input", required=True, help="로컬 영상/오디오 파일 경로 또는 s3://버킷/키")
    p.add_argument("--output-dir", required=True, help="결과를 쓸 폴더(없으면 만든다)")
    p.add_argument("--output-s3", help="끝난 뒤 결과 폴더를 올릴 s3://버킷/접두사/")
    p.add_argument("--no-copyright", action="store_true", help="저작권 분석 생략")
    a = p.parse_args(argv)
    if not JOB_ID_RE.match(a.job_id):
        p.error("--job-id 는 영문·숫자·_- 1~64자")
    try:
        if a.input.startswith("s3://"):
            split_s3(a.input)
        if a.output_s3:
            split_s3(a.output_s3)
    except ValueError as e:
        p.error(str(e))
    if not a.input.startswith("s3://"):
        a.input = Path(a.input).resolve()
        if not a.input.is_file():
            p.error(f"입력 파일 없음: {a.input}")
    a.output_dir = Path(a.output_dir).resolve()
    return a


def run_job(args) -> dict:
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    do_copyright = not args.no_copyright

    result = {
        "job_id": args.job_id,
        "status": "failed",
        "input": str(args.input),
        "copyright_requested": do_copyright,
        "copyright_ok": None,
        "outputs": {},
        "steps": [],
        "started_at": _now(),
    }
    steps = Steps()
    system = None
    tmp = None
    try:
        # 엔진이 import 시점에 읽으므로 그 전에: Transcribe 임시 오디오를 transcribe-tmp/{job_id}/ 아래로
        scope_transcribe_prefix(args.job_id)
        # 무거운 import(whisper·torch·faiss·cv2)는 인자 검사 뒤에
        import mvp_ver_1_9_3 as v193
        mvp = v193.mvp
        require_env(REQUIRED_ENV)   # .env 로드(엔진 import) 뒤, 다운로드·분석 전

        if v193.run_logging is not None:
            log_path = v193.run_logging.setup(log_dir=str(out / "logs"), prefix="worker")
            result["outputs"]["log"] = log_path
        print(f"🧾 job {args.job_id} 시작: {args.input}")

        video = args.input
        if isinstance(video, str):   # s3://
            tmp = Path(tempfile.mkdtemp(prefix="natam_in_"))
            video = steps.run("download", download_input, video, tmp)

        system = steps.run("load", mvp.CrisisConsultantSystem, report_dir=str(out))

        runner = v193.CopyrightRunner(job_id=args.job_id) if do_copyright else None
        report, json_path = steps.run(
            "analyze", v193.analyze_only, system, str(video),
            on_video_ready=runner.start if runner else None,
            transcript_dir=str(out / "transcripts"))

        copyright_results = steps.run("copyright_wait", runner.result) if runner else None
        if runner:
            result["copyright_ok"] = copyright_results is not None

        md_path, pdf_path = steps.run(
            "finalize", v193.finalize_reports, report, json_path, copyright_results,
            include_copyright=do_copyright, report_dir=str(out))

        result["outputs"].update(json=json_path, md=md_path, pdf=pdf_path)
        result["quality"], retry_reasons, fail_reasons = quality_issues(report or {})
        if not all(p and os.path.isfile(p) for p in (json_path, md_path, pdf_path)):
            result["error"] = "리포트 3종 중 누락 있음(위 로그 참조)"
        elif fail_reasons:
            result["error"] = "판정 누락: " + ", ".join(fail_reasons)
        elif retry_reasons:
            result["status"] = "retry_later"
            result["error"] = "Gemini 일시 장애로 판정 누락: " + ", ".join(retry_reasons)
        else:
            result["status"] = "ok" if result["copyright_ok"] is not False else "partial"
    except Exception as e:
        traceback.print_exc()
        result["error"] = f"{type(e).__name__}: {e}"
        is_transient, retry_after = transient_error(e)
        if is_transient:
            result["status"] = "retry_later"
            result["retry_after_sec"] = retry_after
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        result["steps"] = steps.items
        result["finished_at"] = _now()
        if resource is not None:
            result["maxrss_mb"] = {"self": _maxrss_mb(resource.RUSAGE_SELF),
                                   "children": _maxrss_mb(resource.RUSAGE_CHILDREN)}
        if system is not None:
            result["llm_cost"] = system.cost.summary()   # 저작권 감지기 비용은 미포함
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    status = JobStatus.from_env()
    if not status.enabled:
        print("ℹ️ Supabase 상태 기록 꺼짐 → job_result.json status_write_errors=null")
    status.update(args.job_id, status="running", started_at=_now())

    secrets_dir = Path(tempfile.mkdtemp(prefix="natam_sec_"))   # 결과 폴더(S3 업로드 대상) 밖
    try:
        try:
            install_google_credentials(secrets_dir)
        except ValueError:
            print("⚠️ GOOGLE_CREDENTIALS_JSON 이 JSON 이 아님 → Google Vision 없이 진행")
        result = run_job(args)
    finally:
        shutil.rmtree(secrets_dir, ignore_errors=True)
    path = args.output_dir / "job_result.json"

    def _write():
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    status.add_steps(args.job_id, result["steps"])
    result["status_write_errors"] = status.error_count()
    _write()
    if args.output_s3:
        try:
            n = upload_outputs(args.output_dir, args.output_s3, result["outputs"])
            result["outputs"]["s3_prefix"] = args.output_s3
            print(f"☁️  S3 업로드 {n}개 → {args.output_s3}")
        except Exception as e:
            traceback.print_exc()
            result["status"] = "failed"
            result["error"] = f"S3 업로드 실패: {type(e).__name__}: {e}"
            _write()

    # 최종 상태는 업로드 뒤에 쓴다(웹은 ok 를 보고 파일을 받는다). 여기서 실패하면 job_result.json 만 다시 올린다
    before = status.errors
    status.update(args.job_id, status=result["status"], finished_at=result["finished_at"],
                  error=result.get("error"),
                  report_prefix=result["outputs"].get("s3_prefix"),
                  llm_cost_usd=(result.get("llm_cost") or {}).get("est_usd"))
    if status.errors != before:
        result["status_write_errors"] = status.error_count()
        _write()
        if result["outputs"].get("s3_prefix"):
            try:
                upload_outputs(args.output_dir, args.output_s3, result["outputs"], only={path.name})
            except Exception as e:
                print(f"⚠️ job_result.json 재업로드 실패: {type(e).__name__}")
    print(f"🧾 job {args.job_id} → {result['status']}  ({path})")
    if result["status"] == "retry_later":
        return EXIT_RETRY_LATER
    return 0 if result["status"] in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
