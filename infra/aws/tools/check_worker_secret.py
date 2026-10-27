"""
check_worker_secret.py — 시크릿 lavcca/dev/worker 에 태스크 정의가 요구하는 키가 다 있는지 값 없이 확인한다.

  python infra/aws/tools/check_worker_secret.py            # 기본: --profile lavcca-admin, ap-northeast-2

출력은 키별 set / EMPTY / MISSING 뿐이다. 값은 aws CLI 의 stdout 을 이 프로세스가 받아 바로 버리고,
화면·파이프·셸 변수·파일 어디에도 남기지 않는다. 필요한 키 목록은 CloudFormation 템플릿의 Secrets 에서 읽는다
(템플릿과 어긋나면 태스크가 컨테이너 시작 전에 실패하기 때문).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "cloudformation" / "lavcca-worker-dev.yaml"
SECRET_ID = "lavcca/dev/worker"
AWS = ["aws", "--profile", "lavcca-admin", "--region", "ap-northeast-2"]


def required_keys(template_text: str) -> list[str]:
    return re.findall(r"\{\s*Name:\s*(\w+),\s*ValueFrom:\s*!Sub\s*\"\$\{WorkerSecret\}:\1::\"", template_text)


def check(secret_string: str, keys: list[str]) -> dict[str, str]:
    data = json.loads(secret_string)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return {k: ("MISSING" if k not in data else "set" if str(data[k]).strip() else "EMPTY") for k in keys}


def main(run=subprocess.run) -> int:
    keys = required_keys(TEMPLATE.read_text(encoding="utf-8"))
    r = run(AWS + ["secretsmanager", "get-secret-value", "--secret-id", SECRET_ID,
                   "--query", "SecretString", "--output", "text"], capture_output=True, text=True)
    if r.returncode != 0:
        # AWS 오류 메시지에는 값이 들어가지 않는다(값이 없거나 권한·세션 문제)
        print(f"조회 실패(exit {r.returncode}): {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")
        return 2
    try:
        result = check(r.stdout, keys)
    except ValueError:
        print("시크릿 값이 JSON 이 아님 - 콘솔에서 Key/value 형식으로 다시 입력")   # 값은 출력하지 않는다
        return 2
    for k, state in result.items():
        print(f"{k}: {state}")
    required_ok = result.get("GEMINI_API_KEY") == "set" and "MISSING" not in result.values()
    print("OK" if required_ok else "NOT READY (GEMINI_API_KEY 는 set, 나머지 키는 최소한 존재해야 함)")
    return 0 if required_ok else 1


if __name__ == "__main__":
    sys.exit(main())
