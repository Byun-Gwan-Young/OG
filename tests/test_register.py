"""회사 등록(2단계) 테스트: 가입 코드, 회사 코드, ADMIN 로그인, 빈 회사."""
from __future__ import annotations

import json

import pytest

import mcp_server as s
import storage

pytestmark = pytest.mark.usefixtures("fresh_db")
DENY = s.MSG["DENIED_OR_NOT_FOUND"]


@pytest.fixture(autouse=True)
def _no_signup_code(monkeypatch):
    monkeypatch.delenv("OFFICEGENIE_SIGNUP_CODE", raising=False)


def _register(name="테스트상사", password="admin-pass-1", signup_code=""):
    return s.ui_register_company(name, password, signup_code)


def test_register_creates_code_and_admin_login_works():
    r = _register()
    assert r["ok"] and r["code"] == "REGISTERED"
    data = r["data"]
    assert data["company_id"].startswith("c_")
    assert storage.is_valid_company_code(data["company_code"])
    assert data["account_id"] == "ADMIN" and data["company_name"] == "테스트상사"
    assert "admin-pass-1" not in json.dumps(r, ensure_ascii=False)

    login = s.ui_login(data["company_code"].lower(), "ADMIN", "admin-pass-1")
    assert login["ok"] and login["data"]["role"] == "admin"
    assert login["data"]["company_id"] == data["company_id"]
    actions = [e["action"] for e in storage.list_audit_log(data["company_id"])]
    assert "company_registered" in actions


@pytest.mark.pg
def test_same_name_gets_a_different_code_and_id():
    first, second = _register()["data"], _register()["data"]
    assert first["company_code"] != second["company_code"]
    assert first["company_id"] != second["company_id"]


def test_register_validation():
    assert _register(password="short")["code"] == "ADMIN_PASSWORD_SHORT"
    assert _register(name="   ")["code"] == "COMPANY_NAME_INVALID"
    assert _register(name="가" * 101)["code"] == "COMPANY_NAME_INVALID"


def test_signup_code_required_when_configured(monkeypatch):
    assert s.ui_signup_settings()["data"] == {"signup_code_required": False}
    monkeypatch.setenv("OFFICEGENIE_SIGNUP_CODE", "let-me-in")
    settings = s.ui_signup_settings()
    assert settings["data"] == {"signup_code_required": True}
    assert "let-me-in" not in json.dumps(settings)
    before = len(storage.list_companies_overview())
    assert _register(signup_code="")["code"] == "SIGNUP_CODE_INVALID"
    assert _register(signup_code="wrong")["code"] == "SIGNUP_CODE_INVALID"
    assert len(storage.list_companies_overview()) == before
    assert _register(signup_code=" let-me-in ")["ok"]


def test_empty_company_admin_overview_and_tools_do_not_break():
    company = _register()["data"]
    cid = company["company_id"]
    overview = s.ui_admin_overview(cid, "ADMIN")
    assert overview["ok"] and overview["data"]["has_data"] is False
    assert overview["data"]["company_code"] == company["company_code"]
    # 사원이 아직 없으므로 사원 툴은 세션 오류로 끝난다(예외 없음).
    assert s.search_policy("식대", cid, "EMP001")["code"] == "INVALID_SESSION"
    assert s.ui_get_sidebar(cid, "EMP001")["code"] == "INVALID_SESSION"
    assert s.ui_login(company["company_code"], "EMP001", "1234")["code"] == "LOGIN_FAILED"


def test_company_with_only_employees_returns_not_found_responses():
    company = _register()["data"]
    cid = company["company_id"]
    documents = {
        "policies": {"policies": [], "search_policy_config": {}},
        "employees": {"employees": [{
            "employee_id": "E1", "name": "홍길동", "department": "영업팀",
            "annual_leave": {"remaining_days": 15}, "password": "1357",
        }]},
        "locations": {"locations": []},
        "handovers": {"handovers": []},
        "tickets": {"allowed_equipment": [], "tickets": []},
    }
    storage.import_company_documents(cid, documents, company_name=company["company_name"])
    assert storage.get_company(cid)["company_name"] == "테스트상사"
    assert storage.get_company(cid)["company_code"] == company["company_code"]

    assert s.ui_login(company["company_code"], "E1", "1357")["ok"]
    policy = s.search_policy("점심 식대 한도", cid, "E1")
    assert not policy["ok"] and policy["message"] == DENY
    assert s.get_office_location("대회의실", cid, "E1")["message"] == DENY
    assert s.get_handover_doc(cid, "E1")["message"] == DENY
    ticket = s.create_support_ticket("모니터", cid, "E1")
    assert not ticket["ok"]
    assert s.get_employee_info(cid, "E1")["data"]["name"] == "홍길동"
    sidebar = s.ui_get_sidebar(cid, "E1")
    assert sidebar["ok"] and sidebar["data"]["company_name"] == "테스트상사"
    assert s.ui_admin_overview(cid, "ADMIN")["data"]["has_data"] is True


def test_admin_tools_require_admin_of_that_company():
    company = _register()["data"]
    assert s.ui_admin_overview("bridgesoft", "EMP001")["code"] == "PERMISSION_DENIED"
    # 다른 회사 관리자 계정 이름으로 bridgesoft 조회 불가 (bridgesoft에는 ADMIN이 없다)
    assert s.ui_admin_overview("bridgesoft", "ADMIN")["code"] == "PERMISSION_DENIED"
    assert s.ui_admin_overview(company["company_id"], "EMP001")["code"] == "PERMISSION_DENIED"
    assert s.ui_admin_overview("", "")["code"] == "PERMISSION_DENIED"
