"""앱 설치(PWA) 안내와 아이콘 표시 실제 브라우저 테스트.

- 크롬·엣지: beforeinstallprompt를 받으면 "앱 설치" 버튼이 보이고, 누르면 설치 창(prompt)을 띄운다.
  appinstalled 또는 이미 앱으로 실행 중(display-mode: standalone)이면 숨긴다.
- iPhone Safari: "공유 → 홈 화면에 추가" 안내를 보이고, 닫으면 localStorage로 기억한다(저장소가 막혀도 동작).
- 사원 홈 "앱으로 설치하기" 카드 링크가 같은 설치 동작을 부른다.
- 화면 어디에도 아이콘 이름 글자(chevron_right 등)가 보이지 않는다. 아이콘 글꼴을 못 받으면 이름 글자를 감춘다.

Playwright가 없거나 설치된 streamlit에 PWA가 적용되지 않았으면 건너뛴다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from scripts import pwa_setup, ui_screenshots  # noqa: E402
from tests.browser_support import browser_env, launch_chromium, running_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CODE = "INSTAL"
PC = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

PREPARE = r"""
import sys
sys.path.insert(0, sys.argv[1])
import storage
from sqlalchemy import update
from scripts import import_company
storage.init_db()
import_company.import_company_directory(f"{sys.argv[1]}/data/bridgesoft", reset_tickets=True)
storage._run_db(lambda c: c.execute(
    update(storage.companies).where(storage.companies.c.company_id == "bridgesoft")
    .values(company_code=sys.argv[2])), write=True)
