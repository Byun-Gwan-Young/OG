"""원격 MCP 개인 토큰(DB 저장)과 운영 설정 검사 테스트."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

import storage

ROOT = Path(__file__).resolve().parents[1]
A, B = "bridgesoft", "nextwave"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _all_rows() -> list[dict]:
    return storage._run_db(
        lambda conn: [dict(r) for r in conn.execute(select(storage.mcp_tokens)).mappings()]
    )


@pytest.mark.pg
def test_only_issued_token_passes_and_revoke_is_immediate():
    token, info = storage.issue_mcp_token(A, "EMP001", "노트북")
    owner = storage.verify_mcp_token(token)
    assert owner == {"company_id": A, "employee_id": "EMP001", "token_id": info["token_id"]}
    assert storage.verify_mcp_token(token + "x") is None
    assert storage.verify_mcp_token("og_" + "a" * 43) is None
    assert storage.verify_mcp_token("") is None

    assert storage.revoke_mcp_token(A, info["token_id"], employee_id="EMP001", actor="EMP001")
    assert storage.verify_mcp_token(token) is None  # 같은 프로세스, 재시작 없음
    assert not storage.revoke_mcp_token(A, info["token_id"], employee_id="EMP001", actor="EMP001")


@pytest.mark.pg
def test_raw_token_never_stored():
    token, _ = storage.issue_mcp_token(A, "EMP001", "원문 확인")
    rows = _all_rows()
    assert len(rows) == 1
    flat = " ".join(str(v) for v in rows[0].values())
    assert token not in flat and token[3:] not in flat
    assert rows[0]["token_hash"] == storage._token_hash(token)
    audit = " ".join(str(r) for r in storage.list_audit_log(A))
    assert token not in audit


@pytest.mark.pg
def test_three_tokens_per_employee():
    for i in range(storage.MCP_TOKENS_PER_EMPLOYEE):
        storage.issue_mcp_token(A, "EMP001", f"t{i}")
    with pytest.raises(storage.TokenLimitReached):
        storage.issue_mcp_token(A, "EMP001", "넘침")
    # 다른 사원·다른 회사는 영향 없음
    storage.issue_mcp_token(A, "EMP002", "다른 사원")
    storage.issue_mcp_token(B, "EMP001", "다른 회사")
    # 하나를 폐기하면 다시 발급할 수 있다
    first = storage.list_mcp_tokens(A, "EMP001")[0]
    assert storage.revoke_mcp_token(A, first["token_id"], employee_id="EMP001", actor="EMP001")
    storage.issue_mcp_token(A, "EMP001", "재발급")
    assert len(storage.list_mcp_tokens(A, "EMP001")) == storage.MCP_TOKENS_PER_EMPLOYEE


@pytest.mark.pg
def test_cannot_revoke_other_employee_token():
    token, info = storage.issue_mcp_token(A, "EMP002", "남의 것")
    assert not storage.revoke_mcp_token(A, info["token_id"], employee_id="EMP001", actor="EMP001")
    assert not storage.revoke_mcp_token(B, info["token_id"], actor="ADMIN")  # 다른 회사 관리자
    assert storage.verify_mcp_token(token) is not None
    assert storage.revoke_mcp_token(A, info["token_id"], actor="ADMIN")  # 같은 회사 관리자
    assert storage.verify_mcp_token(token) is None


def test_disabled_account_token_rejected():
    token, _ = storage.issue_mcp_token(A, "EMP001", "비활성")
    storage._run_db(
        lambda conn: conn.execute(
            storage.update(storage.accounts)
            .where(storage._account_key(A, "EMP001"))
            .values(disabled=True)
        ),
        write=True,
    )
    assert storage.verify_mcp_token(token) is None


def test_audit_log_records_issue_and_revoke():
    _, info = storage.issue_mcp_token(A, "EMP001", "감사")
    storage.revoke_mcp_token(A, info["token_id"], actor="ADMIN")
    actions = [r["action"] for r in storage.list_audit_log(A, 10)]
    assert "mcp_token_issue" in actions and "mcp_token_revoke" in actions


def test_label_is_trimmed_and_listing_hides_hash():
    _, info = storage.issue_mcp_token(A, "EMP001", "  아주 " + "긴" * 100)
    assert len(info["label"]) <= storage.MCP_TOKEN_LABEL_MAX
    listed = storage.list_mcp_tokens(A, "EMP001")[0]
    assert set(listed) == {"token_id", "employee_id", "label", "issued_at",
                           "last_used_at", "revoked_at", "active"}
    assert len(listed["token_id"]) == 12


# ---------------------------------------------------------------------------
# 운영 설정: production에서는 DATABASE_URL 없이 시작하지 않는다
# ---------------------------------------------------------------------------

def _prod_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OFFICEGENIE_TEST") and k not in ("DATABASE_URL", "OFFICEGENIE_TESTING")}
    env.update(OFFICEGENIE_ENV="production", PYTHONPATH=str(ROOT), **extra)
    return env


def test_check_runtime_config(monkeypatch):
    monkeypatch.delenv("OFFICEGENIE_TESTING")
    monkeypatch.setenv("OFFICEGENIE_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(storage.ConfigurationError):
        storage.check_runtime_config()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(storage.ConfigurationError):
        storage.check_runtime_config()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com/x")
    storage.check_runtime_config()
    monkeypatch.setenv("OFFICEGENIE_ENV", "development")
    monkeypatch.delenv("DATABASE_URL")
    storage.check_runtime_config()


@pytest.mark.parametrize("script", ["remote_server.py", "mcp_server.py", "scripts/preflight.py"])
def test_production_without_database_url_fails_at_startup(script, tmp_path):
    # .env를 읽지 않는 빈 작업 폴더에서 실행해도 결과가 같아야 한다(.env는 BASE_DIR 기준이라
    # 저장소 .env의 DATABASE_URL이 섞이지 않도록 DATABASE_URL을 빈 값으로 덮는다).
    done = subprocess.run(
        [sys.executable, str(ROOT / script)],
        env=_prod_env(DATABASE_URL=""), cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode != 0
    assert "DATABASE_URL" in done.stderr
    assert not (tmp_path / "officegenie.db").exists()


def test_preflight_warns_when_pwa_missing(tmp_path, monkeypatch, caplog):
    from scripts import preflight, pwa_setup

    monkeypatch.setattr(pwa_setup, "streamlit_static_dir", lambda: tmp_path)
    (tmp_path / "index.html").write_text("<html><head></head></html>", "utf-8")
    with caplog.at_level("WARNING", logger="officegenie.preflight"):
        assert preflight.main() == 0  # 경고만, 시작은 계속
    assert "PWA 미적용" in caplog.text
