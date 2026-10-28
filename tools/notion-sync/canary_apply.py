# -*- coding: utf-8 -*-
"""
LAVCCA (라브카) Notion -> Obsidian CANARY APPLY (single notion_id only).

This module has no import-time side effects (no token, no API calls, no file
writes) so it can be unit-tested offline. The Notion API is injected as a
callable `api(method, path, body=None) -> (http_status, json)`.

Scope of this module:
- exactly ONE explicitly named notion_id per invocation
- only an existing ACTIVE managed:notion markdown file is ever written
- never: create, delete, move, rename, archive targets, 06_KNOWLEDGE,
  managed:obsidian, attachments download, Notion writes

Write path (apply=True), in order; any failure before step 9 leaves the
original file untouched:
  1. CLI id validation           6. render Notion blocks -> Markdown
  2. local target selection      7. build new file (frontmatter preserved)
  3. strict frontmatter check    8. validate new file (identity, UTF-8, hash)
  4. remote checks + descendant  9. byte-for-byte backup, atomic replace
  5. conflict check             10. atomic state.json write (rollback on failure)
"""
import os, re, json, hashlib, tempfile, urllib.parse
from datetime import datetime, timezone

ID_RE = re.compile(r'^[0-9a-f]{32}$')
IDENTITY_KEYS = ("project", "owner", "source", "managed", "notion_id", "notion_path")
SYNC_KEYS = ("notion_last_edited_time", "last_synced_content_hash", "last_synced_at")
EMPTY_TITLE = "제목 없음"
MAX_PARENT_HOPS = 50
IDENTITY_SKIP_TOP = {".obsidian", ".git", ".trash"}
# Brand rename NATAM -> LAVCCA (2026-10-28): values written before it stay valid until the vault
# itself is migrated (docs/brand-migration.md). New names first, legacy names as fallback.
PROJECT_VALUES = ("LAVCCA", "NATAM")
LEGACY_CONFIG_KEYS = {"lavcca_root_path": "natam_root_path", "lavcca_root_id": "natam_root_id"}


def normalize_config(cfg):
    """Accept a config.json written before the rename. Old and new key with different values -> refuse."""
    out = dict(cfg)
    for new, old in LEGACY_CONFIG_KEYS.items():
        if old in out:
            if new in out and out[new] != out[old]:
                raise ValueError(f"config.json: {new} and legacy {old} disagree")
            out[new] = out.pop(old)
    if not isinstance(out.get("owner_roots"), dict):
        # without it every attachment would silently fail closed as "not under an owner root"
        raise ValueError('config.json: "owner_roots" ({folder: owner}) is required')
    return out


def env(name, default=None):
    """LAVCCA_<name>, else the pre-rename NATAM_<name>."""
    v = os.environ.get("LAVCCA_" + name)
    return v if v is not None else os.environ.get("NATAM_" + name, default)


class CanaryRefused(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


class PaginationError(Exception):
    pass


class ContentFetchError(Exception):
    pass


class RenderUnsupported(Exception):
    def __init__(self, items):
        self.items = items
        super().__init__(f"unsupported/critical blocks: {items}")


class WriteVerificationError(Exception):
    pass


def norm_id(x):
    return (x or "").replace("-", "").strip().lower()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


# ============================================================
# CLI gating (pure; used by notion_sync.py and tests)
# ============================================================
def resolve_cli_mode(apply_flag, notion_ids, adopt_baseline=False, dry_run=False, apply_updates=False):
    """Returns (mode, notion_id_or_None, refusal_message_or_None).
    mode: 'full_dry_run' | 'canary_dry_run' | 'canary_apply' | 'adopt_baseline_preview' |
          'adopt_baseline_write' | 'apply_updates_preview' | 'apply_updates_write' | 'refuse'."""
    notion_ids = notion_ids or []
    if apply_updates:
        if apply_flag or notion_ids or adopt_baseline:
            return "refuse", None, "--apply-updates cannot be combined with --apply, --notion-id or --adopt-baseline"
        return ("apply_updates_preview" if dry_run else "apply_updates_write"), None, None
    if adopt_baseline:
        if apply_flag or notion_ids:
            return "refuse", None, "--adopt-baseline cannot be combined with --apply or --notion-id"
        # write mode touches state.json only; markdown is never written by adoption
        return ("adopt_baseline_preview" if dry_run else "adopt_baseline_write"), None, None
    if len(notion_ids) > 1:
        return "refuse", None, "only one --notion-id is allowed (multi-ID apply is not implemented)"
    if apply_flag and not notion_ids:
        return "refuse", None, "--apply requires --notion-id <ID>; full APPLY is not implemented"
    if not notion_ids:
        return "full_dry_run", None, None
    nid = norm_id(notion_ids[0])
    if not ID_RE.match(nid):
        return "refuse", None, f"--notion-id must be exactly one 32-hex Notion id (got {notion_ids[0]!r})"
    return ("canary_apply" if apply_flag else "canary_dry_run"), nid, None


# ============================================================
# Frontmatter (strict for the write path)
# ============================================================
FM_LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*):(?:\s(.*))?$')


