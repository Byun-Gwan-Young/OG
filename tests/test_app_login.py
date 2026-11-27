"""Streamlit 화면 흐름 테스트 (AppTest). LLM 없이 실행된다.

AppTest는 실제 app.py를 실행하고, app.py는 MCP 서버를 하위 프로세스로 띄워
테스트 DB(OFFICEGENIE_TEST_DATABASE_URL)에 연결한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import storage
from tests import datafiles as D

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
pytestmark = pytest.mark.usefixtures("fresh_db")


def _password(company_id: str, employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / company_id / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


def _all_markdown(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _login(at: AppTest, code: str, account_id: str, password: str) -> AppTest:
    at.text_input(key="login_company_code").input(code)
    at.text_input(key="login_account_id").input(account_id)
    at.text_input(key="login_password").input(password)
    _button(at, "로그인").click()
    return at.run()


def _new_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    return at.run()


def test_login_shows_own_dashboard_then_logout_clears_session():
    at = _new_app()
    assert not at.exception
    assert "나의 온보딩" not in _all_markdown(at)
    assert at.text_input(key="login_company_code")

    code = storage.get_company("bridgesoft")["company_code"]
    at = _login(at, code.lower(), "EMP001", _password("bridgesoft", "EMP001"))
    assert not at.exception
    page = _all_markdown(at)
    assert "나의 온보딩" in page and D.employee("bridgesoft", "EMP001")["name"] in page
    assert "김신입(백엔드개발팀)" in page
    assert at.session_state["auth"]["employee_id"] == "EMP001"

    _button(at, "로그아웃").click()
    at.run()
    assert "auth" not in at.session_state
    assert "chat_session" not in at.session_state
    assert "나의 온보딩" not in _all_markdown(at)


def test_wrong_login_shows_generic_error():
    at = _new_app()
    code = storage.get_company("bridgesoft")["company_code"]
    at = _login(at, code, "EMP001", "wrong")
    assert [e.value for e in at.error] == ["회사 코드, 아이디 또는 비밀번호가 올바르지 않습니다."]
    assert "나의 온보딩" not in _all_markdown(at)


def test_must_change_password_comes_first():
    documents_password = _password("bridgesoft", "EMP001")
    storage._run_db(
        lambda conn: conn.execute(
            storage.accounts.update()
            .where(storage.accounts.c.company_id == "bridgesoft")
            .where(storage.accounts.c.account_id == "EMP001")
            .values(must_change=True)
        ),
        write=True,
    )
    at = _new_app()
    code = storage.get_company("bridgesoft")["company_code"]
    at = _login(at, code, "EMP001", documents_password)
    assert "나의 온보딩" not in _all_markdown(at)
    at.text_input(key="pw_old").input(documents_password)
    at.text_input(key="pw_new").input("2468")
    at.text_input(key="pw_again").input("2468")
    _button(at, "비밀번호 변경").click()
    at.run()
    assert not at.exception
    assert "나의 온보딩" in _all_markdown(at)


def test_idle_session_logs_out():
    at = _new_app()
    code = storage.get_company("bridgesoft")["company_code"]
    at = _login(at, code, "EMP001", _password("bridgesoft", "EMP001"))
    assert "나의 온보딩" in _all_markdown(at)
    at.session_state["last_activity"] = at.session_state["last_activity"] - 31 * 60
    at.run()
    assert "auth" not in at.session_state
    assert "나의 온보딩" not in _all_markdown(at)
    assert any("30분" in i.value for i in at.info)


def test_register_company_then_login_as_admin(monkeypatch):
    monkeypatch.delenv("OFFICEGENIE_SIGNUP_CODE", raising=False)
    at = _new_app()
    _button(at, "회사 등록하기").click()
    at.run()
    at.text_input(key="register_company_name").input("에이티상사")
    at.text_input(key="register_password").input("admin-pass-1")
    at.text_input(key="register_password_again").input("admin-pass-1")
    _button(at, "회사 등록").click()
    at.run()
    assert not at.exception
    code = at.session_state["registered_company"]["company_code"]
    assert storage.is_valid_company_code(code)
    assert code in _all_markdown(at)
    assert any("사원들에게 이 코드를 알려주세요" in s.value for s in at.success)

    _button(at, "관리자로 로그인하기").click()
    at.run()
    assert at.text_input(key="login_company_code").value == code
    assert at.text_input(key="login_account_id").value == "ADMIN"
    at.text_input(key="login_password").input("admin-pass-1")
    _button(at, "로그인").click()
    at.run()
    assert not at.exception
    assert at.session_state["auth"]["role"] == "admin"
    page = _all_markdown(at)
    assert "관리자 · 에이티상사" in page and code in page
    assert any("아직 회사 데이터가 없습니다" in i.value for i in at.info)
    assert "나의 온보딩" not in page


def _admin_app(company_code: str, password: str) -> AppTest:
    at = _new_app()
    at = _login(at, company_code, "ADMIN", password)
    assert not at.exception
    assert at.session_state["auth"]["role"] == "admin"
    return at


def test_admin_uploads_policy_preview_then_apply(monkeypatch):
    """회사 등록 → 코드 표시 → ADMIN 로그인 → 규정 업로드 → 미리보기 → 반영.

    AppTest는 file_uploader를 조작할 수 없어서, 업로더가 세션에 담는 것과 같은
    형태({name, content})로 파일을 넣고 같은 '미리보기'·'반영하기' 버튼을 누른다.
    """
    monkeypatch.delenv("OFFICEGENIE_SIGNUP_CODE", raising=False)
    at = _new_app()
    _button(at, "회사 등록하기").click()
    at.run()
    at.text_input(key="register_company_name").input("업로드상사")
    at.text_input(key="register_password").input("admin-pass-1")
    at.text_input(key="register_password_again").input("admin-pass-1")
    _button(at, "회사 등록").click()
    at.run()
    company = at.session_state["registered_company"]
    assert company["company_code"] in _all_markdown(at)

    at = _admin_app(company["company_code"], "admin-pass-1")
    content = (ROOT / "data" / "bridgesoft" / "policies.json").read_text("utf-8")
    at.session_state["admin_upload_files"] = [{"name": "policies.json", "content": content}]
    at.run()
    _button(at, "미리보기").click()
    at.run()
    assert not at.exception
    assert at.session_state["admin_preview"]["data"]["changes"][0]["added"] > 0
    assert storage.get_company(company["company_id"])["data_version"] == 1  # 아직 미반영
    _button(at, "반영하기").click()
    at.run()
    assert not at.exception
    assert any("반영했습니다" in m.value for m in at.success)
    assert storage.get_company(company["company_id"])["data_version"] == 2
    assert storage.company_overview(company["company_id"])["counts"]["policies"] > 0


def test_admin_approves_ticket_and_employee_sees_scheduled():
    from scripts import set_admin_password

    set_admin_password.main(["bridgesoft", "--password", "admin-pass-1"])
    code = storage.get_company("bridgesoft")["company_code"]
    # 기준 티켓 중 EMP001의 접수완료 비품 신청 하나를 승인까지 진행한다
    target = next(
        t for t in D.baseline_tickets_doc("bridgesoft")["tickets"]
        if t["employee_id"] == "EMP001" and t["status"] == "접수완료" and t["type"] == "비품"
    )
    at = _admin_app(code, "admin-pass-1")
    for step in ("검토중", "승인"):
        at.selectbox(key="admin_ticket_pick").set_value(target["ticket_id"])
        at.run()
        at.selectbox(key="admin_ticket_next").set_value(step)
        _button(at, "상태 변경").click()
        at.run()
        assert not at.exception
        assert any("신청 상태를 변경했습니다" in m.value for m in at.success)
    assert next(t for t in storage.get_tickets("bridgesoft") if t["ticket_id"] == target["ticket_id"])["status"] == "승인"
    _button(at, "로그아웃").click()
    at.run()

    employee = _login(at, code, "EMP001", _password("bridgesoft", "EMP001"))
    page = _all_markdown(employee)
    assert target["item"] in page and "지급 예정" in page


@pytest.mark.reimport
@pytest.mark.parametrize("typed_code", ["TCNCZD", "tcnczd", " TCNCZD "])
def test_employee_login_with_leading_zero_password_from_screen(typed_code):
    """화면에서만 사원 로그인이 실패하던 문제 재현: 0으로 시작하는 숫자 비밀번호."""
    from sqlalchemy import update

    storage._run_db(
        lambda conn: conn.execute(
            update(storage.companies)
            .where(storage.companies.c.company_id == "bridgesoft")
            .values(company_code="TCNCZD")
        ),
        write=True,
    )
    storage.invalidate_company_cache()
    assert storage.reset_employee_passwords(
        "bridgesoft", [{"employee_id": "EMP001", "password": "0899"}]
    ) == 1

    at = _new_app()
    at = _login(at, typed_code, "EMP001", "0899")
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    assert at.session_state["auth"]["employee_id"] == "EMP001"
    assert "나의 온보딩" in _all_markdown(at)
    account = storage.get_account("bridgesoft", "EMP001")
    assert account["failed_count"] == 0
