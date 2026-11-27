"""홈(사원증) · 관리자 홈 · 직원 화면 미리보기 테스트. LLM 없이 실행된다."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import agent as agent_module
import mcp_server as s
import storage

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
A, B = "bridgesoft", "nextwave"
ADMIN_PW = "admin-pass-1"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _doc(company_id: str, kind: str) -> dict:
    return json.loads((ROOT / "data" / company_id / f"{kind}.json").read_text("utf-8"))


def _employee(company_id: str, employee_id: str) -> dict:
    return next(e for e in _doc(company_id, "employees")["employees"] if e["employee_id"] == employee_id)


def _markdown(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def _login(account_id: str, password: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["view"] = "login"
    at.run()
    at.text_input(key="login_company_code").input(storage.get_company(A)["company_code"])
    at.text_input(key="login_account_id").input(account_id)
    at.text_input(key="login_password").input(password)
    next(b for b in at.button if b.label == "로그인").click()
    at.run()
    assert not at.exception
    return at


def test_home_payload_is_own_data_only():
    result = s.ui_get_home(A, "EMP001")
    assert result["ok"]
    data = result["data"]
    me = _employee(A, "EMP001")
    assert data["company_name"] == _doc(A, "employees")["company_name"]
    assert data["employee"]["name"] == me["name"]
    assert data["employee"]["job_title"] == me["job_title"]
    assert data["employee"]["onboarding_week"] == me["onboarding_week"]
    assert data["annual_leave"]["remaining_days"] == me["annual_leave"]["remaining_days"]
    own = [t for t in storage.get_tickets(A) if t["employee_id"] == "EMP001"]
    assert data["tickets_total"] == len(own) == sum(data["ticket_counts"].values())
    assert data["latest_ticket"]["ticket_id"] in {t["ticket_id"] for t in own}
    handover = next(h for h in _doc(A, "handovers")["handovers"] if h["department"] == me["department"])
    assert data["first_week"]["tasks"] == handover["summary"][s.FIRST_WEEK_KEY]
    assert len(data["announcements"]) <= 3
    assert all(n["is_public"] for n in data["announcements"])
    assert "password" not in json.dumps(data, ensure_ascii=False)


def test_home_requires_valid_session():
    assert s.ui_get_home(A, "EMP999")["code"] == "INVALID_SESSION"
    assert s.ui_get_home(B, "ADMIN")["code"] == "INVALID_SESSION"


def test_employee_home_shows_id_card_and_widgets():
    me = _employee(A, "EMP001")
    at = _login("EMP001", str(me["password"]))
    page = _markdown(at)
    assert "class='id-card'" in page and me["name"] in page and "EMP001" in page
    assert f"입사 {me['onboarding_week']}주차" in page
    assert "장식용 표시" in page  # 바코드는 인증 수단이 아님을 표시
    for title in ("잔여 연차", "내 비품 신청", "최근 공지", "1주 차 할 일"):
        assert f"<h3>{title}</h3>" in page
    at.button(key="home_more_tickets").click().run()
    assert at.query_params["page"] in ("equipment", ["equipment"])


@pytest.fixture
def admin_ready():
    storage.set_admin_password(A, ADMIN_PW)
    storage.set_admin_password(B, ADMIN_PW + "b")


def test_admin_home_requires_admin_and_reports_pending(admin_ready):
    assert s.ui_admin_home(A, "EMP001")["code"] == "PERMISSION_DENIED"
    data = s.ui_admin_home(A, "ADMIN")["data"]
    pending = [t for t in storage.admin_list_tickets(A) if t["status"] in ("접수완료", "검토중")]
    assert data["pending_count"] == len(pending)
    assert data["company_code"] == storage.get_company(A)["company_code"]
    assert data["employee_count"] == len(_doc(A, "employees")["employees"])


def test_admin_preview_checks_admin_company_and_audits(admin_ready):
    assert s.ui_admin_preview_employee(A, "EMP001", "EMP002")["code"] == "PERMISSION_DENIED"
    # 없는 사번·다른 회사에만 있는 사번은 이 회사 범위에서 찾지 못한다
    b_only = [e["employee_id"] for e in _doc(B, "employees")["employees"] if storage.get_employee(A, e["employee_id"]) is None]
    for employee_id in ["NOPE999", *b_only[:1]]:
        assert s.ui_admin_preview_employee(A, "ADMIN", employee_id)["code"] == "NOT_FOUND"
    result = s.ui_admin_preview_employee(A, "ADMIN", "EMP001")
    assert result["ok"] and result["data"]["preview"] is True
    assert result["data"]["employee"]["name"] == _employee(A, "EMP001")["name"]
    # B 회사 관리자가 A 회사 사원을 미리 볼 수 없다(신원은 세션의 회사로만)
    assert s.ui_admin_preview_employee(B, "ADMIN", "EMP001")["data"]["employee"]["name"] == _employee(B, "EMP001")["name"]
    rows = [r for r in storage.list_audit_log(A) if r["action"] == "preview_employee"]
    assert rows and rows[0]["actor"] == "ADMIN" and rows[0]["target"] == "EMP001"


def test_admin_preview_is_read_only_and_cannot_create_tickets(admin_ready):
    at = _login("ADMIN", ADMIN_PW)
    at.selectbox(key="preview_pick").set_value("EMP001")
    at.button(key="preview_open").click().run()
    assert not at.exception
    page = _markdown(at)
    assert "직원 화면 미리보기" in page and "읽기 전용" in page
    assert "class='id-card'" in page and _employee(A, "EMP001")["name"] in page
    keys = {b.key for b in at.button}
    assert not any((k or "").startswith("home_more_") for k in keys)
    assert "chat_fab_toggle" not in keys  # 관리자 화면에는 챗봇·신청 수단이 없다
    assert at.session_state["auth"]["role"] == "admin"  # 신원은 바뀌지 않는다
    before = len(storage.get_tickets(A))
    allowed = _doc(A, "tickets")
    item = (allowed.get("equipment_rules") or allowed.get("allowed_equipment") or [{"item": "모니터"}])[0]
    item_name = item.get("item") or item.get("name") or "모니터"
    # 관리자 세션 값으로는 신청을 확정할 수 없다(사원 세션이 아님)
    assert s.ui_confirm_ticket(A, "ADMIN", item_name, 1, "미리보기 시도")["code"] == "INVALID_SESSION"
    assert len(storage.get_tickets(A)) == before
    at.button(key="preview_close").click().run()
    assert "관리자 홈" in _markdown(at)


def test_home_tools_are_not_llm_tools():
    for name in ("ui_get_home", "ui_admin_home", "ui_admin_preview_employee"):
        assert name not in agent_module.LLM_TOOLS