def split_frontmatter_strict(text):
    """Returns (fm_dict, fm_lines, body). body = everything after the closing '---' line."""
    if "\r" in text:
        raise CanaryRefused("MALFORMED_FRONTMATTER", "CR/CRLF line endings are not supported by the write path")
    if not text.startswith("---\n"):
        raise CanaryRefused("MALFORMED_FRONTMATTER", "file does not start with '---' frontmatter")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise CanaryRefused("MALFORMED_FRONTMATTER", "closing '---' not found")
    fm_lines = text[4:end].split("\n") if end > 3 else []
    body = text[end + 5:]
    fm = {}
    for ln in fm_lines:
        m = FM_LINE_RE.match(ln)
        if not m:
            raise CanaryRefused("MALFORMED_FRONTMATTER", f"unparseable frontmatter line {ln!r}")
        k, v = m.group(1), (m.group(2) or "").strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        if k in fm:
            raise CanaryRefused("MALFORMED_FRONTMATTER", f"duplicate key {k!r}")
        fm[k] = v
    return fm, fm_lines, body


def validate_identity(fm, nid):
    missing = [k for k in IDENTITY_KEYS if not fm.get(k)]
    if missing:
        raise CanaryRefused("MALFORMED_FRONTMATTER", f"missing identity keys {missing}")
    if fm["managed"] != "notion":
        raise CanaryRefused("MANAGED_NOT_NOTION", f"managed={fm['managed']!r}")
    if fm["project"] not in PROJECT_VALUES or fm["source"] != "notion":
        raise CanaryRefused("MALFORMED_FRONTMATTER", f"project={fm['project']!r} source={fm['source']!r}")
    if norm_id(fm["notion_id"]) != nid or not ID_RE.match(norm_id(fm["notion_id"])):
        raise CanaryRefused("MALFORMED_FRONTMATTER", f"notion_id {fm['notion_id']!r} does not match {nid}")


def build_frontmatter(fm_lines, updates):
    """Keep every existing line verbatim and in order; replace/append only SYNC_KEYS."""
    assert set(updates) <= set(SYNC_KEYS)
    out, seen = [], set()
    for ln in fm_lines:
        m = FM_LINE_RE.match(ln)
        k = m.group(1) if m else None
        if k in updates:
            out.append(f'{k}: "{updates[k]}"')
            seen.add(k)
        else:
            out.append(ln)
    for k in SYNC_KEYS:
        if k in updates and k not in seen:
            out.append(f'{k}: "{updates[k]}"')
    return "---\n" + "\n".join(out) + "\n---\n"


# ============================================================
# Vault identity inventory + location policy
# ============================================================
LENIENT_FM_RE = re.compile(r'^---\r?\n(.*?)\r?\n---\r?\n', re.DOTALL)


def _lenient_fm_value(fm_text, key):
    m = re.search(rf'^{key}:\s*"?([^"\r\n]*)"?\s*$', fm_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def classify_location(vault_rel, cfg, lavcca_rel):
    if not vault_rel.startswith(lavcca_rel + "/"):
        return "outside_lavcca"
    sub = vault_rel[len(lavcca_rel) + 1:]
    if sub.startswith("99_ARCHIVE/"):
        return "archive"
    for p in cfg["protected_relative_paths"]:
        if sub == p or sub.startswith(p + "/"):
            return "protected"
    return "active"


def scan_vault_identities(cfg):
    vault = cfg["vault_root"]
    lavcca_rel = os.path.relpath(cfg["lavcca_root_path"], vault).replace(os.sep, "/")
    rows = []
    for root, dirs, files in os.walk(vault):
        rel_root = os.path.relpath(root, vault).replace(os.sep, "/")
        if rel_root.split("/")[0] in IDENTITY_SKIP_TOP or \
                any(rel_root == f"{lavcca_rel}/{p}" or rel_root.startswith(f"{lavcca_rel}/{p}/") for p in ("_sync", "_migration")):
            dirs[:] = []
            continue
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, vault).replace(os.sep, "/")
            with open(full, "rb") as f:
                head = f.read(8192).decode("utf-8", errors="replace")
            m = LENIENT_FM_RE.match(head)
            fm_malformed = m is None
            if fm_malformed:
                # Frontmatter opened but never closed: still record the identity so the
                # target is refused as MALFORMED rather than reported as missing.
                if not head.startswith("---"):
                    continue
                fm_text = head
            else:
                fm_text = m.group(1)
            nid = norm_id(_lenient_fm_value(fm_text, "notion_id"))
            if not nid:
                continue
            rows.append({"vault_path": rel, "abs_path": full, "notion_id": nid,
                         "managed": _lenient_fm_value(fm_text, "managed"),
                         "fm_malformed": fm_malformed,
                         "location": classify_location(rel, cfg, lavcca_rel)})
    return rows


def select_target(nid, identities):
    matches = [r for r in identities if r["notion_id"] == nid]
    if not matches:
        raise CanaryRefused("NOT_FOUND_LOCAL", "no vault markdown carries this notion_id (CREATE is not supported)")
    if any(r["managed"] != "notion" for r in matches):
        raise CanaryRefused("MANAGED_NOT_NOTION",
                            f"notion_id appears on non-managed:notion file(s): "
                            f"{[r['vault_path'] for r in matches if r['managed'] != 'notion']}")
    if len(matches) > 1:
        raise CanaryRefused("DUPLICATE_LOCAL_ID", f"{[r['vault_path'] for r in matches]}")
    t = matches[0]
    if t["location"] == "archive":
        raise CanaryRefused("ARCHIVE_TARGET", f"{t['vault_path']} is in 99_ARCHIVE (archive APPLY not supported)")
    if t["location"] != "active":
        raise CanaryRefused("PROTECTED_TARGET", f"{t['vault_path']} is in a protected location ({t['location']})")
    if t.get("fm_malformed"):
        raise CanaryRefused("MALFORMED_FRONTMATTER", f"{t['vault_path']}: frontmatter is not closed")
    return t


