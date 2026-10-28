"""GitHub Actions job: OneDrive vault <-> runner mirror around ci.run (engine unchanged).

  token refresh -> remote lock -> mirror restore (hash-verified) -> ci.run -> upload what the engine
  changed (if-match eTags, read-back hash) -> state.json + cursor last -> release lock

OneDrive is the only source of truth; the cached mirror only saves downloads.
exit: 0 ok | 10 audit mismatch (uploaded, job fails) | 73 locked | 75 transient | 78 re-auth needed
      70 upload partially done (lock kept as upload_failed: automation stops, human checks)
      71 engine deleted a file (nothing uploaded) | other = engine exit code (nothing uploaded)
"""
import os, sys, json, glob, shutil, argparse, tempfile
from datetime import datetime, timezone

import canary_apply as ca
from ci import graph, run as cirun

EXIT_UPLOAD_PARTIAL, EXIT_ENGINE_DELETED, EXIT_LOCKED, EXIT_TRANSIENT, EXIT_REAUTH = 70, 71, 73, 75, 78
SKIP_TOP = (".obsidian", ".trash", ".git")
SYNC_STATE_FILES = ("state.json", cirun.CURSOR_FILE)
LOCK_HOST = "github-actions"


class Locked(Exception):
    pass


class Scope:
    """Which vault files the runner mirrors (download) and may write back (upload).
    Engine needs: every .md in the vault (identity scan) + everything in the managed root.
    _sync/: only state + cursor come down; new backups/ and logs/ files go up (create-only)."""

    def __init__(self, root_dir):
        self.root, self.sync = root_dir, f"{root_dir}/_sync/"

    def download(self, rel):
        if rel.split("/")[0] in SKIP_TOP:
            return False
        if rel.startswith(self.sync):
            return rel[len(self.sync):] in SYNC_STATE_FILES
        return rel.startswith(self.root + "/") or rel.endswith(".md")

    def upload(self, rel):
        return self.download(rel) or rel.startswith((self.sync + "backups/", self.sync + "logs/"))

    def upload_rank(self, rel):  # backups first, state + cursor last
        if rel.startswith(self.sync + "backups/"):
            return 0
        if rel.startswith(self.sync + "logs/"):
            return 2
        return 3 if rel.startswith(self.sync) else 1


def _iso_to_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def walk_remote(g, remote_root, scope, rel=""):
    """(rel, item) for every file in scope; folders are only entered when something below can match."""
    for it in g.children(f"{remote_root}/{rel}".rstrip("/")):
        child = f"{rel}/{it['name']}".lstrip("/")
        if "folder" in it:
            if child.split("/")[0] in SKIP_TOP or (child + "/").startswith(scope.sync) and child + "/" != scope.sync:
                continue
            yield from walk_remote(g, remote_root, scope, child)
        elif "file" in it and scope.download(child):
            yield child, it


def restore_mirror(g, remote_root, local_root, scope):
    """Make local_root equal to OneDrive for every file in scope. Returns the manifest
    {rel: {"id", "eTag", "hash"}} used for conditional uploads. Raises on any unverifiable file."""
    manifest = {}
    for rel, it in walk_remote(g, remote_root, scope):
        kind, want = graph.remote_hash(it)
        if not kind:
            raise graph.VerifyError(f"{rel}: no content hash in metadata")
        dst = os.path.join(local_root, *rel.split("/"))
        if not (os.path.isfile(dst) and graph.local_hash(dst, kind) == want):
            data = g.download(it)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".dl_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if graph.local_hash(tmp, kind) != want:
                os.remove(tmp)
                raise graph.VerifyError(f"{rel}: downloaded bytes do not match {kind}")
            os.replace(tmp, dst)
        mtime = (it.get("fileSystemInfo") or {}).get("lastModifiedDateTime")
        if mtime:  # engine conflict guards compare mtimes; download time would mislead them
            os.utime(dst, (_iso_to_ts(mtime), _iso_to_ts(mtime)))
        manifest[rel] = {"id": it["id"], "eTag": it["eTag"], "hash": f"{kind}:{want}"}
    for rel in snapshot(local_root, scope.download):  # cached files deleted remotely
        if rel not in manifest:
            os.remove(os.path.join(local_root, *rel.split("/")))
    return manifest


