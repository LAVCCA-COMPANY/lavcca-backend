"""Team PC: LAVCCA-COMPANY/docs obsidian-vault/Notion/  ->  <existing Obsidian vault>/LAVCCA/  (one way).

Notion is the source: <vault>/LAVCCA/ belongs to this script and is made equal to the docs copy
(local edits are overwritten, files gone upstream are removed). Nothing outside <vault>/LAVCCA/ is
ever written. Every file it replaces or removes is first copied to <state>/trash/<run>/ (outside the
vault, kept 30 days), and each write is atomic (temp file + os.replace).

Runs every minute from Task Scheduler. A run that finds docs unchanged (one `git ls-remote`, no
REST API quota) and LAVCCA/ unchanged (file names, sizes, mtimes -- no reads, so OneDrive
online-only files stay online) ends without touching anything. A full byte comparison runs when
either side changed and at least hourly.

Policy: one vault per team member, installed on one PC. If the same OneDrive vault is installed on several
PCs anyway, LAVCCA/.lavcca-sync-owner.json names the PC that writes; the others stand by (exit 0) and take
over once the owner has not refreshed it for 30 minutes. This is a best-effort marker, NOT a distributed
lock: OneDrive carries it with a delay (seconds to minutes, longer offline) and the PCs' clocks may differ,
so two PCs can both write during that window (first install, both just started, one offline). They write
the same Notion content atomically, so the cost is an occasional OneDrive conflict copy, which the next full
comparison moves to trash -- not lost or mixed content.

Links: the publisher writes vault-absolute wikilinks as [[Notion/...]]; they are rewritten to
[[LAVCCA/...]] here. Attachment links are relative and keep working because the layout is kept.

usage: python obsidian_pull.py --vault "<vault>" [--apply] [--log FILE]   sync (dry run without --apply)
       python obsidian_pull.py --vault "<vault>" --status                  last runs, errors, owner
       python obsidian_pull.py --login                                     GitHub sign-in in a browser
exit:  0 ok (or standby) | 2 bad setup | 3 git/network/login failed (retried next run) | 4 another run active
"""
import os, sys, json, time, shutil, socket, hashlib, argparse, subprocess
from datetime import datetime, timezone

TARGET = "LAVCCA"
SOURCE = ("obsidian-vault", "Notion")
LINK_FROM, LINK_TO = "[[Notion/", f"[[{TARGET}/"
TRASH_KEEP_DAYS = 30
FULL_CHECK_EVERY = 3600          # seconds between full byte comparisons when nothing changed
OWNER_FILE = ".lavcca-sync-owner.json"
OWNER_REFRESH = 600              # the owner rewrites the marker at most every 10 minutes
OWNER_STALE = 1800               # a marker older than 30 minutes may be taken over
STATE_FILE = "pull_state.json"
# Background runs must never open a sign-in window or wait for a password: fail and log instead.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
AUTH_HINTS = ("authentication failed", "could not read username", "terminal prompts disabled",
              "repository not found", "403", "401", "logon failed", "credential")


def default_state():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LAVCCA")


def host_id():
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def desired_files(src):
    """{rel: bytes} for every file under the docs copy, with wikilinks moved to LAVCCA/."""
    out = {}
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(d, f)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            with open(p, "rb") as fh:
                data = fh.read()
            if f.endswith(".md") and not rel.startswith("_attachments/"):  # attachments stay byte for byte
                data = data.decode("utf-8").replace(LINK_FROM, LINK_TO).encode("utf-8")
            out[rel] = data
    return out


def existing_files(dst):
    out = {}
    for d, dirs, files in os.walk(dst):
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), dst).replace(os.sep, "/")
            if rel != OWNER_FILE:
                out[rel] = os.path.join(d, f)
    return out


def fingerprint(dst):
    """Names, sizes and mtimes of LAVCCA/ (stat only: online-only OneDrive files are not downloaded)."""
    h = hashlib.sha256()
    for rel, p in sorted(existing_files(dst).items()) if os.path.isdir(dst) else ():
        st = os.stat(p)
        h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


def plan(src, dst):
    """-> (desired, creates, updates, deletes); lists of relative paths, sorted."""
    want = desired_files(src)
    have = existing_files(dst) if os.path.isdir(dst) else {}
    creates = sorted(r for r in want if r not in have)
    updates = []
    for r in sorted(r for r in want if r in have):
        with open(have[r], "rb") as fh:
            if fh.read() != want[r]:
                updates.append(r)
    deletes = sorted(r for r in have if r not in want)
    return want, creates, updates, deletes


def _inside(base, path):
    base, path = os.path.realpath(base), os.path.realpath(path)
    return path == base or path.startswith(base + os.sep)


