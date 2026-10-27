"""
supa.py — Lambda 에서 쓰는 최소 Supabase REST 클라이언트 (표준 라이브러리만, requests 없음).

  · get_user(token)        사용자 access token 검증 (GET /auth/v1/user)
  · insert/update/select   PostgREST, service_role 키로 호출(RLS 우회 — 서버 전용)
  · count                  Prefer: count=exact 로 행 수만

키: 서버 키는 새 secret 키(sb_secret_…) 기본, 레거시 service_role JWT 도 받는다. anon 자리는 publishable(sb_publishable_…).
새 sb_ 키는 apikey 헤더로만 보낸다 — Authorization: Bearer 에 넣으면 플랫폼이 JWT 로 파싱해 거부한다.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class SupabaseError(RuntimeError):
    pass


class Supabase:
    def __init__(self, url: str, service_key: str, anon_key: str = "", opener=None, timeout=10):
        if service_key.startswith("sb_publishable_"):
            raise SupabaseError("서버 키 자리에 publishable 키가 들어옴 — sb_secret_ 키를 넣는다")
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.anon_key = anon_key or service_key
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _req(self, method, path, body=None, headers=None, bearer=None, apikey=None):
        key = apikey or self.service_key
        h = {"apikey": key, "Content-Type": "application/json"}
        # 사용자 JWT 는 그대로 Bearer. 키 자체는 레거시 JWT 일 때만 Bearer 에도 넣는다(sb_ 키는 거부됨)
        token = bearer or (None if key.startswith("sb_") else key)
        if token:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method, headers=h)
        try:
            with self.opener(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, dict(r.headers), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), None

    def get_user(self, access_token: str) -> dict | None:
        """유효한 로그인 토큰이면 사용자 dict, 아니면 None."""
        if not access_token:
            return None
        status, _, data = self._req("GET", "/auth/v1/user", bearer=access_token, apikey=self.anon_key)
        return data if status == 200 and isinstance(data, dict) and data.get("id") else None

    @staticmethod
    def _qs(params: dict) -> str:
        return urllib.parse.urlencode(params, safe=",.()*:")

    def insert(self, table: str, row: dict) -> None:
        status, _, _ = self._req("POST", f"/rest/v1/{table}", row, {"Prefer": "return=minimal"})
        if status >= 300:
            raise SupabaseError(f"insert {table}: HTTP {status}")

    def update(self, table: str, filters: dict, fields: dict) -> list:
        """filters 예: {"id": "eq.j1", "status": "in.(queued,running)"}. 바뀐 행 목록을 돌려준다."""
        status, _, data = self._req("PATCH", f"/rest/v1/{table}?{self._qs(filters)}", fields,
                                    {"Prefer": "return=representation"})
        if status >= 300:
            raise SupabaseError(f"update {table}: HTTP {status}")
        return data or []

    def select(self, table: str, params: dict) -> list:
        status, _, data = self._req("GET", f"/rest/v1/{table}?{self._qs(params)}")
        if status >= 300:
            raise SupabaseError(f"select {table}: HTTP {status}")
        return data or []

    def count(self, table: str, params: dict) -> int:
        q = dict(params, select="id")
        status, headers, _ = self._req("GET", f"/rest/v1/{table}?{self._qs(q)}", None,
                                       {"Prefer": "count=exact", "Range": "0-0"})
        if status >= 300:
            raise SupabaseError(f"count {table}: HTTP {status}")
        cr = {k.lower(): v for k, v in headers.items()}.get("content-range", "")
        total = cr.rpartition("/")[2]          # "0-0/5" 또는 "*/0"
        if not total.isdigit():
            raise SupabaseError(f"count {table}: content-range 없음({cr!r})")
        return int(total)
