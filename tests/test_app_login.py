"""Streamlit 화면 흐름 테스트 (AppTest). LLM 없이 실행된다.

AppTest는 실제 app.py를 실행하고, app.py는 MCP 서버를 하위 프로세스로 띄워
테스트 DB(OFFICEGENIE_TEST_DATABASE_URL)에 연결한다.
"""
from __future__ import annotations

import json
from datetime import date
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


def _goto(at: AppTest, page: str) -> AppTest:
    """메뉴 버튼(상단 메뉴)으로 이동한다."""
    at.button(key=f"nav_{page}").click()
    at.run()
    assert not at.exception
    assert at.query_params["page"] == [page] or at.query_params["page"] == page
    return at


def _new_app(entry: str = "로그인") -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    assert not at.text_input
    _button(at, entry).click()
    return at.run()


@pytest.mark.parametrize("direct", [False, True])
def test_login_back_returns_to_landing_without_submitting(direct, monkeypatch):
    from agent import OfficeGenieAgent

    login_calls = []
    monkeypatch.setattr(OfficeGenieAgent, "login", lambda *args: login_calls.append(args))
    if direct:
        at = AppTest.from_file(APP, default_timeout=60)
        at.query_params["view"] = "login"
        at.run()
    else:
        at = _new_app()

    at.text_input(key="login_company_code").input("TCNCZD")
    at.text_input(key="login_account_id").input("EMP001")
    at.text_input(key="login_password").input("test-value")
    at.button(key="login_back").click().run()
    assert not at.exception
    assert at.button(key="landing_login")
    assert not at.text_input
    assert not login_calls
    assert "view" not in at.query_params


def test_login_shows_own_dashboard_then_logout_clears_session():
    at = _new_app()
    assert not at.exception
    assert "나의 온보딩" not in _all_markdown(at)
    assert at.text_input(key="login_company_code")
    assert all(button.label != "회사 등록하기" for button in at.button)

    code = storage.get_company("bridgesoft")["company_code"]
    at = _login(at, code.lower(), "EMP001", _password("bridgesoft", "EMP001"))
    assert not at.exception
    page = _all_markdown(at)
    assert "나의 온보딩" in page and D.employee("bridgesoft", "EMP001")["name"] in page
    assert "백엔드개발팀" in page
    assert at.session_state["auth"]["employee_id"] == "EMP001"
    assert not any(b.key == "nav_employees" for b in at.button)
    at.query_params["page"] = "employees"  # 사원이 관리자 메뉴 주소를 직접 입력
    at.run()
    assert "나의 온보딩" in _all_markdown(at)
    assert not any(b.key == "nav_employees" for b in at.button)

    _button(at, "로그아웃").click()
    at.run()
    assert "auth" not in at.session_state
    assert "chat_session" not in at.session_state
    assert "나의 온보딩" not in _all_markdown(at)


def test_wrong_login_preserves_identity_fields_and_allows_retry():
    at = _new_app()
    code = storage.get_company("bridgesoft")["company_code"]
    submit_id = at.button(key="login_submit").id
    at = _login(at, code, "EMP001", "")
    assert [w.value for w in at.warning] == ["회사 코드, 사번, 비밀번호를 모두 입력해 주세요."]
    assert storage.get_account("bridgesoft", "EMP001")["failed_count"] == 0

    at = _login(at, code, "EMP001", "wrong")
    assert [e.value for e in at.error] == ["회사 코드, 아이디 또는 비밀번호가 올바르지 않습니다."]
    assert "나의 온보딩" not in _all_markdown(at)
    assert at.text_input(key="login_company_code").value == code
    assert at.text_input(key="login_account_id").value == "EMP001"
    assert at.text_input(key="login_password").value == ""
    assert at.button(key="login_submit").id == submit_id
    assert not at.button(key="login_submit").proto.disabled

    at.text_input(key="login_password").input(_password("bridgesoft", "EMP001"))
    _button(at, "로그인").click()
    at.run()
    assert not at.exception and not at.error
    assert "나의 온보딩" in _all_markdown(at)
    assert "등록된 공지사항이 없습니다." in _all_markdown(at)
    assert at.session_state["chat_panel_open"] is False
    _button(at, "챗봇 열기").click()
    at.run()
    assert not at.exception
    assert "오피스 지니 챗봇" in _all_markdown(at)
    assert at.session_state["chat_panel_open"] is True


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


