# -*- coding: utf-8 -*-
"""
PreToolUse 가드 훅 (작업 9) — settings.json 의 deny 규칙이 못 막는 셸 우회를 막는다.

  · Read/Edit 의 deny 는 Bash(cat .env 등)로 우회할 수 있어서 셸 명령도 검사
  · 히스토리 재작성 / 강제 푸시 / main 직접 푸시 차단

차단 시 exit 2 + stderr 메시지 → Claude 에게 사유가 전달되고 해당 호출은 실행되지 않는다.
"""
import json
import re
import sys

try:   # Windows 콘솔(cp949)에서 한글 사유가 깨지지 않도록
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SECRET_PATTERNS = [
    r"(^|[\s/\\\"'])\.env(\b|$)",            # .env, mvp 1_9_3/.env (단 .env.example 은 허용)
    r"google-credentials\.json",
    r"credentials[^\s]*\.json",
    r"\.sqlite3?\b",
    r"\.pem\b",
    r"\.key\b",
]
SECRET_ALLOW = [r"\.env\.example"]

GIT_BLOCK = [
    (r"\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b)", "강제 푸시는 금지입니다(JH 직접)."),
    (r"\bgit\s+filter-(repo|branch)\b",                              "히스토리 재작성은 금지입니다(JH 직접)."),
    (r"\bgit\s+push\b[^|;&]*[\s:+](main|master)(\s|$)",                     "main 직접 푸시는 금지입니다. 브랜치 + PR 로 올리세요."),
    (r"\bgit\s+reset\s+--hard\b",                                    "git reset --hard 는 사용자 확인 후에만 실행하세요."),
]


def _block(msg: str) -> None:
    print(f"[guard] 차단: {msg}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return   # 입력을 못 읽으면 막지 않는다(훅 고장으로 작업 전체가 멈추지 않도록)

    if data.get("tool_name") not in ("Bash", "PowerShell"):
        return
    cmd = (data.get("tool_input") or {}).get("command") or ""

    for pat, msg in GIT_BLOCK:
        if re.search(pat, cmd):
            _block(msg)

    scrubbed = cmd
    for pat in SECRET_ALLOW:
        scrubbed = re.sub(pat, "", scrubbed)
    for pat in SECRET_PATTERNS:
        if re.search(pat, scrubbed, flags=re.IGNORECASE):
            _block("비밀정보 파일(.env / credentials / sqlite / 키)에 셸로 접근할 수 없습니다.")


if __name__ == "__main__":
    main()
