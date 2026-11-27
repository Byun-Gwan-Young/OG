"""원격 MCP 서버용 개인 토큰 발급 / 목록 / 폐기 (운영자 CLI).

평소에는 사원이 웹 홈의 "AI 앱 연결" 카드에서 직접 발급·폐기한다. 이 스크립트는 운영자용이다.

발급: python scripts/issue_token.py bridgesoft EMP001 --name "멘토 A"
목록: python scripts/issue_token.py --list [--all]        (--all: 폐기된 토큰도 표시)
폐기: python scripts/issue_token.py --revoke <토큰 원문>
      python scripts/issue_token.py --revoke-id <회사 ID> <token_id>

토큰은 DB(mcp_tokens)에 SHA-256 해시로만 저장된다. 원문은 발급할 때 한 번만 출력된다.
원격 서버는 요청마다 DB를 조회하므로 발급·폐기가 서버 재시작 없이 바로 반영된다.
DATABASE_URL이 없으면 로컬 SQLite에 저장된다.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mcp_server as core  # noqa: E402

storage = core.storage


def main() -> None:
    p = argparse.ArgumentParser(description="원격 MCP 개인 토큰 관리")
    p.add_argument("company_id", nargs="?")
    p.add_argument("employee_id", nargs="?")
    p.add_argument("--name", default="", help="토큰 이름(예: 회사 노트북 Claude Code)")
    p.add_argument("--list", action="store_true")
    p.add_argument("--all", action="store_true", help="--list에서 폐기된 토큰도 표시")
    p.add_argument("--revoke", metavar="TOKEN", help="토큰 원문으로 폐기")
    p.add_argument("--revoke-id", nargs=2, metavar=("COMPANY_ID", "TOKEN_ID"))
    a = p.parse_args()
    storage.check_runtime_config()
    storage.init_db()

    if a.list:
        rows = storage.list_all_mcp_tokens(include_revoked=a.all)
        if not rows:
            print("토큰이 없습니다.")
        for t in rows:
            state = "폐기됨" if t["revoked_at"] else "활성"
            print(f"{t['token_id']}  {t['company_id']:<12} {t['employee_id']:<8} {state:<4} "
                  f"{t['label']}  (발급 {t['issued_at']}, 마지막 사용 {t['last_used_at'] or '-'})")
        return
    if a.revoke:
        if not storage.revoke_mcp_token_by_value(a.revoke.strip()):
            sys.exit("일치하는 활성 토큰이 없습니다.")
        print("폐기했습니다. 바로 반영됩니다.")
        return
    if a.revoke_id:
        company_id, token_id = a.revoke_id
        if not storage.revoke_mcp_token(company_id, token_id, actor="operator"):
            sys.exit("일치하는 활성 토큰이 없습니다.")
        print("폐기했습니다. 바로 반영됩니다.")
        return
    if not (a.company_id and a.employee_id):
        p.error("company_id와 employee_id를 입력하세요.")

    account = storage.get_account(a.company_id, a.employee_id)
    if account is None or account["role"] != storage.ROLE_EMPLOYEE or account["disabled"]:
        sys.exit(f"회사 또는 사번을 찾을 수 없습니다: {a.company_id} / {a.employee_id}")
    try:
        token, info = storage.issue_mcp_token(
            a.company_id, a.employee_id, a.name, actor="operator"
        )
    except storage.TokenLimitReached:
        sys.exit(f"이 사원은 이미 활성 토큰이 {storage.MCP_TOKENS_PER_EMPLOYEE}개입니다. 먼저 하나를 폐기하세요.")
    print(f"발급 완료: {a.company_id} / {a.employee_id} {info['label']} (token_id {info['token_id']})")
    print(f"토큰: {token}")
    print("원문은 지금 한 번만 표시됩니다. 서버에는 해시만 저장됩니다.")


if __name__ == "__main__":
    main()
