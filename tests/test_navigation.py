"""메뉴형 화면: URL(?page=) 동기화, 관리자 메뉴 차단·감사 로그, 새로고침 후 로그인 유지(세션 토큰).

LLM 없이 실행된다. 화면은 AppTest, 서버 세션은 mcp_server 툴을 직접 호출해 확인한다.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from streamlit.testing.v1 import AppTest

import agent as agent_module
import mcp_server as s
import storage

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
A, B = "bridgesoft", "nextwave"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _password(company_id: str, employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / company_id / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


def _code(company_id: str = A) -> str:
    return storage.get_company(company_id)["company_code"]


def _markdown(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def _login_app(account_id: str = "EMP001", password: str | None = None, page: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    if page:
        at.query_params["page"] = page
    at.query_params["view"] = "login"
    at.run()
    at.text_input(key="login_company_code").input(_code())
    at.text_input(key="login_account_id").input(account_id)
    at.text_input(key="login_password").input(password or _password(A, account_id))
    next(b for b in at.button if b.label == "로그인").click()
    at.run()
    assert not at.exception
    assert at.session_state["auth"]
    return at


def _page(at: AppTest) -> str:
    value = at.query_params.get("page")
    return value[0] if isinstance(value, list) else value


# ---------------------------------------------------------------------------
# 메뉴 이동과 URL 동기화
# ---------------------------------------------------------------------------


def test_employee_menu_moves_and_syncs_url():
    at = _login_app()
    assert _page(at) == "home"
    assert [b.key for b in at.button if (b.key or "").startswith("nav_")] == [
        "nav_home", "nav_notices", "nav_library", "nav_equipment",
    ]
    for page, heading in (("notices", "공지사항"), ("library", "자료실"), ("equipment", "비품 신청")):
        at.button(key=f"nav_{page}").click().run()
        assert not at.exception
        assert _page(at) == page
        assert f"<h1>{heading}</h1>" in _markdown(at)
        assert at.button(key=f"nav_{page}").proto.type == "primary"
    # 모바일 하단 탭도 같은 이동
    at.button(key="tab_home").click().run()
    assert _page(at) == "home"
    assert "나의 온보딩" in _markdown(at)


def test_page_query_is_remembered_through_login():
    """바로가기(?page=notices)로 들어와 로그인하면 그 메뉴로 간다."""
    at = _login_app(page="notices")
    assert _page(at) == "notices"
    assert "<h1>공지사항</h1>" in _markdown(at)


def test_unknown_page_falls_back_to_home():
    at = _login_app()
    at.query_params["page"] = "../../etc"
    at.run()
    assert _page(at) == "home"
    assert not at.warning
    assert not [r for r in storage.list_audit_log(A) if r["action"] == "page_denied"]


@pytest.mark.pg
@pytest.mark.parametrize("admin_page", ["employees", "data"])
def test_employee_opening_admin_page_goes_home_and_is_audited(admin_page):
    at = _login_app()
    at.query_params["page"] = admin_page
    at.run()
    assert not at.exception
    assert _page(at) == "home"
    assert "나의 온보딩" in _markdown(at)
    assert any("관리자만 사용할 수 있는 메뉴" in w.value for w in at.warning)
    assert at.session_state["auth"]["role"] == "employee"  # 신원·권한은 그대로
    rows = [r for r in storage.list_audit_log(A) if r["action"] == "page_denied"]
    assert rows and rows[0]["actor"] == "EMP001" and rows[0]["target"] == admin_page


def test_record_page_denied_requires_valid_session_and_sanitizes():
    assert s.ui_record_page_denied(A, "EMP999", "data")["code"] == "INVALID_SESSION"
    result = s.ui_record_page_denied(A, "EMP001", "data<script>")
    assert result["ok"] and result["code"] == "PAGE_DENIED"
    assert storage.list_audit_log(A)[0]["target"] == "datascript"


def test_admin_sees_admin_menus():
    storage.set_admin_password(A, "admin-pass-1")
    at = _login_app("ADMIN", "admin-pass-1", page="employees")
    assert _page(at) == "employees"
    assert [b.key for b in at.button if (b.key or "").startswith("nav_")] == [
        "nav_home", "nav_notices", "nav_library", "nav_equipment", "nav_employees", "nav_data",
    ]
    assert not any(b.key == "chat_fab_toggle" for b in at.button)  # 챗봇은 사원 화면에만


def test_employee_pages_have_chat_button_everywhere():
    at = _login_app()
    for page in ("home", "notices", "library", "equipment"):
        at.button(key=f"nav_{page}").click().run()
        assert at.button(key="chat_fab_toggle")


def test_explicit_logout_resets_page():
    at = _login_app(page="library")
    next(b for b in at.button if b.label == "로그아웃").click().run()
    assert "auth" not in at.session_state
    assert "page" not in at.query_params


# ---------------------------------------------------------------------------
# 새로고침 후 로그인 유지: 서버 세션 토큰
# ---------------------------------------------------------------------------


def _session_rows() -> list[dict]:
    return storage._run_db(
        lambda conn: [dict(r) for r in conn.execute(select(storage.login_sessions)).mappings()]
    )


def _login_token(account_id: str = "EMP001", password: str | None = None) -> str:
    result = s.ui_login(_code(), account_id, password or _password(A, account_id))
    assert result["ok"]
    return result["data"]["session_token"]


@pytest.mark.pg
def test_login_token_is_stored_only_as_hash_and_resumes_same_identity():
    token = _login_token()
    rows = _session_rows()
    assert len(rows) == 1
    assert token not in json.dumps(rows, default=str)
    assert rows[0]["token_hash"] == storage._token_hash(token)
    resumed = s.ui_resume_session(token)
    assert resumed["ok"]
    assert resumed["data"]["employee_id"] == "EMP001" and resumed["data"]["company_id"] == A
    assert resumed["data"]["role"] == "employee"
    assert "session_token" not in resumed["data"]
    audit = json.dumps(storage.list_audit_log(A), default=str)
    assert token not in audit


def test_resume_rejects_unknown_or_malformed_token():
    for token in ("", "x" * 500, "not-a-real-token-value-123456", None):
        assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"


def test_logout_revokes_token():
    token = _login_token()
    assert s.ui_logout(token)["ok"]
    assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"
    assert _session_rows() == []


def test_admin_role_comes_from_server_not_token_owner_claims():
    storage.set_admin_password(A, "admin-pass-1")
    token = _login_token("ADMIN", "admin-pass-1")
    data = s.ui_resume_session(token)["data"]
    assert data["role"] == "admin" and data["account_id"] == "ADMIN"
    employee_token = _login_token()
    assert s.ui_resume_session(employee_token)["data"]["role"] == "employee"


def test_idle_and_absolute_expiry():
    token = _login_token()
    old = storage._iso(storage._utc_now() - timedelta(seconds=storage.SESSION_IDLE_SECONDS + 5))
    storage._run_db(lambda c: c.execute(update(storage.login_sessions).values(last_seen_at=old)), write=True)
    assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"
    assert _session_rows() == []  # 만료된 토큰은 지운다

    token = _login_token()
    past = storage._iso(storage._utc_now() - timedelta(seconds=1))
    storage._run_db(lambda c: c.execute(update(storage.login_sessions).values(expires_at=past)), write=True)
    assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"


def test_password_change_revokes_old_sessions_and_issues_new_one():
    first = _login_token()
    second = _login_token()
    old_password = _password(A, "EMP001")
    changed = s.ui_change_password(A, "EMP001", old_password, old_password + "9")
    assert changed["ok"]
    assert s.ui_resume_session(first)["code"] == "INVALID_SESSION"
    assert s.ui_resume_session(second)["code"] == "INVALID_SESSION"
    assert s.ui_resume_session(changed["data"]["session_token"])["ok"]


def test_admin_password_reset_and_disabled_account_end_sessions():
    token = _login_token()
    storage.admin_reset_password(A, "EMP001", "ADMIN")
    assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"

    token = _login_token("EMP003")
    storage._run_db(
        lambda c: c.execute(
            update(storage.accounts)
            .where(storage._account_key(A, "EMP003"))
            .values(disabled=True)
        ),
        write=True,
    )
    assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"


def test_app_login_keeps_token_in_session_and_logout_revokes():
    at = _login_app()
    token = at.session_state["session_token"]
    assert s.ui_resume_session(token)["ok"]
    assert "session_token" not in at.session_state["auth"]  # 신원 정보와 분리
    next(b for b in at.button if b.label == "로그아웃").click().run()
    assert s.ui_resume_session(token)["code"] == "INVALID_SESSION"


def test_session_tools_are_not_llm_tools():
    for name in ("ui_resume_session", "ui_logout", "ui_record_page_denied"):
        assert name not in agent_module.LLM_TOOLS
