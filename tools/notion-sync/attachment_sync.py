# -*- coding: utf-8 -*-
"""
Notion-hosted attachments -> <owner root>/Imported-Notion/Attachments/.

Supported (Notion-hosted, i.e. file type "file"):
    docx   -- `file` blocks whose name ends in .docx          -> [name](Attachments/<file>)
    image  -- `image` blocks with .png/.jpg/.jpeg/.webp         -> ![caption](Attachments/<file>)
              (images carry no API name: notion-image-<block id><ext>, ext from the
               stable file path of the URL, never from its signature/query)
Every other Notion-hosted attachment keeps the renderer's fail-closed behaviour
(<type>:notion_hosted_unresolved).

Identity is the Notion block id, never the signed URL. state.json keeps one record
per synced block under the top-level "attachments" key:
    block_id, block_type, attachment_type, parent_notion_id, owner, original_filename,
    local_path (LAVCCA-relative), content_sha256, size_bytes, block_last_edited_time,
    last_synced_at
The signed URL is used only as download transport inside download_to_temp(); it is
never returned, stored, logged, or put into an exception message.

Planning (pure, deterministic, read-only) decides each block's local path and the
markdown link target, so the page hash computed at classification, at preflight and
on every later run is identical:
    recorded block, local file intact & readable  -> reuse (or refresh if the block's
                                                     last_edited_time changed)
    unrecorded block                              -> download to <owner dir>/<file>,
                                                     or <stem> (<block id>)<ext> if taken
Any doubt (owner not deterministic, local attachment modified / missing / unreadable
-- e.g. locked by Word --, name or type changed, parent changed, both names taken) is a
critical render item for that block only: the page is not written this run, nothing
is overwritten, and the next run re-verifies. Planned downloads of OTHER blocks on such
a page can still be committed on their own (production_apply: attachment-only).

No import-time side effects.
"""
import os, io, re, time, zipfile, hashlib, posixpath, tempfile, unicodedata, urllib.parse, urllib.request, urllib.error
from datetime import datetime

import canary_apply as ca

DOCX_EXTS = (".docx",)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
SUPPORTED_EXTS = DOCX_EXTS  # kept for callers of is_supported() (DOCX file blocks)
SUPPORTED_TYPE = "docx"
IMAGE_TYPE = "image"
# Owner roots are fixed by the vault layout; the frontmatter owner must agree.
# {top-level folder: frontmatter owner}; set from config.json "owner_roots" (team names are not code).
OWNER_ROOTS = {}


def configure_owner_roots(mapping):
    OWNER_ROOTS.clear()
    OWNER_ROOTS.update(mapping)
ATTACHMENT_SUBDIR = "Imported-Notion/Attachments"
STATE_KEY = "attachments"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS = 3
DOCX_REQUIRED_MEMBERS = ("[Content_Types].xml", "word/document.xml")
RECORD_FIELDS = ("block_id", "block_type", "attachment_type", "parent_notion_id", "owner", "original_filename",
                 "local_path", "content_sha256", "size_bytes", "block_last_edited_time", "last_synced_at")

# Windows-invalid characters plus characters that break Obsidian markdown link targets
# (# = heading anchor, ^ = block ref, [ ] | = link syntax, % = percent-encoding).
UNSAFE_FILENAME_CHARS = set('\\/:*?"<>|#^[]%')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


class AttachmentError(Exception):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


def is_supported(name):
    return (name or "").lower().endswith(DOCX_EXTS)


def sanitize_filename(name, exts=DOCX_EXTS):
    """Original name kept as far as possible: NFC, unsafe/control chars -> '_',
    whitespace collapsed, trailing dots/spaces stripped, reserved device names
    prefixed. Returns '' if nothing usable remains or the extension is not in exts."""
    n = unicodedata.normalize("NFC", name or "")
    n = "".join("_" if (ch in UNSAFE_FILENAME_CHARS or ord(ch) < 32) else ch for ch in n)
    n = re.sub(r"\s+", " ", n).strip()
    prev = None
    while prev != n:
        prev = n
        n = n.strip().rstrip(".")
    ext = next((e for e in exts if n.lower().endswith(e)), None)
    stem = n[:-len(ext)] if ext else ""
    if not stem.strip():
        return ""
    if stem.upper() in WINDOWS_RESERVED:
        n = "_" + n
    return n


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _url_path_ext(d):
    """Extension of the file path of a Notion-hosted URL (the path is stable for one
    uploaded file; the query carries the signature and is never looked at)."""
    url = (d.get(d.get("type")) or {}).get("url") or ""
    return os.path.splitext(urllib.parse.unquote(urllib.parse.urlparse(url).path))[1].lower()