storage.set_admin_password("bridgesoft", "admin-pass-1")
"""

# 크롬의 설치 가능 판정은 헤드리스에서 일어나지 않으므로 같은 모양의 이벤트를 직접 보낸다.
FIRE_PROMPT = """
() => {
  window.__prompted = 0;
  const e = new Event('beforeinstallprompt', {cancelable: true});
  e.prompt = () => { window.__prompted += 1; return Promise.resolve(); };
  e.userChoice = Promise.resolve({outcome: 'accepted'});
  window.dispatchEvent(e);
  return e.defaultPrevented;
}
"""


def _password() -> str:
    rows = json.loads((ROOT / "data" / "bridgesoft" / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == "EMP001")


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    if pwa_setup.check(pwa_setup.streamlit_static_dir()):
        pytest.skip("설치된 streamlit에 PWA가 적용되지 않았습니다(python scripts/pwa_setup.py).")
    env = browser_env(tmp_path_factory.mktemp("install") / "install.db")
    subprocess.run([sys.executable, "-c", PREPARE, str(ROOT), CODE], env=env, cwd=ROOT, check=True, capture_output=True)
    with running_app(env) as url:
        yield url


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as pw:
        chromium = launch_chromium(pw)
        yield chromium
        chromium.close()


def _open(browser, url: str, path: str = "/", **context_args):
    context = browser.new_context(locale="ko-KR", **{"viewport": PC, **context_args})
    page = context.new_page()
    page.goto(url + path)
    page.wait_for_function("() => !!window.officegenieInstall", timeout=30000)
    return context, page


def test_install_button_prompts_and_hides_after_install(app_url, browser):
    context, page = _open(browser, app_url)
    pill = page.locator(".og-install-pill")
    assert not pill.is_visible()  # 설치 가능 신호 전에는 숨김
    assert page.evaluate(FIRE_PROMPT) is True  # 기본 미니 배너 대신 우리 버튼을 쓴다
    pill.wait_for(state="visible")
    assert pill.get_by_text("앱 설치").is_visible()
    assert page.evaluate("document.documentElement.classList.contains('og-can-install')")
    pill.locator(".og-go").click()
    page.wait_for_function("() => window.__prompted === 1")
    # 다시 설치 가능 신호가 오면 다시 보이고, 설치가 끝나면 숨긴다.
    page.evaluate(FIRE_PROMPT)
    pill.wait_for(state="visible")
    page.evaluate("window.dispatchEvent(new Event('appinstalled'))")
    pill.wait_for(state="hidden")
    context.close()


def test_install_button_hidden_when_already_standalone(app_url, browser):
    context = browser.new_context(viewport=PC)
    context.add_init_script("""
      const real = window.matchMedia.bind(window);
      window.matchMedia = (q) => q.includes('display-mode: standalone')
        ? {matches: true, media: q, addEventListener() {}, removeEventListener() {}} : real(q);
    """)
    page = context.new_page()
    page.goto(app_url)
    page.wait_for_function("() => !!window.officegenieInstall")
    page.evaluate(FIRE_PROMPT)
    time.sleep(0.5)
    assert not page.locator(".og-install-pill").is_visible()
    assert page.evaluate("document.documentElement.classList.contains('og-standalone')")
    context.close()


def test_iphone_hint_once_and_remembered(app_url, browser):
    context, page = _open(browser, app_url, user_agent=IPHONE_UA, viewport=MOBILE, has_touch=True, is_mobile=True)
    hint = page.locator(".og-ios-hint")
    hint.wait_for(state="visible")
    assert "홈 화면에 추가" in hint.inner_text()
    hint.locator(".og-x").click()
    hint.wait_for(state="hidden")
    page.reload()
    page.wait_for_function("() => !!window.officegenieInstall")
    time.sleep(0.5)
    assert not hint.is_visible()  # 닫은 것을 기억
    context.close()


def test_iphone_hint_works_when_storage_blocked(app_url, browser):
    context = browser.new_context(user_agent=IPHONE_UA, viewport=MOBILE, has_touch=True, is_mobile=True)
    context.add_init_script("""
      Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }});
      Object.defineProperty(window, 'sessionStorage', {get() { throw new Error('blocked'); }});
    """)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(app_url)
    page.wait_for_function("() => !!window.officegenieInstall")
    hint = page.locator(".og-ios-hint")
    hint.wait_for(state="visible")
    hint.locator(".og-x").click()
    hint.wait_for(state="hidden")
    assert not [e for e in errors if "blocked" in e]
    context.close()


def test_iphone_hint_does_not_cover_chat_or_tabs(app_url, browser):
    context, page = _open(browser, app_url, "/?view=login", user_agent=IPHONE_UA, viewport=MOBILE,
                          has_touch=True, is_mobile=True)
    ui_screenshots.login(page, app_url, CODE, "EMP001", _password())
    hint = page.locator(".og-ios-hint")
    hint.wait_for(state="visible")
    box = hint.bounding_box()
    for selector in (".st-key-chat_fab button", ".st-key-bottom_tabs"):
        other = page.locator(selector).first.bounding_box()
        assert box["y"] + box["height"] <= other["y"] + 1, selector
    context.close()


def test_home_install_card_and_no_icon_names(app_url, browser):
    context, page = _open(browser, app_url, "/?view=login")
    ui_screenshots.login(page, app_url, CODE, "EMP001", _password())
    card = page.locator(".st-key-home_widget_install")
    card.wait_for(state="visible")
    assert "앱으로 설치하기" in card.inner_text()
    page.evaluate(FIRE_PROMPT)
    card.locator("a.install-cta").click()
    page.wait_for_function("() => window.__prompted === 1")
    assert "page=home" in page.url  # 화면 이동 없이 설치 창만
    result = page.evaluate(ui_screenshots.AUDIT_JS)
    assert result["fontReady"] and result["problems"] == []
    context.close()


def test_icon_names_hidden_when_icon_font_fails(app_url, browser):
    context = browser.new_context(viewport=PC, locale="ko-KR")
    context.route("**/MaterialSymbols-Rounded*.woff2", lambda route: route.abort())
    page = context.new_page()
    ui_screenshots.login(page, app_url, CODE, "EMP001", _password())
    page.wait_for_function("() => document.documentElement.classList.contains('og-icon-font-missing')",
                           timeout=20000)
    icons = page.locator('[data-testid="stIconMaterial"]')
    assert icons.count() > 0
    for i in range(icons.count()):
        el = icons.nth(i)
        if not el.is_visible():
            continue
        style = el.evaluate("e => [getComputedStyle(e).color, e.getBoundingClientRect().width,"
                            " parseFloat(getComputedStyle(e).fontSize)]")
        assert style[0] in ("rgba(0, 0, 0, 0)", "transparent")  # 이름 글자를 감춤
        assert style[1] <= style[2] * 1.1  # 1글자 폭을 넘지 않아 옆 글자와 겹치지 않음
    context.close()


def test_admin_preview_has_no_install_card(app_url, browser):
    context, page = _open(browser, app_url, "/?view=login")
    ui_screenshots.login(page, app_url, CODE, "ADMIN", "admin-pass-1")
    page.locator(".st-key-preview_open button").click()
    ui_screenshots.settle(page)
    assert page.locator(".st-key-home_widget_install").count() == 0
    assert page.evaluate(ui_screenshots.AUDIT_JS)["problems"] == []
    context.close()
