"""Real-account Microsoft Graph checks (G2-G13) before the runner touches any vault.

Writes ONLY under <OneDrive>/LAVCCA-SYNC-TEST/selftest-<ts>/ (deleted at the end); reads a few
files of the real vault (hashes and mtimes only). Uses ~/.lavcca/ms_refresh_token from
ci.device_login and rotates it. Prints results; never prints a token.

usage: python -m ci.graph_selftest --client-id ID --vault-remote "Documents/Obsidian Vault"
         --vault-local "C:/.../OneDrive/문서/Obsidian Vault" --onedrive-local "C:/.../OneDrive" [--root-dir NATAM]
"""
import os, sys, json, time, argparse, threading
from datetime import datetime, timezone

from ci import graph, onedrive_sync as ods
from ci.device_login import TOKEN_FILE, save_token_file

RESULTS = {}


def record(key, ok, **detail):
    RESULTS[key] = {"ok": ok, **detail}
    print(f"{key}: {'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)}")


def put_bytes(g, path, data, if_match=None):
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), f"gst_{threading.get_ident()}_{time.time_ns()}")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        return g.upload_verified(path, tmp, if_match)
    finally:
        os.remove(tmp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--vault-remote", required=True)
    ap.add_argument("--vault-local", required=True)
    ap.add_argument("--onedrive-local", required=True)
    ap.add_argument("--root-dir", default="NATAM")
    ap.add_argument("--report", required=True)
    a = ap.parse_args()

    # G2 refresh + rotation, G4 revoked/garbage token classification
    rt = TOKEN_FILE.read_text(encoding="utf-8").strip()
    t0 = time.time()
    at, rt2 = graph.refresh_access_token(a.client_id, rt)
    save_token_file(rt2)
    record("G2_refresh", True, rotated=rt2 != rt, seconds=round(time.time() - t0, 2))
    try:
        graph.refresh_access_token(a.client_id, "M.not-a-token", sleep=lambda s: None)
        record("G4_bad_token", False, note="garbage token accepted")
    except graph.ReauthRequired as e:
        record("G4_bad_token", True, classified=str(e))
    except graph.Transient as e:
        record("G4_bad_token", False, classified=f"transient: {e}")
    g = graph.Graph(at)

    _, drive = g._req("GET", f"{g.base}/me/drive")
    q = drive.get("quota") or {}
    record("drive", True, type=drive.get("driveType"), used_mb=round(q.get("used", 0) / 2**20),
           total_mb=round(q.get("total", 0) / 2**20), state=q.get("state"))

    # vault path as Graph sees it (Explorer shows localized names)
    root_item = g.item(a.vault_remote)
    record("vault_path", root_item is not None, remote=a.vault_remote)
    if root_item is None:
        return finish(a)

    # G10/G11 read-only: real vault hashes and mtimes vs the local OneDrive copy
    scope = ods.Scope(a.root_dir)
    samples, n = [], 0
    for rel, it in ods.walk_remote(g, a.vault_remote, scope):
        if not rel.startswith(a.root_dir + "/") or not rel.endswith(".md"):
            continue
        local = os.path.join(a.vault_local, *rel.split("/"))
        sha = ((it.get("file") or {}).get("hashes") or {}).get("sha1Hash")
        quick = ((it.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        lm = (it.get("fileSystemInfo") or {}).get("lastModifiedDateTime")
        samples.append({"sha1_present": bool(sha), "quickxor_present": bool(quick),
                        "sha1_equal": bool(sha) and os.path.isfile(local) and graph.sha1_file(local) == sha.upper(),
                        "mtime_delta_s": abs(ods._iso_to_ts(lm) - os.path.getmtime(local)) if lm and os.path.isfile(local) else None})
        n += 1
        if n >= 10:
            break
    record("G10_sha1", bool(samples) and all(s["sha1_equal"] for s in samples),
           files=len(samples), sha1_present=sum(s["sha1_present"] for s in samples),
           quickxor_present=sum(s["quickxor_present"] for s in samples))
    deltas = [s["mtime_delta_s"] for s in samples if s["mtime_delta_s"] is not None]
    record("G11_mtime", bool(deltas) and max(deltas) <= 2, max_delta_s=max(deltas) if deltas else None)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"LAVCCA-SYNC-TEST/selftest-{ts}"
    try:
        # G12 upload -> read-back -> appears on this PC through the OneDrive client
        t0 = time.time()
        item = put_bytes(g, f"{base}/한글 파일 이름.md", "라브카 selftest\n".encode())
        up_s = time.time() - t0
        local = os.path.join(a.onedrive_local, "LAVCCA-SYNC-TEST", f"selftest-{ts}", "한글 파일 이름.md")
        t1 = time.time()
        while time.time() - t1 < 180 and not os.path.isfile(local):
            time.sleep(1)
        record("G12_upload_to_pc", os.path.isfile(local), upload_s=round(up_s, 2),
               cloud_to_pc_s=round(time.time() - t1, 1) if os.path.isfile(local) else None)

        # G8 if-match: current eTag replaces, stale eTag is refused and leaves content alone
        e1 = item["eTag"]
        item2 = put_bytes(g, f"{base}/한글 파일 이름.md", b"v2\n", if_match=e1)
        try:
            put_bytes(g, f"{base}/한글 파일 이름.md", b"v3 must not land\n", if_match=e1)
            stale_refused = False
        except graph.PreconditionFailed:
            stale_refused = True
        now_sha = g.item(f"{base}/한글 파일 이름.md")["file"]["hashes"]["sha1Hash"]
        record("G8_if_match", stale_refused and now_sha == item2["file"]["hashes"]["sha1Hash"])

        # G9 metadata-only change: does the eTag move? (if yes, a rename elsewhere blocks our upload: safe side)
        before = g.item(f"{base}/한글 파일 이름.md")
        g._req("PATCH", f"{g.base}/me/drive/items/{before['id']}", json.dumps({"description": "meta"}).encode(),
               {"Content-Type": "application/json"})
        after = g.item(f"{base}/한글 파일 이름.md")
        record("G9_meta_change", True, etag_changed=before["eTag"] != after["eTag"],
               ctag_changed=before.get("cTag") != after.get("cTag"))

        # G6 second create of the same lock -> 409
        lock = f"{base}/_sync/.sync.lock"
        etag, _ = ods.acquire_lock(g, lock, "selftest-1", ts)
        try:
            put_bytes(g, lock, b"{}")
            record("G6_lock_twice", False, note="second create succeeded")
        except graph.Conflict:
            record("G6_lock_twice", True)

        # orphan takeover / foreign lock on the real service
        _, note = ods.acquire_lock(g, lock, "selftest-2", ts)
        windows = f"{base}/_sync/win.lock"
        put_bytes(g, windows, json.dumps({"pid": 1, "host": "DESKTOP-EXAMPLE", "run_id": "x"}).encode())
        try:
            ods.acquire_lock(g, windows, "selftest-3", ts)
            foreign_blocked = False
        except ods.Locked:
            foreign_blocked = True
        record("lock_orphan_and_foreign", bool(note) and foreign_blocked, takeover_note=note)

        # G7 the two primitives the lock relies on, raced for real (3 writers per round):
        #   create (conflictBehavior=fail) and replace (if-match the same eTag) must each have exactly one winner,
        #   and the surviving bytes must be the winner's
        def race(path, make, expect_loss):
            wins, errs = [], []

            def worker(i):
                try:
                    make(i)
                    wins.append(i)
                except expect_loss:
                    pass
                except Exception as e:  # noqa: BLE001  recorded, never hidden
                    errs.append(type(e).__name__)
            th = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
            [t.start() for t in th]
            [t.join() for t in th]
            body = g.download(g.item(path)).decode()
            ok = len(wins) == 1 and not errs and body == f"racer-{wins[0]}"
            return ok, {"wins": len(wins), "errors": errs}

        create_bad, replace_bad = [], []
        for r in range(20):
            path = f"{base}/race/c{r}.lock"
            ok, d = race(path, lambda i, p=path: put_bytes(g, p, f"racer-{i}".encode()), graph.Conflict)
            if not ok:
                create_bad.append({"round": r, **d})
        for r in range(10):
            path = f"{base}/race/r{r}.lock"
            etag0 = put_bytes(g, path, b"orphan")["eTag"]
            ok, d = race(path, lambda i, p=path, e=etag0: put_bytes(g, p, f"racer-{i}".encode(), if_match=e),
                         graph.PreconditionFailed)
            if not ok:
                replace_bad.append({"round": r, **d})
        record("G7_create_race", not create_bad, rounds=20, anomalies=create_bad[:5])
        record("G7_replace_race", not replace_bad, rounds=10, anomalies=replace_bad[:5])

        # G13 a PC edit lands while we hold an old eTag -> our upload must be refused
        e_old = g.item(f"{base}/한글 파일 이름.md")["eTag"]
        with open(local, "w", encoding="utf-8") as f:
            f.write("PC edit\n")
        t2 = time.time()
        while time.time() - t2 < 180 and g.item(f"{base}/한글 파일 이름.md")["eTag"] == e_old:
            time.sleep(2)
        pc_to_cloud = round(time.time() - t2, 1)
        try:
            put_bytes(g, f"{base}/한글 파일 이름.md", b"runner overwrite\n", if_match=e_old)
            refused = False
        except graph.PreconditionFailed:
            refused = True
        record("G13_pc_edit_conflict", refused, pc_to_cloud_s=pc_to_cloud)
    finally:
        try:
            g._req("DELETE", g._path_url(base))
        except graph.GraphError as e:
            print(f"cleanup failed: {e}")
    return finish(a)


def finish(a):
    with open(a.report, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    return 0 if all(v["ok"] for v in RESULTS.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
