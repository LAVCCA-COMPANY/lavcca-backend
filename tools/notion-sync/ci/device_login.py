"""One-time Microsoft sign-in for the sync (device code flow, personal account, Files.ReadWrite).

Stores the refresh token -- never printed -- in two places:
  ~/.lavcca/ms_refresh_token (user-only file; local Graph self-test reads it)
  Environment secret MS_REFRESH_TOKEN of lavcca-backend/notion-sync (via `gh secret set`, stdin)

usage: python -m ci.device_login --client-id <Entra app id> [--no-github]
"""
import os, sys, json, time, argparse, subprocess
from pathlib import Path
from urllib.parse import urlencode

from ci import graph

DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
REPO = "LAVCCA-COMPANY/lavcca-backend"
TOKEN_FILE = Path.home() / ".lavcca" / "ms_refresh_token"


def _post(url, form):
    status, _, data = graph.urllib_http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"},
                                        urlencode(form).encode())
    try:
        return status, json.loads(data or b"{}")
    except ValueError:
        return status, {}


def save_token_file(token, path=TOKEN_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, path)
    if os.name == "nt":  # 0o600 means nothing on NTFS: drop inherited ACLs, keep the current user only
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"],
                       capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--no-github", action="store_true", help="only write the local token file")
    a = ap.parse_args()
    status, dc = _post(DEVICE_URL, {"client_id": a.client_id, "scope": graph.SCOPE})
    if status != 200:
        print(f"device code request failed: HTTP {status} {dc.get('error', '')} {dc.get('error_codes', '')}")
        return 2
    print(dc["message"])  # "To sign in, open https://microsoft.com/devicelogin and enter code XXXX" -- no secret
    deadline = time.time() + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(int(dc.get("interval", 5)))
        status, tok = _post(graph.TOKEN_URL, {"client_id": a.client_id, "device_code": dc["device_code"],
                                              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if status == 200 and tok.get("refresh_token"):
            break
        if tok.get("error") not in ("authorization_pending", "slow_down"):
            print(f"sign-in failed: {tok.get('error')} {tok.get('error_codes', '')}")
            return 2
    else:
        print("sign-in timed out")
        return 2
    print(f"signed in; scope={tok.get('scope')}")
    save_token_file(tok["refresh_token"])
    print(f"refresh token saved to {TOKEN_FILE} (not shown)")
    if not a.no_github:
        r = subprocess.run(["gh", "secret", "set", "MS_REFRESH_TOKEN", "--env", "notion-sync", "--repo", REPO],
                           input=tok["refresh_token"], text=True, capture_output=True)
        print("GitHub secret MS_REFRESH_TOKEN:", "set" if r.returncode == 0 else f"NOT set ({r.stderr.strip()[:200]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
