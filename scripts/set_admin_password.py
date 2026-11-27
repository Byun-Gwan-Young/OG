"""회사 관리자(ADMIN) 비밀번호를 설정한다. 운영자용.

    python scripts/set_admin_password.py bridgesoft                 # getpass로 입력
    python scripts/set_admin_password.py bridgesoft --password '...'  # 자동화용

계정이 없으면 만들고, 있으면 비밀번호를 바꾸고 잠금을 풉니다. 비밀번호는 8자 이상.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="회사 ADMIN 계정 비밀번호를 설정합니다.")
    parser.add_argument("company_id", help="내부 회사 ID (list_companies.py로 확인)")
    parser.add_argument("--password", help="비밀번호 (생략하면 화면에서 입력)")
    args = parser.parse_args(argv)

    password = args.password
    if password is None:
        try:
            password = getpass.getpass("새 관리자 비밀번호(8자 이상): ")
            again = getpass.getpass("한 번 더 입력: ")
        except (EOFError, KeyboardInterrupt):
            print("취소했습니다.", file=sys.stderr)
            return 1
        if password != again:
            print("두 비밀번호가 다릅니다.", file=sys.stderr)
            return 2
    if len(password) < storage.ADMIN_MIN_PASSWORD:
        print(
            f"관리자 비밀번호는 {storage.ADMIN_MIN_PASSWORD}자 이상이어야 합니다.",
            file=sys.stderr,
        )
        return 2

    try:
        storage.init_db()
        storage.set_admin_password(args.company_id, password, actor="operator:cli")
        company = storage.get_company(args.company_id)
    except KeyError:
        print(f"회사를 찾을 수 없습니다: {args.company_id}", file=sys.stderr)
        return 2
    except storage.StorageUnavailable as exc:
        print(f"설정 중단: {exc}", file=sys.stderr)
        return 2
    print(
        f"관리자 비밀번호를 설정했습니다: {company['company_name']} "
        f"/ 회사 코드 {company['company_code']} / 아이디 {storage.ADMIN_ACCOUNT_ID}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
