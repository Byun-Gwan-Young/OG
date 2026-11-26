"""원격 MCP 서버용 개인 토큰 발급 / 목록 / 폐기.

발급: python scripts/issue_token.py bridgesoft EMP001 --name "멘토 A"
목록: python scripts/issue_token.py --list
폐기: python scripts/issue_token.py --revoke <토큰 앞 8자리>

토큰은 tokens.json에 저장된다 (.gitignore에 포함, 저장소에 올리지 말 것).
발급·폐기 후에는 remote_server.py를 다시 시작해야 반영된다.
"""
import argparse
import json
import secrets
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mcp_server as core  # noqa: E402

TOKENS = ROOT / "tokens.json"


def load() -> dict:
    return json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.is_file() else {}


def save(data: dict) -> None:
    TOKENS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("company_id", nargs="?")
    p.add_argument("employee_id", nargs="?")
    p.add_argument("--name", default="")
    p.add_argument("--list", action="store_true")
    p.add_argument("--revoke")
    a = p.parse_args()
    tokens = load()

    if a.list:
        for t, v in tokens.items():
            print(f"{t[:8]}…  {v['company_id']:<12} {v['employee_id']:<8} {v.get('name', '')}  ({v['issued_at']})")
        return
    if a.revoke:
        hit = [t for t in tokens if t.startswith(a.revoke)]
        if len(hit) != 1:
            sys.exit("일치하는 토큰이 없거나 여러 개입니다.")
        tokens.pop(hit[0])
        save(tokens)
        print("폐기했습니다. 서버를 다시 시작하세요.")
        return
    if not (a.company_id and a.employee_id):
        p.error("company_id와 employee_id를 입력하세요.")

    _, err = core.resolve_session(a.company_id, a.employee_id)
    if err:
        sys.exit(f"회사 또는 사번을 찾을 수 없습니다: {a.company_id} / {a.employee_id}")

    token = "og_" + secrets.token_urlsafe(24)
    tokens[token] = {
        "client_id": f"{a.company_id}:{a.employee_id}",
        "scopes": ["officegenie"],
        "company_id": a.company_id,
        "employee_id": a.employee_id,
        "name": a.name,
        "issued_at": datetime.now().isoformat(timespec="seconds"),
    }
    save(tokens)
    print(f"발급 완료: {a.company_id} / {a.employee_id} {a.name}\n토큰: {token}\n(서버를 다시 시작하면 반영됩니다)")


if __name__ == "__main__":
    main()