def snapshot(local_root, include):
    out = {}
    for path in glob.glob(os.path.join(local_root, "**", "*"), recursive=True):
        if os.path.isfile(path):
            rel = os.path.relpath(path, local_root).replace(os.sep, "/")
            if include(rel):
                out[rel] = graph.sha1_file(path)
    return out


# ---- remote lock (same file name and JSON shape as the engine's local lock) ----
def _write_json_tmp(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


def _put(g, remote_path, obj, if_match=None):
    tmp = _write_json_tmp(obj)
    try:
        return g.upload_verified(remote_path, tmp, if_match)["eTag"]
    finally:
        os.remove(tmp)


def acquire_lock(g, lock_path, run_id, now):
    """-> (eTag, note). Only one workflow run holds the concurrency group, so a running-state lock
    left by github-actions is an orphan of a killed run and is taken over. Windows-host locks and
    upload_failed locks are never taken over."""
    info = {"pid": 0, "host": LOCK_HOST, "run_id": run_id, "started_at": now, "state": "running"}
    try:
        return _put(g, lock_path, info), None
    except graph.Conflict:
        pass
    item = g.item(lock_path)
    if item is None:  # released between our create and our read: one more try
        return _put(g, lock_path, info), None
    try:
        held = json.loads(g.download(item))
    except ValueError:
        raise Locked("unreadable lock file")
    if held.get("host") != LOCK_HOST or held.get("state") != "running":
        raise Locked(f"held by host={held.get('host')} run={held.get('run_id')} state={held.get('state')}")
    return _put(g, lock_path, info, if_match=item["eTag"]), f"took over orphan lock of run {held.get('run_id')}"


def mark_upload_failed(g, lock_path, etag, run_id, now, error):
    try:
        _put(g, lock_path, {"pid": 0, "host": LOCK_HOST, "run_id": run_id, "started_at": now,
                            "state": "upload_failed", "error": str(error)[:300]}, if_match=etag)
    except Exception:
        pass  # the running-state lock stays: still blocks Windows engines; a later run would take it over


def upload_changes(g, remote_root, local_root, scope, manifest, pre):
    """Upload exactly what changed since `pre`. Returns uploaded rels. Raises on the first failure."""
    post = snapshot(local_root, scope.upload)
    deleted = sorted(r for r in pre if r not in post and scope.download(r))
    if deleted:
        raise RuntimeError(f"engine deleted files: {deleted[:5]}")
    changed = sorted((r for r in post if pre.get(r) != post[r]), key=lambda r: (scope.upload_rank(r), r))
    for rel in changed:
        etag = (manifest.get(rel) or {}).get("eTag")
        if etag is None and rel in manifest:
            raise RuntimeError(f"{rel}: no eTag")
        g.upload_verified(f"{remote_root}/{rel}", os.path.join(local_root, *rel.split("/")), if_match=etag)
    return changed


def prepare_engine(repo_dir, sync_dir, local_root, root_dir, config):
    """Engine code + a runner config.json: `config` (from the NOTION_SYNC_CONFIG secret; the
    public repo only has config.example.json) with paths pointing into the mirror."""
    os.makedirs(sync_dir, exist_ok=True)
    for f in glob.glob(os.path.join(repo_dir, "*.py")):
        shutil.copy(f, sync_dir)
    cfg = ca.normalize_config(config)
    cfg["vault_root"] = local_root
    cfg["lavcca_root_path"] = os.path.join(local_root, root_dir)
    with open(os.path.join(sync_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def sync(g, remote_root, local_root, root_dir, repo_dir, run_id, config, event=None, full=False, dry_run=False,
         run_ci=cirun.run, now=None):
    """Returns (exit_code, summary). summary holds counts only (safe for public-to-team logs)."""
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scope = Scope(root_dir)
    lock_path = f"{remote_root}/{root_dir}/_sync/.sync.lock"
    summary = {"dry_run": dry_run}
    try:
        etag, note = acquire_lock(g, lock_path, run_id, now)
    except Locked as e:
        return EXIT_LOCKED, {**summary, "locked": str(e)}
    except (graph.GraphError, graph.VerifyError) as e:
        return EXIT_TRANSIENT, {**summary, "error": f"lock: {type(e).__name__}"}
    summary["lock_note"] = note
    uploading = False
    try:
        try:
            manifest = restore_mirror(g, remote_root, local_root, scope)
        except (graph.GraphError, graph.VerifyError, OSError) as e:
            return EXIT_TRANSIENT, {**summary, "error": f"restore: {type(e).__name__}"}
        summary["mirrored_files"] = len(manifest)
        sync_dir = os.path.join(local_root, root_dir, "_sync")
        prepare_engine(repo_dir, sync_dir, local_root, root_dir, config)
        pre = snapshot(local_root, scope.upload)
        engine = cirun.engine_runner(sync_dir)
        if dry_run:
            def engine(args, _run=engine):  # noqa: E306  preview: the engine writes nothing
                return _run([*args, "--dry-run"])
        code, report = run_ci(sync_dir, event, full, search=None if full else _search(sync_dir), run_engine=engine)
        os.makedirs(os.path.join(sync_dir, "logs"), exist_ok=True)  # ids/paths stay in OneDrive, not in Actions logs
        with open(os.path.join(sync_dir, "logs", f"ci_run_{run_id}.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        summary.update(mode=report.get("mode"), targets=len(report.get("targets") or []),
                       engine_exit=report.get("engine_exit"), audit_problems=len(report.get("audit") or []))
        if code not in (0, cirun.EXIT_AUDIT_MISMATCH):
            return code, summary
        if dry_run:
            return code, summary
        uploading = True
        try:
            summary["uploaded"] = len(upload_changes(g, remote_root, local_root, scope, manifest, pre))
        except RuntimeError as e:
            if "engine deleted" in str(e):
                uploading = False
                return EXIT_ENGINE_DELETED, {**summary, "error": "engine deleted files"}
            raise
        uploading = False
        return code, summary
    except (graph.GraphError, graph.VerifyError, OSError, RuntimeError) as e:
        if uploading:
            mark_upload_failed(g, lock_path, etag, run_id, now, type(e).__name__)
            etag = None
            return EXIT_UPLOAD_PARTIAL, {**summary, "error": f"upload: {type(e).__name__}"}
        return EXIT_TRANSIENT, {**summary, "error": type(e).__name__}
    finally:
        if etag:
            try:
                g.delete(lock_path, if_match=etag)
            except graph.GraphError:
                pass  # left running-state: the next run takes it over as an orphan


def _search(sync_dir):
    def search(since):
        with open(os.path.join(sync_dir, "config.json"), encoding="utf-8") as f:
            version = json.load(f)["notion_version"]
        return cirun.search_since(os.environ["NOTION_TOKEN"], version, since)
    return search


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", required=True, help="local mirror directory (actions/cache)")
    ap.add_argument("--remote-root", required=True, help="vault folder path in OneDrive")
    ap.add_argument("--root-dir", required=True, help="managed folder inside the vault (NATAM until migrated)")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        access, new_rt = graph.refresh_access_token(os.environ["MS_CLIENT_ID"], os.environ["MS_REFRESH_TOKEN"])
    except graph.ReauthRequired as e:
        print(json.dumps({"exit": EXIT_REAUTH, "error": str(e)}))  # AADSTS code only, no token
        sys.exit(EXIT_REAUTH)
    except graph.Transient:
        print(json.dumps({"exit": EXIT_TRANSIENT, "error": "token endpoint"}))
        sys.exit(EXIT_TRANSIENT)
    print(f"::add-mask::{access}")
    print(f"::add-mask::{new_rt}")
    rt_file = os.environ.get("NEW_REFRESH_TOKEN_FILE")
    if rt_file and new_rt != os.environ["MS_REFRESH_TOKEN"]:
        fd = os.open(rt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new_rt)
    event = None
    if os.environ.get("GITHUB_EVENT_PATH"):
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
            event = json.load(f)
    run_id = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    config = json.loads(os.environ["NOTION_SYNC_CONFIG"])  # secret: real ids and owner names never live in git
    code, summary = sync(graph.Graph(access), a.remote_root.strip("/"), os.path.abspath(a.mirror), a.root_dir, repo,
                         run_id, config, event, a.full, a.dry_run)
    print(json.dumps({**summary, "exit": code}))
    sys.exit(code)


if __name__ == "__main__":
    main()
