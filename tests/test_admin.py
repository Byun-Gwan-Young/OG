"""관리자 툴(3단계) 테스트: 업로드 미리보기·반영, 격리, 신청 상태 흐름, 계정 관리, 감사 로그."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_server as s
import storage
from scripts import set_admin_password

ROOT = Path(__file__).resolve().parents[1]
A, B = "bridgesoft", "nextwave"
ADMIN_PW = "admin-pass-1"
pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture(autouse=True)
def _admins(fresh_db):
    set_admin_password.main([A, "--password", ADMIN_PW])
    set_admin_password.main([B, "--password", ADMIN_PW + "b"])


def _doc(kind: str, company_id: str = A) -> dict:
    return json.loads((ROOT / "data" / company_id / f"{kind}.json").read_text("utf-8"))


def _file(name: str, document) -> dict:
    content = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
    return {"name": name, "content": content}


def _preview(files, company_id=A, actor="ADMIN"):
    return s.ui_admin_preview_upload(company_id, actor, files)


def _apply(files, company_id=A, actor="ADMIN"):
    preview = _preview(files, company_id, actor)
    assert preview["ok"], preview
    data = preview["data"]
    return s.ui_admin_apply_upload(
        company_id, actor, files, data["base_version"], data["fingerprint"]
    )


def _policies_with_meal(amount: str) -> dict:
    document = _doc("policies")
    for row in document["policies"]:
        if row["id"] == "BS-103":
            row["content"] = row["content"].replace("12,000원", amount)
    return document


def _version(company_id=A) -> int:
    return storage.get_company(company_id)["data_version"]


# ---- 업로드: 형식 검사 ---------------------------------------------------------

def test_invalid_json_reports_line_number():
    r = _preview([_file("policies.json", '{\n  "policies": [,]\n}')])
    assert r["code"] == "UPLOAD_INVALID"
    issue = r["data"]["issues"][0]
    assert issue["file"] == "policies.json" and "2행" in issue["problem"]


def test_missing_field_reported_as_table_row_and_nothing_changes():
    document = _doc("employees")
    document["employees"][2].pop("name")
    before = _version()
    r = _preview([_file("employees.json", document)])
    assert r["code"] == "UPLOAD_INVALID"
    issue = r["data"]["issues"][0]
    assert (issue["file"], issue["item"], issue["field"]) == ("employees.json", "3번째 항목", "name")
    assert _version() == before


def test_over_two_megabytes_and_non_json_are_rejected():
    big = '{"policies": [], "pad": "' + "x" * (2 * 1024 * 1024) + '"}'
    r = _preview([_file("policies.json", big)])
    assert r["code"] == "UPLOAD_INVALID" and "2MB" in r["data"]["issues"][0]["problem"]
    r = _preview([_file("policies.txt", "{}")])
    assert r["code"] == "UPLOAD_INVALID"
    assert _preview([])["code"] == "UPLOAD_EMPTY"


def test_unknown_kind_and_duplicate_kind_are_rejected():
    assert _preview([_file("data.json", {"foo": []})])["code"] == "UPLOAD_INVALID"
    files = [_file("policies.json", _doc("policies")), _file("rules.json", _doc("policies"))]
    r = _preview(files)
    assert r["code"] == "UPLOAD_INVALID"


# ---- 업로드: 미리보기 · 반영 ------------------------------------------------------

@pytest.mark.reimport
def test_preview_does_not_change_db_and_apply_changes_search_result():
    files = [_file("policies.json", _policies_with_meal("13,000원"))]
    before_version = _version()
    preview = _preview(files)
    assert preview["ok"] and preview["code"] == "PREVIEW"
    change = preview["data"]["changes"][0]
    assert change["kind"] == "policies" and change["updated_ids"] == ["BS-103"]
    assert change["added"] == 0 and change["removed"] == 0
    # 미리보기만으로는 아무것도 바뀌지 않는다.
    assert _version() == before_version
    assert "12,000원" in s.search_policy("점심 식대 한도", A, "EMP001")["data"]["results"][0]["content"]

    applied = s.ui_admin_apply_upload(
        A, "ADMIN", files, preview["data"]["base_version"], preview["data"]["fingerprint"]
    )
    assert applied["ok"] and applied["data"]["data_version"] == before_version + 1
    top = s.search_policy("점심 식대 한도", A, "EMP001")["data"]["results"][0]
    assert "13,000원" in top["content"]
    # 올리지 않은 종류는 그대로
    assert s.get_office_location("대회의실", A, "EMP001")["ok"]
    assert s.get_employee_info(A, "EMP001")["data"]["annual_leave"]["remaining_days"] == 11


@pytest.mark.reimport
def test_company_id_inside_file_is_ignored():
    document = _policies_with_meal("14,000원")
    document["company_id"] = B
    document["company_name"] = "다른 회사"
    for row in document["policies"]:
        row["company_id"] = B
    nextwave_version = _version(B)
    assert _apply([_file("policies.json", document)])["ok"]
    assert "14,000원" in s.search_policy("점심 식대 한도", A, "EMP001")["data"]["results"][0]["content"]
    assert _version(B) == nextwave_version
    assert "15,000원" in s.search_policy("점심 식대 한도", B, "EMP001")["data"]["results"][0]["content"]
    assert storage.get_company(A)["company_name"] != "다른 회사"


@pytest.mark.reimport
def test_ticket_rules_upload_keeps_existing_tickets_and_next_number():
    before_tickets = storage.get_tickets(A)
    before_next = storage.get_company_document(A, "tickets")["next_sequence"]
    document = _doc("tickets")
    document["tickets"] = []          # 파일 속 신청 내역은 무시된다
    document["next_sequence"] = 1     # 다음 번호도 무시된다
    document["allowed_equipment"] = [
        row for row in document["allowed_equipment"] if row["item"] != "노트북 거치대"
    ]
    r = _apply([_file("tickets.json", document)])
    assert r["ok"]
    assert storage.get_tickets(A) == before_tickets
    assert storage.get_company_document(A, "tickets")["next_sequence"] == before_next
    assert s.create_support_ticket("노트북 거치대", A, "EMP001")["code"] == "REJECTED"
    created = s.ui_confirm_ticket(A, "EMP001", "모니터", 1, "", "비품")
    assert created["ok"], created
    assert created["data"]["ticket"]["ticket_id"] == f"REQ-{before_next:03d}"


@pytest.mark.pg
@pytest.mark.reimport
def test_stale_preview_cannot_be_applied():
    first = [_file("policies.json", _policies_with_meal("13,000원"))]
    preview = _preview(first)["data"]
    assert _apply([_file("policies.json", _policies_with_meal("16,000원"))])["ok"]
    stale = s.ui_admin_apply_upload(A, "ADMIN", first, preview["base_version"], preview["fingerprint"])
    assert stale["code"] == "UPLOAD_STALE"
    # 미리보기와 다른 파일을 보내도 거부
    fresh = _preview(first)["data"]
    other = [_file("policies.json", _policies_with_meal("17,000원"))]
    assert s.ui_admin_apply_upload(A, "ADMIN", other, fresh["base_version"], fresh["fingerprint"])["code"] == "UPLOAD_STALE"


@pytest.mark.reimport
def test_employee_upload_creates_initial_passwords_once_and_locks_removed():
    document = _doc("employees")
    removed = document["employees"].pop()
    document["employees"].append({
        "employee_id": "EMP777", "name": "새사원", "department": "백엔드개발팀",
        "annual_leave": {"remaining_days": 15},
    })
    preview = _preview([_file("employees.json", document)])
    change = preview["data"]["changes"][0]
    assert change["added_ids"] == ["EMP777"] and change["removed_ids"] == [removed["employee_id"]]
    assert preview["data"]["initial_passwords_to_create"] == 1
    applied = _apply([_file("employees.json", document)])
    rows = applied["data"]["initial_passwords"]
    assert [r["employee_id"] for r in rows] == ["EMP777"]
    password = rows[0]["initial_password"]
    code = storage.get_company(A)["company_code"]
    assert s.ui_login(code, "EMP777", password)["data"]["must_change"] is True
    assert s.ui_login(code, removed["employee_id"], str(removed["password"]))["code"] == "LOGIN_FAILED"
    # 같은 파일을 다시 반영해도 초기 비밀번호를 다시 만들지 않는다.
    again = _apply([_file("employees.json", document)])
    assert again["data"]["initial_passwords"] == []


def test_register_then_upload_full_company_data():
    company = s.ui_register_company("새회사", ADMIN_PW, "")["data"]
    cid = company["company_id"]
    files = [_file(f"{kind}.json", _doc(kind)) for kind in ("policies", "employees", "locations", "handovers", "tickets")]
    applied = _apply(files, company_id=cid)
    assert applied["ok"]
    assert applied["data"]["initial_passwords"] == []  # 원본에는 비밀번호가 있다
    assert storage.get_company(cid)["company_name"] == "새회사"
    assert storage.get_tickets(cid) == []  # 업로드 파일의 신청 내역은 가져오지 않는다
    assert "12,000원" in s.search_policy("점심 식대 한도", cid, "EMP001")["data"]["results"][0]["content"]
    assert s.ui_admin_overview(cid, "ADMIN")["data"]["has_data"] is True
    pw = next(str(e["password"]) for e in _doc("employees")["employees"] if e["employee_id"] == "EMP001")
    assert s.ui_login(company["company_code"], "EMP001", pw)["ok"]
    ticket = s.create_support_ticket("노트북 거치대", cid, "EMP001", confirmed=True)
    assert ticket["ok"] and ticket["data"]["ticket"]["ticket_id"] == "REQ-001"


def test_templates_are_valid_uploads():
    company = s.ui_register_company("양식회사", ADMIN_PW, "")["data"]
    files = [
        {"name": f"{kind}.json", "content": (ROOT / "templates" / f"{kind}.json").read_text("utf-8")}
        for kind in ("policies", "employees", "locations", "handovers", "tickets")
    ]
    applied = _apply(files, company_id=company["company_id"])
    assert applied["ok"], applied
    assert [r["employee_id"] for r in applied["data"]["initial_passwords"]] == ["EMP001"]


# ---- 격리 ----------------------------------------------------------------------

ADMIN_TOOLS = [
    (s.ui_admin_overview, {}),
    (s.ui_admin_preview_upload, {"files": [_file("policies.json", {"policies": []})]}),
    (s.ui_admin_apply_upload, {"files": [], "base_version": 1, "fingerprint": ""}),
    (s.ui_admin_list_employees, {}),
    (s.ui_admin_employee_detail, {"employee_id": "EMP001"}),
    (s.ui_admin_list_tickets, {}),
    (s.ui_admin_update_ticket, {"ticket_id": "REQ-003", "to_status": "검토중"}),
    (s.ui_admin_list_accounts, {}),
    (s.ui_admin_reset_password, {"employee_id": "EMP001"}),
    (s.ui_admin_unlock_account, {"employee_id": "EMP001"}),
]


@pytest.mark.parametrize("tool,args", ADMIN_TOOLS, ids=lambda v: getattr(v, "__name__", ""))
def test_employee_account_and_other_company_cannot_use_admin_tools(tool, args):
    assert tool(A, "EMP001", **args)["code"] == "PERMISSION_DENIED"
    assert tool(A, "NOBODY", **args)["code"] == "PERMISSION_DENIED"
    # B 회사 관리자 세션이 company_id만 A로 바꿔도 A에는 그 계정이 없다 → 아래가 그 경우.
    company = s.ui_register_company("격리회사", ADMIN_PW, "")["data"]
    assert tool(company["company_id"], "EMP001", **args)["code"] == "PERMISSION_DENIED"


def test_admin_sees_only_own_company():
    company = s.ui_register_company("격리회사", ADMIN_PW, "")["data"]
    cid = company["company_id"]
    assert s.ui_admin_list_employees(cid, "ADMIN")["data"]["employees"] == []
    assert s.ui_admin_list_tickets(cid, "ADMIN")["data"]["tickets"] == []
    assert s.ui_admin_employee_detail(cid, "ADMIN", "EMP001")["code"] == "NOT_FOUND"
    assert s.ui_admin_update_ticket(cid, "ADMIN", "REQ-003", "검토중")["code"] == "NOT_FOUND"
    assert s.ui_admin_reset_password(cid, "ADMIN", "EMP001")["code"] == "NOT_FOUND"
    # 업로드는 로그인한 관리자 회사(cid)에만 들어간다.
    before = _version(A)
    assert _apply([_file("policies.json", _policies_with_meal("19,000원"))], company_id=cid)["ok"]
    assert _version(A) == before
    a_tickets = s.ui_admin_list_tickets(A, "ADMIN")["data"]["tickets"]
    assert {t["ticket_id"] for t in a_tickets} >= {"REQ-001", "REQ-002", "REQ-003"}


# ---- 사원 · 신청 관리 ---------------------------------------------------------------

def test_employee_list_and_detail():
    rows = s.ui_admin_list_employees(A, "ADMIN")["data"]["employees"]
    first = rows[0]
    assert first["employee_id"] == "EMP001" and first["remaining_days"] == 11
    assert first["account_status_label"] == "정상"
    detail = s.ui_admin_employee_detail(A, "ADMIN", "EMP001")["data"]
    assert detail["employee"]["name"] == "김신입"
    assert {t["ticket_id"] for t in detail["tickets"]} == {"REQ-001", "REQ-002", "REQ-003"}
    assert detail["handovers"] and all("summary" in h for h in detail["handovers"])
    assert "password" not in json.dumps(detail, ensure_ascii=False)


def test_ticket_status_flow_and_history():
    def move(to, note=""):
        return s.ui_admin_update_ticket(A, "ADMIN", "REQ-003", to, note)

    assert move("지급완료")["code"] == "INVALID_TRANSITION"   # 건너뛰기
    assert move("승인")["code"] == "INVALID_TRANSITION"
    assert move("반려")["code"] == "NOTE_REQUIRED"
    assert move("검토중")["ok"]
    assert move("접수완료")["code"] == "INVALID_TRANSITION"  # 역방향
    r = move("승인", "재고 있음")
    assert r["ok"]
    history = r["data"]["ticket"]["history"]
    assert [(h["from"], h["to"]) for h in history] == [("접수완료", "검토중"), ("검토중", "승인")]
    assert history[1]["by"] == "ADMIN" and history[1]["note"] == "재고 있음" and history[1]["at"]
    # 직원 대시보드에 바로 반영
    sidebar = s.ui_get_sidebar(A, "EMP001")["data"]["tickets"]
    assert next(t for t in sidebar if t["ticket_id"] == "REQ-003")["status"] == "승인"
    assert move("반려", "예산 초과")["ok"]
    assert move("지급완료")["code"] == "INVALID_TRANSITION"   # 반려 후 진행 불가
    # 지급완료에서는 반려도 불가
    assert s.ui_admin_update_ticket(A, "ADMIN", "REQ-001", "반려", "x")["code"] == "INVALID_TRANSITION"
    actions = [e for e in storage.list_audit_log(A) if e["action"] == "ticket_status_changed"]
    assert len(actions) == 3 and actions[0]["detail"]["to"] == "반려"


def test_new_employee_ticket_shows_in_admin_list():
    created = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1, "", "비품")
    ticket_id = created["data"]["ticket"]["ticket_id"]
    tickets = s.ui_admin_list_tickets(A, "ADMIN")["data"]["tickets"]
    assert tickets[0]["ticket_id"] == ticket_id and tickets[0]["name"] == "김신입"
    assert tickets[0]["allowed_next"] == ["검토중", "반려"]


# ---- 계정 -----------------------------------------------------------------------

def test_reset_password_and_unlock():
    code = storage.get_company(A)["company_code"]
    r = s.ui_admin_reset_password(A, "ADMIN", "EMP001")
    password = r["data"]["initial_password"]
    assert len(password) == 4 and password.isdigit()
    login = s.ui_login(code, "EMP001", password)
    assert login["ok"] and login["data"]["must_change"] is True

    for _ in range(5):
        s.ui_login(code, "EMP002", "wrong")
    accounts = {a["employee_id"]: a for a in s.ui_admin_list_accounts(A, "ADMIN")["data"]["accounts"]}
    assert accounts["EMP002"]["account_status_label"] == "잠금"
    assert accounts["EMP001"]["account_status_label"] == "변경 필요"
    assert s.ui_admin_unlock_account(A, "ADMIN", "EMP002")["ok"]
    emp2_pw = next(str(e["password"]) for e in _doc("employees")["employees"] if e["employee_id"] == "EMP002")
    assert s.ui_login(code, "EMP002", emp2_pw)["ok"]
    assert s.ui_admin_reset_password(A, "ADMIN", "ADMIN")["code"] == "NOT_FOUND"


@pytest.mark.reimport
def test_admin_responses_have_no_password_material():
    responses = [
        s.ui_admin_overview(A, "ADMIN"),
        s.ui_admin_list_employees(A, "ADMIN"),
        s.ui_admin_employee_detail(A, "ADMIN", "EMP001"),
        s.ui_admin_list_tickets(A, "ADMIN"),
        s.ui_admin_list_accounts(A, "ADMIN"),
        _preview([_file("employees.json", _doc("employees"))]),
        _apply([_file("employees.json", _doc("employees"))]),
        s.ui_admin_unlock_account(A, "ADMIN", "EMP001"),
        s.ui_register_company("응답검사", ADMIN_PW, ""),
    ]
    rows = storage._run_db(
        lambda conn: [dict(r) for r in conn.execute(storage.accounts.select()).mappings()]
    )
    secrets_ = {r["password_hash"] for r in rows} | {r["salt"] for r in rows} | {ADMIN_PW}
    secrets_ |= {str(e["password"]) for e in _doc("employees")["employees"]}
    for response in responses:
        text = json.dumps(response, ensure_ascii=False)
        assert "salt" not in text and "scrypt$" not in text and "password_hash" not in text
        for secret in secrets_:
            assert f'"{secret}"' not in text


@pytest.mark.reimport
def test_audit_log_records_admin_actions():
    s.ui_admin_list_employees(A, "ADMIN")
    s.ui_admin_list_employees(A, "ADMIN")  # 같은 조회는 5분에 한 번만 기록
    _apply([_file("policies.json", _policies_with_meal("13,000원"))])
    s.ui_admin_update_ticket(A, "ADMIN", "REQ-003", "검토중")
    s.ui_admin_reset_password(A, "ADMIN", "EMP003")
    log = storage.list_audit_log(A)
    actions = [e["action"] for e in log]
    assert actions.count("view_employees") == 1
    for action in ("upload_preview", "upload_applied", "ticket_status_changed", "password_reset", "admin_password_set"):
        assert action in actions
    applied = next(e for e in log if e["action"] == "upload_applied")
    assert applied["actor"] == "ADMIN" and applied["detail"]["kinds"] == ["policies"]
    log_text = json.dumps(log, ensure_ascii=False, default=str)
    assert "scrypt$" not in log_text and ADMIN_PW not in log_text
