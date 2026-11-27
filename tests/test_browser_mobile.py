"""모바일 폭(390x844) 실제 브라우저 테스트: 로그인 → 하단 탭 이동 → 비품 신청·확정 → 내역,
새로고침 후 로그인 유지, 챗봇 버튼과 탭바 겹침, PWA(manifest·서비스 워커·오프라인 화면).

Playwright가 없으면 건너뛴다. 설치된 streamlit에 PWA가 적용되지 않았으면 PWA 테스트만 건너뛴다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from tests.browser_support import browser_env, launch_chromium, running_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CODE = "MOBILE"
MOBILE = {"width": 390, "height": 844}

PREPARE = r"""
import sys
sys.path.insert(0, sys.argv[1])
import storage
from sqlalchemy import update
from scripts import import_company
storage.init_db()
for name in ("bridgesoft", "nextwave"):
    import_company.import_company_directory(f"{sys.argv[1]}/data/{name}", reset_tickets=True)
storage._run_db(lambda c: c.execute(
    update(storage.companies)
    .where(storage.companies.c.company_id == "bridgesoft")
    .values(company_code=sys.argv[2])), write=True)
"""


def _password(employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / "bridgesoft" / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    env = browser_env(tmp_path_factory.mktemp("mobile") / "mobile.db")
    subprocess.run([sys.executable, "-c", PREPARE, str(ROOT), CODE], env=env, cwd=ROOT, check=True, capture_output=True)
    with running_app(env) as url:
        yield url


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as pw:
        chromium = launch_chromium(pw)
        yield chromium
        chromium.close()


def _login(context, url: str, account_id: str = "EMP001"):
    page = context.new_page()
    page.goto(f"{url}/?view=login")
    page.get_by_role("textbox", name="회사 코드").fill(CODE)
    page.get_by_role("textbox", name="사번").fill(account_id)
    page.get_by_label("비밀번호", exact=True).fill(_password(account_id))
    page.get_by_label("비밀번호", exact=True).press("Enter")
    page.get_by_role("button", name="로그아웃").wait_for(timeout=30_000)
    return page


def _tab(page, label: str):
    return page.locator(".st-key-bottom_tabs").get_by_role("button", name=re.compile(label))


def _overlap(a: dict, b: dict) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def test_mobile_tabs_equipment_request_and_reload(app_url, browser):
    context = browser.new_context(viewport=MOBILE, device_scale_factor=1, has_touch=True)
    page = _login(context, app_url)
    try:
        assert page.locator(".st-key-top_nav").is_hidden()  # 모바일은 상단 메뉴 대신 하단 탭바
        tabbar = page.locator(".st-key-bottom_tabs").bounding_box()
        assert tabbar["y"] + tabbar["height"] == pytest.approx(MOBILE["height"], abs=1)
        for label in ("홈", "공지", "자료실", "비품"):
            box = _tab(page, label).bounding_box()
            assert box["height"] >= 44 and box["width"] >= 44  # 터치 영역
        assert not _overlap(page.locator(".st-key-chat_fab").bounding_box(), tabbar)

        for label, page_name, heading in (("공지", "notices", "공지사항"), ("자료실", "library", "자료실"), ("비품", "equipment", "비품 신청")):
            _tab(page, label).click()
            page.wait_for_url(f"**page={page_name}**", timeout=15_000)
            page.get_by_role("heading", name=heading, exact=True).wait_for(timeout=15_000)

        page.get_by_role("radio", name="노트북 거치대", exact=True).click()
        page.get_by_text("남은 신청 가능 수량 1개").wait_for(timeout=15_000)
        page.get_by_role("textbox", name="3. 사용 목적 (선택)").fill("모바일 신청 테스트")
        page.get_by_role("button", name="신청 내용 확인").click()
        page.get_by_text("신청 내용을 확인해 주세요.").wait_for(timeout=15_000)
        page.get_by_role("button", name="접수 확정").click()
        page.get_by_text(re.compile(r"신청이 접수되었습니다. 티켓 번호: REQ-\d+")).wait_for(timeout=15_000)
        ticket = re.search(r"REQ-\d+", page.get_by_text(re.compile("티켓 번호")).first.inner_text()).group(0)
        page.get_by_text(f"{ticket} · 비품").wait_for(timeout=15_000)  # 내 신청 내역

        page.reload()  # 새로고침해도 로그인과 메뉴가 유지된다
        page.get_by_role("button", name="로그아웃").wait_for(timeout=30_000)
        page.get_by_role("heading", name="비품 신청", exact=True).wait_for(timeout=15_000)
        assert "page=equipment" in page.url

        page.locator(".st-key-chat_fab button").click()  # 챗봇은 모바일에서 화면 전체 시트
        panel = page.locator(".st-key-chat_panel")
        panel.wait_for(timeout=15_000)
        box = panel.bounding_box()
        assert box["width"] == pytest.approx(MOBILE["width"], abs=2)
        assert box["height"] == pytest.approx(MOBILE["height"], abs=2)
    finally:
        context.close()


def test_mobile_employee_cannot_open_admin_page_by_url(app_url, browser):
    context = browser.new_context(viewport=MOBILE)
    page = _login(context, app_url)
    try:
        page.goto(f"{app_url}/?page=data")
        page.get_by_text("관리자만 사용할 수 있는 메뉴입니다").wait_for(timeout=30_000)
        page.get_by_role("heading", name="나의 온보딩", exact=True).wait_for()
        assert "page=home" in page.url
    finally:
        context.close()


def _pwa_applied() -> bool:
    from scripts import pwa_setup

    return not pwa_setup.check(pwa_setup.streamlit_static_dir())


@pytest.mark.skipif(not _pwa_applied(), reason="python scripts/pwa_setup.py를 먼저 실행해야 합니다.")
def test_pwa_manifest_service_worker_and_offline_page(app_url, browser):
    context = browser.new_context(viewport=MOBILE)
    page = context.new_page()
    try:
        page.goto(f"{app_url}/?source=pwa")
        href = page.locator("link[rel=manifest]").get_attribute("href")
        response = page.request.get(f"{app_url}/{href.lstrip('./')}")
        assert response.status == 200 and "manifest+json" in response.headers["content-type"]
        assert response.json()["start_url"] == "/?source=pwa"
        scope = page.evaluate("navigator.serviceWorker.ready.then(r => r.scope)")
        assert scope == f"{app_url}/"
        page.wait_for_timeout(500)
        cached = page.evaluate(
            "caches.keys().then(async ks => (await Promise.all(ks.map(async k =>"
            " (await (await caches.open(k)).keys()).map(r => new URL(r.url).pathname)))).flat())"
        )
        assert sorted(cached) == sorted([
            "/pwa/offline.html", "/pwa/icon-192.png", "/pwa/icon-512.png",
            "/pwa/icon-maskable-512.png", "/pwa/apple-touch-icon.png",
        ])
        page.reload()  # 서비스 워커가 페이지를 제어한 뒤 오프라인 전환
        context.set_offline(True)
        page.reload()
        page.get_by_role("heading", name="인터넷에 연결되어 있지 않습니다").wait_for(timeout=15_000)
        after = page.evaluate(
            "caches.keys().then(async ks => (await Promise.all(ks.map(async k =>"
            " (await (await caches.open(k)).keys()).length))).reduce((a, b) => a + b, 0))"
        )
        assert after == 5  # 페이지·API 응답은 저장하지 않는다
        context.set_offline(False)  # 오프라인 화면은 연결되면 스스로 새로고침한다
    finally:
        context.close()
