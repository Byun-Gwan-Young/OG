"""배포 도구(warmup.py, deploy_check.py)를 로컬에서 띄운 웹·MCP 서버에 실행해 본다."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from scripts import deploy_check, pwa_setup, warmup

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise AssertionError(f"{url} 응답 없음")


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    if pwa_setup.check(pwa_setup.streamlit_static_dir()):
        pytest.skip("설치된 streamlit에 PWA가 적용되지 않았습니다(python scripts/pwa_setup.py).")
    tmp = tmp_path_factory.mktemp("deploy")
    env = {**os.environ, "OFFICEGENIE_TESTING": "1",
           "OFFICEGENIE_TEST_DATABASE_URL": f"sqlite:///{tmp / 'deploy.db'}"}
    for key in ("OFFICEGENIE_TEST_SCHEMA", "OFFICEGENIE_MCP_PUBLIC_URL", "OFFICEGENIE_PORT", "PORT"):
        env.pop(key, None)
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "import_company.py"), "--all", "--yes"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    web_port, mcp_port = _free_port(), _free_port()
    procs = [
        subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
                          "--server.port", str(web_port), "--server.address", "127.0.0.1",
                          "--server.headless", "true"],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, str(ROOT / "remote_server.py")], cwd=ROOT,
                         env={**env, "OFFICEGENIE_HOST": "127.0.0.1", "PORT": str(mcp_port)},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    web, mcp = f"http://127.0.0.1:{web_port}", f"http://127.0.0.1:{mcp_port}"
    try:
        _wait(web + "/_stcore/health")
        _wait(mcp + "/health")
        yield web, mcp
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(10)


def test_warmup_reports_both_services(servers, capsys):
    web, mcp = servers
    assert warmup.main([web, mcp + "/mcp", "--timeout", "20"]) == 0
    out = capsys.readouterr().out
    assert "웹" in out and "MCP" in out and "정상" in out and "실패" not in out.split("시도")[1]


def test_deploy_check_passes_locally_with_http_warnings(servers, capsys):
    web, mcp = servers
    assert deploy_check.main([web, mcp, "--allow-http"]) == 0
    out = capsys.readouterr().out
    checks = deploy_check.run(web, mcp, allow_http=True)
    results = {c.name: c.result for c in checks}
    assert results["웹 주소"] == results["MCP 주소"] == deploy_check.WARN
    for name in ("첫 화면 응답", "헬스 경로 /_stcore/health", "index.html PWA 태그", "manifest.webmanifest",
                 "서비스 워커 sw.js", "offline.html", "/health", "잘못된 토큰 거부", "토큰 없음 거부"):
        assert results[name] == deploy_check.PASS, (name, checks)
    assert "수동 확인 목록" in out


def test_deploy_check_fails_on_http_without_flag(servers):
    web, mcp = servers
    assert deploy_check.main([web, mcp]) == 1


def test_warmup_reports_unreachable_service(capsys):
    dead = f"http://127.0.0.1:{_free_port()}"
    assert warmup.main([dead, dead, "--timeout", "1"]) == 1
    assert "실패" in capsys.readouterr().out