def test_registration_back_returns_to_login_without_submitting(monkeypatch):
    from agent import OfficeGenieAgent

    registrations = []
    monkeypatch.setattr(OfficeGenieAgent, "register_company", lambda *args: registrations.append(args))
    at = _new_app("회사 등록")
    assert ">회사 등록</h1>" in _all_markdown(at)
    at.text_input(key="register_company_name").input("입력 중인 회사")
    at.text_input(key="register_password").input("admin-pass-1")
    at.text_input(key="register_password_again").input("admin-pass-1")
    at.button(key="register_back").click().run()
    assert not at.exception
    assert at.text_input(key="login_company_code")
    assert not registrations
    at.button(key="login_back").click().run()
    assert at.button(key="landing_login")


def test_register_company_then_login_as_admin(monkeypatch):
    monkeypatch.delenv("OFFICEGENIE_SIGNUP_CODE", raising=False)
    at = _new_app("회사 등록")
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
    assert "에이티상사" in page
    auth = dict(at.session_state["auth"])
    _goto(at, "employees")
    assert at.session_state["auth"] == auth
    _goto(at, "data")
    assert code in _all_markdown(at)
    assert any("아직 회사 데이터가 없습니다" in i.value for i in at.info)
    assert "나의 온보딩" not in _all_markdown(at)


def _admin_app(company_code: str, password: str) -> AppTest:
    at = _new_app()
    at = _login(at, company_code, "ADMIN", password)
    assert not at.exception
    assert at.session_state["auth"]["role"] == "admin"
    assert at.button(key="nav_data")
    return at


def test_admin_uploads_policy_preview_then_apply(monkeypatch):
    """회사 등록 → 코드 표시 → ADMIN 로그인 → 규정 업로드 → 미리보기 → 반영.

    AppTest는 file_uploader를 조작할 수 없어서, 업로더가 세션에 담는 것과 같은
    형태({name, content})로 파일을 넣고 같은 '미리보기'·'반영하기' 버튼을 누른다.
    """
    monkeypatch.delenv("OFFICEGENIE_SIGNUP_CODE", raising=False)
    at = _new_app("회사 등록")
    at.text_input(key="register_company_name").input("업로드상사")
    at.text_input(key="register_password").input("admin-pass-1")
    at.text_input(key="register_password_again").input("admin-pass-1")
    _button(at, "회사 등록").click()
    at.run()
    company = at.session_state["registered_company"]
    assert company["company_code"] in _all_markdown(at)

    at = _admin_app(company["company_code"], "admin-pass-1")
    _goto(at, "data")
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
    _goto(at, "equipment")
    at.session_state["equipment_tabs"] = "신청 상태·이력"
    at.run()
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


def _demo_admin() -> tuple[AppTest, str]:
    storage.set_admin_password("bridgesoft", "admin-pass-1")
    code = storage.get_company("bridgesoft")["company_code"]
    return _admin_app(code, "admin-pass-1"), code


# 예전 관리자 탭 이름 → (메뉴, 하위 탭 key, 하위 탭 이름)
_MANAGER_TABS = {
    "사원 관리": ("employees", "employees_tabs", "사원 목록"),
    "비품 관리": ("equipment", "equipment_tabs", "비품 지급"),
    "공지사항 관리": ("notices", None, None),
    "데이터 관리": ("data", "data_tabs", "사내 규정"),
}


def _manager_tab(at: AppTest, label: str) -> None:
    page, tabs_key, tab = _MANAGER_TABS[label]
    _goto(at, page)
    if tabs_key:
        at.session_state[tabs_key] = tab
        at.run()
    assert not at.exception


def test_manager_header_navigation_preserves_admin_session():
    at, code = _demo_admin()
    auth = at.session_state["auth"].copy()
    for page in ("notices", "library", "equipment", "employees", "data", "home"):
        _goto(at, page)
        assert at.button(key=f"nav_{page}").proto.type == "primary"
        assert at.button(key=f"tab_{page}")  # 모바일 하단 탭에도 같은 메뉴
        assert _button(at, "로그아웃")
    _goto(at, "data")
    assert code in _all_markdown(at)
    assert at.session_state["auth"] == auth
    assert not at.exception


