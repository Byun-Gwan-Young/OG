"""회사 데이터 문서 5종의 검증 · 업로드 해석 · 변경 비교.

파일이나 DB를 읽지 않는 순수 함수만 둔다. 같은 검증을
- scripts/import_company.py (운영자 CLI, 폴더의 JSON 5종)
- mcp_server.py 관리자 업로드 (메모리의 JSON, 일부 종류만)
가 함께 쓴다.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DOC_KINDS = ("policies", "employees", "locations", "handovers", "tickets")
DOC_LABELS = {
    "policies": "규정",
    "employees": "사원",
    "locations": "장소",
    "handovers": "인수인계서",
    "tickets": "비품·신청 규칙",
}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# 종류별 항목 목록 키와 항목 ID 필드
COLLECTIONS = {
    "policies": ("policies", "id"),
    "employees": ("employees", "employee_id"),
    "locations": ("locations", "location_id"),
    "handovers": ("handovers", "handover_id"),
    "tickets": ("allowed_equipment", "item"),
}
REQUIRED_FIELDS = {
    "policies": ("id", "category", "article", "title", "content"),
    "employees": ("employee_id", "name", "department", "annual_leave"),
    "locations": ("location_id", "name", "building", "floor", "zone"),
    "handovers": ("handover_id", "department", "title", "summary"),
    "tickets": ("item",),
}
# 업로드에서는 회사 식별 필드를 무시하고 로그인한 관리자의 회사로 강제한다.
COMPANY_FIELDS = ("company_id", "company_name", "company")
# 비품·신청 규칙 업로드에서는 기존 신청 내역과 다음 번호를 건드리지 않는다.
TICKET_HISTORY_FIELDS = ("tickets", "next_sequence", "ticket_id_format")
TICKET_ROW_FIELDS = (
    "ticket_id",
    "employee_id",
    "type",
    "item",
    "quantity",
    "status",
    "created_at",
)


def issue(
    file: str,
    problem: str,
    *,
    collection: str | None = None,
    index: int | None = None,
    field: str | None = None,
) -> dict:
    """검증 오류 1건. message는 사람이 읽는 한 줄(파일명 · 항목 · 필드 · 문제)."""
    where = file
    if collection and index is not None:
        where += f" {collection} 항목 {index}"
    elif collection:
        where += f" {collection}"
    message = f"{where}: {problem}"
    return {
        "file": file,
        "item": f"{index}번째 항목" if index is not None else "-",
        "field": field or "-",
        "problem": problem,
        "message": message,
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_rows(
    kind: str, document: dict, file: str, issues: list[dict]
) -> list[dict]:
    collection, id_field = COLLECTIONS[kind]
    rows = document.get(collection)
    if not isinstance(rows, list):
        issues.append(
            issue(file, f"{collection}는 배열이어야 합니다.", field=collection)
        )
        return []
    seen: set[str] = set()
    valid: list[dict] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            issues.append(
                issue(file, "항목은 객체여야 합니다.", collection=collection, index=index)
            )
            continue
        missing = [
            key for key in REQUIRED_FIELDS[kind] if key not in row or row[key] is None
        ]
        for key in missing:
            issues.append(
                issue(
                    file,
                    f"필수 필드가 없습니다: {key}",
                    collection=collection,
                    index=index,
                    field=key,
                )
            )
        if missing:
            continue
        key_value = str(row[id_field])
        if key_value in seen:
            issues.append(
                issue(
                    file,
                    f"중복 {id_field}={key_value}",
                    collection=collection,
                    index=index,
                    field=id_field,
                )
            )
        seen.add(key_value)
        valid.append((index, row))
    return valid


def validate_document(kind: str, document: Any, *, mode: str = "upload") -> list[dict]:
    """문서 1개를 검증해 오류 목록을 돌려준다(없으면 빈 목록).

    mode="import": 운영자 CLI. tickets.json의 신청 내역(tickets, next_sequence)도 검사한다.
    mode="upload": 관리자 업로드. 신청 내역 필드는 무시되므로 검사하지 않는다.
    """
    file = f"{kind}.json"
    issues: list[dict] = []
    if not isinstance(document, dict):
        return [issue(file, "최상위 값은 객체여야 합니다.")]
    collection, _ = COLLECTIONS[kind]
    rows = _check_rows(kind, document, file, issues)

    if kind == "policies":
        config = document.get("search_policy_config")
        if not isinstance(config, dict):
            issues.append(
                issue(
                    file,
                    "search_policy_config는 객체여야 합니다.",
                    field="search_policy_config",
                )
            )
        elif config.get("preferred_policy_type") is None:
            issues.append(
                issue(
                    file,
                    "필수 필드가 없습니다: preferred_policy_type",
                    collection="search_policy_config",
                    field="preferred_policy_type",
                )
            )
        for index, row in rows:
            if "keywords" in row and not isinstance(row["keywords"], list):
                issues.append(
                    issue(
                        file,
                        "keywords는 배열이어야 합니다.",
                        collection=collection,
                        index=index,
                        field="keywords",
                    )
                )
    elif kind == "employees":
        for index, row in rows:
            if not isinstance(row["annual_leave"], dict):
                issues.append(
                    issue(
                        file,
                        "annual_leave는 객체여야 합니다.",
                        collection=collection,
                        index=index,
                        field="annual_leave",
                    )
                )
            password = row.get("password")
            if password is not None and str(password).strip() != "" and (
                not isinstance(password, (str, int)) or isinstance(password, bool)
                or len(str(password).strip()) < 4
            ):
                issues.append(
                    issue(
                        file,
                        "password는 4자 이상이어야 합니다(비워 두면 초기 비밀번호를 만듭니다).",
                        collection=collection,
                        index=index,
                        field="password",
                    )
                )
    elif kind == "locations":
        if not isinstance(document.get("explicitly_not_registered", []), list):
            issues.append(
                issue(
                    file,
                    "explicitly_not_registered는 배열이어야 합니다.",
                    field="explicitly_not_registered",
                )
            )
        for index, row in rows:
            if not _is_int(row["floor"]):
                issues.append(
                    issue(
                        file,
                        "floor는 정수여야 합니다.",
                        collection=collection,
                        index=index,
                        field="floor",
                    )
                )
            if "aliases" in row and not isinstance(row["aliases"], list):
                issues.append(
                    issue(
                        file,
                        "aliases는 배열이어야 합니다.",
                        collection=collection,
                        index=index,
                        field="aliases",
                    )
                )
    elif kind == "handovers":
        for index, row in rows:
            if not isinstance(row["summary"], dict):
                issues.append(
                    issue(
                        file,
                        "summary는 객체여야 합니다.",
                        collection=collection,
                        index=index,
                        field="summary",
                    )
                )
    elif kind == "tickets":
        if not isinstance(document.get("creation_rule"), dict):
            issues.append(
                issue(file, "creation_rule은 객체여야 합니다.", field="creation_rule")
            )
        if "status_values" in document and not isinstance(
            document["status_values"], list
        ):
            issues.append(
                issue(file, "status_values는 배열이어야 합니다.", field="status_values")
            )
        for index, row in rows:
            limit = row.get("max_quantity_per_employee")
            if limit is not None and (not _is_int(limit) or limit < 1):
                issues.append(
                    issue(
                        file,
                        "max_quantity_per_employee는 1 이상의 정수여야 합니다.",
                        collection=collection,
                        index=index,
                        field="max_quantity_per_employee",
                    )
                )
        if mode == "import":
            ticket_rows = document.get("tickets")
            if not isinstance(ticket_rows, list):
                issues.append(issue(file, "tickets는 배열이어야 합니다.", field="tickets"))
                ticket_rows = []
            if "next_sequence" not in document:
                issues.append(
                    issue(file, "next_sequence 필드가 없습니다.", field="next_sequence")
                )
            else:
                try:
                    if int(document["next_sequence"]) < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    issues.append(
                        issue(
                            file,
                            "next_sequence는 1 이상의 정수여야 합니다.",
                            field="next_sequence",
                        )
                    )
            seen: set[str] = set()
            for index, row in enumerate(ticket_rows, 1):
                if not isinstance(row, dict):
                    issues.append(
                        issue(file, "항목은 객체여야 합니다.", collection="tickets", index=index)
                    )
                    continue
                missing = [k for k in TICKET_ROW_FIELDS if row.get(k) is None]
                for key in missing:
                    issues.append(
                        issue(
                            file,
                            f"필수 필드가 없습니다: {key}",
                            collection="tickets",
                            index=index,
                            field=key,
                        )
                    )
                if not missing:
                    if str(row["ticket_id"]) in seen:
                        issues.append(
                            issue(
                                file,
                                f"중복 ticket_id={row['ticket_id']}",
                                collection="tickets",
                                index=index,
                                field="ticket_id",
                            )
                        )
                    seen.add(str(row["ticket_id"]))
    return issues


def detect_kind(file_name: str, document: Any) -> str | None:
    """파일 이름(policies.json 등)이나 내용의 목록 키로 데이터 종류를 정한다."""
    stem = file_name.rsplit("/", 1)[-1].lower()
    for suffix in (".json",):
        stem = stem.removesuffix(suffix)
    for kind in DOC_KINDS:
        if stem == kind or stem.startswith(f"{kind}.") or stem.startswith(f"{kind}_"):
            return kind
    if isinstance(document, dict):
        found = [
            kind
            for kind in DOC_KINDS
            if COLLECTIONS[kind][0] in document
            and not (kind == "tickets" and "employees" in document)
        ]
        if len(found) == 1:
            return found[0]
    return None


def parse_upload(file_name: str, raw: bytes) -> tuple[Any, list[dict]]:
    """업로드된 바이트를 JSON으로 해석한다. 파일로 저장하지 않는다."""
    name = str(file_name or "upload.json")
    if not name.lower().endswith(".json"):
        return None, [issue(name, "JSON 파일(.json)만 올릴 수 있습니다.")]
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, [issue(name, "파일 크기는 2MB 이하여야 합니다.")]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [issue(name, "UTF-8 텍스트 파일이 아닙니다.")]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [
            issue(
                name,
                f"JSON 문법 오류 {exc.lineno}행 {exc.colno}열 ({exc.msg})",
                field=f"{exc.lineno}행",
            )
        ]
    if not isinstance(value, dict):
        return None, [issue(name, "최상위 값은 객체여야 합니다.")]
    return value, []


def normalize_upload(kind: str, document: dict) -> dict:
    """회사 식별 필드·양식 안내(_로 시작) 필드를 버린다. 티켓 규칙은 신청 내역 필드를 버린다."""
    result = {
        key: value
        for key, value in document.items()
        if key not in COMPANY_FIELDS and not str(key).startswith("_")
    }
    collection, _ = COLLECTIONS[kind]
    if isinstance(result.get(collection), list):
        result[collection] = [
            {
                key: value
                for key, value in row.items()
                if key != "company_id" and not str(key).startswith("_")
            }
            if isinstance(row, dict)
            else row
            for row in result[collection]
        ]
    if kind == "tickets":
        for key in TICKET_HISTORY_FIELDS:
            result.pop(key, None)
    return result


def upload_fingerprint(files: list[dict]) -> str:
    """미리보기한 파일과 반영하는 파일이 같은지 확인하는 해시."""
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda f: str(f.get("name"))):
        digest.update(str(item.get("name")).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.get("content")).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _clean(item)
            for key, item in value.items()
            if key not in ("company_id", "password")
            and item is not None
            and item != []
            and item != {}
        }
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def diff_document(kind: str, current: dict, new: dict) -> dict:
    """종류별 추가/수정/삭제 개수와 수정된 항목 ID."""
    collection, id_field = COLLECTIONS[kind]
    old_rows = {
        str(row[id_field]): _clean(row)
        for row in current.get(collection, [])
        if isinstance(row, dict) and id_field in row
    }
    new_rows = {
        str(row[id_field]): _clean(row)
        for row in new.get(collection, [])
        if isinstance(row, dict) and id_field in row
    }
    added = [key for key in new_rows if key not in old_rows]
    removed = [key for key in old_rows if key not in new_rows]
    updated = [
        key for key in new_rows if key in old_rows and new_rows[key] != old_rows[key]
    ]
    settings_changed = False
    if kind == "policies":
        settings_changed = _clean(current.get("search_policy_config") or {}) != _clean(
            new.get("search_policy_config") or {}
        )
    elif kind == "locations":
        settings_changed = list(current.get("explicitly_not_registered") or []) != list(
            new.get("explicitly_not_registered") or []
        )
    elif kind == "tickets":
        settings_changed = _clean(current.get("creation_rule") or {}) != _clean(
            new.get("creation_rule") or {}
        ) or list(current.get("status_values") or []) != list(
            new.get("status_values") or []
        )
    return {
        "kind": kind,
        "label": DOC_LABELS[kind],
        "added": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "unchanged": len(new_rows) - len(added) - len(updated),
        "added_ids": added,
        "updated_ids": updated,
        "removed_ids": removed,
        "settings_changed": settings_changed,
    }


def empty_documents() -> dict[str, dict]:
    """데이터가 하나도 없는 회사의 문서 5종."""
    return {
        "policies": {"policies": [], "search_policy_config": {}},
        "employees": {"employees": []},
        "locations": {"locations": [], "explicitly_not_registered": []},
        "handovers": {"handovers": []},
        "tickets": {"allowed_equipment": [], "creation_rule": {}, "status_values": []},
    }


def merge_documents(current: dict[str, dict], uploaded: dict[str, dict]) -> dict[str, dict]:
    """올린 종류는 통째로 바꾸고, 올리지 않은 종류는 현재 데이터를 유지한다."""
    merged: dict[str, dict] = {}
    base = empty_documents()
    for kind in DOC_KINDS:
        if kind in uploaded:
            document = dict(uploaded[kind])
            if kind == "tickets":
                # 신청 번호 형식은 기존 값을 유지한다.
                for key in TICKET_HISTORY_FIELDS:
                    if key in (current.get(kind) or {}):
                        document[key] = current[kind][key]
            merged[kind] = document
        else:
            merged[kind] = dict(current.get(kind) or base[kind])
    return merged
