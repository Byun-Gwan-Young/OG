"""자료실 테스트: 부서 전용 자료 권한, 형식·용량 제한, 관리자 전용 변경, 회사 간 격리, 감사 로그.

LLM 없이 실행된다. 서버 툴(mcp_server)을 직접 호출하고, 화면은 AppTest로 확인한다.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect, text
from streamlit.testing.v1 import AppTest

import agent as agent_module
import mcp_server as s
import storage

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
A, B = "bridgesoft", "nextwave"
ADMIN_PW = "admin-pass-1"
DENIED = s.MSG["DENIED_OR_NOT_FOUND"]
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture(autouse=True)
def _admins(fresh_db):
    storage.set_admin_password(A, ADMIN_PW)
    storage.set_admin_password(B, ADMIN_PW + "b")


def _employees(company_id: str) -> list[dict]:
    return json.loads((ROOT / "data" / company_id / "employees.json").read_text("utf-8"))["employees"]


def _dept(company_id: str, employee_id: str) -> str:
    return next(e["department"] for e in _employees(company_id) if e["employee_id"] == employee_id)


def _other_dept_employee(company_id: str, employee_id: str) -> str:
    mine = _dept(company_id, employee_id)
    return next(e["employee_id"] for e in _employees(company_id) if e["department"] != mine)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _save(company_id=A, actor="ADMIN", **kwargs) -> dict:
    args = dict(title="자료", category="양식", kind="file", file_name="guide.pdf", content_base64=_b64(PDF))
    args.update(kwargs)
    return s.ui_admin_save_resource(company_id, actor, **args)


def _ids(company_id: str, employee_id: str) -> set[str]:
    return {r["resource_id"] for r in s.ui_list_resources(company_id, employee_id)["data"]["resources"]}


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------


def test_init_db_adds_resources_table_to_existing_db():
    storage._run_db(lambda c: c.execute(text("DROP TABLE resources")), write=True)
    assert "resources" not in sa_inspect(storage.get_engine()).get_table_names(schema=storage._engine_schema)
    storage.init_db()  # 기존 회사 데이터는 그대로 두고 빠진 테이블만 만든다
    assert "resources" in sa_inspect(storage.get_engine()).get_table_names(schema=storage._engine_schema)
    assert storage.get_company(A) is not None
    assert _save()["ok"]


# ---------------------------------------------------------------------------
# 부서 전용 자료
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_department_resource_hidden_and_download_denied_for_other_department():
    dept = _dept(A, "EMP001")
    other = _other_dept_employee(A, "EMP001")
    rid = _save(title="팀 전용 가이드", visibility="department", department=dept)["data"]["resource"]["resource_id"]
    common = _save(title="전체 공개 양식")["data"]["resource"]["resource_id"]

    assert {rid, common} <= _ids(A, "EMP001")
    assert rid not in _ids(A, other) and common in _ids(A, other)

    mine = s.ui_download_resource(A, "EMP001", rid)
    assert mine["ok"] and base64.b64decode(mine["data"]["content_base64"]) == PDF
    denied = s.ui_download_resource(A, other, rid)
    assert not denied["ok"] and denied["message"] == DENIED
    assert "content_base64" not in json.dumps(denied)


def test_private_resource_is_not_listed_or_downloadable():
    rid = _save(title="비공개 초안", is_public=False)["data"]["resource"]["resource_id"]
    assert rid not in _ids(A, "EMP001")
    assert s.ui_download_resource(A, "EMP001", rid)["message"] == DENIED
    admin_view = s.ui_admin_list_resources(A, "ADMIN")["data"]["resources"]
    assert any(r["resource_id"] == rid and not r["is_public"] for r in admin_view)


def test_list_never_contains_file_bytes():
    _save()
    for payload in (s.ui_list_resources(A, "EMP001"), s.ui_admin_list_resources(A, "ADMIN")):
        dumped = json.dumps(payload, ensure_ascii=False, default=str)
        assert "content" not in {k for r in payload["data"]["resources"] for k in r}
        assert _b64(PDF) not in dumped


def test_department_must_exist_in_company():
    result = _save(visibility="department", department="없는부서")
    assert result["code"] == "RESOURCE_INVALID" and "부서" in result["message"]


# ---------------------------------------------------------------------------
# 형식 · 용량 · 파일명
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "file_name,raw,code",
    [
        ("virus.exe", b"MZ\x90\x00", "RESOURCE_TYPE"),
        ("script.html", b"<script>", "RESOURCE_TYPE"),
        ("noext", b"%PDF-1.4", "RESOURCE_TYPE"),
        ("fake.pdf", b"MZ\x90\x00not a pdf", "RESOURCE_MISMATCH"),
        ("fake.png", PDF, "RESOURCE_MISMATCH"),
        ("empty.pdf", b"", "RESOURCE_FILE_REQUIRED"),
        ("binary.txt", b"\xff\xfe\x00\xd8\x00\x00\xff", "RESOURCE_MISMATCH"),
    ],
)
def test_rejects_disallowed_or_mismatched_files(file_name, raw, code):
    result = _save(file_name=file_name, content_base64=_b64(raw))
    assert not result["ok"] and result["code"] == code
    assert result["message"]  # 이유를 보여 준다
    assert storage.list_resources(A) == []


def test_rejects_files_over_5mb_with_reason():
    too_big = b"%PDF" + b"0" * (s.RESOURCE_MAX_BYTES - 3)
    result = _save(content_base64=_b64(too_big))
    assert result["code"] == "RESOURCE_TOO_LARGE" and "5MB" in result["message"]
    exactly = b"%PDF" + b"0" * (s.RESOURCE_MAX_BYTES - 4)
    assert _save(content_base64=_b64(exactly))["ok"]


def test_accepts_allowed_types_and_sanitizes_names():
    saved = _save(file_name="../../etc/보고서 <최종>.PDF")["data"]["resource"]
    assert saved["file_name"] == "보고서 _최종_.pdf"
    assert saved["mime_type"] == "application/pdf"  # 클라이언트가 아닌 확장자로 정한다
    assert _save(file_name="logo.png", content_base64=_b64(PNG))["ok"]
    korean = "안내문입니다".encode("cp949")
    stored = _save(file_name="안내.txt", content_base64=_b64(korean))["data"]["resource"]
    raw = base64.b64decode(s.ui_download_resource(A, "EMP001", stored["resource_id"])["data"]["content_base64"])
    assert raw.decode("utf-8") == "안내문입니다"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://files.example.com", "https://a b", "", "data:text/html,x"])
def test_link_requires_http_url(url):
    assert _save(kind="link", url=url, content_base64="")["code"] == "RESOURCE_URL"


def test_link_resource_opens_url():
    rid = _save(kind="link", url="https://example.com/guide", content_base64="")["data"]["resource"]["resource_id"]
    data = s.ui_download_resource(A, "EMP001", rid)["data"]
    assert data == {"kind": "link", "url": "https://example.com/guide", "title": "자료"}


# ---------------------------------------------------------------------------
# 관리자 전용 · 수정 · 삭제 · 감사 로그
# ---------------------------------------------------------------------------


def test_only_admin_can_list_save_and_delete():
    rid = _save()["data"]["resource"]["resource_id"]
    assert _save(actor="EMP001")["code"] == "PERMISSION_DENIED"
    assert s.ui_admin_list_resources(A, "EMP001")["code"] == "PERMISSION_DENIED"
    assert s.ui_admin_delete_resource(A, "EMP001", rid)["code"] == "PERMISSION_DENIED"
    assert s.ui_admin_download_resource(A, "EMP001", rid)["code"] == "PERMISSION_DENIED"
    assert len(storage.list_resources(A)) == 1


def test_edit_keeps_file_switch_to_link_and_delete_are_audited():
    rid = _save(title="처음 제목")["data"]["resource"]["resource_id"]
    edited = _save(title="바꾼 제목", content_base64="", file_name="", resource_id=rid)
    assert edited["ok"]
    assert storage.get_resource(A, rid, with_content=True)["content"] == PDF  # 새 파일이 없으면 유지
    linked = _save(kind="link", url="https://example.com", content_base64="", resource_id=rid)["data"]["resource"]
    assert linked["kind"] == "link" and linked["file_name"] is None
    assert storage.get_resource(A, rid, with_content=True)["content"] is None
    assert s.ui_admin_delete_resource(A, "ADMIN", rid)["ok"]
    assert storage.get_resource(A, rid) is None
    actions = [r["action"] for r in storage.list_audit_log(A) if r["target"] == rid]
    assert actions == ["resource_deleted", "resource_updated", "resource_updated", "resource_created"]
    assert _b64(PDF) not in json.dumps(storage.list_audit_log(A), ensure_ascii=False, default=str)


def test_new_file_resource_requires_file():
    assert _save(content_base64="")["code"] == "RESOURCE_FILE_REQUIRED"


@pytest.mark.pg
def test_company_isolation():
    rid = _save(title="A사 자료")["data"]["resource"]["resource_id"]
    assert rid not in _ids(B, "EMP001")
    assert s.ui_download_resource(B, "EMP001", rid)["message"] == DENIED
    assert s.ui_admin_download_resource(B, "ADMIN", rid)["code"] == "NOT_FOUND"
    assert s.ui_admin_delete_resource(B, "ADMIN", rid)["code"] == "NOT_FOUND"
    assert _save(company_id=B, title="수정 시도", resource_id=rid)["code"] == "NOT_FOUND"
    assert storage.get_resource(A, rid)["title"] == "A사 자료"


def test_policy_shelf_lists_company_rules_first_with_reference_flag():
    groups = s.ui_list_resources(A, "EMP001")["data"]["policies"]
    flags = [g["is_reference_only"] for g in groups]
    assert flags == sorted(flags)  # 회사 내규(False)가 먼저
    assert any(flags) and not all(flags)


def test_handovers_are_own_department_only():
    handovers = s.ui_list_resources(A, "EMP001")["data"]["handovers"]
    titles = {h["title"] for h in handovers}
    docs = json.loads((ROOT / "data" / A / "handovers.json").read_text("utf-8"))["handovers"]
    assert titles == {h["title"] for h in docs if h["department"] == _dept(A, "EMP001")}


def test_resource_tools_are_not_llm_tools():
    for name in (
        "ui_list_resources", "ui_download_resource", "ui_admin_list_resources",
        "ui_admin_download_resource", "ui_admin_save_resource", "ui_admin_delete_resource",
    ):
        assert name not in agent_module.LLM_TOOLS


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------


def _password(employee_id: str) -> str:
    return next(str(e["password"]) for e in _employees(A) if e["employee_id"] == employee_id)


def _app(account_id: str, password: str, page: str = "library") -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["view"] = "login"
    at.query_params["page"] = page
    at.run()
    at.text_input(key="login_company_code").input(storage.get_company(A)["company_code"])
    at.text_input(key="login_account_id").input(account_id)
    at.text_input(key="login_password").input(password)
    next(b for b in at.button if b.label == "로그인").click()
    at.run()
    assert not at.exception
    return at


def _markdown(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_library_screen_shows_department_files_only_to_that_department():
    dept = _dept(A, "EMP001")
    _save(title="백엔드 전용 체크리스트", visibility="department", department=dept)
    _save(title="휴가 신청서 양식", file_name="leave.docx", content_base64=_b64(b"PK\x03\x04" + b"0" * 20))
    at = _app("EMP001", _password("EMP001"))
    page = _markdown(at)
    assert "백엔드 전용 체크리스트" in page and "휴가 신청서 양식" in page
    at.session_state["library_tabs"] = "부서 자료"
    at.run()
    page = _markdown(at)
    assert "백엔드 전용 체크리스트" in page and "휴가 신청서 양식" not in page
    assert "인수인계서" in page
    at.session_state["library_tabs"] = "규정집"
    at.run()
    assert "참고용" in _markdown(at)

    other = _other_dept_employee(A, "EMP001")
    at = _app(other, _password(other))
    assert "백엔드 전용 체크리스트" not in _markdown(at)
    assert "휴가 신청서 양식" in _markdown(at)


def test_library_empty_state():
    at = _app("EMP001", _password("EMP001"))
    at.session_state["library_tabs"] = "양식·기타"
    at.run()
    assert "아직 등록된 자료가 없습니다" in _markdown(at)


def test_admin_registers_link_and_sees_errors_from_screen():
    at = _app("ADMIN", ADMIN_PW)
    at.button(key="resource_new").click().run()
    at.text_input(key="res_title").input("사내 위키")
    at.selectbox(key="res_category").select("가이드")
    at.radio(key="res_kind").set_value("링크").run()
    at.text_input(key="res_url").input("javascript:alert(1)")
    at.button(key="resource_save").click().run()
    assert any("http" in e.value for e in at.error)  # 이유를 보여 주고 거부
    assert storage.list_resources(A) == []
    at.text_input(key="res_url").input("https://wiki.example.com")
    at.button(key="resource_save").click().run()
    assert not at.exception
    saved = storage.list_resources(A)
    assert [r["title"] for r in saved] == ["사내 위키"]
    assert "사내 위키" in _markdown(at)
    rid = saved[0]["resource_id"]
    at.button(key=f"admin_res_delete_{rid}").click().run()
    at.button(key="resource_delete_yes").click().run()
    assert storage.list_resources(A) == []