def describe_block(t, d):
    """(attachment_type, api_name, ext) for a supported Notion-hosted block, else None."""
    if (d or {}).get("type") != "file":
        return None
    api_name = d.get("name") or ""
    if t == "file":
        name = api_name or os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(
            (d.get("file") or {}).get("url") or "").path))
        return (SUPPORTED_TYPE, name, ".docx") if is_supported(name) else None
    if t == "image":
        ext = os.path.splitext(api_name)[1].lower() if api_name else _url_path_ext(d)
        return (IMAGE_TYPE, api_name, ext) if ext in IMAGE_EXTS else None
    return None


def owner_dir_for(md_lavcca_rel, fm_owner):
    """(owner, '<root>/Imported-Notion/Attachments') or (None, reason)."""
    top = (md_lavcca_rel or "").split("/")[0]
    owner = OWNER_ROOTS.get(top)
    if owner is None:
        return None, f"markdown {md_lavcca_rel!r} is not under an owner root {sorted(OWNER_ROOTS)}"
    if (fm_owner or "") != owner:
        return None, f"frontmatter owner {fm_owner!r} != owner of {top} ({owner!r})"
    return owner, f"{top}/{ATTACHMENT_SUBDIR}"


def state_attachments(state):
    att = (state or {}).get(STATE_KEY) or {}
    return att if isinstance(att, dict) else {}


class AttachmentPlanner:
    """One per run phase. Holds the recorded attachments and the paths claimed by
    blocks planned in this run (keyed by block id, so re-rendering the same page
    with the same planner yields the same paths)."""

    def __init__(self, lavcca_root, recorded, legacy_cutoff=None):
        self.lavcca_root = lavcca_root
        self.recorded = dict(recorded or {})
        self.claims = {}  # lower-cased LAVCCA-relative path -> block_id
        # Blocks created after this instant (the migration) are never matched to a migrated
        # local file by NAME: a new upload with an old file's name gets its own download.
        self.legacy_cutoff = _parse_ts(legacy_cutoff) if isinstance(legacy_cutoff, str) else legacy_cutoff

    def for_page(self, nid, md_lavcca_rel, fm_owner):
        return PageAttachments(self, nid, md_lavcca_rel, fm_owner)

    def _abs(self, lavcca_rel):
        return os.path.join(self.lavcca_root, lavcca_rel.replace("/", os.sep))

    def taken(self, lavcca_rel, bid):
        key = lavcca_rel.lower()
        if os.path.lexists(self._abs(lavcca_rel)):
            return True
        if any(r.get("local_path", "").lower() == key and b != bid for b, r in self.recorded.items()):
            return True
        return self.claims.get(key, bid) != bid


