"""원격 MCP 서버 통합 테스트: 실제 HTTP 서버를 띄우고 토큰으로 접속한다."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

ROOT = Path(__file__).resolve().parents[1]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("remote")
    shutil.copytree(ROOT / "data", tmp / "data")
    tokens = {
        "tokA": {"client_id": "a", "scopes": [], "company_id": "bridgesoft", "employee_id": "EMP001"},
        "tokB": {"client_id": "b", "scopes": [], "company_id": "nextwave", "employee_id": "EMP001"},
    }
    (tmp / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    port = _free_port()
    env = {**os.environ, "OFFICEGENIE_DATA_ROOT": str(tmp / "data"),
           "OFFICEGENIE_TOKENS": str(tmp / "tokens.json"),
           "OFFICEGENIE_HOST": "127.0.0.1", "OFFICEGENIE_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "remote_server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
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


async def test_identity_comes_from_token(server):
    async with Client(server + "/mcp", auth=BearerAuth("tokA")) as c:
        tools = await c.list_tools()
        for t in tools:
            schema = getattr(t, "input_schema", None) or t.inputSchema
            assert not {"company_id", "employee_id"} & set(schema.get("properties", {}))
        assert call(await c.call_tool("get_employee_info", {}))["data"]["name"] == "김신입"
        assert call(await c.call_tool("get_employee_info", {"target": "EMP002"}))["code"] == "PERMISSION_DENIED"
        r = call(await c.call_tool("search_policy", {"query": "점심 식대 한도"}))
        assert "12,000원" in r["data"]["results"][0]["content"]
    async with Client(server + "/mcp", auth=BearerAuth("tokB")) as c:
        assert call(await c.call_tool("get_employee_info", {}))["data"]["name"] == "이하준"


async def test_two_step_ticket(server):
    async with Client(server + "/mcp", auth=BearerAuth("tokA")) as c:
        assert call(await c.call_tool("create_support_ticket", {"item": "노트북 거치대"}))["code"] == "NEEDS_CONFIRMATION"
        r = call(await c.call_tool("confirm_support_ticket", {"item": "노트북 거치대"}))
        assert r["code"] == "CREATED" and r["data"]["ticket"]["ticket_id"] == "REQ-004"
        mine = call(await c.call_tool("get_my_tickets", {}))["data"]["tickets"]
        assert mine[0]["ticket_id"] == "REQ-004"
