"""Brand migration NATAM -> LAVCCA: DRY-RUN ONLY. Reads the vault, never writes it.

Answers, before anyone renames the managed folder (docs/brand-migration.md M3):
  - how many [[OLD/...]] links exist, in which files (managed by the engine or written by people),
    and whether each one resolves today (a rename must not change that)
  - whether every state.json item's file body still hashes to local_content_hash (if not, a
    migration would bake an unexplained difference into the new baseline)
  - the new body hash each managed file would get once its links are rewritten

usage: python -m ci.brand_migration --vault "<Obsidian Vault>" --report <file outside the vault>
exit:  0 consistent | 1 inconsistencies found (see report) | 2 refused (bad arguments)
"""
import os, re, sys, json, argparse

import canary_apply as ca

SKIP_TOP = (".obsidian", ".trash", ".git")


def link_re(old):
    # wikilinks and embeds: [[OLD/path|title]] / ![[OLD/path#h]]
    return re.compile(r"\[\[" + re.escape(old) + r"/([^\]|#]*)")


def rewrite_links(text, old, new):
    return link_re(old).sub(lambda m: "[[" + new + "/" + m.group(1), text)


def _resolves(vault, old, target):
    p = os.path.join(vault, old, *target.split("/"))
    return os.path.isfile(p) or os.path.isfile(p + ".md")


def scan(vault, old="NATAM", new="LAVCCA", state=None):
    vault = os.path.abspath(vault)
    rx = link_re(old)
    sync_rel = f"{old}/_sync"
    files, totals = [], {"md_files_scanned": 0, "links": 0, "links_unresolved": 0, "files_with_links": 0,
                         "managed_files_with_links": 0, "human_files_with_links": 0}
    items_by_path = {v.get("obsidian_path"): (k, v) for k, v in ((state or {}).get("items") or {}).items()}
    state_checks = []
    for root, dirs, names in os.walk(vault):
        rel_root = os.path.relpath(root, vault).replace(os.sep, "/")
        if rel_root.split("/")[0] in SKIP_TOP or rel_root == sync_rel or rel_root.startswith(sync_rel + "/"):
            dirs[:] = []
            continue
        for n in names:
            if not n.endswith(".md"):
                continue
            rel = n if rel_root == "." else f"{rel_root}/{n}"
            with open(os.path.join(root, n), "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
            totals["md_files_scanned"] += 1
            targets = rx.findall(text)
            managed = text.startswith("---\n") and "\nmanaged: notion\n" in text.split("\n---\n", 1)[0] + "\n"
            entry = None
            if targets:
                unresolved = sorted({t for t in targets if not _resolves(vault, old, t)})
                entry = {"path": rel, "managed": managed, "links": len(targets), "unresolved_targets": unresolved}
                totals["links"] += len(targets)
                totals["links_unresolved"] += sum(1 for t in targets if t in unresolved)
                totals["files_with_links"] += 1
                totals["managed_files_with_links" if managed else "human_files_with_links"] += 1
                files.append(entry)
            st_rel = rel[len(old) + 1:] if rel.startswith(old + "/") else None
            if st_rel in items_by_path:
                nid, item = items_by_path.pop(st_rel)
                try:
                    body = ca.split_frontmatter_strict(text)[2]
                except ca.CanaryRefused as e:
                    state_checks.append({"notion_id": nid, "path": rel, "problem": f"frontmatter: {e.code}"})
                    continue
                cur = ca.sha256_text(body)
                chk = {"notion_id": nid, "path": rel, "body_hash_matches_state": cur == item.get("local_content_hash"),
                       "api_hash_matches_local": item.get("api_rendered_content_hash") == item.get("local_content_hash"),
                       "links": len(targets)}
                if targets:
                    chk["new_body_hash"] = ca.sha256_text(rewrite_links(body, old, new))
                state_checks.append(chk)
    missing = [{"notion_id": nid, "path": f"{old}/{p}", "problem": "file missing"} for p, (nid, _) in items_by_path.items()]
    bad = [c for c in state_checks if c.get("problem") or not c["body_hash_matches_state"]] + missing
    totals.update(state_items=len((state or {}).get("items") or {}), state_items_inconsistent=len(bad),
                  state_items_needing_new_hash=sum(1 for c in state_checks if c.get("new_body_hash")))
    return {"old": old, "new": new, "totals": totals, "files": files,
            "state_inconsistent": bad, "state_checks": state_checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description="NATAM -> LAVCCA brand migration DRY-RUN (read-only)")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--old", default="NATAM")
    ap.add_argument("--new", default="LAVCCA")
    ap.add_argument("--report", required=True, help="JSON report path, must be outside the vault")
    ap.add_argument("--expect-links", type=int, help="fail if the link count differs (e.g. 538)")
    a = ap.parse_args(argv)
    vault = os.path.realpath(a.vault)
    report_path = os.path.realpath(a.report)
    if os.path.commonpath([vault, report_path]) == vault:
        print("REFUSED: report must be written outside the vault")
        return 2
    state_path = os.path.join(vault, a.old, "_sync", "state.json")
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    res = scan(vault, a.old, a.new, state)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    t = res["totals"]
    print(json.dumps(t))
    ok = t["state_items_inconsistent"] == 0 and (a.expect_links is None or t["links"] == a.expect_links)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
