"""Validate and import one or all company JSON input datasets into the DB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402


DATA_ROOT = ROOT / "data"
SEED_ROOT = ROOT / "seed"
JSON_NAMES = ("policies", "employees", "locations", "handovers", "tickets")


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


def _require_fields(
    record: Any, required: tuple[str, ...], file_name: str, collection: str, index: int
) -> None:
    label = f"{file_name}: {collection} 항목 {index}"
    if not isinstance(record, dict):
        raise ImportValidationError(f"{label}은 객체여야 합니다.")
    missing = [key for key in required if key not in record or record[key] is None]
    if missing:
        raise ImportValidationError(
            f"{label}에 필수 필드가 없습니다: {', '.join(missing)}"
        )


def _require_list(document: dict, key: str, file_name: str) -> list:
    value = document.get(key)
    if not isinstance(value, list):
        raise ImportValidationError(f"{file_name}: {key}는 배열이어야 합니다.")
    return value


def _check_unique(
    records: list[dict], key: str, file_name: str, collection: str
) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        value = str(record[key])
        if value in seen:
            raise ImportValidationError(
                f"{file_name}: {collection} 항목 {index}에서 중복 {key}={value}"
            )
        seen.add(value)


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

    policies_doc = documents["policies"]
    policy_rows = _require_list(policies_doc, "policies", "policies.json")
    _require_fields(
        policies_doc.get("search_policy_config"),
        ("preferred_policy_type",),
        "policies.json",
        "search_policy_config",
        1,
    )
    for index, row in enumerate(policy_rows, 1):
        _require_fields(
            row,
            ("id", "category", "article", "title", "content"),
            "policies.json",
            "policies",
            index,
        )
        if "keywords" in row and not isinstance(row["keywords"], list):
            raise ImportValidationError(
                f"policies.json: policies 항목 {index}의 keywords는 배열이어야 합니다."
            )
    _check_unique(policy_rows, "id", "policies.json", "policies")

    employees_rows = _require_list(
        documents["employees"], "employees", "employees.json"
    )
    for index, row in enumerate(employees_rows, 1):
        _require_fields(
            row,
            ("employee_id", "name", "department", "annual_leave"),
            "employees.json",
            "employees",
            index,
        )
        if not isinstance(row["annual_leave"], dict):
            raise ImportValidationError(
                f"employees.json: employees 항목 {index}의 annual_leave는 객체여야 합니다."
            )
    _check_unique(employees_rows, "employee_id", "employees.json", "employees")
    # position is deliberately optional; password is deliberately ignored by storage.

    locations_rows = _require_list(
        documents["locations"], "locations", "locations.json"
    )
    if not isinstance(
        documents["locations"].get("explicitly_not_registered", []), list
    ):
        raise ImportValidationError(
            "locations.json: explicitly_not_registered는 배열이어야 합니다."
        )
    for index, row in enumerate(locations_rows, 1):
        _require_fields(
            row,
            ("location_id", "name", "building", "floor", "zone"),
            "locations.json",
            "locations",
            index,
        )
    _check_unique(locations_rows, "location_id", "locations.json", "locations")

    handover_rows = _require_list(documents["handovers"], "handovers", "handovers.json")
    for index, row in enumerate(handover_rows, 1):
        _require_fields(
            row,
            ("handover_id", "department", "title", "summary"),
            "handovers.json",
            "handovers",
            index,
        )
        if not isinstance(row["summary"], dict):
            raise ImportValidationError(
                f"handovers.json: handovers 항목 {index}의 summary는 객체여야 합니다."
            )
    _check_unique(handover_rows, "handover_id", "handovers.json", "handovers")

    tickets_doc = documents["tickets"]
    ticket_rows = _require_list(tickets_doc, "tickets", "tickets.json")
    if "next_sequence" not in tickets_doc:
        raise ImportValidationError("tickets.json: next_sequence 필드가 없습니다.")
    try:
        if int(tickets_doc["next_sequence"]) < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise ImportValidationError(
            "tickets.json: next_sequence는 1 이상의 정수여야 합니다."
        ) from None
    if not isinstance(tickets_doc.get("creation_rule"), dict):
        raise ImportValidationError("tickets.json: creation_rule은 객체여야 합니다.")
    equipment_rows = _require_list(tickets_doc, "allowed_equipment", "tickets.json")
    for index, row in enumerate(ticket_rows, 1):
        _require_fields(
            row,
            (
                "ticket_id",
                "employee_id",
                "type",
                "item",
                "quantity",
                "status",
                "created_at",
            ),
            "tickets.json",
            "tickets",
            index,
        )
    _check_unique(ticket_rows, "ticket_id", "tickets.json", "tickets")
    for index, row in enumerate(equipment_rows, 1):
        _require_fields(row, ("item",), "tickets.json", "allowed_equipment", index)
    _check_unique(equipment_rows, "item", "tickets.json", "allowed_equipment")
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
    rows = _require_list(document, "tickets", seed_path.name)
    for index, row in enumerate(rows, 1):
        _require_fields(
            row,
            (
                "ticket_id",
                "employee_id",
                "type",
                "item",
                "quantity",
                "status",
                "created_at",
            ),
            seed_path.name,
            "tickets",
            index,
        )
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
    companies: list[tuple[str, dict[str, dict]]], reset: bool, yes: bool
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
    print(f"작업 내용: {operation}")
    try:
        answer = input("계속 진행할까요? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


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

        if not _confirm_cloud_import(prepared, args.reset_tickets, args.yes):
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
            if result["ticket_action"] == "preserved":
                print("티켓은 기존 DB 상태 유지(초기화하려면 --reset-tickets)")
        return 0
    except (ImportValidationError, storage.StorageUnavailable, ValueError) as exc:
        print(f"가져오기 중단: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
