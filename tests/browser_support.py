"""Playwright 브라우저 테스트 공통: Chromium 실행 파일 찾기.

pip로 설치한 playwright가 기대하는 브라우저가 없으면, 미리 설치된 Chromium
(PLAYWRIGHT_CHROMIUM 환경 변수 또는 /opt/pw-browsers)을 쓴다. 둘 다 없으면 건너뛴다.
"""
from __future__ import annotations

import glob
import os

import pytest


def launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception as first:
        candidates = [os.environ.get("PLAYWRIGHT_CHROMIUM", "")]
        candidates += sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux64/chrome"), reverse=True)
        for path in candidates:
            if path and os.path.exists(path):
                return pw.chromium.launch(executable_path=path)
        pytest.skip(f"Chromium을 띄울 수 없습니다: {type(first).__name__}")


import contextlib  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def browser_env(db_path: Path) -> dict:
    """공유 DB·LLM 키 없이 임시 SQLite로만 앱을 띄우는 환경 변수."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DATABASE_URL", "LLM_API_KEY", "LLM_BASE_URL", "OFFICEGENIE_TEST_SCHEMA"}
    }
    env.update(
        OFFICEGENIE_TESTING="1",
        OFFICEGENIE_TEST_FAST_HASH="1",
        OFFICEGENIE_TEST_DATABASE_URL=f"sqlite:///{db_path}",
    )
    return env


@contextlib.contextmanager
def running_app(env: dict):
    """Streamlit 앱을 띄우고 주소를 돌려준다. 끝나면 종료한다."""
    port = free_port()
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