def id_to_vault_path(identities):
    """notion_id -> vault-relative path, for wikilinks (active and archive managed:notion only)."""
    out = {}
    for r in identities:
        if r["managed"] == "notion" and r["location"] in ("active", "archive") and not r.get("fm_malformed"):
            out.setdefault(r["notion_id"], r["vault_path"])
    return out


# ============================================================
# Remote: descendant verification + block tree fetch (read-only)
# ============================================================
def verify_descendant(api, parent, root_id):
    chain, seen, cur = [], set(), parent
    for _ in range(MAX_PARENT_HOPS):
        if not cur:
            return "UNKNOWN", chain + ["<parent missing>"]
        t = cur.get("type")
        if t == "workspace":
            return "OUTSIDE", chain + ["workspace"]
        if t == "page_id":
            kind, pid = "page", cur.get("page_id")
        elif t in ("database_id", "data_source_id"):
            kind, pid = ("database", cur.get("database_id")) if cur.get("database_id") else ("data_source", cur.get("data_source_id"))
        elif t == "block_id":
            kind, pid = "block", cur.get("block_id")
        else:
            return "UNKNOWN", chain + [f"<unsupported parent type {t!r}>"]
        pid = norm_id(pid)
        if not pid:
            return "UNKNOWN", chain + [f"<{t} without id>"]
        chain.append(f"{kind}:{pid}")
        if pid == root_id:
            return "DESCENDANT", chain
        if pid in seen:
            return "UNKNOWN", chain + ["<parent cycle>"]
        seen.add(pid)
        endpoint = {"page": "/pages/", "database": "/databases/", "data_source": "/data_sources/", "block": "/blocks/"}[kind] + pid
        code, obj = api("GET", endpoint)
        if code != 200:
            return "UNKNOWN", chain + [f"<HTTP {code} on {kind}>"]
        cur = obj.get("parent")
    return "UNKNOWN", chain + [f"<exceeded {MAX_PARENT_HOPS} hops>"]


def paginate(fetch_page_fn, max_pages):
    """Same guards as notion_sync.paginate: fetch failure, has_more without
    cursor, cursor repeat, max-page guard, duplicate id within one result set."""
    seen_cursors, seen_ids, cursor, pages = set(), set(), None, 0
    while True:
        pages += 1
        if pages > max_pages:
            raise PaginationError(f"max_pagination_pages ({max_pages}) exceeded")
        ok, results, has_more, next_cursor = fetch_page_fn(cursor)
        if not ok:
            raise PaginationError("page fetch failed (API error)")
        for r in results:
            rid = norm_id(r.get("id"))
            if rid:
                if rid in seen_ids:
                    raise PaginationError(f"duplicate id {rid} within a single paginated result set")
                seen_ids.add(rid)
            yield r
        if not has_more:
            return
        if not next_cursor:
            raise PaginationError("has_more=true but next_cursor missing/empty")
        if next_cursor in seen_cursors:
            raise PaginationError("next_cursor repeated -- would loop forever")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def fetch_block_tree(api, block_id, cfg, _depth=0, _max_depth=25):
    """Full block tree with children attached as b['_children']. Raises
    ContentFetchError on any failure -- never returns a partial tree."""
    if _depth > _max_depth:
        raise ContentFetchError(f"{block_id}: block tree deeper than {_max_depth}")

    def fetch_page(cursor):
        path = f"/blocks/{block_id}/children?page_size={cfg['api_page_size']}"
        if cursor:
            path += f"&start_cursor={urllib.parse.quote(cursor)}"
        code, resp = api("GET", path)
        if code != 200:
            return False, [], False, None
        return True, resp.get("results", []), resp.get("has_more", False), resp.get("next_cursor")

    try:
        blocks = list(paginate(fetch_page, cfg["max_pagination_pages"]))
    except PaginationError as e:
        raise ContentFetchError(f"{block_id}: {e}")
    for b in blocks:
        if b.get("has_children") and b.get("type") not in ("child_page", "child_database"):
            b["_children"] = fetch_block_tree(api, b["id"], cfg, _depth + 1, _max_depth)
        else:
            b["_children"] = []
    return blocks


def page_title(page):
    for v in (page.get("properties") or {}).values():
        if isinstance(v, dict) and v.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in v.get("title", []))
    return ""


# ============================================================
# Renderer: Notion blocks -> Markdown
# ============================================================
NOTION_HREF_ID_RE = re.compile(r'(?:notion\.so/|^/)[^?#]*?([0-9a-f]{32})(?:[?#]|$)')
LIST_KINDS = {"bulleted", "numbered", "to_do"}
CONTAINER_TYPES = {"column_list", "column", "synced_block"}
NONCONTENT_PLACEHOLDER_TYPES = {"table_of_contents", "breadcrumb"}
FILE_TYPES = {"image", "file", "pdf", "video", "audio"}
INDENT = "    "


def _match_bracket(s, i, open_ch, close_ch):
    """s[i] == open_ch. Returns index of the matching close_ch, honouring nesting
    and backslash escapes, or -1. Newlines end the search (links are single-line)."""
    depth, j = 0, i
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            return -1
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def parse_md_links(text):
    """Inline markdown links [text](target) / ![text](target), scanned character by
    character so link text may contain nested or escaped brackets
    (e.g. '[사업 계획서 [자동 저장].pptx](...)'). Wikilinks ([[...]]) are not links
    here: their closing bracket is not followed by '('. Returns (is_image, text, target)."""
    out, i, n = [], 0, len(text or "")
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] != "[":
            i += 1
            continue
        end_text = _match_bracket(text, i, "[", "]")
        if end_text == -1 or end_text + 1 >= n or text[end_text + 1] != "(":
            i += 1
            continue
        end_target = _match_bracket(text, end_text + 1, "(", ")")
        if end_target == -1:
            i += 1
            continue
        label = re.sub(r'\\(.)', r'\1', text[i + 1:end_text])
        target = text[end_text + 2:end_target].strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        out.append((i > 0 and text[i - 1] == "!", label, target))
        i = end_target + 1
    return out