def _keep(dst, trash, rel):
    """Copy the current file to trash before it is replaced or removed."""
    out = os.path.join(trash, *rel.split("/"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.copy2(os.path.join(dst, *rel.split("/")), out)


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.lavcca-tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def apply(dst, trash, want, creates, updates, deletes):
    for rel in creates + updates + deletes:  # never follow a path out of LAVCCA/
        if not _inside(dst, os.path.join(dst, *rel.split("/"))):
            raise ValueError(f"path escapes {TARGET}/: {rel}")
    for rel in updates + deletes:
        _keep(dst, trash, rel)
    for rel in creates + updates:
        _atomic_write(os.path.join(dst, *rel.split("/")), want[rel])
    for rel in deletes:
        os.remove(os.path.join(dst, *rel.split("/")))
    for d, dirs, files in os.walk(dst, topdown=False):  # folders emptied by removals
        if d != dst and not os.listdir(d):
            os.rmdir(d)


# ---- git (never interactive) ----
def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV))


def _git_error(step, r):
    msg = (r.stderr or r.stdout or "").strip()
    if any(h in msg.lower() for h in AUTH_HINTS):
        return (f"GitHub sign-in needed ({step}). Run once in PowerShell: "
                f"python \"{os.path.abspath(__file__)}\" --login")
    return f"git {step} failed (network or GitHub, retried next run): {msg[:200]}"


def remote_head(repo):
    """-> (sha, error). One small request; Git traffic does not count against the REST API quota."""
    r = _git(repo, "ls-remote", "--quiet", "origin", "refs/heads/main")
    if r.returncode or not r.stdout.strip():
        return None, _git_error("ls-remote", r)
    return r.stdout.split()[0], None


