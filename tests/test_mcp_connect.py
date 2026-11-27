"""사원 'AI 앱 연결' 카드와 관리자 토큰 현황 테스트. LLM 없이 실행된다."""
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
PUBLIC = "https://officegenie-mcp.example.test/mcp"
pytestmark = pytest.mark.usefixtures("fresh_db")

TOKEN_TOOLS = ("ui_list_my_mcp_tokens", "ui_issue_mcp_token", "ui_revoke_my_mcp_token",
               "ui_admin_list_mcp_tokens", "ui_admin_revoke_mcp_token")


def _password(company_id: str, employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / company_id / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


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


def test_token_tools_are_not_llm_tools():
    for name in TOKEN_TOOLS:
        assert name not in agent_module.LLM_TOOLS


def test_issue_returns_raw_once_and_list_never_does():
    issued = s.ui_issue_mcp_token(A, "EMP001", "회사 노트북")
    assert issued["ok"] and issued["code"] == "TOKEN_ISSUED"
    token = issued["data"]["token"]
    assert token.startswith("og_") and issued["data"]["mcp_url"] == PUBLIC
    listed = s.ui_list_my_mcp_tokens(A, "EMP001")
    assert listed["ok"] and token not in json.dumps(listed, ensure_ascii=False)
    assert [t["label"] for t in listed["data"]["tokens"]] == ["회사 노트북"]
    assert storage.verify_mcp_token(token)["employee_id"] == "EMP001"


@pytest.mark.pg
def test_limit_three_via_ui_tool():
    for i in range(3):
        assert s.ui_issue_mcp_token(A, "EMP001", f"t{i}")["ok"]
    over = s.ui_issue_mcp_token(A, "EMP001", "넘침")
    assert over["code"] == "TOKEN_LIMIT" and "token" not in (over["data"] or {})


@pytest.mark.pg
def test_employee_cannot_revoke_other_employee_token():
    other = s.ui_issue_mcp_token(A, "EMP002", "남의 것")["data"]
    token_id = other["issued"]["token_id"]
    assert s.ui_revoke_my_mcp_token(A, "EMP001", token_id)["code"] == "NOT_FOUND"
    assert storage.verify_mcp_token(other["token"]) is not None
    assert s.ui_revoke_my_mcp_token(A, "EMP002", token_id)["code"] == "TOKEN_REVOKED"
    assert storage.verify_mcp_token(other["token"]) is None


def test_admin_account_and_unknown_employee_cannot_issue():
    storage.set_admin_password(A, ADMIN_PW)
    for employee_id in ("ADMIN", "NOPE999"):
        result = s.ui_issue_mcp_token(A, employee_id, "x")
        assert not result["ok"]
    assert storage.list_mcp_tokens(A) == []


def test_admin_list_and_force_revoke_are_admin_only_and_audited():
    storage.set_admin_password(A, ADMIN_PW)
    storage.set_admin_password(B, ADMIN_PW + "b")
    issued = s.ui_issue_mcp_token(A, "EMP001", "노트북")["data"]
    token_id = issued["issued"]["token_id"]
    assert s.ui_admin_list_mcp_tokens(A, "EMP001")["code"] == "PERMISSION_DENIED"
    assert s.ui_admin_revoke_mcp_token(A, "EMP001", token_id)["code"] == "PERMISSION_DENIED"
    rows = s.ui_admin_list_mcp_tokens(A, "ADMIN")["data"]["tokens"]
    assert rows[0]["employee_id"] == "EMP001" and rows[0]["name"]
    assert issued["token"] not in json.dumps(rows, ensure_ascii=False)
    assert s.ui_admin_revoke_mcp_token(B, "ADMIN", token_id)["code"] == "NOT_FOUND"  # 다른 회사 관리자
    assert s.ui_admin_revoke_mcp_token(A, "ADMIN", token_id)["code"] == "TOKEN_REVOKED"
    assert storage.verify_mcp_token(issued["token"]) is None
    actions = [(r["actor"], r["action"]) for r in storage.list_audit_log(A, 20)]
    assert ("EMP001", "mcp_token_issue") in actions and ("ADMIN", "mcp_token_revoke") in actions


def test_mcp_url_empty_when_not_configured(monkeypatch):
    monkeypatch.delenv("OFFICEGENIE_MCP_PUBLIC_URL")
    assert s.ui_list_my_mcp_tokens(A, "EMP001")["data"]["mcp_url"] == ""
    monkeypatch.setenv("OFFICEGENIE_MCP_PUBLIC_URL", "https://mcp.example.com/")
    assert s.mcp_public_url() == "https://mcp.example.com/mcp"


def test_employee_home_card_issues_shows_once_and_revokes():
    at = _login("EMP001", _password(A, "EMP001"))
    page = "\n".join(str(m.value) for m in at.markdown)
    assert "<h3>AI 앱 연결</h3>" in page
    at.text_input(key="mcp_token_label").input("회사 노트북 Claude Code")
    at.button(key="mcp_issue_button").click().run()
    assert not at.exception
    tokens = storage.list_mcp_tokens(A, "EMP001")
    assert [t["label"] for t in tokens] == ["회사 노트북 Claude Code"]
    codes = [c.value for c in at.code]
    raw = next(c for c in codes if c.startswith("og_"))
    assert storage.verify_mcp_token(raw)
    assert any(PUBLIC in c and "claude mcp add" in c and raw in c for c in codes)
    assert any(PUBLIC in c and '"mcpServers"' in c for c in codes)
    at.button(key="mcp_new_token_done").click().run()
    assert not any(raw in c.value for c in at.code)  # 다시 볼 수 없다
    at.button(key=f"mcp_revoke_{tokens[0]['token_id']}").click().run()
    assert not at.exception
    assert storage.verify_mcp_token(raw) is None
    assert storage.list_mcp_tokens(A, "EMP001") == []


def test_admin_preview_has_no_token_controls():
    storage.set_admin_password(A, ADMIN_PW)
    s.ui_issue_mcp_token(A, "EMP001", "기존")
    at = _login("ADMIN", ADMIN_PW)
    at.selectbox(key="preview_pick").set_value("EMP001")
    at.button(key="preview_open").click().run()
    assert not at.exception
    page = "\n".join(str(m.value) for m in at.markdown)
    assert "<h3>AI 앱 연결</h3>" in page and "미리보기에서는 토큰을 발급하거나 폐기할 수 없습니다" in page
    keys = {b.key or "" for b in at.button}
    assert not any(k.startswith(("mcp_issue", "mcp_revoke")) for k in keys)
    assert len(storage.list_mcp_tokens(A, "EMP001")) == 1
