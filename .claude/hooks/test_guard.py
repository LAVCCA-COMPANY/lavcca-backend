"""guard.py 차단 규칙 회귀 테스트. 실행: python -m pytest .claude/hooks -q (저장소 루트)"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).with_name("guard.py")


def run(cmd: str) -> int:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    return subprocess.run([sys.executable, str(GUARD)], input=payload, encoding="utf-8",
                          capture_output=True).returncode


@pytest.mark.parametrize("cmd", [
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin HEAD:refs/heads/main",
    "git push origin +ai/x:main",
    "git -C . push origin main",
    'git -C "mvp 1_9_3" push origin main',
    "git push -f origin ai/x",
    "git -C . push --force origin ai/x",
    "git filter-repo --path x",
    "git reset --hard HEAD~1",
    "cat .env",
    "type mvp 1_9_3/.env",
    "cat google-credentials.json",
])
def test_blocked(cmd):
    assert run(cmd) == 2


@pytest.mark.parametrize("cmd", [
    "git push -u origin ai/123-fix",
    "git push origin chore/lavcca-worker-deploy",
    "git status",
    "cat .env.example",
    "python -m pytest tests -q",
])
def test_allowed(cmd):
    assert run(cmd) == 0


def test_non_shell_tool_ignored():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": ".env"}})
    assert subprocess.run([sys.executable, str(GUARD)], input=payload, encoding="utf-8").returncode == 0