def _parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class PageAttachments:
    def __init__(self, planner, nid, md_lavcca_rel, fm_owner):
        self.planner, self.nid, self.md = planner, nid, md_lavcca_rel
        self.fm_owner = fm_owner
        self.plans = []

    def is_recorded(self, bid):
        return bid in self.planner.recorded

    def legacy_allowed(self, block):
        """Name-based reuse of a migrated local link only for blocks that existed at the
        migration (created_time <= cutoff). Unknown created_time -> allowed (old data)."""
        cutoff = self.planner.legacy_cutoff
        created = _parse_ts((block or {}).get("created_time"))
        return cutoff is None or created is None or created <= cutoff

    def handles(self, t, bid, d):
        return self.is_recorded(bid) or describe_block(t, d) is not None

    def link_target(self, local_path):
        return posixpath.relpath(local_path, posixpath.dirname(self.md))

    def resolve(self, t, bid, d, block):
        """Returns (link_target, None) or (None, critical_item)."""
        desc = describe_block(t, d)
        shown = (desc[1] if desc else None) or d.get("name")

        def problem(kind, detail):
            return None, {"type": f"{t}:{kind}", "id": bid, "name": shown, "detail": detail}

        if desc is None:
            return problem("attachment_type_unsupported", f"recorded block is no longer a supported {t}")
        atype, api_name, ext = desc
        owner, odir = owner_dir_for(self.md, self.fm_owner)
        if owner is None:
            return problem("attachment_owner_unresolved", odir)
        let = (block or {}).get("last_edited_time")
        rec = self.planner.recorded.get(bid)
        if rec is not None:
            lp = rec.get("local_path") or ""
            if rec.get("parent_notion_id") != self.nid:
                return problem("attachment_parent_changed", f"recorded parent {rec.get('parent_notion_id')}")
            if (rec.get("block_type") or "file") != t or (rec.get("attachment_type") or SUPPORTED_TYPE) != atype:
                return problem("attachment_type_changed", f"recorded {rec.get('block_type')}/{rec.get('attachment_type')}")
            if (rec.get("original_filename") or "") != (api_name or ""):
                return problem("attachment_name_changed", f"recorded {rec.get('original_filename')!r}")
            if not lp.lower().endswith(ext) or not lp.startswith(odir + "/"):
                return problem("attachment_path_mismatch", lp)
            ap = self.planner._abs(lp)
            if os.path.islink(ap) or (os.path.lexists(ap) and not os.path.isfile(ap)):
                return problem("attachment_path_not_a_file", lp)
            if not os.path.lexists(ap):
                action = "redownload"  # recorded copy is gone: restored to the same path (self-heal)
            else:
                try:
                    digest = sha256_file(ap)
                except OSError as e:  # e.g. locked by Word: this block only, re-verified next run
                    return problem("attachment_local_unreadable", f"{lp}: {type(e).__name__}")
                if digest != rec.get("content_sha256"):
                    return problem("attachment_local_modified", lp)  # local edit: never overwritten
                action = "reuse" if rec.get("block_last_edited_time") == let else "refresh"
        else:
            if atype == IMAGE_TYPE:
                fn = sanitize_filename(api_name, IMAGE_EXTS) if api_name else f"notion-image-{bid}{ext}"
            else:
                fn = sanitize_filename(api_name, DOCX_EXTS)
            if not fn:
                return problem("attachment_name_unusable", "no usable filename after sanitization")
            lp = f"{odir}/{fn}"
            if self.planner.taken(lp, bid):
                stem, fext = os.path.splitext(fn)
                lp = f"{odir}/{stem} ({bid}){fext}"
                if self.planner.taken(lp, bid):
                    return problem("attachment_collision", f"{odir}/{fn} and its collision-safe name are both taken")
            action = "download"
        self.planner.claims[lp.lower()] = bid
        target = self.link_target(lp)
        self.plans.append({"block_id": bid, "block_type": t, "attachment_type": atype, "ext": ext,
                           "parent_notion_id": self.nid, "owner": owner, "original_filename": api_name or None,
                           "local_path": lp, "link_target": target, "parent_markdown": self.md,
                           "block_last_edited_time": let, "action": action, "supported_type": atype})
        return target, None


