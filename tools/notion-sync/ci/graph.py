"""Minimal Microsoft Graph (personal OneDrive) client: conditional upload + read-back verification.
Every write goes through an upload session (if-match / conflictBehavior=fail are honoured there,
not on simple PUT). An upload counts only after a fresh GET shows the expected content hash
(sha1Hash where OneDrive still reports it, else quickXorHash)."""
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


_QX_MASK = (1 << 160) - 1
QX_CHUNK = 160 * 65536  # read size; must stay a multiple of 160 so lanes line up across chunks


def quickxor_file(path):
    """OneDrive QuickXorHash (base64), per Microsoft's reference implementation: byte j is xored into a
    160-bit circular register at bit (j*11) % 160, then the length (8 bytes LE) is xored into the top
    8 bytes. Personal OneDrive is replacing sha1Hash with this since 2023-07."""
    acc, n = 0, 0  # acc: 160-byte lanes, byte j lands in lane j % 160 (its rotation depends only on that)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(QX_CHUNK)
            if not chunk:
                break
            n += len(chunk)
            mv = memoryview(chunk)
            for r in range(0, len(chunk), 160):  # a short last row is zero-padded on the high side
                acc ^= int.from_bytes(mv[r:r + 160], "little")
    h = 0
    for i, b in enumerate(acc.to_bytes(160, "little")):
        if b:
            v = b << ((i * 11) % 160)
            h ^= (v & _QX_MASK) | (v >> 160)
    out = bytearray(h.to_bytes(20, "little"))
    for k, lb in enumerate(n.to_bytes(8, "little")):
        out[12 + k] ^= lb
    import base64
    return base64.b64encode(bytes(out)).decode()


def remote_hash(item):
    """(kind, value) of the content hash Graph reports for a file item; (None, None) if none."""
    hashes = ((item or {}).get("file") or {}).get("hashes") or {}
    if hashes.get("sha1Hash"):
        return "sha1", hashes["sha1Hash"].upper()
    if hashes.get("quickXorHash"):
        return "quickxor", hashes["quickXorHash"]
    return None, None


def local_hash(path, kind):
    return sha1_file(path) if kind == "sha1" else quickxor_file(path)


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
        up = self.upload(remote_path, local_path, if_match)
        fresh = self.item(remote_path)  # never trust the upload response alone
        if not fresh or fresh.get("id") != (up or {}).get("id"):
            raise VerifyError(f"{remote_path}: item missing or replaced after upload")
        kind, got = remote_hash(fresh)
        if not kind:
            raise VerifyError(f"{remote_path}: no sha1Hash or quickXorHash in metadata")
        want = local_hash(local_path, kind)
        if got != want:
            raise VerifyError(f"{remote_path}: remote {kind} {got} != local {want}")
        return fresh
