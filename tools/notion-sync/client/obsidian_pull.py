"""Team PC: LAVCCA-COMPANY/docs obsidian-vault/Notion/  ->  <existing Obsidian vault>/LAVCCA/  (one way).

Notion is the source: <vault>/LAVCCA/ belongs to this script and is made equal to the docs copy.
Nothing outside <vault>/LAVCCA/ is ever written. Every file it replaces or removes is first copied
to <state>/trash/<run>/ (outside the vault), and each write is atomic (temp file + os.replace), so a
failed run leaves each file either old or new and can be rolled back from trash.

Links: the publisher writes vault-absolute wikilinks as [[Notion/...]]; they are rewritten to
[[LAVCCA/...]] here. Attachment links are relative (../_attachments/...) and keep working because the
folder layout is copied as is.

usage: python obsidian_pull.py --vault "<vault>" [--repo <docs clone>] [--state <dir>] [--apply] [--no-fetch]
       dry run unless --apply. Exit 0 ok, 2 bad setup, 3 git update failed, 4 another run holds the lock.
"""
import os, sys, json, time, shutil, hashlib, argparse, subprocess
from datetime import datetime, timezone

TARGET = "LAVCCA"
SOURCE = ("obsidian-vault", "Notion")
LINK_FROM, LINK_TO = "[[Notion/", f"[[{TARGET}/"
TRASH_KEEP_DAYS = 30


def default_state():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LAVCCA")


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
            out[os.path.relpath(os.path.join(d, f), dst).replace(os.sep, "/")] = os.path.join(d, f)
    return out


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


def git_update(repo):
    # Windows Git defaults to core.autocrlf=true, which would rewrite line endings of pages and text attachments
    subprocess.run(["git", "-C", repo, "config", "core.autocrlf", "false"], capture_output=True)
    for args in (["fetch", "--quiet", "origin", "main"], ["merge", "--ff-only", "--quiet", "origin/main"]):
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
        if r.returncode:
            return f"git {args[0]} failed: {r.stderr.strip()[:300]}"
    return None


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


def _prune_trash(trash_root, now):
    if not os.path.isdir(trash_root):
        return
    for name in os.listdir(trash_root):
        p = os.path.join(trash_root, name)
        if os.path.isdir(p) and now - os.path.getmtime(p) > TRASH_KEEP_DAYS * 86400:
            shutil.rmtree(p, ignore_errors=True)


def run(vault, repo, state, do_apply, fetch=True, out=print):
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
    try:
        if fetch:
            err = git_update(repo)
            if err:
                out(err)
                return 3
        want, creates, updates, deletes = plan(src, dst)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out(json.dumps({"mode": "apply" if do_apply else "dry-run", "target": dst, "create": len(creates),
                        "update": len(updates), "delete": len(deletes)}, ensure_ascii=False))
        for kind, rels in (("create", creates), ("update", updates), ("delete", deletes)):
            for r in rels:
                out(f"{kind}\t{TARGET}/{r}")
        if do_apply and (creates or updates or deletes):
            trash_root = os.path.join(state, "trash")
            apply(dst, os.path.join(trash_root, stamp), want, creates, updates, deletes)
            _prune_trash(trash_root, time.time())
        return 0
    finally:
        os.remove(lock)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--repo", default=os.path.join(default_state(), "docs"))
    ap.add_argument("--state", default=default_state())
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--log", help="append output to this file instead of printing")
    a = ap.parse_args()
    if sys.stdout:  # pythonw (the scheduled task) has no stdout at all
        sys.stdout.reconfigure(encoding="utf-8")  # Korean Windows consoles default to cp949, which lacks some title chars
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