# ============================================================
# Download (apply mode only)
# ============================================================
def http_get_file(url, max_bytes=MAX_DOWNLOAD_BYTES, timeout=60):
    """GET a pre-signed URL (no Notion auth header). Returns (status, headers, bytes).
    Errors are reported by status/type only -- the URL never appears in a message."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise AttachmentError("TOO_LARGE", f"more than {max_bytes} bytes")
            return r.getcode(), {k.lower(): v for k, v in r.headers.items()}, data
    except urllib.error.HTTPError as e:
        return e.code, {}, b""


def validate_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            bad = z.testzip()
    except (zipfile.BadZipFile, ValueError, OSError) as e:
        raise AttachmentError("NOT_A_DOCX", type(e).__name__)
    missing = [m for m in DOCX_REQUIRED_MEMBERS if m not in names]
    if missing or bad:
        raise AttachmentError("NOT_A_DOCX", f"missing {missing}" if missing else "corrupt member")


def image_format(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image(data, ext):
    """Real image signature, and it must agree with the planned file extension."""
    fmt = image_format(data)
    if fmt is None:
        raise AttachmentError("NOT_AN_IMAGE", "no PNG/JPEG/WEBP signature")
    expected = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}.get(ext)
    if fmt != expected:
        raise AttachmentError("IMAGE_FORMAT_MISMATCH", f"content is {fmt}, planned extension {ext}")


def validate_content(plan, data):
    if plan.get("attachment_type", SUPPORTED_TYPE) == IMAGE_TYPE:
        validate_image(data, plan["ext"])
    else:
        validate_docx(data)


def _fetch_current_url(api, plan):
    code, b = api("GET", f"/blocks/{plan['block_id']}")
    if code != 200:
        raise AttachmentError("BLOCK_FETCH_FAILED", f"HTTP {code}")
    if b.get("archived") or b.get("in_trash"):
        raise AttachmentError("BLOCK_ARCHIVED", "block archived or in trash")
    t = b.get("type")
    d = b.get(t) or {}
    desc = describe_block(t, d)
    if t != plan.get("block_type", "file") or desc is None or desc[0] != plan.get("attachment_type", SUPPORTED_TYPE):
        raise AttachmentError("BLOCK_CHANGED", f"block is now {t}/{d.get('type')}")
    if (desc[1] or None) != (plan["original_filename"] or None) or desc[2] != plan.get("ext", desc[2]):
        raise AttachmentError("BLOCK_CHANGED", "file name/extension changed since planning")
    parent = b.get("parent") or {}
    if ca.norm_id(parent.get("page_id")) != plan["parent_notion_id"]:
        raise AttachmentError("BLOCK_CHANGED", "parent changed since planning")
    url = (d.get("file") or {}).get("url") or ""
    loopback_ok = ca.env("E2E_LOOPBACK") == "1" and url.startswith("http://127.0.0.1:")
    if not url.startswith("https://") and not loopback_ok:  # loopback: E2E test harness only
        raise AttachmentError("NO_DOWNLOAD_URL", "no https download url")
    return url


def download_to_temp(api, plan, tmp_dir, http_get=http_get_file, max_attempts=MAX_DOWNLOAD_ATTEMPTS,
                     sleep=time.sleep):
    """Fresh block fetch -> signed URL -> GET -> HTTP 200, non-empty, Content-Length,
    content validation (DOCX structure / image signature) -> temp file + fsync +
    read-back hash. A failed or expired download re-fetches the block for a fresh URL,
    at most max_attempts times in total.
    Returns {"tmp_path", "sha256", "size_bytes"}; raises AttachmentError."""
    last = "no attempt"
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            sleep(2 ** (attempt - 2))
        url = _fetch_current_url(api, plan)  # structural changes are final, not retried
        try:
            status, headers, data = http_get(url)
        except AttachmentError:
            raise
        except Exception as e:  # network error: retry with a fresh URL
            last = f"network error ({type(e).__name__})"
            continue
        if status != 200:
            last = f"HTTP {status}"  # 403/400 = signature expired -> fresh URL next attempt
            continue
        if not data:
            raise AttachmentError("EMPTY_DOWNLOAD", "0 bytes received")
        cl = (headers or {}).get("content-length")
        if cl is not None and str(cl).isdigit() and int(cl) != len(data):
            last = f"Content-Length {cl} != {len(data)} bytes received"
            continue
        validate_content(plan, data)
        digest = hashlib.sha256(data).hexdigest()
        os.makedirs(tmp_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".notion_att_", suffix=".part", dir=tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                ca._fsync(f)
            if sha256_file(tmp) != digest:
                raise AttachmentError("TEMP_VERIFY_FAILED", "temp file hash mismatch")
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return {"tmp_path": tmp, "sha256": digest, "size_bytes": len(data)}
    raise AttachmentError("DOWNLOAD_FAILED", f"{max_attempts} attempts: {last}")


def commit_new_file(src, dst):
    """Move src to dst, refusing to overwrite: os.rename fails on Windows if dst
    exists; elsewhere a hard link (which also fails if dst exists) + unlink."""
    if os.name == "nt":
        os.rename(src, dst)
    else:
        os.link(src, dst)
        os.remove(src)


def public_plan(p):
    """Report view of a plan: no temp paths, no bytes, never a URL (plans hold none)."""
    return {k: p[k] for k in ("owner", "attachment_type", "original_filename", "block_id", "local_path",
                              "parent_markdown", "supported_type", "action", "link_target") if k in p}