class RenderContext:
    def __init__(self, id_to_path, existing_body="", attachments=None, db_links=None):
        self.id_to_path = id_to_path
        # attachment_sync.PageAttachments (production sync only); None keeps every
        # Notion-hosted file on the fail-closed path.
        self.attachments = attachments
        # {child_database_id: (label, href)} for THIS page, from load_child_database_links();
        # None/missing id keeps the child_database placeholder.
        self.db_links = db_links or {}
        self.local_links = [(label, target) for (_, label, target) in parse_md_links(existing_body or "")
                            if not re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I)]
        self.placeholders = []
        self.critical = []

    def wikilink(self, nid, title):
        title = (title or EMPTY_TITLE).replace("|", " ").replace("]]", "] ]")
        path = self.id_to_path.get(nid)
        if path:
            return f"[[{path[:-3] if path.endswith('.md') else path}|{title}]]"
        self.placeholders.append({"kind": "unmapped_page_link", "id": nid})
        return f"{title} <!-- notion-sync: page {nid} is not present in the vault -->"

    def resolve_local_attachment(self, name):
        """Reuse an attachment link that already exists in the current local file.
        Exactly one distinct local target must match, otherwise None."""
        if not name:
            return None
        variants = {name, name.replace(" ", "_")}
        targets = {t for (text, t) in self.local_links
                   if text in variants or os.path.basename(urllib.parse.unquote(t)) in variants}
        return targets.pop() if len(targets) == 1 else None


MIGRATION_LINK_MAP = ("_migration", "link-mapping.csv")
URL_SCHEME_RE = re.compile(r'^[a-z][a-z0-9+.-]*:', re.I)


def load_child_database_links(lavcca_root):
    """{source markdown LAVCCA-relative path: {child_database_id: (label, href)}} taken from
    the migration link map. A row is used only if: status=resolved, target is a .csv that
    exists, target_notion_id is a 32-hex id, the raw href (relative to the source markdown)
    resolves to exactly that target, label/href are link-safe, and the (markdown, database)
    pair is unique. Anything else keeps the child_database placeholder -- never a guess."""
    import csv
    import posixpath
    path = os.path.join(lavcca_root, *MIGRATION_LINK_MAP)
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return {}
    found, dup = {}, set()
    for r in rows:
        src, tgt = (r.get("source_obsidian_path") or ""), (r.get("target_obsidian_path") or "")
        href, label, dbid = (r.get("raw_href_or_wikilink_target") or ""), (r.get("link_text") or ""), \
            norm_id(r.get("target_notion_id"))
        if r.get("status") != "resolved" or not ID_RE.match(dbid) or not tgt.lower().endswith(".csv"):
            continue
        if not href or not label or URL_SCHEME_RE.match(href) or any(c in href for c in ")\n<>") \
                or any(c in label for c in "[]\n"):
            continue
        if posixpath.normpath(posixpath.join(posixpath.dirname(src), urllib.parse.unquote(href))) != tgt:
            continue
        if not os.path.isfile(os.path.join(lavcca_root, *tgt.split("/"))):
            continue
        key = (src, dbid)
        if key in found:
            dup.add(key)
        found[key] = (label, href)
    out = {}
    for (src, dbid), link in found.items():
        if (src, dbid) not in dup:
            out.setdefault(src, {})[dbid] = link
    return out


