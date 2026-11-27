"""등록된 회사 목록(company_id, 회사명, 회사 코드, 사원 수)을 출력한다. 운영자용.

    python scripts/list_companies.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    try:
        storage.init_db()
        rows = storage.list_companies_overview()
    except storage.StorageUnavailable as exc:
        print(f"조회 실패: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("등록된 회사가 없습니다.")
        return 0
    print("company_id\t회사명\t회사 코드\t사원 수")
    for row in rows:
        print(
            f"{row['company_id']}\t{row['company_name']}\t"
            f"{row['company_code'] or '-'}\t{row['employee_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
