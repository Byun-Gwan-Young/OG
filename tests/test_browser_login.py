"""실제 브라우저(Playwright Chromium)로 사원 로그인을 재현하는 테스트.

화면에서만 브릿지소프트 사원 로그인이 실패하던 문제의 재현용이다.
- 임시 SQLite DB를 만들어 data/의 회사 JSON(운영 DB와 같은 구조)을 가져온다.
- 회사 코드를 운영과 같은 형식(TCNCZD)으로 맞춘다.
- Streamlit을 실제로 띄우고 브라우저로 입력·제출한다.

Playwright가 없으면 건너뛴다:
    pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
CODE = "TCNCZD"

PREPARE = r"""
import sys
sys.path.insert(0, sys.argv[1])
import storage
from sqlalchemy import update
from scripts import import_company
storage.init_db()
for name in ("bridgesoft", "nextwave"):
    import_company.import_company_directory(f"{sys.argv[1]}/data/{name}")
storage._run_db(lambda c: c.execute(
    update(storage.companies)
    .where(storage.companies.c.company_id == "bridgesoft")
    .values(company_code=sys.argv[2])), write=True)
storage.reset_employee_passwords("bridgesoft", [{"employee_id": "EMP001", "password": "0899"}])
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _json_password(employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / "bridgesoft" / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    db = tmp_path_factory.mktemp("browser") / "browser.db"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DATABASE_URL", "LLM_API_KEY", "LLM_BASE_URL"}
    }
    env.update(
        OFFICEGENIE_TESTING="1",
        OFFICEGENIE_TEST_FAST_HASH="1",
        OFFICEGENIE_TEST_DATABASE_URL=f"sqlite:///{db}",
    )
    env.pop("OFFICEGENIE_TEST_SCHEMA", None)
    subprocess.run(
        [sys.executable, "-c", PREPARE, str(ROOT), CODE],
        env=env, cwd=ROOT, check=True, capture_output=True,
    )
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
         "--server.headless", "true", "--server.port", str(port),
         "--browser.gatherUsageStats", "false"],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{url}/_stcore/health", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("Streamlit이 시작되지 않았습니다.")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # 브라우저 바이너리 미설치
            pytest.skip(f"Chromium을 띄울 수 없습니다: {type(exc).__name__}")
        yield chromium
        chromium.close()


def _login(browser, url: str, code: str, account_id: str, password: str):
    page = browser.new_page()
    page.goto(url)
    page.get_by_role("button", name="로그인", exact=True).click()
    page.get_by_label("회사 코드").fill(code)
    page.get_by_label("사번").fill(account_id)
    page.get_by_label("비밀번호").fill(password)
    page.get_by_label("비밀번호").press("Enter")
    return page


@pytest.mark.parametrize(
    "code,account_id,password",
    [
        (CODE, "EMP001", "0899"),
        (CODE.lower(), "EMP001", "0899"),
        (CODE, "EMP004", None),  # employees.json 비밀번호
    ],
)
def test_bridgesoft_employee_logs_in_from_real_browser(
    app_url, browser, code, account_id, password
):
    page = _login(browser, app_url, code, account_id, password or _json_password(account_id))
    try:
        page.get_by_role("heading", name="나의 온보딩", exact=True).wait_for(timeout=30_000)
        page.get_by_role("heading", name="내 비품 내역", exact=True).wait_for()
        assert page.get_by_text("로그인").count() == 0 or page.get_by_text("로그아웃").count()
        assert page.get_by_text("화면을 불러오는 중 문제가 발생했습니다").count() == 0
    finally:
        page.close()


def test_wrong_password_from_real_browser_allows_retry(app_url, browser):
    page = _login(browser, app_url, CODE, "EMP001", "0000x")
    try:
        page.get_by_role("alert").first.wait_for(timeout=30_000)
        assert page.get_by_text("나의 온보딩").count() == 0
        assert page.get_by_label("회사 코드").input_value() == CODE
        assert page.get_by_label("사번").input_value() == "EMP001"
        password = page.get_by_label("비밀번호")
        assert password.input_value() == ""
        password.fill("0899")
        password.press("Enter")
        page.get_by_role("heading", name="나의 온보딩", exact=True).wait_for(timeout=30_000)
    finally:
        page.close()