def _wrap(s, mark):
    core = s.strip()
    if not core:
        return s
    lead = s[:len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


def _href(url):
    return f"<{url}>" if (" " in url or ")" in url) else url


def rich_text_to_md(rts, ctx):
    out = []
    for r in rts or []:
        t = r.get("type")
        text = r.get("plain_text", "")
        href = r.get("href")
        if t == "mention":
            m = r.get("mention") or {}
            mt = m.get("type")
            if mt in ("page", "database"):
                out.append(ctx.wikilink(norm_id(m[mt].get("id")), text))
                continue
            if mt == "link_mention":
                href = (m.get("link_mention") or {}).get("href") or href
        if t == "equation":
            out.append(f"${(r.get('equation') or {}).get('expression', text)}$")
            continue
        if not text:
            continue
        if href:
            hm = NOTION_HREF_ID_RE.search(href)
            if hm and hm.group(1) in ctx.id_to_path:
                out.append(ctx.wikilink(hm.group(1), text))
                continue
        a = r.get("annotations") or {}
        s = text
        if a.get("code"):
            fence = "``" if "`" in s else "`"
            s = _wrap(s, fence) if s.strip() else s
        if a.get("strikethrough"):
            s = _wrap(s, "~~")
        if a.get("italic"):
            s = _wrap(s, "*")
        if a.get("bold"):
            s = _wrap(s, "**")
        if href:
            s = f"[{s}]({_href(href)})"
        out.append(s)
    return "".join(out)


def _indent(text, prefix=INDENT):
    return "\n".join((prefix + ln) if ln else ln for ln in text.split("\n"))


def _continuation(text, width):
    """Multi-line list item text: indent lines after the first."""
    lines = text.split("\n")
    return "\n".join([lines[0]] + [(" " * width + ln) if ln else ln for ln in lines[1:]])


def _quote(text):
    return "\n".join(("> " + ln) if ln else ">" for ln in text.split("\n"))


def _table_cell(rts, ctx):
    return rich_text_to_md(rts, ctx).replace("|", "\\|").replace("\n", "<br>")


def render_blocks(blocks, ctx):
    out, prev = "", None
    for b in blocks:
        kind, text = render_block(b, ctx)
        if text is None or text == "":
            continue
        if out:
            out += "\n" if (kind in LIST_KINDS and kind == prev) else "\n\n"
        out += text
        prev = kind
    return out


def _with_children(head, b, ctx):
    child = render_blocks(b.get("_children") or [], ctx)
    return head + ("\n" + _indent(child) if child else "")


def render_block(b, ctx):
    t = b.get("type")
    d = b.get(t) or {}
    bid = norm_id(b.get("id"))
    rt = lambda key="rich_text": rich_text_to_md(d.get(key) or [], ctx)

    if t == "paragraph":
        txt = rt()
        child = render_blocks(b.get("_children") or [], ctx)
        return "paragraph", txt + ("\n\n" + _indent(child) if child else "")
    if t in ("heading_1", "heading_2", "heading_3"):
        head = "#" * int(t[-1]) + " " + rt()
        child = render_blocks(b.get("_children") or [], ctx)  # toggleable headings
        return "heading", head + ("\n\n" + child if child else "")
    if t == "bulleted_list_item":
        return "bulleted", _with_children("- " + _continuation(rt(), 2), b, ctx)
    if t == "numbered_list_item":
        return "numbered", _with_children("1. " + _continuation(rt(), 3), b, ctx)
    if t == "to_do":
        box = "- [x] " if d.get("checked") else "- [ ] "
        return "to_do", _with_children(box + _continuation(rt(), 6), b, ctx)
    if t == "toggle":
        return "bulleted", _with_children("- " + _continuation(rt(), 2), b, ctx)
    if t == "quote":
        child = render_blocks(b.get("_children") or [], ctx)
        return "quote", _quote(rt() + ("\n\n" + child if child else ""))
    if t == "callout":
        icon = d.get("icon") or {}
        emoji = icon.get("emoji") + " " if icon.get("type") == "emoji" and icon.get("emoji") else ""
        child = render_blocks(b.get("_children") or [], ctx)
        return "callout", "> [!note]\n" + _quote(emoji + rt() + ("\n\n" + child if child else ""))
    if t == "code":
        code = "".join(p.get("plain_text", "") for p in d.get("rich_text") or [])
        lang = d.get("language") or ""
        lang = "" if lang == "plain text" else lang
        fence = "~~~~" if "```" in code else "```"
        cap = rt("caption")
        return "code", f"{fence}{lang}\n{code}\n{fence}" + (f"\n{cap}" if cap else "")
    if t == "divider":
        return "divider", "---"
    if t in ("bookmark", "embed", "link_preview"):
        url = d.get("url")
        if not url:
            ctx.critical.append({"type": f"{t}:no_url", "id": bid})
            return t, f"<!-- notion-sync: {t} without url ({bid}) -->"
        cap = rt("caption") if t != "link_preview" else ""
        return "link", f"[{cap or url}]({_href(url)})"
    if t in FILE_TYPES:
        return "file", render_file(t, d, bid, ctx, b)
    if t == "child_page":
        return "page_link", ctx.wikilink(bid, d.get("title", ""))
    if t == "child_database":
        link = ctx.db_links.get(bid)
        if link:  # verified local CSV export of this database (migration link map)
            return "link", f"[{link[0]}]({link[1]})"
        ctx.placeholders.append({"kind": "child_database", "id": bid})
        return "placeholder", (f"{d.get('title') or EMPTY_TITLE} "
                               f"<!-- notion-sync: child_database {bid}; its rows are synced as separate pages -->")
    if t == "link_to_page":
        lt = d.get("type")
        target = norm_id(d.get(lt)) if lt in ("page_id", "database_id") else ""
        if not target:
            ctx.critical.append({"type": "link_to_page:unknown_target", "id": bid})
            return t, f"<!-- notion-sync: link_to_page with unknown target ({bid}) -->"
        return "page_link", ctx.wikilink(target, "")
    if t == "table":
        rows = b.get("_children") or []
        if any(r.get("type") != "table_row" for r in rows):
            ctx.critical.append({"type": "table:non_row_child", "id": bid})
            return t, f"<!-- notion-sync: table with non-row children ({bid}) -->"
        width = d.get("table_width") or max((len((r.get("table_row") or {}).get("cells") or []) for r in rows), default=0)
        lines = []
        for i, r in enumerate(rows):
            cells = list((r.get("table_row") or {}).get("cells") or [])
            cells += [[]] * (width - len(cells))
            lines.append("| " + " | ".join(_table_cell(c, ctx) for c in cells[:width]) + " |")
            if i == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        return "table", "\n".join(lines)
    if t == "table_row":
        ctx.critical.append({"type": "table_row:outside_table", "id": bid})
        return t, f"<!-- notion-sync: table_row outside table ({bid}) -->"
    if t in CONTAINER_TYPES:
        return "container", render_blocks(b.get("_children") or [], ctx)
    if t == "equation":
        return "equation", f"$$\n{d.get('expression', '')}\n$$"
    if t in NONCONTENT_PLACEHOLDER_TYPES:
        ctx.placeholders.append({"kind": t, "id": bid})
        return "placeholder", f"<!-- notion-sync: {t} block (no content to render) -->"
    # Anything else carries content this renderer cannot represent: never dropped silently.
    ctx.critical.append({"type": t, "id": bid})
    return t, f"<!-- notion-sync: UNSUPPORTED block type {t!r} ({bid}) -->"


def render_file(t, d, bid, ctx, block=None):
    ftype = d.get("type")
    cap = rich_text_to_md(d.get("caption") or [], ctx)
    if ftype == "external":
        url = (d.get("external") or {}).get("url") or ""
        name = d.get("name") or os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path)) or url
        if not url:
            # Notion data itself is empty (e.g. a File block that was never filled):
            # nothing identifies a file, so nothing can be mapped -- refuse, never drop.
            kind = "empty_external_block" if not d.get("name") and not d.get("caption") else "external_without_url"
            ctx.critical.append({"type": f"{t}:{kind}", "id": bid})
            return f"<!-- notion-sync: {t} without url ({bid}) -->"
        return f"{'!' if t == 'image' else ''}[{cap or name}]({_href(url)})"
    if ftype in ("file", "file_upload"):
        url = (d.get(ftype) or {}).get("url") or ""
        name = d.get("name") or os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        att = ctx.attachments
        # Order: a block already downloaded by the sync -> its recorded copy; otherwise an
        # attachment link already present in the local file (migrated attachments, unchanged
        # behaviour); otherwise a new supported (DOCX) attachment -> planned download.
        recorded = att is not None and ftype == "file" and att.is_recorded(bid)
        # name-based reuse of a migrated local link only for blocks that existed at the migration
        legacy = att is None or att.legacy_allowed(block)
        target = None if (recorded or not legacy) else ctx.resolve_local_attachment(name)
        if target is None and att is not None and ftype == "file" and att.handles(t, bid, d):
            target, problem = att.resolve(t, bid, d, block)
            if problem:
                ctx.critical.append(problem)
                return f"<!-- notion-sync: Notion-hosted {t} {bid} cannot be synced locally -->"
            if t == "image":
                return f"![{cap}]({_href(target)})"
            return f"[{cap or name}]({_href(target)})"
        if target is None:
            # Signed Notion URLs expire and downloading is out of scope: refuse rather than degrade the file.
            ctx.critical.append({"type": f"{t}:notion_hosted_unresolved", "id": bid, "name": name})
            return f"<!-- notion-sync: Notion-hosted {t} {name!r} has no unique local attachment ({bid}) -->"
        return f"{'!' if t == 'image' else ''}[{cap or name}]({target})"
    ctx.critical.append({"type": f"{t}:unknown_file_type:{ftype}", "id": bid})
    return f"<!-- notion-sync: {t} with unknown file type {ftype!r} ({bid}) -->"


