"""Render 같은 HTTPS 프록시 뒤에서 동작: 로그인 쿠키 Secure, 공개 주소."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import storage

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
A = "bridgesoft"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _password() -> str:
    rows = json.loads((ROOT / "data" / A / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == "EMP001")


def _cookie_scripts(headers: dict, monkeypatch) -> list[str]:
    monkeypatch.setattr(type(st.context), "headers", property(lambda self: headers))
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["view"] = "login"
    at.run()
    at.text_input(key="login_company_code").input(storage.get_company(A)["company_code"])
    at.text_input(key="login_account_id").input("EMP001")
    at.text_input(key="login_password").input(_password())
    next(b for b in at.button if b.label == "로그인").click()
    at.run()
    assert not at.exception
    bodies = [str(getattr(h.proto, "body", "")) for h in at.get("html")]
    return [b for b in bodies if "document.cookie" in b]


def test_login_cookie_secure_behind_https_proxy(monkeypatch):
    scripts = _cookie_scripts({"X-Forwarded-Proto": "https", "Host": "officegenie-web.onrender.com"}, monkeypatch)
    assert scripts and all("SameSite=Strict; Secure" in s for s in scripts)


def test_login_cookie_first_forwarded_value_wins(monkeypatch):
    scripts = _cookie_scripts({"X-Forwarded-Proto": "https, http"}, monkeypatch)
    assert scripts and "SameSite=Strict; Secure" in scripts[0]


def test_login_cookie_plain_http_keeps_js_fallback(monkeypatch):
    scripts = _cookie_scripts({"X-Forwarded-Proto": "http"}, monkeypatch)
    assert scripts and "SameSite=Strict; Secure\"" not in scripts[0]
    assert "location.protocol === 'https:'" in scripts[0]


def test_render_blueprint_matches_deploy_contract():
    yaml = pytest.importorskip("yaml")
    tomllib = pytest.importorskip("tomllib")
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text("utf-8"))
    services = {svc["name"]: svc for svc in blueprint["services"]}
    assert set(services) == {"officegenie-web", "officegenie-mcp"}
    web, mcp = services["officegenie-web"], services["officegenie-mcp"]
    assert web["buildCommand"] == "pip install -r requirements.txt && python scripts/pwa_setup.py"
    assert web["startCommand"].endswith(
        "streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true")
    assert web["startCommand"].startswith("python scripts/preflight.py && ")
    assert web["healthCheckPath"] == "/_stcore/health"
    assert mcp["buildCommand"] == "pip install -r requirements.txt"
    assert mcp["startCommand"] == "python remote_server.py"
    assert mcp["healthCheckPath"] == "/health"
    version = (ROOT / ".python-version").read_text().strip()
    for svc in services.values():
        assert svc["plan"] == "free" and svc["runtime"] == "python"
        assert svc["region"] == "singapore"
        env = {e["key"]: e for e in svc["envVars"]}
        assert env["OFFICEGENIE_ENV"]["value"] == "production"
        assert env["PYTHON_VERSION"]["value"] == version
        for secret in ("DATABASE_URL", "LLM_API_KEY", "LLM_BASE_URL",
                       "OFFICEGENIE_SIGNUP_CODE", "OFFICEGENIE_MCP_PUBLIC_URL"):
            assert env[secret] == {"key": secret, "sync": False}
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text("utf-8"))
    assert config["server"]["headless"] is True
    assert config["server"]["enableXsrfProtection"] is True
    assert config["browser"]["gatherUsageStats"] is False


def test_remote_server_reads_port_env():
    import subprocess
    import sys

    code = "import remote_server as r; print(r.HOST, r.PORT)"
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("OFFICEGENIE_PORT", "OFFICEGENIE_HOST")}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          env={**env, "PORT": "10000"}, timeout=60)
    assert done.stdout.split() == ["0.0.0.0", "10000"], done.stderr
