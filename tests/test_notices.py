"""공지사항 메뉴(사원) · 공지사항 관리(관리자) 테스트. LLM 없이 실행된다."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from streamlit.testing.v1 import AppTest

import mcp_server as s
import storage

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
A, B = "bridgesoft", "nextwave"
ADMIN_PW = "admin-pass-1"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _password(employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / A / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


def _markdown(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def _app(account_id: str, password: str, page: str) -> AppTest:
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


def _notice(title: str, body: str, public: bool = True, days_ago: int = 0) -> str:
    assert storage.admin_save_announcement(A, "ADMIN", None, title, body, public)
    nid = next(n["announcement_id"] for n in storage.list_announcements(A) if n["title"] == title)
    if days_ago:
        when = storage._iso(storage._utc_now() - timedelta(days=days_ago))
        storage._run_db(
            lambda c: c.execute(
                update(storage.announcements)
                .where(storage.announcements.c.announcement_id == nid)
                .values(published_at=when, created_at=when)
            ),
            write=True,
        )
    return nid


def test_list_notices_is_public_only_and_needs_session():
    _notice("공개 공지", "본문")
    _notice("비공개 공지", "본문", public=False)
    data = s.ui_list_notices(A, "EMP001")["data"]["announcements"]
    titles = [n["title"] for n in data]
    assert "공개 공지" in titles and "비공개 공지" not in titles
    assert s.ui_list_notices(A, "EMP999")["code"] == "INVALID_SESSION"
    assert all(n["title"] != "공개 공지" for n in s.ui_list_notices(B, "EMP001")["data"]["announcements"])


def test_employee_notice_page_new_badge_search_and_detail():
    _notice("이번 주 보안 교육", "1. 교육 일정\n2. 장소")
    _notice("지난달 회식 안내", "회식 장소 안내", days_ago=20)
    at = _app("EMP001", _password("EMP001"), "notices")
    page = _markdown(at)
    assert "이번 주 보안 교육<em class='new-badge'>NEW</em>" in page
    assert "지난달 회식 안내</span>" in page  # 7일이 지난 공지는 NEW 없음
    assert "1. 교육 일정&#10;2. 장소" in page  # 상세(펼치기) 본문
    at.text_input(key="notice_query").input("회식").run()
    page = _markdown(at)
    assert "지난달 회식 안내" in page and "이번 주 보안 교육" not in page
    at.text_input(key="notice_query").input("없는단어").run()
    assert "검색 결과가 없습니다" in _markdown(at)


def test_employee_notice_page_empty_state():
    at = _app("EMP001", _password("EMP001"), "notices")
    assert "등록된 공지사항이 없습니다" in _markdown(at)


def test_admin_creates_edits_and_toggles_inline():
    storage.set_admin_password(A, ADMIN_PW)
    at = _app("ADMIN", ADMIN_PW, "notices")
    next(b for b in at.button if b.label == "공지 작성").click().run()
    at.text_input(key="manager_notice_title").input("새 공지")
    at.text_area(key="manager_notice_body").input("본문 한 줄")
    next(b for b in at.button if b.label == "저장").click().run()
    assert not at.exception
    notice = storage.list_announcements(A)[0]
    assert notice["title"] == "새 공지" and notice["is_public"]
    nid = notice["announcement_id"]

    # 목록에서 바로 수정: 같은 화면 안에 폼이 열린다
    at.button(key=f"manager_notices_row_{nid}").click().run()
    assert at.text_input(key="manager_notice_title").value == "새 공지"
    assert at.button(key=f"notice_toggle_{nid}")  # 목록이 그대로 보인다
    at.text_input(key="manager_notice_title").input("수정한 공지")
    next(b for b in at.button if b.label == "저장").click().run()
    assert storage.list_announcements(A)[0]["title"] == "수정한 공지"

    at.button(key=f"notice_toggle_{nid}").click().run()
    assert not storage.list_announcements(A)[0]["is_public"]
    assert all(n["title"] != "수정한 공지" for n in s.ui_list_notices(A, "EMP001")["data"]["announcements"])
    assert any(r["target"] == nid for r in storage.list_audit_log(A))  # 변경은 감사 로그에 남는다


def test_admin_notice_tools_require_admin():
    assert s.ui_admin_save_announcement(A, "EMP001", "제목", "본문", True)["code"] == "PERMISSION_DENIED"
