"""화면 점검용 스크린샷과 아이콘 깨짐 검사 (개발용, Playwright 필요).

    pip install playwright && python -m playwright install chromium
    python scripts/ui_screenshots.py http://127.0.0.1:8501 --out docs/images/ui-review/after \\
        --code ABC123 --employee EMP001 --password 1234 --admin-password 'admin-pass'

- PC(1440×900)와 모바일(390×844)에서 로그인 전 화면, 사원 화면(홈·공지·자료실·비품·챗봇·AI 앱 연결),
  관리자 화면(홈·공지·자료실·비품·사원·데이터·직원 화면 미리보기), 오류 화면을 찍는다.
- 화면마다 "아이콘 이름 글자"를 찾는다: Material 아이콘 자리에 글리프 대신 chevron_right 같은 글자가
  그려졌거나(폭이 글자 크기보다 훨씬 넓음), 아이콘 요소가 아닌 곳에 아이콘 이름이 보이면 문제로 기록한다.
- 결과 요약은 <out>/report.json. 문제가 하나라도 있으면 종료 코드 1.
- 기능은 건드리지 않는다(로그인·이동·열기만). 비밀번호는 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ICON_NAME = re.compile(r"^(?:[a-z]+(?:_[a-z0-9]+)+|close|check|menu|search|download|badge|campaign|"
                       r"inventory|groups|settings|logout|home|add|remove|delete|edit|info|warning|error)$")

AUDIT_JS = """
() => {
  const NAME = new RegExp(%s);
  const problems = [];
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  // 1) Material 아이콘 요소: 글리프가 그려지면 폭이 대략 글자 크기 정도다.
  document.querySelectorAll('[data-testid="stIconMaterial"], .material-symbols-rounded').forEach((el) => {
    if (!visible(el)) return;
    const cs = getComputedStyle(el);
    const size = parseFloat(cs.fontSize) || 16;
    const fam = cs.fontFamily || '';
    if (!/Material Symbols/.test(fam) || el.scrollWidth > size * 1.6) {
      problems.push({kind: 'icon-as-text', text: el.textContent.trim(), font: fam.slice(0, 50),
                     width: Math.round(el.scrollWidth), size});
    }
  });
  // 2) 아이콘 요소가 아닌 곳에 아이콘 이름 글자가 그대로 보이는 경우
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const t = (node.textContent || '').trim();
    if (!t || !NAME.test(t)) continue;
    const el = node.parentElement;
    if (!el || !visible(el)) continue;
    if (el.closest('[data-testid="stIconMaterial"], .material-symbols-rounded, code, pre, script, style, textarea')) continue;
    problems.push({kind: 'icon-name-text', text: t, tag: el.tagName});
  }
  // 3) 가로 넘침(페이지가 화면보다 넓음)
  const overflow = document.documentElement.scrollWidth - window.innerWidth;
  if (overflow > 2) problems.push({kind: 'horizontal-overflow', px: overflow});
  return {fontReady: document.fonts.check('24px "Material Symbols Rounded"'), problems};
}
""" % json.dumps(ICON_NAME.pattern)

VIEWPORTS = {"pc": (1440, 900), "mobile": (390, 844)}


def settle(page, seconds: float = 1.2) -> None:
    page.wait_for_load_state("networkidle")
    try:
        page.wait_for_selector('[data-testid="stAppViewContainer"]', timeout=20000)
        page.wait_for_function("() => !document.querySelector('[data-testid=\"stStatusWidget\"] [data-testid=\"stSpinner\"]')",
                               timeout=20000)
    except Exception:
        pass
    page.evaluate("document.fonts.ready")
    time.sleep(seconds)


def goto(page, base: str, query: str) -> None:
    page.goto(base + query)
    settle(page)


def click_key(page, key: str) -> bool:
    loc = page.locator(f".st-key-{key} button").first
    if loc.count() == 0:
        return False
    loc.click()
    settle(page)
    return True


def login(page, base: str, code: str, account: str, password: str) -> None:
    goto(page, base, "/?view=login")
    page.get_by_placeholder("회사 코드를 입력해 주세요.").fill(code)
    page.get_by_placeholder("사번을 입력해 주세요.").fill(account)
    page.get_by_placeholder("비밀번호를 입력해 주세요.").fill(password)
    page.locator(".st-key-login_submit button, [data-testid='stFormSubmitButton'] button").first.click()
    page.wait_for_url(re.compile(r"page="), timeout=30000)
    settle(page, 2)


def logout(page) -> None:
    page.context.clear_cookies()


CONTENT_HEIGHT_JS = """
() => {
  const main = document.querySelector('[data-testid="stMain"]') || document.scrollingElement;
  return Math.max(main.scrollHeight, document.documentElement.scrollHeight);
}
"""


def capture(page, out: Path, name: str, report: dict, full: bool = True) -> None:
    """화면을 찍는다. Streamlit은 안쪽 컨테이너가 스크롤되므로 full이면 창 높이를 내용 높이로 늘려 찍는다."""
    path = out / f"{name}.png"
    result = page.evaluate(AUDIT_JS)
    size = page.viewport_size
    if full:
        height = min(int(page.evaluate(CONTENT_HEIGHT_JS)), 6000)
        if height > size["height"]:
            page.set_viewport_size({"width": size["width"], "height": height})
            time.sleep(0.8)
    page.screenshot(path=str(path))
    page.set_viewport_size(size)
    report[name] = result
    flag = "문제 " + str(len(result["problems"])) if result["problems"] else "정상"
    print(f"  {name:<32} {flag}")


def run_viewport(pw, label: str, size: tuple[int, int], a, out: Path, report: dict) -> None:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1,
                              locale="ko-KR", timezone_id="Asia/Seoul",
                              is_mobile=label == "mobile", has_touch=label == "mobile")
    page = ctx.new_page()
    p = lambda n: f"{label}-{n}"  # noqa: E731

    goto(page, a.base, "/")
    capture(page, out, p("landing"), report)
    goto(page, a.base, "/?view=login")
    capture(page, out, p("login"), report)
    # 오류 화면: 틀린 비밀번호(잠금 횟수에 걸리지 않게 존재하지 않는 회사 코드로)
    page.get_by_placeholder("회사 코드를 입력해 주세요.").fill("ZZZZZZ")
    page.get_by_placeholder("사번을 입력해 주세요.").fill("EMP999")
    page.get_by_placeholder("비밀번호를 입력해 주세요.").fill("0000")
    page.locator("[data-testid='stFormSubmitButton'] button").first.click()
    settle(page, 2)
    capture(page, out, p("login-error"), report)
    goto(page, a.base, "/")
    if click_key(page, "landing_register"):
        capture(page, out, p("register"), report)

    # 사원
    login(page, a.base, a.code, a.employee, a.password)
    capture(page, out, p("home"), report)
    exp = page.locator(".st-key-home_widget_mcp [data-testid='stExpander'] summary").first
    if exp.count():
        exp.click()
        settle(page)
        page.locator(".st-key-home_widget_mcp").first.scroll_into_view_if_needed()
        capture(page, out, p("home-ai-connect"), report)
    for name in ("notices", "library", "equipment"):
        goto(page, a.base, f"/?page={name}")
        capture(page, out, p(name), report)
    goto(page, a.base, "/?page=home")
    if click_key(page, "chat_fab_toggle"):
        capture(page, out, p("chat"), report, full=False)
    logout(page)

    # 관리자
    if a.admin_password:
        login(page, a.base, a.code, "ADMIN", a.admin_password)
        capture(page, out, p("admin-home"), report)
        for name in ("notices", "library", "equipment", "employees", "data"):
            goto(page, a.base, f"/?page={name}")
            capture(page, out, p(f"admin-{name}"), report)
        goto(page, a.base, "/?page=home")
        if click_key(page, "preview_open"):
            capture(page, out, p("admin-preview"), report)
        logout(page)
    browser.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="화면 스크린샷과 아이콘 깨짐 검사")
    ap.add_argument("base")
    ap.add_argument("--out", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--employee", default="EMP001")
    ap.add_argument("--password", required=True)
    ap.add_argument("--admin-password", default="")
    ap.add_argument("--only", choices=list(VIEWPORTS), help="한 화면 크기만")
    a = ap.parse_args(argv)
    a.base = a.base.rstrip("/")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    report: dict = {}
    with sync_playwright() as pw:
        for label, size in VIEWPORTS.items():
            if a.only and label != a.only:
                continue
            print(f"[{label} {size[0]}×{size[1]}]")
            run_viewport(pw, label, size, a, out, report)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    bad = {k: v["problems"] for k, v in report.items() if v["problems"]}
    print(f"\n화면 {len(report)}개, 문제 있는 화면 {len(bad)}개 → {out / 'report.json'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