# ------------------------------------------------------------
# Database-row properties, in the Notion ZIP-export layout used by the migration:
#   "# Title" / blank / one "Name: value" line per NON-EMPTY property (title excluded)
#   / blank / body.   Dates: "2026년 5월 4일", ranges "start → end".
# Only property types present in LAVCCA are supported; any other type (or a value
# shape not verified against the export, such as a date with a time) is critical.
# Order: property name by Unicode code point (the export's view order is not
# available through the API; this matches the one multi-property LAVCCA row).
# ------------------------------------------------------------
SUPPORTED_PROPERTY_TYPES = ("title", "rich_text", "date", "multi_select")
DB_ROW_PARENT_TYPES = ("database_id", "data_source_id")
DATE_ONLY_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')


def _kr_date(iso):
    m = DATE_ONLY_RE.match(iso or "")
    if not m:
        return None
    return f"{int(m.group(1))}년 {int(m.group(2))}월 {int(m.group(3))}일"


def render_properties(page, ctx):
    if not page or (page.get("parent") or {}).get("type") not in DB_ROW_PARENT_TYPES:
        return []
    lines = []
    for name in sorted((page.get("properties") or {}).keys()):
        prop = page["properties"][name] or {}
        t = prop.get("type")
        v = prop.get(t)
        if t == "title":
            continue
        if t not in SUPPORTED_PROPERTY_TYPES:
            ctx.critical.append({"type": f"UNSUPPORTED_PROPERTY_TYPE:{t}", "property": name})
            continue
        if t == "rich_text":
            text = "".join(r.get("plain_text", "") for r in v or [])
            if text:
                lines.append(f"{name}: {text}")
        elif t == "multi_select":
            names = [o.get("name", "") for o in v or []]
            if names:
                lines.append(f"{name}: {', '.join(names)}")
        elif t == "date":
            if not v:
                continue
            start, end = _kr_date(v.get("start")), (_kr_date(v.get("end")) if v.get("end") else "")
            if start is None or end is None or v.get("time_zone"):
                ctx.critical.append({"type": "UNSUPPORTED_PROPERTY_VALUE:date_with_time", "property": name})
                continue
            lines.append(f"{name}: {start}" + (f" → {end}" if end else ""))
    return lines


def render_body(title, blocks, ctx, page=None):
    """Body = text after the closing frontmatter line, matching the migration layout
    ('\\n# Title\\n\\n[properties\\n\\n]...'). Raises RenderUnsupported if any
    critical block or property was met."""
    props = render_properties(page, ctx)
    md = render_blocks(blocks, ctx)
    if ctx.critical:
        raise RenderUnsupported(ctx.critical)
    parts = [p for p in ("\n".join(props), md) if p]
    return f"\n# {title or EMPTY_TITLE}\n" + ("\n" + "\n\n".join(parts) + "\n" if parts else "")


