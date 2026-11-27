"""원격 MCP 서버 통합 테스트: 실제 HTTP 서버를 띄우고 토큰으로 접속한다."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from tests import datafiles as D

ROOT = Path(__file__).resolve().parents[1]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def remote_db(tmp_path_factory):
    """원격 서버 전용 임시 SQLite. 가져오기 CLI로 data/ + seed 티켓을 넣는다."""
    tmp = tmp_path_factory.mktemp("remote")
    url = f"sqlite:///{tmp / 'remote.db'}"
    env = {**os.environ, "OFFICEGENIE_TESTING": "1", "OFFICEGENIE_TEST_DATABASE_URL": url}
    env.pop("OFFICEGENIE_TEST_SCHEMA", None)
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "import_company.py"), "--all", "--reset-tickets", "--yes"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return tmp, env


def _issue(db_env, company_id, employee_id, name="test"):
    """운영자 CLI로 DB에 토큰을 발급하고 원문을 돌려준다(tokens.json 없음)."""
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "issue_token.py"), company_id, employee_id, "--name", name],
        env=db_env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return next(line.split(": ", 1)[1] for line in done.stdout.splitlines() if line.startswith("토큰: "))


@pytest.fixture(scope="module")
def tokens(remote_db):
    _, db_env = remote_db
    return {"A": _issue(db_env, "bridgesoft", "EMP001"), "B": _issue(db_env, "nextwave", "EMP001")}


@pytest.fixture(scope="module")
def server(remote_db, tokens):
    tmp, db_env = remote_db
    port = _free_port()
    env = {**db_env, "OFFICEGENIE_HOST": "127.0.0.1", "PORT": str(port)}
    env.pop("OFFICEGENIE_PORT", None)
    env.pop("OFFICEGENIE_MCP_PUBLIC_URL", None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "remote_server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    yield base
    proc.terminate()
    proc.wait(10)


def call(r):
    return r.structured_content


def test_homepage_and_health_open_without_token(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "온보딩 MCP 서버" in html and f"{server}/mcp" in html
    assert json.loads(urllib.request.urlopen(server + "/health").read())["status"] == "ok"


async def test_wrong_token_rejected(server):
    with pytest.raises(Exception):
        async with Client(server + "/mcp", auth=BearerAuth("wrong")) as c:
            await c.list_tools()


async def test_identity_comes_from_token(server, tokens):
    async with Client(server + "/mcp", auth=BearerAuth(tokens["A"])) as c:
        tools = await c.list_tools()
        for t in tools:
            schema = getattr(t, "input_schema", None) or t.inputSchema
            assert not {"company_id", "employee_id"} & set(schema.get("properties", {}))
        assert call(await c.call_tool("get_employee_info", {}))["data"]["name"] == D.employee("bridgesoft", "EMP001")["name"]
        assert call(await c.call_tool("get_employee_info", {"target": "EMP002"}))["code"] == "PERMISSION_DENIED"
        r = call(await c.call_tool("search_policy", {"query": "점심 식대 한도"}))
        assert "12,000원" in r["data"]["results"][0]["content"]
    async with Client(server + "/mcp", auth=BearerAuth(tokens["B"])) as c:
        assert call(await c.call_tool("get_employee_info", {}))["data"]["name"] == D.employee("nextwave", "EMP001")["name"]


async def test_two_step_ticket(server, tokens):
    async with Client(server + "/mcp", auth=BearerAuth(tokens["A"])) as c:
        assert call(await c.call_tool("create_support_ticket", {"item": "노트북 거치대"}))["code"] == "NEEDS_CONFIRMATION"
        r = call(await c.call_tool("confirm_support_ticket", {"item": "노트북 거치대"}))
        assert r["code"] == "CREATED" and r["data"]["ticket"]["ticket_id"] == "REQ-004"
        mine = call(await c.call_tool("get_my_tickets", {}))["data"]["tickets"]
        assert mine[0]["ticket_id"] == "REQ-004"


async def test_web_and_remote_processes_issue_unique_ticket_numbers(server, remote_db, tokens):
    """웹용 stdio MCP 프로세스와 원격 서버 프로세스가 같은 DB에 동시에 티켓을 만들어도 번호가 겹치지 않는다."""
    import asyncio

    from fastmcp.client.transports import PythonStdioTransport

    _, db_env = remote_db
    stdio = Client(PythonStdioTransport(script_path=str(ROOT / "mcp_server.py"), env=db_env), timeout=60)
    remote = Client(server + "/mcp", auth=BearerAuth(tokens["A"]))
    n = 8
    async with stdio, remote:
        jobs = [
            remote.call_tool("confirm_support_ticket", {"item": f"원격계정{i}", "request_type": "계정"})
            for i in range(n)
        ] + [
            stdio.call_tool("ui_confirm_ticket", {
                "company_id": "bridgesoft", "employee_id": "EMP002",
                "item": f"웹계정{i}", "request_type": "계정",
            })
            for i in range(n)
        ]
        results = [call(r) for r in await asyncio.gather(*jobs)]
        assert all(r["code"] == "CREATED" for r in results), results
        ids = [r["data"]["ticket"]["ticket_id"] for r in results]
        assert len(set(ids)) == 2 * n

        side = call(await stdio.call_tool("ui_get_sidebar", {"company_id": "bridgesoft", "employee_id": "EMP002"}))
        stored = {t["ticket_id"] for t in side["data"]["tickets"]}
        assert {i for i, r in zip(ids, results) if "웹계정" in r["data"]["ticket"]["item"]} <= stored


def test_tokens_file_not_used(server, remote_db):
    tmp, _ = remote_db
    assert not (tmp / "tokens.json").exists() and not (ROOT / "tokens.json").exists()


async def test_issue_and_revoke_apply_without_restart(server, remote_db):
    """서버가 떠 있는 동안 새로 발급한 토큰은 바로 통과하고, 폐기하면 바로 거부된다."""
    _, db_env = remote_db
    fresh = _issue(db_env, "bridgesoft", "EMP002", "재시작 없음")
    async with Client(server + "/mcp", auth=BearerAuth(fresh)) as c:
        assert call(await c.call_tool("get_employee_info", {}))["ok"]
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "issue_token.py"), "--revoke", fresh],
        env=db_env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    with pytest.raises(Exception):
        async with Client(server + "/mcp", auth=BearerAuth(fresh)) as c:
            await c.list_tools()


def test_homepage_uses_forwarded_https_host(server):
    req = urllib.request.Request(server + "/", headers={
        "X-Forwarded-Proto": "https", "X-Forwarded-Host": "officegenie-mcp.onrender.com"})
    html = urllib.request.urlopen(req).read().decode()
    assert "https://officegenie-mcp.onrender.com/mcp" in html
    assert "http://officegenie-mcp.onrender.com" not in html
