"""비품 신청 메뉴와 get_my_tickets(LLM 툴) 테스트. LLM 없이 실행된다."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import agent as agent_module
import mcp_server as s
import storage
from agent import OfficeGenieAgent
from tests import datafiles as D
from tests.test_agent import ScriptedLLM

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
A, B = "bridgesoft", "nextwave"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _password(company_id: str, employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / company_id / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


# ---------------------------------------------------------------------------
# get_my_tickets (LLM 툴)
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_get_my_tickets_returns_only_own_tickets_newest_first():
    result = s.get_my_tickets(A, "EMP001")
    assert result["ok"]
    rows = result["data"]["tickets"]
    own = [t for t in D.baseline_tickets_doc(A)["tickets"] if t["employee_id"] == "EMP001"]
    assert {r["ticket_id"] for r in rows} == {t["ticket_id"] for t in own}
    assert result["data"]["count"] == len(own)
    created = [r["created_at"] for r in rows]
    assert created == sorted(created, reverse=True)
    for row in rows:
        assert set(row) == {
            "ticket_id", "type", "item", "quantity", "status", "created_at", "updated_at", "scheduled_date", "note",
        }
    others = {t["ticket_id"] for t in D.baseline_tickets_doc(A)["tickets"] if t["employee_id"] != "EMP001"}
    assert not others & {r["ticket_id"] for r in rows}


def test_get_my_tickets_reflects_admin_status_change_and_has_no_invented_date():
    target = next(t for t in s.get_my_tickets(A, "EMP001")["data"]["tickets"] if t["status"] == "접수완료")
    assert target["scheduled_date"] is None  # 관리자가 정하지 않은 지급 예정일은 비어 있다
    storage.admin_update_ticket(A, target["ticket_id"], "검토중", "ADMIN", "")
    row = next(t for t in s.get_my_tickets(A, "EMP001")["data"]["tickets"] if t["ticket_id"] == target["ticket_id"])
    assert row["status"] == "검토중"
    assert row["updated_at"] >= row["created_at"]
    storage.admin_update_ticket(A, target["ticket_id"], "반려", "ADMIN", "재고 없음")
    row = next(t for t in s.get_my_tickets(A, "EMP001")["data"]["tickets"] if t["ticket_id"] == target["ticket_id"])
    assert row["status"] == "반려" and row["note"] == "재고 없음"


def test_get_my_tickets_requires_session_and_empty_message():
    assert s.get_my_tickets(A, "EMP999")["code"] == "INVALID_SESSION"
    employees = json.loads((ROOT / "data" / A / "employees.json").read_text("utf-8"))["employees"]
    with_tickets = {t["employee_id"] for t in storage.get_tickets(A)}
    nobody = next(e["employee_id"] for e in employees if e["employee_id"] not in with_tickets)
    empty = s.get_my_tickets(A, nobody)
    assert empty["ok"] and empty["data"]["tickets"] == [] and empty["message"]


def test_get_my_tickets_is_llm_tool_with_hidden_identity():
    assert "get_my_tickets" in agent_module.LLM_TOOLS
    assert "지어내지" in agent_module._system_prompt("회사", {"name": "김신입"})


def test_llm_cannot_read_other_employees_tickets(fresh_db):
    llm = ScriptedLLM({"신청": [("get_my_tickets", {"employee_id": "EMP002", "company_id": B})]})
    agent = OfficeGenieAgent(llm=llm)
    try:
        agent.new_session(A, "EMP001").send("내 신청 어떻게 됐어?")
        result = llm.last_tool_results[0]
        own = {t["ticket_id"] for t in storage.get_tickets(A, "EMP001")}
        assert {t["ticket_id"] for t in result["data"]["tickets"]} == own
        schema = next(d for d in llm.seen_decls if d["name"] == "get_my_tickets")["parameters_json_schema"]
        assert not schema.get("properties")
    finally:
        agent.close()


# ---------------------------------------------------------------------------
# 비품 신청 폼 (서버)
# ---------------------------------------------------------------------------


def test_equipment_options_show_remaining_quantity():
    items = {o["item"]: o for o in s.ui_equipment_options(A, "EMP001")["data"]["items"]}
    allowed = {e["item"]: e for e in json.loads((ROOT / "data" / A / "tickets.json").read_text("utf-8"))["allowed_equipment"]}
    assert set(items) == set(allowed)
    for name, option in items.items():
        rule = allowed[name]
        if rule.get("quantity_limit_defined"):
            assert option["remaining"] == rule["max_quantity_per_employee"] - option["held_or_pending"]
        else:
            assert option["remaining"] is None and option["limit"] is None
    assert s.ui_equipment_options(A, "EMP999")["code"] == "INVALID_SESSION"


def test_preview_does_not_create_and_rejects_like_server_rule():
    before = len(storage.get_tickets(A))
    preview = s.ui_preview_ticket(A, "EMP001", "노트북 거치대", 1, "")
    assert preview["code"] == "NEEDS_CONFIRMATION"
    assert len(storage.get_tickets(A)) == before
    assert s.ui_preview_ticket(A, "EMP001", "안마의자", 1, "")["code"] == "REJECTED"
    assert s.ui_preview_ticket(A, "EMP001", "노트북 거치대", 9, "")["code"] == "REJECTED"
    assert len(storage.get_tickets(A)) == before


def test_tools_split_between_llm_and_ui():
    for name in ("ui_equipment_options", "ui_preview_ticket", "ui_my_tickets"):
        assert name not in agent_module.LLM_TOOLS


# ---------------------------------------------------------------------------
# 화면: 품목 선택 → 수량·목적 → 확인 카드 → 접수 확정 → 내역
# ---------------------------------------------------------------------------


def _markdown(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def _equipment_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["view"] = "login"
    at.query_params["page"] = "equipment"
    at.run()
    at.text_input(key="login_company_code").input(storage.get_company(A)["company_code"])
    at.text_input(key="login_account_id").input("EMP001")
    at.text_input(key="login_password").input(_password(A, "EMP001"))
    next(b for b in at.button if b.label == "로그인").click()
    at.run()
    assert not at.exception
    return at


@pytest.mark.pg
def test_employee_requests_equipment_from_menu_and_sees_history():
    expected = f"REQ-{D.baseline_next_sequence(A):03d}"  # PRD 정상 2: REQ-004
    at = _equipment_app()
    assert "<h3>새 비품 신청</h3>" in _markdown(at)
    assert "<h3>내 신청 내역</h3>" in _markdown(at)
    at.button_group(key="eq_item").set_value("노트북 거치대").run()
    assert any("남은 신청 가능 수량 1개" in i.value for i in at.info)
    at.text_input(key="eq_purpose").input("개발 업무 자세 교정")
    at.button(key="eq_check").click().run()
    assert not at.exception
    page = _markdown(at)
    assert "신청 내용을 확인해 주세요" in page and "개발 업무 자세 교정" in page
    assert len([t for t in storage.get_tickets(A) if t["item"] == "노트북 거치대" and t["employee_id"] == "EMP001"]) == 0

    at.button(key="eq_confirm").click().run()
    assert not at.exception
    created = [t for t in storage.get_tickets(A, "EMP001") if t["item"] == "노트북 거치대"]
    assert len(created) == 1 and created[0]["ticket_id"] == expected
    assert any(expected in m.value for m in at.success)
    page = _markdown(at)
    assert f"{expected} · 비품" in page  # 내 신청 내역에 바로 표시
    assert "class='ticket-timeline'" in page
    # 한도(1개)에 도달하면 더 신청할 수 없다고 안내한다
    at.button_group(key="eq_item").set_value("노트북 거치대").run()
    assert any("더 신청할 수 없습니다" in w.value for w in at.warning)
    assert not any(b.key == "eq_check" for b in at.button)


def test_equipment_edit_returns_to_form_without_creating():
    at = _equipment_app()
    at.button_group(key="eq_item").set_value("헤드셋").run()
    assert any("한도가 정해져 있지 않습니다" in i.value for i in at.info)
    before = len(storage.get_tickets(A))
    at.button(key="eq_check").click().run()
    at.button(key="eq_edit").click().run()
    assert not any(b.key == "eq_confirm" for b in at.button)
    assert len(storage.get_tickets(A)) == before