# ============================================================
# Conflict detection
# ============================================================
def detect_conflict(nid, local_body, fm, state_items, file_mtime, bootstrap_ts):
    """Returns a reason string if the local file may contain edits that a write would destroy."""
    local_hash = sha256_text(local_body)
    st = state_items.get(nid) or {}
    # synced items: last_synced_content_hash; adopted baselines: local_content_hash
    baseline = st.get("last_synced_content_hash") or st.get("local_content_hash")
    fm_hash = fm.get("last_synced_content_hash")
    if baseline:
        if fm_hash and st.get("last_synced_content_hash") and fm_hash != baseline:
            return "frontmatter last_synced_content_hash disagrees with state.json"
        if local_hash != baseline:
            return "local body changed since last successful sync"
        return None
    if fm_hash:
        return None if local_hash == fm_hash else \
            "local body differs from frontmatter last_synced_content_hash (state entry missing)"
    if bootstrap_ts is None:
        return "no sync baseline and no bootstrap_migration_completed_at configured"
    if file_mtime > bootstrap_ts + 2:  # 2s tolerance for filesystem timestamp granularity
        return "no sync baseline yet and the file was modified after the migration completed -- possible local edit"
    return None


# ============================================================
# Atomic filesystem primitives (module-level so tests can inject faults)
# ============================================================
def _fsync(f):
    f.flush()
    os.fsync(f.fileno())


def _replace(src, dst):
    os.replace(src, dst)