@pytest.mark.reimport
def test_manager_adds_and_edits_employee_preserving_company_scope():
    at, _ = _demo_admin()
    _manager_tab(at, "사원 관리")
    original = storage.admin_list_employees("bridgesoft")
    other = storage.get_employee("nextwave", "EMP001")
    _button(at, "사원 추가").click().run()
    department = original[0]["department"]
    at.text_input(key="manager_employee_id").input("emp001")
    at.text_input(key="manager_employee_name").input("관리 화면 테스트")
    at.selectbox(key="manager_employee_dept").select(department)
    at.text_input(key="manager_employee_position").input("사원")
    _button(at, "저장").click().run()
    assert any("이미 이 회사에 등록된 사번" in item.value for item in at.error)
    assert at.text_input(key="manager_employee_name").value == "관리 화면 테스트"
    at.text_input(key="manager_employee_id").input("EMP155")
    _button(at, "저장").click().run()
    assert len(storage.admin_list_employees("bridgesoft")) == len(original) + 1
    assert any("사원이 등록되었습니다" in item.value for item in at.success)
    at.text_input(key="manager_employees_filters_query").input("EMP155")
    _button(at, "검색").click().run()
    at.button(key="manager_employees_row_EMP155").click().run()
    assert at.text_input(key="manager_employee_id").proto.disabled
    at.text_input(key="manager_employee_name").input("저장하지 않을 이름")
    _button(at, "취소").click().run()
    assert storage.get_employee("bridgesoft", "EMP155")["name"] == "관리 화면 테스트"
    at.button(key="manager_employees_row_EMP155").click().run()
    at.text_input(key="manager_employee_name").input("수정된 사원")
    at.number_input(key="manager_employee_leave").set_value(4.5)
    at.selectbox(key="manager_employee_status").select("휴직")
    _button(at, "저장").click().run()
    assert "수정된 사원" in _all_markdown(at) and "4.5일" in _all_markdown(at)
    assert ">휴직</span>" in _all_markdown(at)
    assert storage.get_employee("nextwave", "EMP001") == other
    assert storage.get_employee("nextwave", "EMP155") is None


def test_manager_equipment_date_and_announcement_visibility_in_employee_dashboard():
    at, code = _demo_admin()
    _manager_tab(at, "비품 관리")
    target = next(
        ticket for ticket in storage.admin_list_tickets("bridgesoft")
        if ticket["employee_id"] == "EMP001" and ticket["type"] == "비품"
        and ticket["status"] == "접수완료"
    )
    at.button(key=f"manager_equipment_row_{target['ticket_id']}").click().run()
    at.selectbox(key="manager_equipment_status").select("지급 예정")
    at.date_input(key="manager_equipment_date").set_value(date(2026, 12, 7))
    _button(at, "저장").click().run()
    assert any("비품 신청 정보가 저장되었습니다" in item.value for item in at.success)

    _manager_tab(at, "공지사항 관리")
    _button(at, "공지 작성").click().run()
    at.text_input(key="manager_notice_title").input("사원 공개 공지")
    at.text_area(key="manager_notice_body").input("1. 첫 줄\n\n2. 둘째 줄")
    _button(at, "저장").click().run()
    notice = storage.list_announcements("bridgesoft")[0]
    _button(at, "로그아웃").click().run()
    _login(at, code, "EMP001", _password("bridgesoft", "EMP001"))
    page = _all_markdown(at)
    assert "사원 공개 공지" in page and "NEW" in page  # 홈 최근 공지 위젯
    _goto(at, "notices")
    assert "사원 공개 공지" in _all_markdown(at) and "&#10;&#10;2. 둘째 줄" in _all_markdown(at)
    _goto(at, "equipment")
    page = _all_markdown(at)
    assert "지급 예정" in page and "지급 예정일 2026-12-07" in page

    _button(at, "로그아웃").click().run()
    _login(at, code, "ADMIN", "admin-pass-1")
    assert "사원 공개 공지" in _all_markdown(at)
    _manager_tab(at, "공지사항 관리")
    at.button(key=f"manager_notices_row_{notice['announcement_id']}").click().run()
    at.selectbox(key="manager_notice_public").select(False)
    _button(at, "저장").click().run()
    assert "비공개" in _all_markdown(at)
    _button(at, "로그아웃").click().run()
    _login(at, code, "EMP001", _password("bridgesoft", "EMP001"))
    assert "사원 공개 공지" not in _all_markdown(at)
    assert "등록된 공지사항이 없습니다" in _all_markdown(at)
    _goto(at, "notices")
    assert "사원 공개 공지" not in _all_markdown(at)


def test_manager_policy_search_detail_and_back_preserves_filters():
    at, _ = _demo_admin()
    _manager_tab(at, "데이터 관리")
    row = next(p for p in storage.get_company_document("bridgesoft", "policies")["policies"] if p["id"] == "POL-002")
    at.text_input(key="manager_policies_filters_query").input(row["title"])
    at.selectbox(key="manager_policies_filters_choice").select(row["category"])
    _button(at, "검색").click().run()
    at.button(key=f"manager_policies_row_{row['id']}").click().run()
    assert "조항 원문" in _all_markdown(at)
    assert "&#10;②" in _all_markdown(at)
    _button(at, "규정 목록").click().run()
    assert at.text_input(key="manager_policies_filters_query").value == row["title"]
    assert at.selectbox(key="manager_policies_filters_choice").value == row["category"]
    _button(at, "초기화").click().run()
    assert at.text_input(key="manager_policies_filters_query").value == ""
    assert at.selectbox(key="manager_policies_filters_choice").value is None


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
