"""
api_smoke.py — Supabase Data API(PostgREST) 연결 점검 1~4 (dev 전용, 데이터 변경 없음)

  1. publishable 키: 5개 테이블 GET 이 거부되는지(anon 접근 차단)
  2. secret 키: 5개 테이블 GET → HTTP 코드, 행 수
  3. count=exact → Content-Range 형식·총 행 수
  4. 없는 id 로만 jobs PATCH → 0행 갱신(Content-Range */0), 전후 jobs 행 수 동일

실행(PM 터미널, 저장소 루트):  python infra/supabase/tests/api_smoke.py
키: 환경변수 SUPABASE_PUBLISHABLE_KEY / SUPABASE_SECRET_KEY 가 없으면 숨김 입력(getpass)으로 받는다.
    키는 apikey 헤더로만 보낸다(Authorization 없음). 키·헤더·응답 본문은 출력하지 않는다.
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mvp 1_9_3"))
from worker import affected_rows   # noqa: E402  (워커와 같은 Content-Range 해석)

REF = "vbfurkpxfudyksfuzusw"   # Lavcca dev
TABLES = ("jobs", "job_steps", "feedback", "copyright_matches", "labels")


def _key(env, prefix):
    k = os.getenv(env) or getpass.getpass(f"{env} ({prefix}…, 입력은 화면에 안 보임): ")
    k = k.strip()
    if not k.startswith(prefix):
        sys.exit(f"{env} 형식이 아님({prefix}… 이어야 함) - 중단")
    return k


def call(url, key, method="GET", body=None, prefer=None):
    """(HTTP 코드, Content-Range, PostgREST 오류 코드, 행 수). 본문·헤더는 밖으로 내지 않는다."""
    h = {"apikey": key, "Accept": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            rows = json.loads(raw) if raw else None
            return r.status, r.headers.get("Content-Range"), None, len(rows) if isinstance(rows, list) else None
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read() or b"{}").get("code")
        except ValueError:
            code = None
        return e.code, e.headers.get("Content-Range"), code, None
    except Exception as e:
        return f"ERR:{type(e).__name__}", None, None, None


def main(base=None):
    base = (base or os.getenv("SUPABASE_URL") or f"https://{REF}.supabase.co").rstrip("/") + "/rest/v1"
    host = urllib.parse.urlsplit(base).hostname or ""
    if host not in (f"{REF}.supabase.co", "127.0.0.1", "localhost"):   # dev 전용(로컬은 가짜 서버 점검용)
        sys.exit(f"대상이 Lavcca dev({REF})가 아님: {host} - 중단")
    pub = _key("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_")
    sec = _key("SUPABASE_SECRET_KEY", "sb_secret_")
    results = []

    def check(name, ok, detail):
        results.append((name, "PASS" if ok else "FAIL", detail))

    def jobs_count():
        st, cr, _, _ = call(f"{base}/jobs?select=id&limit=1", sec, prefer="count=exact")
        return affected_rows(cr) if st in (200, 206) else None

    before = jobs_count()

    for t in TABLES:                                         # 1. anon 차단
        st, _, code, _ = call(f"{base}/{t}?select=id&limit=1", pub)
        check(f"1 publishable GET {t}", st in (401, 403), f"HTTP {st} {code or ''}".strip())

    for t in TABLES:                                         # 2·3. secret 읽기 + count
        st, cr, code, n = call(f"{base}/{t}?select=id&limit=1", sec, prefer="count=exact")
        total = affected_rows(cr)
        check(f"2 secret GET {t}", st in (200, 206) and n is not None,
              f"HTTP {st} rows={n}" + (f" {code}" if code else ""))
        check(f"3 count=exact {t}", total is not None and cr is not None and "/" in cr and cr.split("/")[1].isdigit(),
              f"Content-Range={cr} total={total}")

    missing = f"api-smoke-{uuid.uuid4().hex[:12]}"           # 4. 없는 id 만 PATCH
    st, cr, code, _ = call(f"{base}/jobs?id=eq.{missing}", sec, method="PATCH",
                           body={"error": "api-smoke"}, prefer="return=minimal, count=exact")
    check("4 PATCH 없는 id → 0행", st == 204 and affected_rows(cr) == 0,
          f"HTTP {st} Content-Range={cr}" + (f" {code}" if code else ""))

    after = jobs_count()
    check("4 jobs 행 수 전후 동일", before is not None and before == after, f"before={before} after={after}")

    w = max(len(r[0]) for r in results)
    for name, res, detail in results:
        print(f"{name:<{w}}  {res}  {detail}")
    failed = sum(r[1] == "FAIL" for r in results)
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