def atomic_write_bytes(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".notion_sync_tmp_", suffix=".part", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            _fsync(f)
        with open(tmp, "rb") as f:
            if f.read() != data:
                raise WriteVerificationError("temp file content does not match intended bytes")
        _replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_backup(sync_dir, stamp, lavcca_rel_path, data):
    dst = os.path.join(sync_dir, "backups", stamp, lavcca_rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "xb") as f:  # never overwrite an existing backup
        f.write(data)
        _fsync(f)
    with open(dst, "rb") as f:
        if f.read() != data:
            raise WriteVerificationError("backup verification failed")
    return dst


def load_state(state_path):
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise ValueError("state.json has no 'items' object")
    return state


def compose_synced_file(fm, fm_lines, nid, page, new_body, synced_at):
    """Build the new file (existing frontmatter lines kept verbatim, only SYNC_KEYS
    updated) and validate it before anything touches disk: identity keys unchanged,
    notion_id re-checked, body/hash round-trip, UTF-8 round-trip, no NUL.
    Returns (new_text, new_bytes, body_sha256). Raises CanaryRefused('VALIDATION_FAILED')."""
    new_hash = sha256_text(new_body)
    new_text = build_frontmatter(fm_lines, {
        "notion_last_edited_time": page.get("last_edited_time") or "",
        "last_synced_content_hash": new_hash,
        "last_synced_at": synced_at,
    }) + new_body
    new_fm, _, check_body = split_frontmatter_strict(new_text)
    validate_identity(new_fm, nid)
    for k in IDENTITY_KEYS:
        if new_fm[k] != fm[k]:
            raise CanaryRefused("VALIDATION_FAILED", f"identity key {k} changed")
    if norm_id(new_fm["notion_id"]) != nid:
        raise CanaryRefused("VALIDATION_FAILED", "notion_id changed")
    if check_body != new_body or new_fm["last_synced_content_hash"] != sha256_text(check_body):
        raise CanaryRefused("VALIDATION_FAILED", "body/hash round-trip mismatch")
    new_bytes = new_text.encode("utf-8")
    if new_bytes.decode("utf-8") != new_text or "\x00" in new_text:
        raise CanaryRefused("VALIDATION_FAILED", "UTF-8 round-trip failed or NUL present")
    return new_text, new_bytes, new_hash


# ============================================================
# Main entry
# ============================================================
def run_canary(notion_id, cfg, api, sync_dir, apply=False, now=None):
    """Always returns a result dict; never raises for expected refusals/failures."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    res = {"mode": "CANARY_APPLY" if apply else "CANARY_DRY_RUN", "run_id": stamp,
           "notion_id": notion_id, "status": None, "wrote_files": []}
    try:
        _run(notion_id, cfg, api, sync_dir, apply, now, stamp, res)
    except CanaryRefused as e:
        res.update(status="REFUSED", refusal_code=e.code, detail=e.detail)
    except ContentFetchError as e:
        res.update(status="REFUSED", refusal_code="CONTENT_FETCH_ERROR", detail=str(e))
    except RenderUnsupported as e:
        res.update(status="REFUSED", refusal_code="RENDER_UNSUPPORTED", detail=e.items)
    except Exception as e:  # unexpected: original is protected by atomic replace/rollback ordering
        res.update(status=res.get("status") or "FAILED", detail=f"{type(e).__name__}: {e}")
    return res


def _run(notion_id, cfg, api, sync_dir, apply, now, stamp, res):
    nid = norm_id(notion_id)
    if not ID_RE.match(nid):
        raise CanaryRefused("INVALID_NOTION_ID", repr(notion_id))
    root_id = norm_id(cfg["lavcca_root_id"])
    state_path = os.path.join(sync_dir, "state.json")

    # state must be readable BEFORE anything is written
    try:
        state = load_state(state_path)
    except (OSError, ValueError) as e:
        raise CanaryRefused("STATE_UNREADABLE", f"{type(e).__name__}: {e}")

    identities = scan_vault_identities(cfg)
    target = select_target(nid, identities)
    abs_path = target["abs_path"]
    lavcca_real = os.path.realpath(cfg["lavcca_root_path"])
    if os.path.islink(abs_path) or not os.path.realpath(abs_path).startswith(lavcca_real + os.sep):
        raise CanaryRefused("PROTECTED_TARGET", "target resolves outside LAVCCA root or is a symlink")
    lavcca_rel = os.path.relpath(abs_path, cfg["lavcca_root_path"]).replace(os.sep, "/")
    res["obsidian_path"] = lavcca_rel

    with open(abs_path, "rb") as f:
        orig_bytes = f.read()
    orig_mtime = os.path.getmtime(abs_path)
    try:
        orig_text = orig_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CanaryRefused("INVALID_UTF8", str(e))
    fm, fm_lines, local_body = split_frontmatter_strict(orig_text)
    validate_identity(fm, nid)

    code, page = api("GET", f"/pages/{nid}")
    if code == 404:
        raise CanaryRefused("NOT_FOUND_REMOTE", "direct retrieve HTTP 404")
    if code == 403:
        raise CanaryRefused("PERMISSION_UNKNOWN", "direct retrieve HTTP 403")
    if code != 200:
        raise CanaryRefused("REMOTE_ERROR", f"direct retrieve HTTP {code}")
    if norm_id(page.get("id")) != nid:
        raise CanaryRefused("REMOTE_ID_MISMATCH", f"{page.get('id')!r}")
    if page.get("in_trash"):
        raise CanaryRefused("TRASHED_REMOTE", "in_trash=true")
    if page.get("archived"):
        raise CanaryRefused("ARCHIVED_REMOTE", "archived=true")
    if nid == root_id:  # the LAVCCA root page itself is in scope; its parent is the workspace
        verdict, chain = "DESCENDANT", [f"page:{root_id} (LAVCCA root itself)"]
    else:
        verdict, chain = verify_descendant(api, page.get("parent"), root_id)
    res["parent_chain"] = chain
    if verdict != "DESCENDANT":
        raise CanaryRefused("NOT_LAVCCA_DESCENDANT", f"verdict={verdict} chain={chain}")

    bootstrap = cfg.get("bootstrap_migration_completed_at")
    bootstrap_ts = datetime.fromisoformat(bootstrap).timestamp() if bootstrap else None
    conflict = detect_conflict(nid, local_body, fm, state["items"], orig_mtime, bootstrap_ts)
    if conflict:
        raise CanaryRefused("CONFLICT", conflict)

    blocks = fetch_block_tree(api, nid, cfg)
    ctx = RenderContext(id_to_vault_path(identities), local_body)
    new_body = render_body(page_title(page), blocks, ctx, page=page)
    new_hash = sha256_text(new_body)
    res["placeholders"] = ctx.placeholders
    res["new_body_sha256"] = new_hash
    res["old_body_sha256"] = sha256_text(local_body)

    synced_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    new_text, new_bytes, _ = compose_synced_file(fm, fm_lines, nid, page, new_body, synced_at)

    if not apply:
        preview = os.path.join(sync_dir, "logs", f"canary_preview_{stamp}_{nid}.md")
        os.makedirs(os.path.dirname(preview), exist_ok=True)
        with open(preview, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text)
        res.update(status="DRY_RUN_OK", preview_path=preview,
                   would_change_body=(new_hash != sha256_text(local_body)))
        return

    # ---- write phase ----
    res["status"] = "FAILED"  # until proven otherwise
    with open(abs_path, "rb") as f:
        if f.read() != orig_bytes:
            raise CanaryRefused("CONCURRENT_MODIFICATION", "target changed while the canary was running")
    backup = write_backup(sync_dir, stamp, lavcca_rel, orig_bytes)
    res["backup_path"] = backup
    res["wrote_files"].append(backup)

    atomic_write_bytes(abs_path, new_bytes)  # raises -> original untouched (temp removed)
    res["wrote_files"].append(abs_path)
    with open(abs_path, "rb") as f:
        if f.read() != new_bytes:
            _restore(abs_path, orig_bytes, res)
            raise WriteVerificationError("post-replace verification failed; original restored")

    # ---- state (only after the file write fully succeeded) ----
    new_state = json.loads(json.dumps(state))
    new_state["items"][nid] = {
        "notion_id": nid,
        "last_seen_edited_time": page.get("last_edited_time"),
        "last_synced_content_hash": new_hash,
        "obsidian_path": lavcca_rel,
        "last_successful_sync": synced_at,
    }
    try:
        atomic_write_bytes(state_path, (json.dumps(new_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        if load_state(state_path)["items"].get(nid, {}).get("last_synced_content_hash") != new_hash:
            raise WriteVerificationError("state.json verification failed")
    except Exception as e:
        _restore(abs_path, orig_bytes, res)
        res.update(status="STATE_WRITE_FAILED_ROLLED_BACK" if res.get("rollback") == "OK" else "STATE_WRITE_FAILED_ROLLBACK_FAILED",
                   detail=f"{type(e).__name__}: {e}")
        return
    res.update(status="APPLIED", synced_at=synced_at)


def _restore(abs_path, orig_bytes, res):
    try:
        atomic_write_bytes(abs_path, orig_bytes)
        with open(abs_path, "rb") as f:
            res["rollback"] = "OK" if f.read() == orig_bytes else "VERIFY_FAILED"
    except Exception as e:
        res["rollback"] = f"FAILED: {type(e).__name__}: {e} (restore manually from backup_path)"
