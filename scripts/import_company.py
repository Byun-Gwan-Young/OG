"""회사 JSON 5종을 검증한 뒤 DB(PostgreSQL/SQLite)로 가져온다.

JSON 파일을 읽는 곳은 이 스크립트뿐이다. 서버는 실행 중에 DB만 쓴다.
DB 주소는 storage.py가 .env의 DATABASE_URL에서 읽는다(없으면 로컬 SQLite).

    python scripts/import_company.py --all                  # 모든 회사
    python scripts/import_company.py data/bridgesoft        # 한 회사
    python scripts/import_company.py --all --reset-tickets  # 티켓을 seed로 초기화
    python scripts/import_company.py --all --reset-employee-passwords
        # 시연용: 사원 계정 비밀번호를 employees.json 값으로 되돌림(ADMIN 제외)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import company_data  # noqa: E402
import storage  # noqa: E402


DATA_ROOT = ROOT / "data"
SEED_ROOT = ROOT / "seed"
JSON_NAMES = company_data.DOC_KINDS


class ImportValidationError(ValueError):
    """A source JSON file does not satisfy the company data contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ImportValidationError(f"{path.name}: 파일이 없습니다.") from None
    except (OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            detail = f"JSON 문법 오류 {exc.lineno}행 {exc.colno}열"
        else:
            detail = "파일을 읽을 수 없습니다."
        raise ImportValidationError(f"{path.name}: {detail}") from None
    if not isinstance(value, dict):
        raise ImportValidationError(f"{path.name}: 최상위 값은 객체여야 합니다.")
    return value


def validate_documents(
    company_id: str, documents: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    if set(documents) != set(JSON_NAMES):
        missing = sorted(set(JSON_NAMES) - set(documents))
        raise ImportValidationError(
            f"회사 JSON 5종이 필요합니다. 누락: {', '.join(missing)}"
        )

    for name, document in documents.items():
        if not isinstance(document, dict):
            raise ImportValidationError(f"{name}.json: 최상위 값은 객체여야 합니다.")
        if document.get("company_id") != company_id:
            raise ImportValidationError(
                f"{name}.json: company_id가 폴더 이름 {company_id!r}과 일치하지 않습니다."
            )

    # 관리자 업로드와 같은 검증 함수(company_data.validate_document)를 쓴다.
    for name in JSON_NAMES:
        issues = company_data.validate_document(name, documents[name], mode="import")
        if issues:
            raise ImportValidationError(issues[0]["message"])
    return documents


def load_company_directory(
    company_dir: str | Path,
) -> tuple[str, dict[str, dict[str, Any]]]:
    directory = Path(company_dir).resolve()
    if not directory.is_dir():
        raise ImportValidationError("회사 데이터 폴더를 찾을 수 없습니다.")
    company_id = directory.name
    documents = {name: _read_json(directory / f"{name}.json") for name in JSON_NAMES}
    return company_id, validate_documents(company_id, documents)


def load_seed_tickets(
    company_id: str, seed_root: str | Path = SEED_ROOT
) -> dict[str, Any]:
    seed_path = Path(seed_root) / company_id / "tickets.json"
    document = _read_json(seed_path)
    if document.get("company_id") != company_id:
        raise ImportValidationError(
            f"{seed_path.name}: company_id가 대상 회사 {company_id!r}과 일치하지 않습니다."
        )
    for issue in company_data.validate_document("tickets", document, mode="import"):
        if issue["field"] in ("tickets",) or "tickets 항목" in issue["message"]:
            raise ImportValidationError(issue["message"].replace("tickets.json", seed_path.name, 1))
    return document


def import_company_directory(
    company_dir: str | Path,
    *,
    reset_tickets: bool = False,
    seed_root: str | Path = SEED_ROOT,
) -> dict[str, Any]:
    company_id, documents = load_company_directory(company_dir)
    seed_document = load_seed_tickets(company_id, seed_root) if reset_tickets else None
    return storage.import_company_documents(
        company_id,
        documents,
        seed_tickets_document=seed_document,
        reset_tickets=reset_tickets,
    )


def _company_directories() -> list[Path]:
    if not DATA_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in DATA_ROOT.iterdir()
        if path.is_dir()
        and all((path / f"{name}.json").is_file() for name in JSON_NAMES)
    )


def _confirm_cloud_import(
    companies: list[tuple[str, dict[str, dict]]],
    reset: bool,
    yes: bool,
    reset_passwords: bool = False,
) -> bool:
    if storage.database_is_sqlite() or yes:
        return True
    host = storage.database_host()
    company_labels = ", ".join(company_id for company_id, _ in companies)
    operation = (
        "회사 데이터 전체 교체 + seed 티켓 초기화"
        if reset
        else "회사 데이터 전체 교체 + 티켓 기존 상태 보존"
    )
    print(f"대상 DB 호스트(비밀번호 제외): {host}")
    print(f"회사: {company_labels}")
    if reset_passwords:
        operation += " + 사원 비밀번호를 employees.json 값으로 초기화(ADMIN 제외)"
    print(f"작업 내용: {operation}")
    try:
        answer = input("계속 진행할까요? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _print_initial_passwords(rows: list[dict]) -> None:
    """password가 없던 사원의 새 초기 비밀번호. DB에는 원문을 남기지 않으므로 지금만 보인다."""
    if not rows:
        return
    print(f"  새 초기 비밀번호 {len(rows)}건 (지금만 표시됩니다. 사원에게 전달하세요):")
    print("  사번\t이름\t초기 비밀번호")
    for row in rows:
        print(f"  {row['employee_id']}\t{row['name']}\t{row['initial_password']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="회사 JSON 데이터를 PostgreSQL/SQLite로 가져옵니다."
    )
    parser.add_argument(
        "company_dir", nargs="?", help="가져올 회사 폴더 (예: data/bridgesoft)"
    )
    parser.add_argument(
        "--all", action="store_true", help="data/ 아래 모든 회사 가져오기"
    )
    parser.add_argument(
        "--reset-tickets", action="store_true", help="각 회사 티켓을 seed 상태로 초기화"
    )
    parser.add_argument(
        "--reset-employee-passwords",
        action="store_true",
        help=(
            "시연용: 사원 계정 비밀번호를 employees.json 값으로 되돌리고 "
            "잠금·실패 횟수 초기화 (ADMIN 제외)"
        ),
    )
    parser.add_argument(
        "--yes", action="store_true", help="클라우드 DB 확인 프롬프트 생략"
    )
    args = parser.parse_args(argv)

    if args.all == bool(args.company_dir):
        parser.error("회사 폴더 하나를 지정하거나 --all 중 하나를 사용하세요.")

    try:
        if args.all:
            directories = _company_directories()
            if not directories:
                raise ImportValidationError(
                    "data/ 아래에 가져올 회사 JSON 폴더가 없습니다."
                )
            prepared = []
            for directory in directories:
                company_id, documents = load_company_directory(directory)
                # Validate every seed before any company starts importing.
                if args.reset_tickets:
                    load_seed_tickets(company_id)
                prepared.append((company_id, documents))
        else:
            company_id, documents = load_company_directory(args.company_dir)
            if args.reset_tickets:
                load_seed_tickets(company_id)
            prepared = [(company_id, documents)]

        if not _confirm_cloud_import(
            prepared, args.reset_tickets, args.yes, args.reset_employee_passwords
        ):
            print("취소했습니다. DB는 변경하지 않았습니다.")
            return 1

        storage.init_db()
        for company_id, documents in prepared:
            seed_document = (
                load_seed_tickets(company_id) if args.reset_tickets else None
            )
            result = storage.import_company_documents(
                company_id,
                documents,
                seed_tickets_document=seed_document,
                reset_tickets=args.reset_tickets,
            )
            print(
                f"가져오기 완료: {company_id} "
                f"(data_version={result['data_version']}, 티켓 {result['ticket_count']}건)"
            )
            print(f"  {result['company_name']} / 회사 코드 {result['company_code']}")
            _print_initial_passwords(result.get("initial_passwords") or [])
            if result["ticket_action"] == "preserved":
                print("티켓은 기존 DB 상태 유지(초기화하려면 --reset-tickets)")
            if args.reset_employee_passwords:
                changed = storage.reset_employee_passwords(
                    company_id, documents["employees"].get("employees") or []
                )
                # 비밀번호 값은 출력하지 않는다.
                print(f"  사원 비밀번호 초기화: {changed}명")
        return 0
    except (ImportValidationError, storage.StorageUnavailable, ValueError) as exc:
        print(f"가져오기 중단: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
