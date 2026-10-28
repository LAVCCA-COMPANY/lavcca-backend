"""Minimal Microsoft Graph (personal OneDrive) client: conditional upload + read-back verification.
Every write goes through an upload session (if-match / conflictBehavior=fail are honoured there,
not on simple PUT). An upload counts only after a fresh GET shows the expected sha1."""
import json, time, hashlib, urllib.request, urllib.error
from urllib.parse import quote

BASE = "https://graph.microsoft.com/v1.0"
CHUNK = 10 * 320 * 1024  # fragments must be multiples of 320 KiB
RETRY_STATUSES = (None, 429, 500, 502, 503, 504)  # None = network error
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPE = "Files.ReadWrite offline_access"


class GraphError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class PreconditionFailed(GraphError):  # 412: someone else changed the file since we read its eTag
    pass


class Conflict(GraphError):  # 409: file exists and conflictBehavior=fail (also: lock already held)
    pass


class VerifyError(Exception):  # upload "succeeded" but OneDrive does not hold our bytes
    pass


class ReauthRequired(Exception):  # refresh token expired/revoked: a human must sign in again
    pass


class Transient(Exception):  # network / throttling / 5xx after retries: try again next run
    pass


REAUTH_ERRORS = {"invalid_grant", "interaction_required", "consent_required", "invalid_client", "unauthorized_client"}


def refresh_access_token(client_id, refresh_token, http=None, sleep=time.sleep):
    """-> (access_token, new_refresh_token). Raises ReauthRequired or Transient; never echoes tokens."""
    from urllib.parse import urlencode
    http = http or urllib_http
    body = urlencode({"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token,
                      "scope": SCOPE}).encode()
    for attempt in range(4):
        status, _, data = http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        try:
            js = json.loads(data) if data else {}
        except ValueError:
            js = {}
        if status == 200 and js.get("access_token"):
            return js["access_token"], js.get("refresh_token") or refresh_token
        if js.get("error") in REAUTH_ERRORS:
            codes = js.get("error_codes") or []
            raise ReauthRequired(f"{js['error']} AADSTS{codes[0] if codes else '?'}")
        if attempt < 3:
            sleep(2 ** attempt)
    raise Transient(f"token endpoint: HTTP {status} {js.get('error', '')}")


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest().upper()


def urllib_http(method, url, headers, body):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except OSError:
        return None, {}, b""


class Graph:
    def __init__(self, token, http=urllib_http, sleep=time.sleep, base=BASE, chunk=CHUNK):
        self.token, self.http, self.sleep, self.base, self.chunk = token, http, sleep, base, chunk

    def _req(self, method, url, body=None, headers=None, auth=True, ok=(200, 201, 202, 204)):
        h = dict(headers or {})
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        for attempt in range(4):
            status, rh, data = self.http(method, url, h, body)
            if status in RETRY_STATUSES and attempt < 3:
                ra = str(rh.get("Retry-After") or "")
                self.sleep(int(ra) if ra.isdigit() else 2 ** attempt)
                continue
            break
        if status in ok:
            return status, json.loads(data) if data else {}
        detail = data[:300].decode("utf-8", "replace") if data else ""
        if status == 412:
            raise PreconditionFailed(status, detail)
        if status == 409:
            raise Conflict(status, detail)
        raise GraphError(status, detail)

    def _path_url(self, remote_path, suffix=""):
        return f"{self.base}/me/drive/root:/{quote(remote_path.strip('/'))}:{suffix}"

    def item(self, remote_path):
        """Fresh driveItem metadata, or None if absent."""
        try:
            return self._req("GET", self._path_url(remote_path))[1]
        except GraphError as e:
            if e.status == 404:
                return None
            raise

    def children(self, remote_path):
        """Direct children of a folder (all pages). remote_path "" = drive root."""
        url = (self._path_url(remote_path, "/children") if remote_path.strip("/") else f"{self.base}/me/drive/root/children")
        url += "?$top=200"
        while url:
            _, page = self._req("GET", url)
            yield from page.get("value", [])
            url = page.get("@odata.nextLink")

    def download(self, item):
        """Bytes of a file item via its pre-authenticated downloadUrl (no bearer token sent there)."""
        url = item.get("@microsoft.graph.downloadUrl")
        if not url:
            raise GraphError(0, f"{item.get('name')}: no downloadUrl")
        status, _, data = None, {}, b""
        for attempt in range(4):
            status, rh, data = self.http("GET", url, {}, None)
            if status not in RETRY_STATUSES or attempt == 3:
                break
            self.sleep(2 ** attempt)
        if status != 200:
            raise GraphError(status, f"download {item.get('name')}")
        return data

    def delete(self, remote_path, if_match):
        self._req("DELETE", self._path_url(remote_path), headers={"If-Match": if_match})

    def upload(self, remote_path, local_path, if_match=None):
        """if_match=eTag: replace only that version (412 otherwise). None: create only (409 if exists)."""
        with open(local_path, "rb") as f:
            data = f.read()
        if not data:
            # shortcut: upload sessions reject 0-byte content; no managed file is empty today.
            raise GraphError(400, f"{remote_path}: empty file not supported")
        item = {"@microsoft.graph.conflictBehavior": "replace" if if_match else "fail"}
        headers = {"Content-Type": "application/json"}
        if if_match:
            headers["If-Match"] = if_match
        _, sess = self._req("POST", self._path_url(remote_path, "/createUploadSession"),
                            json.dumps({"item": item}).encode(), headers)
        url, n, result = sess["uploadUrl"], len(data), None
        for start in range(0, n, self.chunk):
            part = data[start:start + self.chunk]
            # the pre-authenticated upload URL must not carry the Authorization header
            _, result = self._req("PUT", url, part, {"Content-Length": str(len(part)),
                                  "Content-Range": f"bytes {start}-{start + len(part) - 1}/{n}"}, auth=False)
        return result

    def upload_verified(self, remote_path, local_path, if_match=None):
        want = sha1_file(local_path)
        up = self.upload(remote_path, local_path, if_match)
        fresh = self.item(remote_path)  # never trust the upload response alone
        got = ((fresh or {}).get("file") or {}).get("hashes", {}).get("sha1Hash")
        if not fresh or fresh.get("id") != (up or {}).get("id"):
            raise VerifyError(f"{remote_path}: item missing or replaced after upload")
        if not got:
            # shortcut: personal OneDrive reports sha1Hash; add quickXorHash if G10 shows it missing.
            raise VerifyError(f"{remote_path}: no sha1Hash in metadata")
        if got.upper() != want:
            raise VerifyError(f"{remote_path}: remote sha1 {got} != local {want}")
        return fresh
