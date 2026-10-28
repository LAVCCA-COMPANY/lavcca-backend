# -*- coding: utf-8 -*-
"""
NOTION_TOKEN lookup. The token must live OUTSIDE the OneDrive-synced vault.

Precedence:
  A. process environment variable NOTION_TOKEN (non-empty)
  B. <home>/.lavcca/secrets.env  (line: NOTION_TOKEN=<token>)
     <home>/.natam/secrets.env   (pre-rebrand location, read only if B is absent)
  C. otherwise TokenNotConfigured ("TOKEN_NOT_CONFIGURED")

The vault-root .env is deliberately NOT a fallback. The token value is never
logged, printed, or included in exception messages; only its source is reported.
"""
import os
from pathlib import Path

ENV_VAR = "NOTION_TOKEN"


class TokenNotConfigured(Exception):
    code = "TOKEN_NOT_CONFIGURED"


def _read_secret_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(ENV_VAR + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except FileNotFoundError:
        return None
    return None


def default_secret_paths(home=None):
    home = Path(home) if home else Path.home()
    return [home / ".lavcca" / "secrets.env", home / ".natam" / "secrets.env"]


def load_notion_token(environ=None, secret_path=None, home=None):
    """Returns (token, source) where source is 'environment' or the secret file path.
    Raises TokenNotConfigured without revealing anything about the token."""
    environ = os.environ if environ is None else environ
    tok = (environ.get(ENV_VAR) or "").strip()
    if tok:
        return tok, "environment"
    paths = [Path(secret_path)] if secret_path else default_secret_paths(home)
    for path in paths:
        tok = _read_secret_file(path)
        if tok:
            return tok, str(path)
    raise TokenNotConfigured(f"TOKEN_NOT_CONFIGURED: set {ENV_VAR} in the environment or create {paths[0]} "
                             f"with a '{ENV_VAR}=...' line (the vault .env is not used)")