def local_head(repo):
    r = _git(repo, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def git_update(repo):
    # Windows Git defaults to core.autocrlf=true, which would rewrite line endings of pages and text attachments
    _git(repo, "config", "core.autocrlf", "false")
    for args in (["fetch", "--quiet", "origin", "main"], ["merge", "--ff-only", "--quiet", "origin/main"]):
        r = _git(repo, *args)
        if r.returncode:
            return _git_error(args[0], r)
    return None


# ---- state, lock, owner ----
def load_state(state):
    try:
        with open(os.path.join(state, STATE_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state, st):
    _atomic_write(os.path.join(state, STATE_FILE), json.dumps(st, indent=1, sort_keys=True).encode("utf-8"))


def _acquire(lock):
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - os.path.getmtime(lock) < 3600:
            return False
        os.remove(lock)  # a run cannot take an hour: left by a crash
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def read_owner(dst):
    try:
        with open(os.path.join(dst, OWNER_FILE), encoding="utf-8") as fh:
            o = json.load(fh)
        return o if isinstance(o, dict) and o.get("host") else None
    except (OSError, ValueError):
        return None  # missing, half-synced or conflict-damaged: treated as no owner


def owner_check(dst, me, now):
    """-> (may_write, note, marker_due). Read only: mine, missing or stale -> I may write."""
    o = read_owner(dst)
    age = now - float((o or {}).get("updated_at", 0))
    if o and o["host"] != me and age < OWNER_STALE:
        return False, f"standby: {o['host']} keeps this vault in sync", False
    note = f"took over from {o['host']} (no refresh for {int(age // 60)} min)" if o and o["host"] != me else None
    return True, note, not o or o["host"] != me or age >= OWNER_REFRESH


def write_owner(dst, me, now):
    # Written only after a successful run, at most every OWNER_REFRESH: a rare small OneDrive upload.
    # Two PCs racing here can leave a OneDrive conflict copy of this file; it is not the canonical
    # name, so it is treated like any stray file in LAVCCA/ (moved to trash on the next full run).
    os.makedirs(dst, exist_ok=True)
    _atomic_write(os.path.join(dst, OWNER_FILE), json.dumps({"host": me, "updated_at": now}).encode("utf-8"))


def _prune_trash(trash_root, now):
    if not os.path.isdir(trash_root):
        return
    for name in os.listdir(trash_root):
        p = os.path.join(trash_root, name)
        if os.path.isdir(p) and now - os.path.getmtime(p) > TRASH_KEEP_DAYS * 86400:
            shutil.rmtree(p, ignore_errors=True)


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def run(vault, repo, state, do_apply, fetch=True, out=print, now=time.time, me=None):
    if not os.path.isdir(os.path.join(vault, ".obsidian")):
        out(f"not an Obsidian vault (no .obsidian): {vault}")
        return 2
    src = os.path.join(repo, *SOURCE)
    if not os.path.isdir(src):
        out(f"docs copy not found: {src}")
        return 2
    dst = os.path.join(vault, TARGET)
    if os.path.islink(dst):
        out(f"{dst} is a link: refusing")
        return 2
    os.makedirs(state, exist_ok=True)
    lock = os.path.join(state, "pull.lock")
    if not _acquire(lock):
        out("another run is in progress")
        return 4
    st = load_state(state)
    t, me = now(), me or host_id()
    marker_due = False
    try:
        if do_apply:
            ok, note, marker_due = owner_check(dst, me, t)
            if note and note != st.get("owner_note"):
                out(note)  # logged once per change, not every minute
            st["owner_note"] = note
            if not ok:
                st["checked_at"] = t
                return 0
        if fetch:
            remote, err = remote_head(repo)
            if err:
                if err != st.get("last_error"):
                    out(err)
                st.update(last_error=err, last_error_at=t, checked_at=t)
                return 3
            if (do_apply and remote == st.get("applied") and fingerprint(dst) == st.get("fingerprint")
                    and t - st.get("full_check_at", 0) < FULL_CHECK_EVERY):
                st.update(checked_at=t, success_at=t, last_error=None)
                if marker_due:
                    write_owner(dst, me, t)
                return 0  # nothing changed anywhere: no file reads, no log line
            if remote != local_head(repo):
                err = git_update(repo)
                if err:
                    out(err)
                    st.update(last_error=err, last_error_at=t, checked_at=t)
                    return 3
        want, creates, updates, deletes = plan(src, dst)
        changed = bool(creates or updates or deletes)
        if changed or not do_apply:
            out(json.dumps({"mode": "apply" if do_apply else "dry-run", "target": dst, "create": len(creates),
                            "update": len(updates), "delete": len(deletes)}, ensure_ascii=False))
            for kind, rels in (("create", creates), ("update", updates), ("delete", deletes)):
                for r in rels:
                    out(f"{kind}\t{TARGET}/{r}")
        if do_apply:
            if changed:
                trash_root = os.path.join(state, "trash")
                apply(dst, os.path.join(trash_root, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")),
                      want, creates, updates, deletes)
                _prune_trash(trash_root, time.time())
                st["changed_at"] = t
            if marker_due:
                write_owner(dst, me, t)
            st.update(applied=local_head(repo) if fetch else st.get("applied"), fingerprint=fingerprint(dst),
                      full_check_at=t, success_at=t, checked_at=t, last_error=None)
        return 0
    finally:
        try:
            if do_apply:
                save_state(state, st)
        finally:
            os.remove(lock)  # released even when the state cannot be saved (disk full, ...)


def status(vault, state, out=print, now=time.time):
    st = load_state(state)
    owner = read_owner(os.path.join(vault, TARGET)) if vault else None
    out(f"last check     : {_iso(st.get('checked_at'))}")
    out(f"last success   : {_iso(st.get('success_at'))}")
    out(f"last change    : {_iso(st.get('changed_at'))}")
    out(f"full check     : {_iso(st.get('full_check_at'))}")
    out(f"docs commit    : {(st.get('applied') or '')[:7]}")
    out(f"vault owner    : {owner['host'] + ' @ ' + _iso(owner.get('updated_at')) if owner else '-'}  (this PC: {host_id()})")
    out(f"last error     : {st.get('last_error') or '-'} {_iso(st.get('last_error_at')) or ''}".rstrip())
    if not st.get("success_at") or now() - st["success_at"] > 86400:
        out("WARNING: no successful sync in the last 24 hours")
        return 1
    return 0


def login(repo, out=print):
    """Interactive: lets Git Credential Manager open the browser sign-in, then verifies access."""
    r = subprocess.run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/main"])
    out("signed in: docs is reachable" if r.returncode == 0 else "still no access: check the GitHub account has read access to LAVCCA-COMPANY/docs")
    return 0 if r.returncode == 0 else 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault")
    ap.add_argument("--repo", default=os.path.join(default_state(), "docs"))
    ap.add_argument("--state", default=default_state())
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--log", help="append output to this file instead of printing")
    a = ap.parse_args()
    if sys.stdout:  # pythonw (the scheduled task) has no stdout at all
        sys.stdout.reconfigure(encoding="utf-8")  # Korean Windows consoles default to cp949, which lacks some title chars
    if a.login:
        sys.exit(login(a.repo))
    if a.status:
        sys.exit(status(a.vault, a.state))
    if not a.vault:
        ap.error("--vault is required")
    if a.log:
        os.makedirs(os.path.dirname(a.log), exist_ok=True)
        if os.path.isfile(a.log) and os.path.getsize(a.log) > 1024 * 1024:
            os.replace(a.log, a.log + ".1")
        fh = open(a.log, "a", encoding="utf-8")

        def out(line):
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    else:
        fh, out = None, print
    try:
        code = run(a.vault, a.repo, a.state, a.apply, not a.no_fetch, out)
    finally:
        if fh:
            fh.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
