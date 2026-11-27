"""회사 코드 · 계정 · 로그인 테스트 (1단계).

원본 data/는 읽기만 한다. 변형이 필요한 문서는 메모리에서 복사해 고친다.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select, update

import agent as agent_module
import mcp_server as s
import storage
from scripts import import_company, list_companies, set_admin_password
from tests import datafiles as D

ROOT = Path(__file__).resolve().parents[1]
A, B = "bridgesoft", "nextwave"
LOGIN_FAILED = s.MSG["LOGIN_FAILED"]
pytestmark = pytest.mark.usefixtures("fresh_db")


def _json_password(company_id: str, employee_id: str) -> str:
    rows = json.loads((ROOT / "data" / company_id / "employees.json").read_text("utf-8"))
    return next(str(e["password"]) for e in rows["employees"] if e["employee_id"] == employee_id)


def _code(company_id: str) -> str:
    return storage.get_company(company_id)["company_code"]


def _account_row(company_id: str, account_id: str) -> dict:
    return storage._run_db(
        lambda conn: dict(
            conn.execute(
                select(storage.accounts).where(
                    storage.accounts.c.company_id == company_id,
                    storage.accounts.c.account_id == account_id,
                )
            ).mappings().one()
        )
    )


def _documents(company_id: str) -> dict:
    _, documents = import_company.load_company_directory(ROOT / "data" / company_id)
    return copy.deepcopy(documents)


# ---- 회사 코드 -------------------------------------------------------------

@pytest.mark.pg
def test_company_codes_have_valid_format_and_are_unique():
    codes = [_code(A), _code(B)]
    for code in codes:
        assert len(code) == 6
        assert storage.is_valid_company_code(code)
        assert not set(code) & set("0O1IL")
        assert code == code.upper()
    assert len(set(codes)) == 2
    generated = {storage.generate_company_code() for _ in range(300)}
    assert all(storage.is_valid_company_code(code) for code in generated)


@pytest.mark.reimport
def test_reimport_keeps_company_code():
    before = _code(A)
    result = import_company.import_company_directory(ROOT / "data" / A)
    assert result["company_code"] == before
    assert _code(A) == before


def test_init_db_migrates_legacy_database(tmp_path, db_backend):
    """company_code·history 컬럼이 없던 기존 DB에 컬럼과 코드가 안전하게 추가된다."""
    legacy = tmp_path / "legacy.db"
    try:
        storage.configure_database(f"sqlite:///{legacy}")
        storage._run_db(
            lambda conn: [
                conn.exec_driver_sql(sql)
                for sql in (
                    "CREATE TABLE companies (company_id VARCHAR(100) PRIMARY KEY, "
                    "company_name VARCHAR(300) NOT NULL, data_version INTEGER NOT NULL, "
                    "next_ticket_sequence INTEGER NOT NULL, ticket_id_format VARCHAR(100) NOT NULL, "
                    "company_metadata JSON NOT NULL)",
                    "CREATE TABLE tickets (company_id VARCHAR(100), ticket_id VARCHAR(100), "
                    "employee_id VARCHAR(100) NOT NULL, type VARCHAR(100) NOT NULL, "
                    "item VARCHAR(500) NOT NULL, quantity INTEGER NOT NULL, purpose TEXT, "
                    "status VARCHAR(100) NOT NULL, created_at VARCHAR(100) NOT NULL, "
                    "ticket_order INTEGER NOT NULL, extra JSON NOT NULL, "
                    "PRIMARY KEY (company_id, ticket_id))",
                    "INSERT INTO companies VALUES ('legacy', '레거시', 3, 7, 'REQ-{sequence:03d}', '{}')",
                    "INSERT INTO tickets VALUES ('legacy', 'REQ-001', 'EMP001', '비품', '노트북', 1, '', "
                    "'접수완료', '2026-11-01T09:00:00+09:00', 0, '{}')",
                )
            ],
            write=True,
        )
        storage.init_db()
        storage.init_db()  # 두 번 실행해도 안전
        row = storage.get_company("legacy")
        assert storage.is_valid_company_code(row["company_code"])
        assert row["data_version"] == 3
        assert len(storage.get_tickets("legacy")) == 1
        assert storage.list_audit_log("legacy") == []
    finally:
        storage.configure_database(db_backend["url"], schema=db_backend["schema"])


# ---- 로그인 ----------------------------------------------------------------

def test_login_employee_success_ignores_code_case_and_spaces():
    password = _json_password(A, "EMP001")
    r = s.ui_login(f"  {_code(A).lower()} ", "EMP001", password)
    assert r["ok"], r
    data = r["data"]
    assert data["company_id"] == A and data["role"] == "employee"
    assert data["employee_id"] == "EMP001" and data["name"] == D.employee(A, "EMP001")["name"]
    assert data["department"] == "백엔드개발팀"
    assert data["must_change"] is False
    assert set(data) == {
        "company_id", "company_name", "role", "employee_id", "name",
        "department", "position", "must_change",
        "session_token",  # 새로고침 후 로그인 유지용 무작위 토큰(DB에는 해시만)
    }


def test_login_wrong_code_id_or_password_share_one_message():
    password = _json_password(A, "EMP001")
    other_code = _code(B)
    attempts = [
        ("ZZZZZZ", "EMP001", password),        # 없는 회사 코드
        (_code(A), "EMP999", password),        # 없는 사번
        (_code(A), "EMP001", "0000x"),         # 틀린 비밀번호
        (other_code, "EMP001", password),      # 다른 회사 코드(같은 사번)
        (_code(A), "ADMIN", password),         # 관리자 계정 없음
        ("", "", ""),
    ]
    responses = [s.ui_login(*attempt) for attempt in attempts]
    for response in responses:
        assert response == {"ok": False, "code": "LOGIN_FAILED", "message": LOGIN_FAILED, "data": None}


@pytest.mark.pg
def test_five_failures_lock_account_for_ten_minutes():
    code, password = _code(A), _json_password(A, "EMP002")
    for _ in range(4):
        assert s.ui_login(code, "EMP002", "wrong")["code"] == "LOGIN_FAILED"
    assert s.ui_login(code, "EMP002", "wrong")["code"] == "ACCOUNT_LOCKED"
    # 잠긴 동안에는 맞는 비밀번호도 거부
    assert s.ui_login(code, "EMP002", password)["code"] == "ACCOUNT_LOCKED"
    row = _account_row(A, "EMP002")
    locked_until = storage._parse_iso(row["locked_until"])
    remaining = (locked_until - storage._utc_now()).total_seconds()
    assert 9 * 60 < remaining <= 10 * 60
    # 다른 사원은 영향 없음
    assert s.ui_login(code, "EMP001", _json_password(A, "EMP001"))["ok"]
    # 10분이 지나면 다시 로그인 가능
    storage._run_db(
        lambda conn: conn.execute(
            update(storage.accounts)
            .where(storage.accounts.c.company_id == A, storage.accounts.c.account_id == "EMP002")
            .values(locked_until="2000-01-01T00:00:00+00:00")
        ),
        write=True,
    )
    assert s.ui_login(code, "EMP002", password)["ok"]
    assert any(e["action"] == "account_locked" for e in storage.list_audit_log(A))


def test_success_resets_failure_count():
    code, password = _code(A), _json_password(A, "EMP003")
    for _ in range(4):
        s.ui_login(code, "EMP003", "wrong")
    assert s.ui_login(code, "EMP003", password)["ok"]
    for _ in range(4):
        assert s.ui_login(code, "EMP003", "wrong")["code"] == "LOGIN_FAILED"


# ---- 비밀번호 저장 -----------------------------------------------------------

def test_passwords_are_stored_only_as_hashes():
    password = _json_password(A, "EMP001")
    row = _account_row(A, "EMP001")
    assert row["role"] == "employee"
    assert password not in row["password_hash"] and row["password_hash"].startswith("scrypt$")
    assert len(row["salt"]) == 32
    assert "password" not in {c.name for c in storage.employees.c}
    employee = storage.get_employee(A, "EMP001")
    assert "password" not in json.dumps(employee, ensure_ascii=False)
    # 같은 비밀번호라도 솔트가 달라 해시가 다르다
    first, second = storage.hash_password("1234"), storage.hash_password("1234")
    assert first[0] != second[0] and first[1] != second[1]


@pytest.mark.reimport
def test_reimport_keeps_password_changed_by_employee():
    code, old = _code(A), _json_password(A, "EMP001")
    assert s.ui_change_password(A, "EMP001", old, "9182")["ok"]
    import_company.import_company_directory(ROOT / "data" / A)
    assert s.ui_login(code, "EMP001", old)["code"] == "LOGIN_FAILED"
    assert s.ui_login(code, "EMP001", "9182")["ok"]


@pytest.mark.parametrize("company_id", [A, B])
def test_every_employee_logs_in_with_employees_json_password(company_id):
    code = _code(company_id)
    rows = _documents(company_id)["employees"]["employees"]
    with_password = [r for r in rows if str(r.get("password") or "").strip()]
    assert with_password
    for row in with_password:
        r = s.ui_login(code, row["employee_id"], str(row["password"]))
        assert r["ok"], row["employee_id"]
        assert r["data"]["role"] == "employee"
        assert r["data"]["employee_id"] == row["employee_id"]
        assert r["data"]["must_change"] is False


@pytest.mark.reimport
def test_reset_employee_passwords_option_restores_json_passwords(capsys):
    code = _code(A)
    json_pw = _json_password(A, "EMP001")
    rows = _documents(A)["employees"]["employees"]
    # 사원 비밀번호 변경 + 다른 사원 잠금 + 관리자 비밀번호 설정
    assert s.ui_change_password(A, "EMP001", json_pw, "9182")["ok"]
    for _ in range(storage.MAX_FAILED_LOGINS):
        s.ui_login(code, "EMP002", "wrong-password")
    assert not s.ui_login(code, "EMP002", _json_password(A, "EMP002"))["ok"]
    storage._run_db(
        lambda conn: conn.execute(
            update(storage.accounts)
            .where(storage.accounts.c.company_id == A, storage.accounts.c.account_id == "EMP003")
            .values(must_change=True, failed_count=3)
        ),
        write=True,
    )
    assert set_admin_password.main([A, "--password", "admin-pass-1"]) == 0
    admin_before = _account_row(A, "ADMIN")
    capsys.readouterr()

    assert import_company.main([str(ROOT / "data" / A), "--reset-employee-passwords"]) == 0
    out = capsys.readouterr().out

    expected = sum(1 for r in rows if str(r.get("password") or "").strip())
    assert f"사원 비밀번호 초기화: {expected}명" in out
    for row in rows:
        assert str(row.get("password")) not in out
    # 모든 사원이 employees.json 비밀번호로 로그인, 잠금·실패·변경 필요 해제
    for row in rows:
        if not str(row.get("password") or "").strip():
            continue
        account = _account_row(A, row["employee_id"])
        assert not account["must_change"]
        assert account["failed_count"] == 0
        assert account["locked_until"] is None
        r = s.ui_login(code, row["employee_id"], str(row["password"]))
        assert r["ok"], row["employee_id"]
        assert r["data"]["must_change"] is False
    assert not s.ui_login(code, "EMP001", "9182")["ok"]  # 바꿨던 비밀번호는 무효
    # ADMIN은 그대로
    admin_after = _account_row(A, "ADMIN")
    assert admin_after["password_hash"] == admin_before["password_hash"]
    assert admin_after["salt"] == admin_before["salt"]
    assert s.ui_login(code, "ADMIN", "admin-pass-1")["ok"]


def test_reset_employee_passwords_skips_admin_and_unknown_rows():
    assert set_admin_password.main([A, "--password", "admin-pass-1"]) == 0
    before = _account_row(A, "ADMIN")
    changed = storage.reset_employee_passwords(
        A,
        [
            {"employee_id": "ADMIN", "password": "hijack-123"},
            {"employee_id": "NOPE999", "password": "1234"},
            {"employee_id": "EMP001", "password": ""},
        ],
    )
    assert changed == 0
    assert _account_row(A, "ADMIN")["password_hash"] == before["password_hash"]
    assert not s.ui_login(_code(A), "ADMIN", "hijack-123")["ok"]
    assert s.ui_login(_code(A), "EMP001", _json_password(A, "EMP001"))["ok"]


def test_reset_employee_passwords_asks_before_cloud_db(monkeypatch, capsys):
    monkeypatch.setattr(storage, "database_is_sqlite", lambda: False)
    monkeypatch.setattr(storage, "database_host", lambda: "example-host")
    called = []
    monkeypatch.setattr(storage, "reset_employee_passwords", lambda *a, **k: called.append(a) or 0)
    monkeypatch.setattr(storage, "import_company_documents", lambda *a, **k: called.append(a))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert import_company.main([str(ROOT / "data" / A), "--reset-employee-passwords"]) == 1
    out = capsys.readouterr().out
    assert "사원 비밀번호" in out and "ADMIN 제외" in out
    assert called == []


@pytest.mark.reimport
def test_missing_password_creates_one_time_initial_password_and_removed_employee_is_locked():
    documents = _documents(A)
    rows = documents["employees"]["employees"]
    removed = rows.pop()  # 마지막 사원은 퇴사 처리
    rows.append({
        "employee_id": "EMP900", "name": "신규입사", "department": rows[0]["department"],
        "annual_leave": {"remaining_days": 15},
    })
    result = storage.import_company_documents(A, documents)
    assert [r["employee_id"] for r in result["initial_passwords"]] == ["EMP900"]
    initial = result["initial_passwords"][0]["initial_password"]
    assert len(initial) == 4 and initial.isdigit()
    row = _account_row(A, "EMP900")
    assert row["must_change"] is True and initial not in row["password_hash"]

    code = _code(A)
    login = s.ui_login(code, "EMP900", initial)
    assert login["ok"] and login["data"]["must_change"] is True

    removed_id = removed["employee_id"]
    assert _account_row(A, removed_id)["disabled"] is True
    assert s.ui_login(code, removed_id, str(removed["password"]))["code"] == "LOGIN_FAILED"

    # 다시 가져오면 초기 비밀번호를 새로 만들지 않고, 돌아온 사원 계정은 다시 열린다.
    again = import_company.import_company_directory(ROOT / "data" / A)
    assert again["initial_passwords"] == []
    assert _account_row(A, removed_id)["disabled"] is False
    assert s.ui_login(code, removed_id, str(removed["password"]))["ok"]


# ---- 비밀번호 변경 -----------------------------------------------------------

def test_change_password_rules():
    old = _json_password(A, "EMP001")
    assert s.ui_change_password(A, "EMP001", "wrong", "5555")["code"] == "PASSWORD_WRONG"
    assert s.ui_change_password(A, "EMP001", old, "123")["code"] == "PASSWORD_TOO_SHORT"
    assert s.ui_change_password(A, "EMP001", old, old)["code"] == "PASSWORD_SAME"
    assert s.ui_change_password(A, "EMP999", old, "5555")["code"] == "INVALID_SESSION"
    assert s.ui_change_password(A, "EMP001", old, "5555")["ok"]
    assert s.ui_login(_code(A), "EMP001", "5555")["ok"]


def test_admin_password_script_and_admin_login(capsys):
    assert set_admin_password.main([A, "--password", "short"]) == 2
    assert set_admin_password.main([A, "--password", "admin-pass-1"]) == 0
    out = capsys.readouterr().out
    assert _code(A) in out and "admin-pass-1" not in out
    r = s.ui_login(_code(A), " admin ", "admin-pass-1")
    assert r["ok"]
    assert r["data"] == {
        "session_token": r["data"]["session_token"],
        "company_id": A, "company_name": r["data"]["company_name"], "role": "admin",
        "account_id": "ADMIN", "must_change": False,
    }
    assert s.ui_change_password(A, "ADMIN", "admin-pass-1", "1234567")["code"] == "PASSWORD_TOO_SHORT"
    assert s.ui_change_password(A, "ADMIN", "admin-pass-1", "admin-pass-2")["ok"]
    assert set_admin_password.main(["no_such_company", "--password", "admin-pass-1"]) == 2


@pytest.mark.reimport
def test_operator_scripts_print_company_codes(capsys):
    assert list_companies.main([]) == 0
    out = capsys.readouterr().out
    assert _code(A) in out and _code(B) in out and "40" in out
    assert import_company.main([str(ROOT / "data" / A)]) == 0
    out = capsys.readouterr().out
    assert f"회사 코드 {_code(A)}" in out


# ---- 응답에 비밀번호·해시가 없어야 한다 -------------------------------------------

def test_no_password_material_in_any_tool_response():
    set_admin_password.main([A, "--password", "admin-pass-1"])
    password = _json_password(A, "EMP001")
    code = _code(A)
    responses = [
        s.ui_login(code, "EMP001", password),
        s.ui_login(code, "EMP001", "wrong"),
        s.ui_login(code, "ADMIN", "admin-pass-1"),
        s.ui_get_sidebar(A, "EMP001"),
        s.ui_change_password(A, "EMP001", password, "7777"),
        s.get_employee_info(A, "EMP001"),
        s.get_employee_info(A, "EMP001", target="EMP002"),
        s.search_policy("비밀번호", A, "EMP001"),
        s.get_handover_doc(A, "EMP001"),
        s.get_office_location("대회의실", A, "EMP001"),
        s.create_support_ticket("노트북 거치대", A, "EMP001"),
    ]
    secrets_ = {password, "7777", "admin-pass-1"}
    rows = storage._run_db(
        lambda conn: [dict(r) for r in conn.execute(select(storage.accounts)).mappings()]
    )
    secrets_ |= {r["password_hash"] for r in rows} | {r["salt"] for r in rows}
    for response in responses:
        text = json.dumps(response, ensure_ascii=False)
        assert "password" not in text and "salt" not in text and "scrypt$" not in text
        for secret in secrets_:
            assert secret not in text


def test_login_tools_are_not_llm_tools():
    llm_tools = agent_module.LLM_TOOLS
    assert llm_tools == {
        "search_policy", "get_employee_info", "get_office_location",
        "get_handover_doc", "create_support_ticket",
    }
    assert not any(name.startswith("ui_") for name in llm_tools)
    assert not hasattr(s, "ui_list_companies") and not hasattr(s, "ui_list_employees")


@pytest.mark.pg
@pytest.mark.reimport
def test_init_db_adds_missing_schema_in_current_database():
    """현재 테스트 DB(SQLite 또는 PostgreSQL)에서 새 컬럼·테이블이 없던 상태를 만들고 init_db로 복구."""
    def downgrade(conn):
        companies = storage._qualified_name(conn, storage.companies)
        tickets = storage._qualified_name(conn, storage.tickets)
        index = "uq_companies_company_code"
        if conn.dialect.name == "postgresql" and storage._engine_schema:
            index = f'"{storage._engine_schema}".{index}'
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {index}")
        conn.exec_driver_sql(f"ALTER TABLE {companies} DROP COLUMN company_code")
        conn.exec_driver_sql(f"ALTER TABLE {tickets} DROP COLUMN history")
        for table in (storage.accounts, storage.audit_log):
            conn.exec_driver_sql(f"DROP TABLE {storage._qualified_name(conn, table)}")

    before = {cid: storage.get_company(cid)["data_version"] for cid in (A, B)}
    tickets_before = storage.get_tickets(A)
    storage._run_db(downgrade, write=True)
    storage.init_db()
    storage.init_db()  # 두 번 실행해도 안전
    codes = {cid: storage.get_company(cid)["company_code"] for cid in (A, B)}
    assert all(storage.is_valid_company_code(code) for code in codes.values())
    assert len(set(codes.values())) == 2
    assert {cid: storage.get_company(cid)["data_version"] for cid in (A, B)} == before
    assert storage.get_tickets(A) == tickets_before
    assert storage.list_audit_log(A) == []
    assert storage.admin_list_accounts(A) == []
    # 유니크 인덱스가 다시 생겼는지: 같은 코드를 넣으면 실패
    with pytest.raises(storage.StorageUnavailable):
        storage._run_db(
            lambda conn: conn.execute(
                storage.companies.update()
                .where(storage.companies.c.company_id == B)
                .values(company_code=codes[A])
            ),
            write=True,
        )


def test_fast_test_hash_and_production_hash_verify_together(monkeypatch):
    fast_hash, fast_salt = storage.hash_password("1234")
    assert fast_hash.startswith(f"scrypt${storage.SCRYPT_N_TEST_FAST}$")
    monkeypatch.delenv("OFFICEGENIE_TEST_FAST_HASH")
    prod_hash, prod_salt = storage.hash_password("1234")
    assert prod_hash.startswith(f"scrypt${2**14}$") and storage.SCRYPT_N == 2**14
    # 비용 값이 해시에 들어 있으므로 어느 모드에서든 각자 맞게 검증된다.
    for hashed, salt in ((fast_hash, fast_salt), (prod_hash, prod_salt)):
        assert storage.verify_password("1234", hashed, salt)
        assert not storage.verify_password("1235", hashed, salt)
    monkeypatch.setenv("OFFICEGENIE_TEST_FAST_HASH", "1")
    assert storage.verify_password("1234", prod_hash, prod_salt)
    # 테스트 표시(OFFICEGENIE_TESTING)가 없으면 빠른 해시 설정은 무시된다.
    monkeypatch.delenv("OFFICEGENIE_TESTING")
    assert storage.hash_password("x")[0].startswith(f"scrypt${2**14}$")
