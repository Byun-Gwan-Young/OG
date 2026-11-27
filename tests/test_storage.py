"""DB 저장소·가져오기 테스트 (임시 SQLite, TEST_DATABASE_URL이 있으면 PostgreSQL 임시 스키마도).

원본 data/·seed/는 읽기만 한다. 변형이 필요한 JSON은 tmp_path에 복사해서 고친다.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, update

import mcp_server as s
import storage
from scripts import import_company
from tests import datafiles as D

ROOT = Path(__file__).resolve().parents[1]
A, B = "bridgesoft", "nextwave"
pytestmark = pytest.mark.usefixtures("fresh_db")


def _count(table, company_id):
    return storage._run_db(
        lambda conn: conn.execute(
            select(func.count()).select_from(table).where(table.c.company_id == company_id)
        ).scalar_one()
    )


def _company_row(company_id):
    return storage._run_db(
        lambda conn: dict(
            conn.execute(
                select(storage.companies).where(storage.companies.c.company_id == company_id)
            ).mappings().one()
        )
    )


def _copy_company(tmp_path, company_id=A) -> Path:
    target = tmp_path / company_id
    shutil.copytree(ROOT / "data" / company_id, target)
    return target


def _edit_json(path: Path, change) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


# ---- 기본 적재 ---------------------------------------------------------------

def test_row_counts_per_company():
    for company_id in (A, B):
        assert _count(storage.employees, company_id) == D.count(company_id, "employees")
        assert _count(storage.policies, company_id) == D.count(company_id, "policies")
        assert _count(storage.tickets, company_id) == D.baseline_ticket_count(company_id)  # seed 상태
        assert _company_row(company_id)["next_ticket_sequence"] == D.baseline_next_sequence(company_id)


def test_employees_store_no_password():
    columns = {c["name"] for c in inspect(storage.get_engine()).get_columns(
        "employees", schema=storage._engine_schema)}
    assert "password" not in columns
    rows = storage._run_db(lambda conn: conn.execute(select(storage.employees.c.extra)).all())
    assert rows and all("password" not in (extra or {}) for (extra,) in rows)
    assert "password" not in storage.get_employee(A, "EMP001")
    # 원본 JSON에는 password가 있으므로 가져오기에서 걸러졌음을 확인
    source = json.loads((ROOT / "data" / A / "employees.json").read_text(encoding="utf-8"))
    assert any("password" in e for e in source["employees"])


@pytest.mark.reimport
def test_position_is_optional(tmp_path):
    company = _copy_company(tmp_path)
    _edit_json(company / "employees.json", lambda d: d["employees"][0].pop("position", None))
    import_company.import_company_directory(company)
    assert storage.get_employee(A, "EMP001").get("position") is None
    assert s.get_employee_info(A, "EMP001")["ok"]


# ---- 툴 응답 형식과 기존 기대값 -------------------------------------------------

def test_tool_envelope_and_expected_values():
    keys = {"ok", "code", "message", "data"}
    meal = s.search_policy("점심 식대 한도", A, "EMP001")
    leave = s.get_employee_info(A, "EMP001")
    room = s.get_office_location("대회의실", A, "EMP001")
    handover = s.get_handover_doc(A, "EMP001")
    ticket = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)
    denied = [
        s.get_office_location("사내 수면실", A, "EMP001"),
        s.get_handover_doc(A, "EMP001", department="재무회계팀"),
        s.get_employee_info(A, "EMP001", target="EMP002"),
    ]
    for r in [meal, leave, room, handover, ticket, *denied]:
        assert set(r) == keys
    assert "12,000원" in meal["data"]["results"][0]["content"]
    assert meal["data"]["results"][0]["citation"].startswith("복리후생 규정 제4조")
    assert leave["data"]["annual_leave"]["remaining_days"] == 11
    assert room["data"]["locations"][0]["display"] == "본관 3층 A구역"
    assert handover["data"]["department"] == "백엔드개발팀"
    assert ticket["data"]["ticket"]["ticket_id"] == "REQ-004"
    assert all(r["message"] == s.MSG["DENIED_OR_NOT_FOUND"] for r in denied)
    b_meal = s.search_policy("점심 식대 한도", B, "EMP001")
    assert "15,000원" in b_meal["data"]["results"][0]["content"]
    assert s.create_support_ticket("모니터", B, "EMP001", quantity=2)["code"] == "REJECTED"


# ---- 재가져오기와 티켓 정책 ------------------------------------------------------

@pytest.mark.pg
@pytest.mark.reimport
def test_reimport_keeps_ticket_state_and_number(capsys, monkeypatch):
    # --all 경로를 쓰되 기준 상태 회사만 가져온다(data/에 회사가 늘어도 느려지지 않게)
    monkeypatch.setattr(import_company, "_company_directories",
                        lambda: [ROOT / "data" / A, ROOT / "data" / B])
    seq = D.baseline_next_sequence(A)
    created = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)["data"]["ticket"]
    storage.update_ticket_status(A, created["ticket_id"], "승인")
    before_version = _company_row(A)["data_version"]

    assert import_company.main(["--all", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "티켓은 기존 DB 상태 유지(초기화하려면 --reset-tickets)" in out

    row = _company_row(A)
    assert row["data_version"] == before_version + 1
    assert row["next_ticket_sequence"] == seq + 1
    tickets = {t["ticket_id"]: t for t in storage.get_tickets(A)}
    assert len(tickets) == D.baseline_ticket_count(A) + 1
    assert tickets[created["ticket_id"]]["status"] == "승인"
    assert s.ui_confirm_ticket(A, "EMP001", "Slack", 1, request_type="계정")["data"]["ticket"]["ticket_id"] == f"REQ-{seq + 1:03d}"


@pytest.mark.pg
@pytest.mark.reimport
def test_reset_tickets_numbering():
    seq = D.baseline_next_sequence(A)
    for item in ("Slack", "Notion"):
        s.ui_confirm_ticket(A, "EMP001", item, 1, request_type="계정")  # seq, seq + 1
    assert import_company.main([str(ROOT / "data" / A), "--reset-tickets", "--yes"]) == 0
    # seed로 교체하되 번호는 max(seed 다음 번호, 기존 최대 seq + 1 + 1) = seq + 2
    seed_ids = sorted(t["ticket_id"] for t in D.baseline_tickets_doc(A)["tickets"])
    assert sorted(t["ticket_id"] for t in storage.get_tickets(A)) == seed_ids
    assert _company_row(A)["next_ticket_sequence"] == seq + 2
    assert s.ui_confirm_ticket(A, "EMP001", "Jira", 1, request_type="계정")["data"]["ticket"]["ticket_id"] == f"REQ-{seq + 2:03d}"
    # 다른 회사 티켓은 영향 없음
    assert _count(storage.tickets, B) == D.baseline_ticket_count(B)


@pytest.mark.pg
@pytest.mark.reimport
def test_tickets_initialized_from_json_only_when_empty():
    storage.recreate_tables_for_tests()
    import_company.import_company_directory(ROOT / "data" / A)
    # DB에 티켓이 없으면 data/의 tickets.json을 그대로 넣는다(seed가 아니라 data/ 기준)
    source = D.data_doc(A, "tickets")
    assert _count(storage.tickets, A) == len(source["tickets"])
    assert _company_row(A)["next_ticket_sequence"] == D.next_sequence(source)


@pytest.mark.reimport
def test_reset_tickets_falls_back_to_data_when_seed_missing(tmp_path, monkeypatch, capsys):
    """seed/<회사>/tickets.json이 없으면 data/<회사>/tickets.json 기준으로 초기화하고 알린다."""
    monkeypatch.setattr(import_company, "SEED_ROOT", tmp_path / "no_seed")
    s.ui_confirm_ticket(A, "EMP001", "Slack", 1, request_type="계정")
    assert import_company.main([str(ROOT / "data" / A), "--reset-tickets", "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"data/{A}/tickets.json을 기준으로 티켓을 초기화했습니다" in out
    source = D.data_doc(A, "tickets")
    assert sorted(t["ticket_id"] for t in storage.get_tickets(A)) == sorted(
        t["ticket_id"] for t in source["tickets"])


def test_resolve_reset_tickets_none_when_seed_and_data_missing(tmp_path):
    empty = tmp_path / A
    empty.mkdir()
    assert import_company.resolve_reset_tickets(A, empty, seed_root=tmp_path / "no_seed") == (None, None)


@pytest.mark.reimport
def test_reset_tickets_skips_only_company_without_ticket_source(monkeypatch, capsys):
    monkeypatch.setattr(import_company, "_company_directories",
                        lambda: [ROOT / "data" / A, ROOT / "data" / B])
    real = import_company.resolve_reset_tickets
    monkeypatch.setattr(import_company, "resolve_reset_tickets",
                        lambda cid, d, *a, **k: (None, None) if cid == B else real(cid, d, *a, **k))
    version_b = _company_row(B)["data_version"]
    assert import_company.main(["--all", "--reset-tickets", "--yes"]) == 0
    captured = capsys.readouterr()
    assert f"건너뜀: {B}" in captured.err
    assert f"가져오기 완료: {A}" in captured.out and f"가져오기 완료: {B}" not in captured.out
    assert _company_row(B)["data_version"] == version_b


@pytest.mark.reimport
def test_policy_removed_from_json_is_removed_from_db(tmp_path):
    company = _copy_company(tmp_path)
    _edit_json(company / "policies.json",
               lambda d: d.__setitem__("policies", [p for p in d["policies"] if p["id"] != "BS-118"]))
    import_company.import_company_directory(company)
    assert _count(storage.policies, A) == D.count(A, "policies") - 1
    ids = {p["id"] for p in storage.get_company_document(A, "policies")["policies"]}
    assert "BS-118" not in ids and "BS-103" in ids
    assert _count(storage.policies, B) == D.count(B, "policies")


@pytest.mark.pg
def test_import_failure_rolls_back_everything(tmp_path):
    before = (_company_row(A)["data_version"], _count(storage.policies, A), _count(storage.employees, A))
    _, documents = import_company.load_company_directory(ROOT / "data" / A)
    documents["policies"]["policies"] = documents["policies"]["policies"][:10]
    documents["locations"]["locations"][-1]["floor"] = "삼층"  # 삭제·삽입 도중 실패
    with pytest.raises(ValueError):
        storage.import_company_documents(A, documents)
    after = (_company_row(A)["data_version"], _count(storage.policies, A), _count(storage.employees, A))
    assert after == before
    assert s.get_office_location("대회의실", A, "EMP001")["ok"]


# ---- 검증 오류 위치 표시 ---------------------------------------------------------

def test_invalid_json_syntax_reports_file_and_line(tmp_path):
    company = _copy_company(tmp_path)
    (company / "policies.json").write_text('{"company_id": "bridgesoft",\n  "policies": [,]}', encoding="utf-8")
    with pytest.raises(import_company.ImportValidationError) as err:
        import_company.load_company_directory(company)
    assert "policies.json" in str(err.value) and "2행" in str(err.value)


@pytest.mark.pg
def test_missing_field_reports_item_number_and_db_untouched(tmp_path, capsys):
    company = _copy_company(tmp_path)
    _edit_json(company / "employees.json", lambda d: d["employees"][2].pop("name"))
    version = _company_row(A)["data_version"]
    assert import_company.main([str(company), "--yes"]) == 2
    err = capsys.readouterr().err
    assert "employees.json" in err and "항목 3" in err and "name" in err
    assert _company_row(A)["data_version"] == version


def test_cloud_import_requires_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(storage, "database_is_sqlite", lambda: False)
    monkeypatch.setattr(storage, "database_host", lambda: "ep-example.neon.tech")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    version = _company_row(A)["data_version"]
    assert import_company.main(["--all"]) == 1
    out = capsys.readouterr().out
    assert "ep-example.neon.tech" in out and "bridgesoft" in out and "작업 내용" in out
    assert _company_row(A)["data_version"] == version


# ---- 캐시 ------------------------------------------------------------------------

def _policy_content(company_id, policy_id):
    doc = storage.get_company_document(company_id, "policies")
    return next(p["content"] for p in doc["policies"] if p["id"] == policy_id)


def _expire_cache(company_id):
    storage._companies_cache[company_id]["checked_at"] -= storage.STATIC_CACHE_TTL_SECONDS + 1


@pytest.mark.reimport
def test_cache_refreshes_only_when_data_version_changes():
    original = _policy_content(A, "BS-103")

    def change(conn, bump):
        conn.execute(update(storage.policies)
                     .where(storage.policies.c.company_id == A, storage.policies.c.policy_id == "BS-103")
                     .values(content="변경된 내용"))
        if bump:
            conn.execute(update(storage.companies).where(storage.companies.c.company_id == A)
                         .values(data_version=storage.companies.c.data_version + 1))

    storage._run_db(lambda conn: change(conn, bump=False), write=True)
    assert _policy_content(A, "BS-103") == original  # 60초 안: DB를 다시 보지 않음
    _expire_cache(A)
    assert _policy_content(A, "BS-103") == original  # data_version이 그대로면 재사용

    storage._run_db(lambda conn: change(conn, bump=True), write=True)
    assert _policy_content(A, "BS-103") == original
    _expire_cache(A)
    assert _policy_content(A, "BS-103") == "변경된 내용"


def test_tickets_are_never_cached():
    storage.get_company_document(A, "tickets")
    s.ui_confirm_ticket(A, "EMP001", "Slack", 1, request_type="계정")
    assert storage.get_company_document(A, "tickets")["next_sequence"] == D.baseline_next_sequence(A) + 1
    assert len(storage.get_tickets(A, "EMP001")) == len(
        [t for t in storage.get_company_document(A, "tickets")["tickets"] if t["employee_id"] == "EMP001"]
    )


# ---- 동시 발급 -------------------------------------------------------------------

@pytest.mark.pg
def test_concurrent_ticket_creation_has_unique_numbers():
    results, errors = [], []

    def worker(i):
        try:
            results.append(s.ui_confirm_ticket(A, f"EMP{(i % 5) + 2:03d}", f"동시계정{i}", 1, request_type="계정"))
        except Exception as exc:  # pragma: no cover - 실패 시 원인 표시
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(r["code"] == "CREATED" for r in results), results
    ids = [r["data"]["ticket"]["ticket_id"] for r in results]
    assert len(set(ids)) == 10
    assert _company_row(A)["next_ticket_sequence"] == D.baseline_next_sequence(A) + 10


@pytest.mark.pg
def test_duplicate_within_ten_seconds_is_ignored():
    first = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)
    second = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)
    assert first["code"] == "CREATED"
    assert second["code"] == "DUPLICATE_IGNORED"
    assert second["data"]["ticket"]["ticket_id"] == first["data"]["ticket"]["ticket_id"]
    assert _count(storage.tickets, A) == D.baseline_ticket_count(A) + 1


# ---- 로그·보안 --------------------------------------------------------------------

def test_roundtrips_logged_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="officegenie.storage"):
        s.search_policy("점심 식대 한도", A, "EMP001")
    assert any("DB roundtrips tool=search_policy count=" in r.getMessage() for r in caplog.records)


@pytest.mark.pg
def test_unreachable_db_returns_server_unavailable_without_leaking(fresh_db, caplog):
    secret_user, secret_pw = "leakcheckuser", "leakcheck-pw-9813"
    bad_url = f"postgresql://{secret_user}:{secret_pw}@127.0.0.1:1/officegenie"
    storage.configure_database(bad_url)
    try:
        with caplog.at_level(logging.DEBUG):
            r = s.search_policy("점심 식대 한도", A, "EMP001")
        assert r["code"] == "SERVER_UNAVAILABLE"
        assert set(r) == {"ok", "code", "message", "data"}
        text = caplog.text + json.dumps(r, ensure_ascii=False)
        assert secret_pw not in text and secret_user not in text and "127.0.0.1:1" not in text
        # 첫 연결 실패 후 1회 재시도
        attempts = [m for m in caplog.messages if "DB connection failed attempt=" in m]
        assert attempts[:2] == ["DB connection failed attempt=1 (OperationalError)",
                                "DB connection failed attempt=2 (OperationalError)"]
    finally:
        storage.configure_database(fresh_db["url"], schema=fresh_db["schema"])


@pytest.mark.pg
def test_database_url_normalization_and_guards(monkeypatch):
    url = storage.normalize_database_url("postgresql://u:p@ep-x-pooler.example.neon.tech/db")
    assert url.drivername == "postgresql+psycopg" and url.query["sslmode"] == "require"
    assert "p@" not in url.render_as_string(hide_password=True)
    assert storage.normalize_database_url("sqlite:///x.db").drivername == "sqlite"
    with pytest.raises(ValueError) as err:
        storage.normalize_database_url("mysql://u:secretpw@h/db")
    assert "secretpw" not in str(err.value)

    # 테스트 모드에서 테스트 주소가 DATABASE_URL과 같으면 거부
    monkeypatch.setenv("OFFICEGENIE_TEST_DATABASE_URL", "postgresql://u:p@h.example/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h.example/db")
    with pytest.raises(RuntimeError):
        storage.database_url_from_environment()


def test_default_sqlite_when_database_url_missing(monkeypatch, caplog):
    monkeypatch.delenv("OFFICEGENIE_TESTING")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(storage, "load_dotenv", lambda *a, **k: False)
    url = storage.database_url_from_environment()
    assert url.get_backend_name() == "sqlite" and url.database.endswith("officegenie.db")


def test_old_reset_option_points_to_importer():
    done = subprocess.run([sys.executable, str(ROOT / "mcp_server.py"), "--reset-tickets"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert "import_company.py --all --reset-tickets" in done.stderr
