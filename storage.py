"""SQLAlchemy Core storage for OfficeGenie.

Company JSON files are importer inputs only. Runtime callers use this module for
all company data and tickets. No password fields are stored in ``employees``.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import hmac
import secrets
import contextvars
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError


BASE_DIR = Path(__file__).resolve().parent
def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# 관리자 반영 후 다른 서버 프로세스가 새 data_version을 확인하는 주기(초).
STATIC_CACHE_TTL_SECONDS = _env_int("OFFICEGENIE_CACHE_TTL", 60)
CONNECTION_TIMEOUT_SECONDS = 10
KST = timezone(timedelta(hours=9))
log = logging.getLogger("officegenie.storage")

if os.environ.get("OFFICEGENIE_TESTING") != "1":
    load_dotenv(BASE_DIR / ".env")

metadata = MetaData()

companies = Table(
    "companies",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("company_name", String(300), nullable=False),
    Column("data_version", Integer, nullable=False, default=1),
    Column("next_ticket_sequence", Integer, nullable=False, default=1),
    Column(
        "ticket_id_format", String(100), nullable=False, default="REQ-{sequence:03d}"
    ),
    Column("company_metadata", JSON, nullable=False, default=dict),
    # 사원·관리자가 로그인할 때 입력하는 공개 코드. company_id는 내부 키로 유지한다.
    Column("company_code", String(6)),
)
Index("uq_companies_company_code", companies.c.company_code, unique=True)

employees = Table(
    "employees",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("employee_id", String(100), primary_key=True),
    Column("name", String(300), nullable=False),
    Column("department", String(300), nullable=False),
    Column("position", String(200)),
    Column("job_title", String(300)),
    Column("employment_status", String(100)),
    Column("onboarding_week", Integer),
    Column("work_email", String(320)),
    Column("annual_leave", JSON, nullable=False, default=dict),
    Column("employee_order", Integer, nullable=False),
    Column("extra", JSON, nullable=False, default=dict),
)

policies = Table(
    "policies",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("policy_id", String(100), primary_key=True),
    Column("category", String(300), nullable=False),
    Column("article", String(200), nullable=False),
    Column("title", String(500), nullable=False),
    Column("content", String, nullable=False),
    Column("source", String(500)),
    Column("has_template_blanks", Boolean),
    Column("keywords", JSON, nullable=False, default=list),
    Column("authoring_notes", JSON, nullable=False, default=list),
    Column("policy_type", String(100)),
    Column("is_demo_policy", Boolean),
    Column("simulation_only", Boolean),
    Column("template_original_content", String),
    Column("policy_order", Integer, nullable=False),
    Column("extra", JSON, nullable=False, default=dict),
)

search_config = Table(
    "search_config",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("preferred_policy_type", String(100)),
    Column("fallback_policy_type", String(100)),
    Column("fallback_disclaimer", String),
    Column("lookup_rule", String),
    Column("personal_leave_note", String),
    Column("extra", JSON, nullable=False, default=dict),
)

locations = Table(
    "locations",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("location_id", String(100), primary_key=True),
    Column("name", String(300), nullable=False),
    Column("aliases", JSON, nullable=False, default=list),
    Column("building", String(300), nullable=False),
    Column("floor", Integer, nullable=False),
    Column("zone", String(300), nullable=False),
    Column("guide", String),
    Column("access", String(500)),
    Column("capacity", Integer),
    Column("location_order", Integer, nullable=False),
    Column("extra", JSON, nullable=False, default=dict),
)

unregistered_places = Table(
    "unregistered_places",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("place", String(500), primary_key=True),
    Column("place_order", Integer, nullable=False),
)

handovers = Table(
    "handovers",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("handover_id", String(100), primary_key=True),
    Column("department", String(300), nullable=False),
    Column("title", String(500), nullable=False),
    Column("last_updated", String(80)),
    Column("summary", JSON, nullable=False, default=dict),
    Column("notes", JSON, nullable=False, default=list),
    Column("access", String(500)),
    Column("handover_order", Integer, nullable=False),
    Column("extra", JSON, nullable=False, default=dict),
)

equipment_rules = Table(
    "equipment_rules",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("item", String(300), primary_key=True),
    Column("aliases", JSON, nullable=False, default=list),
    Column("max_quantity_per_employee", Integer),
    Column("policy_reference", String(500)),
    Column("quantity_limit_defined", Boolean),
    Column("equipment_order", Integer, nullable=False),
    Column("extra", JSON, nullable=False, default=dict),
)

ticket_rules = Table(
    "ticket_rules",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("ticket_id_format", String(100), nullable=False),
    Column("status_values", JSON, nullable=False, default=list),
    Column("creation_rule", JSON, nullable=False, default=dict),
    Column("unknown_equipment_message", String),
    Column("excess_quantity_message", String),
    Column("extra", JSON, nullable=False, default=dict),
)

tickets = Table(
    "tickets",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("ticket_id", String(100), primary_key=True),
    Column("employee_id", String(100), nullable=False, index=True),
    Column("type", String(100), nullable=False),
    Column("item", String(500), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("purpose", String),
    Column("status", String(100), nullable=False),
    Column("created_at", String(100), nullable=False),
    Column("ticket_order", Integer, nullable=False),
    Column("extra", JSON, nullable=False, default=dict),
    # 관리자 상태 변경 이력 [{at, by, from, to, note}]
    Column("history", JSON),
)

announcements = Table(
    "announcements",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("announcement_id", String(32), primary_key=True),
    Column("title", String(300), nullable=False),
    Column("body", String, nullable=False),
    Column("is_public", Boolean, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("published_at", String(40)),
)

accounts = Table(
    "accounts",
    metadata,
    Column("company_id", String(100), primary_key=True),
    Column("account_id", String(100), primary_key=True),
    Column("role", String(20), nullable=False),
    Column("password_hash", String(300), nullable=False),
    Column("salt", String(100), nullable=False),
    Column("must_change", Boolean, nullable=False, default=False),
    Column("failed_count", Integer, nullable=False, default=0),
    Column("locked_until", String(40)),
    # 사원 JSON에서 빠진 계정. 삭제하지 않고 로그인만 막는다(재입사 대비).
    Column("disabled", Boolean, nullable=False, default=False),
    Column("created_at", String(40), nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", String(100), nullable=False, index=True),
    Column("at", String(40), nullable=False),
    Column("actor", String(100), nullable=False),
    Column("action", String(100), nullable=False),
    Column("target", String(300)),
    Column("detail", JSON, nullable=False, default=dict),
)

# 새로고침해도 로그인을 유지하는 서버 세션. 브라우저 쿠키에는 무작위 토큰만 두고,
# DB에는 그 SHA-256 해시만 저장한다. 신원·역할은 매번 accounts에서 다시 확인한다.
login_sessions = Table(
    "login_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("company_id", String(100), nullable=False, index=True),
    Column("account_id", String(100), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("last_seen_at", String(40), nullable=False),
    Column("expires_at", String(40), nullable=False),
)

_STATIC_TABLES = (
    policies,
    employees,
    search_config,
    locations,
    unregistered_places,
    handovers,
    equipment_rules,
    ticket_rules,
)
_CLEAR_ORDER = tuple(reversed(_STATIC_TABLES))

_engine_lock = threading.RLock()
_cache_lock = threading.RLock()
_engine: Engine | None = None
_engine_url: URL | None = None
_engine_schema: str | None = None
_companies_cache: dict[str, dict[str, Any]] = {}
_company_list_cache: tuple[float, list[dict[str, Any]]] | None = None
_roundtrip_count: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "officegenie_db_roundtrip_count", default=None
)


class VersionConflict(RuntimeError):
    """The company data changed after the caller read it (optimistic lock)."""


class StorageUnavailable(RuntimeError):
    """Sanitized database connection/operation failure."""

    def __init__(self) -> None:
        super().__init__("사내 데이터베이스 연결이 일시적으로 원활하지 않습니다.")


def normalize_database_url(database_url: str) -> URL:
    """Accept Neon postgresql:// URLs and select the psycopg 3 driver."""
    try:
        url = make_url(database_url)
    except Exception:
        raise ValueError("DATABASE_URL 형식이 올바르지 않습니다.") from None

    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    elif url.drivername == "postgresql+psycopg2":
        url = url.set(drivername="postgresql+psycopg")

    if url.drivername == "postgresql+psycopg":
        query = dict(url.query)
        local_hosts = {None, "", "localhost", "127.0.0.1", "::1"}
        if url.host not in local_hosts:
            # Neon 등 원격 PostgreSQL은 항상 TLS를 요구한다.
            query["sslmode"] = "require"
        url = url.set(query=query)
    elif url.get_backend_name() != "sqlite":
        raise ValueError("DATABASE_URL은 PostgreSQL 또는 SQLite여야 합니다.")
    return url


DEFAULT_SQLITE_URL = f"sqlite:///{BASE_DIR / 'officegenie.db'}"


def database_url_from_environment() -> URL:
    if os.environ.get("OFFICEGENIE_TESTING") == "1":
        # 테스트는 공유 DB(DATABASE_URL)를 절대 쓰지 않는다.
        value = os.environ.get("OFFICEGENIE_TEST_DATABASE_URL")
        if not value:
            raise RuntimeError("테스트 전용 DATABASE URL이 설정되지 않았습니다.")
        shared = os.environ.get("DATABASE_URL")
        if shared and database_url_matches(value, shared):
            raise RuntimeError("테스트 DB 주소가 DATABASE_URL과 같아 중단합니다.")
        return normalize_database_url(value)

    load_dotenv(BASE_DIR / ".env")
    value = os.environ.get("DATABASE_URL", "").strip()
    if value:
        return normalize_database_url(value)
    return normalize_database_url(DEFAULT_SQLITE_URL)


def _build_engine(url: URL, schema: str | None = None) -> Engine:
    connect_args: dict[str, Any] = {}
    if url.get_backend_name() == "sqlite":
        connect_args.update(timeout=CONNECTION_TIMEOUT_SECONDS, check_same_thread=False)
    else:
        connect_args["connect_timeout"] = CONNECTION_TIMEOUT_SECONDS
        # Neon 풀러(PgBouncer 트랜잭션 모드)와 호환되도록 서버측 prepared statement를 끈다.
        connect_args["prepare_threshold"] = None
        if schema and not re.fullmatch(r"officegenie_test_[a-f0-9]+", schema):
            raise ValueError("테스트 DB 스키마 이름이 올바르지 않습니다.")

    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    @event.listens_for(engine, "before_cursor_execute")
    def _count_roundtrip(_conn, _cursor, _statement, _parameters, _context, _many):
        count = _roundtrip_count.get()
        if count is not None:
            _roundtrip_count.set(count + 1)

    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(
                    f"PRAGMA busy_timeout={CONNECTION_TIMEOUT_SECONDS * 1000}"
                )
            finally:
                cursor.close()

    if schema and url.get_backend_name() == "postgresql":
        # 풀러에서는 search_path가 유지되지 않으므로 SQL에 스키마 이름을 직접 붙인다.
        return engine.execution_options(schema_translate_map={None: schema})
    return engine


def get_engine() -> Engine:
    global _engine, _engine_url, _engine_schema
    with _engine_lock:
        if _engine is None:
            try:
                url = database_url_from_environment()
                _engine = _build_engine(url, os.environ.get("OFFICEGENIE_TEST_SCHEMA"))
            except Exception as exc:
                # 예외 메시지에는 주소가 들어 있을 수 있으므로 종류만 남긴다.
                log.error("DB 엔진 설정 실패 (%s)", type(exc).__name__)
                raise StorageUnavailable() from None
            _engine_url = url
            _engine_schema = os.environ.get("OFFICEGENIE_TEST_SCHEMA")
            if (
                url.get_backend_name() == "sqlite"
                and os.environ.get("OFFICEGENIE_TESTING") != "1"
            ):
                log.info("로컬 SQLite 사용 중")
        return _engine


def configure_database(database_url: str, *, schema: str | None = None) -> Engine:
    """Replace the process engine; used by isolated tests and explicit tooling."""
    global _engine, _engine_url, _engine_schema, _company_list_cache
    url = normalize_database_url(database_url)
    with _engine_lock:
        old = _engine
        _engine = _build_engine(url, schema)
        _engine_url = url
        _engine_schema = schema
        with _cache_lock:
            _companies_cache.clear()
            _company_list_cache = None
        if old is not None:
            old.dispose()
        return _engine


def database_is_sqlite() -> bool:
    return get_engine().dialect.name == "sqlite"


def database_host() -> str:
    url = _engine_url or database_url_from_environment()
    if url.get_backend_name() == "sqlite":
        return "SQLite (로컬 파일)"
    return url.host or "알 수 없음"


def database_url_matches(left: str, right: str) -> bool:
    """Compare URLs without rendering or logging their credentials."""
    try:
        a = normalize_database_url(left)
        b = normalize_database_url(right)
    except ValueError:
        return left.strip() == right.strip()
    return a == b


def _safe_connection(
    conn: Connection, operation: Callable[[Connection], Any], *, write: bool
) -> Any:
    try:
        result = operation(conn)
        if write:
            conn.commit()
        return result
    except SQLAlchemyError as exc:
        if conn.in_transaction():
            conn.rollback()
        log.warning("DB operation failed (%s)", type(exc).__name__)
        raise StorageUnavailable() from None
    except Exception:
        if conn.in_transaction():
            conn.rollback()
        raise


def _run_db(
    operation: Callable[[Connection], Any],
    *,
    write: bool = False,
    immediate: bool = False,
) -> Any:
    """Use one connection and retry once only while connecting/beginning."""
    engine = get_engine()
    for attempt in range(2):
        conn: Connection | None = None
        try:
            conn = engine.connect()
            if write:
                if engine.dialect.name == "sqlite" and immediate:
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    conn.begin()
        except SQLAlchemyError as exc:
            if conn is not None:
                conn.close()
            log.warning(
                "DB connection failed attempt=%d (%s)", attempt + 1, type(exc).__name__
            )
            if attempt == 0:
                time.sleep(0.2)
                continue
            raise StorageUnavailable() from None

        try:
            return _safe_connection(conn, operation, write=write)
        finally:
            conn.close()
    raise StorageUnavailable()


# 기존 DB에 나중에 추가된 컬럼. init_db()가 없으면 ALTER TABLE로 추가한다.
_ADDED_COLUMNS = ((companies, "company_code"), (tickets, "history"))


def _qualified_name(conn: Connection, table: Table) -> str:
    preparer = conn.dialect.identifier_preparer
    name = preparer.quote(table.name)
    if _engine_schema and conn.dialect.name == "postgresql":
        return f"{preparer.quote_schema(_engine_schema)}.{name}"
    return name


def _migrate_schema(conn: Connection) -> None:
    inspector = inspect(conn)
    schema = _engine_schema if conn.dialect.name == "postgresql" else None
    for table, column_name in _ADDED_COLUMNS:
        existing = {
            column["name"] for column in inspector.get_columns(table.name, schema=schema)
        }
        if column_name in existing:
            continue
        column = table.c[column_name]
        column_type = column.type.compile(dialect=conn.dialect)
        conn.execute(
            text(
                f"ALTER TABLE {_qualified_name(conn, table)} "
                f"ADD COLUMN {conn.dialect.identifier_preparer.quote(column_name)} "
                f"{column_type}"
            )
        )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_company_code "
            f"ON {_qualified_name(conn, companies)} (company_code)"
        )
    )
    for (company_id,) in conn.execute(
        select(companies.c.company_id).where(companies.c.company_code.is_(None))
    ).all():
        conn.execute(
            update(companies)
            .where(companies.c.company_id == company_id)
            .values(company_code=_new_company_code(conn))
        )


def init_db() -> None:
    """Create missing tables/columns without altering existing company data."""

    def operation(conn: Connection) -> None:
        metadata.create_all(conn)
        _migrate_schema(conn)

    _run_db(operation, write=True)
    invalidate_company_cache()


def has_companies() -> bool:
    return bool(
        _run_db(
            lambda conn: conn.execute(select(companies.c.company_id).limit(1)).first()
        )
    )


@contextlib.contextmanager
def track_database_roundtrips(label: str) -> Iterator[None]:
    """Emit a DEBUG count of SQL statements for one MCP request/tool call."""
    existing = _roundtrip_count.get()
    if existing is not None:
        yield
        return
    token = _roundtrip_count.set(0)
    try:
        yield
    finally:
        count = _roundtrip_count.get() or 0
        _roundtrip_count.reset(token)
        log.debug("DB roundtrips tool=%s count=%d", label, count)


def _safe_json_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_json_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _row_extra(record: dict, known: set[str]) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in known and key != "password"
    }


def _ordered_rows(
    conn: Connection, table: Table, company_id: str, order_column: Column
) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            select(table).where(table.c.company_id == company_id).order_by(order_column)
        ).mappings()
    ]


def _document_metadata(company_row: dict) -> dict[str, dict]:
    data = _safe_json_dict(company_row.get("company_metadata"))
    documents = _safe_json_dict(data.get("documents"))
    return {name: _safe_json_dict(value) for name, value in documents.items()}


def _record_employee(row: dict) -> dict:
    result = _safe_json_dict(row.get("extra")).copy()
    result.update(
        {
            "employee_id": row["employee_id"],
            "name": row["name"],
            "department": row["department"],
            "annual_leave": _safe_json_dict(row.get("annual_leave")),
        }
    )
    for key in (
        "position",
        "job_title",
        "employment_status",
        "onboarding_week",
        "work_email",
    ):
        if row.get(key) is not None:
            result[key] = row[key]
    result.pop("password", None)
    return result


def _record_policy(row: dict) -> dict:
    result = _safe_json_dict(row.get("extra")).copy()
    result.update(
        {
            "id": row["policy_id"],
            "category": row["category"],
            "article": row["article"],
            "title": row["title"],
            "content": row["content"],
            "keywords": _safe_json_list(row.get("keywords")),
            "authoring_notes": _safe_json_list(row.get("authoring_notes")),
        }
    )
    for key in (
        "source",
        "has_template_blanks",
        "policy_type",
        "is_demo_policy",
        "simulation_only",
        "template_original_content",
    ):
        if row.get(key) is not None:
            result[key] = row[key]
    return result


def _record_location(row: dict) -> dict:
    result = _safe_json_dict(row.get("extra")).copy()
    result.update(
        {
            "location_id": row["location_id"],
            "name": row["name"],
            "aliases": _safe_json_list(row.get("aliases")),
            "building": row["building"],
            "floor": row["floor"],
            "zone": row["zone"],
        }
    )
    for key in ("guide", "access", "capacity"):
        if row.get(key) is not None:
            result[key] = row[key]
    return result


def _record_handover(row: dict) -> dict:
    result = _safe_json_dict(row.get("extra")).copy()
    result.update(
        {
            "handover_id": row["handover_id"],
            "department": row["department"],
            "title": row["title"],
            "summary": _safe_json_dict(row.get("summary")),
            "notes": _safe_json_list(row.get("notes")),
        }
    )
    for key in ("last_updated", "access"):
        if row.get(key) is not None:
            result[key] = row[key]
    return result


def _record_equipment_rule(row: dict) -> dict:
    result = _safe_json_dict(row.get("extra")).copy()
    result.update(
        {
            "item": row["item"],
            "aliases": _safe_json_list(row.get("aliases")),
            "quantity_limit_defined": bool(row.get("quantity_limit_defined")),
        }
    )
    for key in ("max_quantity_per_employee", "policy_reference"):
        if row.get(key) is not None:
            result[key] = row[key]
    return result


def _record_ticket(row: dict) -> dict:
    result = _safe_json_dict(row.get("extra")).copy()
    result.update(
        {
            "ticket_id": row["ticket_id"],
            "employee_id": row["employee_id"],
            "type": row["type"],
            "item": row["item"],
            "quantity": row["quantity"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
    )
    if row.get("purpose") is not None:
        result["purpose"] = row["purpose"]
    return result


def _load_static_company(conn: Connection, company_id: str) -> dict[str, Any] | None:
    company_row = (
        conn.execute(select(companies).where(companies.c.company_id == company_id))
        .mappings()
        .first()
    )
    if company_row is None:
        return None
    company = dict(company_row)
    document_meta = _document_metadata(company)

    policy_rows = _ordered_rows(conn, policies, company_id, policies.c.policy_order)
    employee_rows = _ordered_rows(
        conn, employees, company_id, employees.c.employee_order
    )
    location_rows = _ordered_rows(
        conn, locations, company_id, locations.c.location_order
    )
    place_rows = _ordered_rows(
        conn, unregistered_places, company_id, unregistered_places.c.place_order
    )
    handover_rows = _ordered_rows(
        conn, handovers, company_id, handovers.c.handover_order
    )
    equipment_rows = _ordered_rows(
        conn, equipment_rules, company_id, equipment_rules.c.equipment_order
    )

    search_row = (
        conn.execute(
            select(search_config).where(search_config.c.company_id == company_id)
        )
        .mappings()
        .first()
    )
    search = {}
    if search_row:
        search = _safe_json_dict(search_row.get("extra")).copy()
        for key in (
            "preferred_policy_type",
            "fallback_policy_type",
            "fallback_disclaimer",
            "lookup_rule",
            "personal_leave_note",
        ):
            if search_row.get(key) is not None:
                search[key] = search_row[key]

    rules_row = (
        conn.execute(
            select(ticket_rules).where(ticket_rules.c.company_id == company_id)
        )
        .mappings()
        .first()
    )
    rules = {}
    if rules_row:
        rules = _safe_json_dict(rules_row.get("extra")).copy()
        rules.update(
            {
                "status_values": _safe_json_list(rules_row.get("status_values")),
                "creation_rule": _safe_json_dict(rules_row.get("creation_rule")),
            }
        )
        if rules_row.get("ticket_id_format"):
            rules["ticket_id_format"] = rules_row["ticket_id_format"]

    name = company["company_name"]
    docs = {
        "policies": document_meta.get("policies", {})
        | {
            "company_id": company_id,
            "company_name": name,
            "policies": [_record_policy(r) for r in policy_rows],
            "search_policy_config": search,
        },
        "employees": document_meta.get("employees", {})
        | {
            "company_id": company_id,
            "company_name": name,
            "employees": [_record_employee(r) for r in employee_rows],
        },
        "locations": document_meta.get("locations", {})
        | {
            "company_id": company_id,
            "company_name": name,
            "locations": [_record_location(r) for r in location_rows],
            "explicitly_not_registered": [r["place"] for r in place_rows],
        },
        "handovers": document_meta.get("handovers", {})
        | {
            "company_id": company_id,
            "company_name": name,
            "handovers": [_record_handover(r) for r in handover_rows],
        },
        "tickets": document_meta.get("tickets", {})
        | {
            "company_id": company_id,
            "company_name": name,
            "allowed_equipment": [_record_equipment_rule(r) for r in equipment_rows],
            "creation_rule": rules.get("creation_rule", {}),
            "status_values": rules.get("status_values", []),
            "ticket_id_format": rules.get(
                "ticket_id_format", company["ticket_id_format"]
            ),
            "next_sequence": company["next_ticket_sequence"],
        },
    }
    return {
        "data_version": company["data_version"],
        "documents": docs,
        "company": company,
    }


def _company_version(company_id: str) -> int | None:
    return _run_db(
        lambda conn: conn.execute(
            select(companies.c.data_version).where(companies.c.company_id == company_id)
        ).scalar_one_or_none()
    )


def _get_static_company(company_id: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _cache_lock:
        cached = _companies_cache.get(company_id)
        if cached and now - cached["checked_at"] < STATIC_CACHE_TTL_SECONDS:
            return cached

        version = _company_version(company_id)
        if version is None:
            _companies_cache.pop(company_id, None)
            return None
        if cached and cached["data_version"] == version:
            cached["checked_at"] = now
            return cached

        loaded = _run_db(lambda conn: _load_static_company(conn, company_id))
        if loaded is None:
            _companies_cache.pop(company_id, None)
            return None
        loaded["checked_at"] = time.monotonic()
        _companies_cache[company_id] = loaded
        return loaded


def list_companies() -> list[dict[str, Any]]:
    global _company_list_cache
    now = time.monotonic()
    with _cache_lock:
        if (
            _company_list_cache
            and now - _company_list_cache[0] < STATIC_CACHE_TTL_SECONDS
        ):
            return copy.deepcopy(_company_list_cache[1])

        rows = _run_db(
            lambda conn: [
                dict(row)
                for row in conn.execute(
                    select(
                        companies.c.company_id,
                        companies.c.company_name,
                        companies.c.data_version,
                    ).order_by(companies.c.company_id)
                ).mappings()
            ]
        )
        present = {row["company_id"] for row in rows}
        for company_id in set(_companies_cache) - present:
            _companies_cache.pop(company_id, None)
        for row in rows:
            cached = _companies_cache.get(row["company_id"])
            if cached and cached["data_version"] != row["data_version"]:
                _companies_cache.pop(row["company_id"], None)
        result = [
            {"company_id": row["company_id"], "company_name": row["company_name"]}
            for row in rows
        ]
        _company_list_cache = (time.monotonic(), result)
        return copy.deepcopy(result)


def list_company_ids() -> list[str]:
    return [row["company_id"] for row in list_companies()]


def get_company_document(company_id: str, document_name: str) -> dict:
    normalized = document_name.removesuffix(".json")
    if normalized not in {
        "companies",
        "policies",
        "employees",
        "locations",
        "handovers",
        "tickets",
    }:
        raise KeyError(document_name)
    if normalized == "companies":
        return {"companies": list_companies()}

    company = _get_static_company(company_id)
    if company is None:
        raise KeyError(company_id)
    if normalized == "tickets":
        # 티켓과 발급 번호는 캐시하지 않고 매번 DB에서 읽는다.
        def read_tickets(conn: Connection) -> tuple[list[dict], int | None]:
            rows = [
                dict(row)
                for row in conn.execute(
                    select(tickets)
                    .where(tickets.c.company_id == company_id)
                    .order_by(tickets.c.ticket_order)
                ).mappings()
            ]
            sequence = conn.execute(
                select(companies.c.next_ticket_sequence).where(
                    companies.c.company_id == company_id
                )
            ).scalar_one_or_none()
            return rows, sequence

        ticket_rows, next_sequence = _run_db(read_tickets)
        result = copy.deepcopy(company["documents"]["tickets"])
        result["tickets"] = [_record_ticket(row) for row in ticket_rows]
        result["next_sequence"] = next_sequence
        return result
    return copy.deepcopy(company["documents"][normalized])


def get_employee(company_id: str, employee_id: str) -> dict | None:
    data = get_company_document(company_id, "employees")
    return next(
        (row for row in data["employees"] if row.get("employee_id") == employee_id),
        None,
    )


def get_tickets(company_id: str, employee_id: str | None = None) -> list[dict]:
    statement = select(tickets).where(tickets.c.company_id == company_id)
    if employee_id is not None:
        statement = statement.where(tickets.c.employee_id == employee_id)
    statement = statement.order_by(tickets.c.ticket_order)
    rows = _run_db(
        lambda conn: [dict(row) for row in conn.execute(statement).mappings()]
    )
    return [_record_ticket(row) for row in rows]


def list_my_tickets(company_id: str, employee_id: str) -> list[dict]:
    """본인 신청만 최신순. 상태 변경 이력(처리자 제외)과 최근 변경 시각을 함께 준다."""
    statement = (
        select(tickets)
        .where(and_(tickets.c.company_id == company_id, tickets.c.employee_id == employee_id))
        .order_by(tickets.c.ticket_order.desc())
    )
    rows = _run_db(lambda conn: [dict(row) for row in conn.execute(statement).mappings()])
    result = []
    for row in rows:
        record = _record_ticket(row)
        history = [
            {
                "at": entry.get("at"),
                "from": entry.get("from"),
                "to": entry.get("to"),
                "note": entry.get("note") or "",
            }
            for entry in (row.get("history") or [])
            if isinstance(entry, dict)
        ]
        result.append(
            {
                "ticket_id": record["ticket_id"],
                "type": record["type"],
                "item": record["item"],
                "quantity": record["quantity"],
                "purpose": record.get("purpose") or "",
                "status": record["status"],
                "created_at": record["created_at"],
                "updated_at": history[-1]["at"] if history else record["created_at"],
                "scheduled_date": record.get("scheduled_date"),
                "history": history,
            }
        )
    return result


def _json_extra(record: dict, known: set[str]) -> dict:
    extra = _row_extra(record, known)
    return {key: value for key, value in extra.items() if key != "password"}


def _insert_rows(conn: Connection, table: Table, rows: list[dict]) -> None:
    if rows:
        conn.execute(insert(table), rows)


def _ticket_sequence_next(document: dict, rows: list[dict]) -> int:
    source_next = document.get("next_sequence", 1)
    try:
        source_next = max(1, int(source_next))
    except (TypeError, ValueError):
        source_next = 1
    maximum = 0
    for ticket in rows:
        match = re.search(r"(\d+)$", str(ticket.get("ticket_id", "")))
        if match:
            maximum = max(maximum, int(match.group(1)))
    return max(source_next, maximum + 1)


def _make_ticket_rows(company_id: str, values: list[dict]) -> list[dict]:
    result = []
    for index, record in enumerate(values):
        known = {
            "ticket_id",
            "employee_id",
            "type",
            "item",
            "quantity",
            "purpose",
            "status",
            "created_at",
        }
        result.append(
            {
                "company_id": company_id,
                "ticket_id": str(record["ticket_id"]),
                "employee_id": str(record["employee_id"]),
                "type": str(record["type"]),
                "item": str(record["item"]),
                "quantity": int(record.get("quantity", 1)),
                "purpose": record.get("purpose"),
                "status": str(record["status"]),
                "created_at": str(record["created_at"]),
                "ticket_order": index,
                "extra": _json_extra(record, known),
            }
        )
    return result


def import_company_documents(
    company_id: str,
    documents: dict[str, dict],
    *,
    seed_tickets_document: dict | None = None,
    reset_tickets: bool = False,
    actor: str = "operator",
    company_name: str | None = None,
    expected_version: int | None = None,
    preserve_tickets: bool = False,
    audit_action: str = "company_import",
    audit_detail: dict | None = None,
) -> dict[str, Any]:
    """Atomically replace static company datasets and optionally initialize/reset tickets."""
    required = {"policies", "employees", "locations", "handovers", "tickets"}
    if set(documents) != required:
        raise ValueError("회사 데이터 입력 문서 5종이 모두 필요합니다.")
    if reset_tickets and seed_tickets_document is None:
        raise ValueError("티켓 초기화에는 seed tickets 문서가 필요합니다.")

    source_ticket_doc = documents["tickets"]
    selected_ticket_doc = seed_tickets_document if reset_tickets else source_ticket_doc
    selected_ticket_rows = _make_ticket_rows(
        company_id, selected_ticket_doc.get("tickets", [])
    )
    company_name = str(
        company_name
        or documents["employees"].get("company_name")
        or documents["policies"].get("company_name")
        or documents["policies"].get("company")
        or company_id
    )
    ticket_format = str(
        selected_ticket_doc.get("ticket_id_format") or "REQ-{sequence:03d}"
    )
    if "{sequence" not in ticket_format:
        raise ValueError(
            "tickets.json의 ticket_id_format에 {sequence} 형식이 필요합니다."
        )

    collection_names = {
        "policies": {"policies"},
        "employees": {"employees"},
        "locations": {"locations", "explicitly_not_registered", "buildings"},
        "handovers": {"handovers"},
        "tickets": {
            "tickets",
            "allowed_equipment",
            "creation_rule",
            "status_values",
            "ticket_id_format",
            "next_sequence",
        },
    }
    document_metadata = {
        name: {
            key: value
            for key, value in doc.items()
            if key not in collection_names[name]
        }
        for name, doc in documents.items()
    }
    company_metadata = {"documents": document_metadata}

    def operation(conn: Connection) -> dict[str, Any]:
        existing = (
            conn.execute(
                select(companies)
                .where(companies.c.company_id == company_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if expected_version is not None and (
            existing is None or int(existing["data_version"]) != int(expected_version)
        ):
            raise VersionConflict()
        current_ticket_count = conn.execute(
            select(func.count())
            .select_from(tickets)
            .where(tickets.c.company_id == company_id)
        ).scalar_one()
        should_initialize_tickets = (
            reset_tickets or current_ticket_count == 0
        ) and not (preserve_tickets and existing)
        tickets_to_insert = selected_ticket_rows if should_initialize_tickets else []
        if should_initialize_tickets:
            # 이미 발급했던 번호는 다시 쓰지 않는다: seed/JSON의 next_sequence와
            # (기존 DB 티켓 + 새로 넣을 티켓)의 최대 번호 + 1 중 큰 값.
            previous_ids = [
                {"ticket_id": row[0]}
                for row in conn.execute(
                    select(tickets.c.ticket_id).where(tickets.c.company_id == company_id)
                )
            ]
            next_sequence = _ticket_sequence_next(
                selected_ticket_doc, tickets_to_insert + previous_ids
            )
            if reset_tickets:
                conn.execute(delete(tickets).where(tickets.c.company_id == company_id))
        else:
            next_sequence = existing["next_ticket_sequence"]
        new_version = int(existing["data_version"]) + 1 if existing else 1

        effective_format = (
            existing["ticket_id_format"] if preserve_tickets and existing else ticket_format
        )
        company_values = {
            "company_id": company_id,
            "company_name": company_name,
            "data_version": new_version,
            "next_ticket_sequence": next_sequence,
            "ticket_id_format": effective_format,
            "company_metadata": company_metadata,
        }
        company_code = existing.get("company_code") if existing else None
        if not company_code:
            # 처음 가져올 때만 만든다. 다시 가져와도 코드는 바뀌지 않는다.
            company_code = _new_company_code(conn)
        company_values["company_code"] = company_code
        if existing:
            conn.execute(
                update(companies)
                .where(companies.c.company_id == company_id)
                .values(**company_values)
            )
        else:
            conn.execute(insert(companies).values(**company_values))

        for table in _CLEAR_ORDER:
            conn.execute(delete(table).where(table.c.company_id == company_id))

        employee_known = {
            "employee_id",
            "name",
            "department",
            "position",
            "job_title",
            "employment_status",
            "onboarding_week",
            "work_email",
            "annual_leave",
            "password",
        }
        _insert_rows(
            conn,
            employees,
            [
                {
                    "company_id": company_id,
                    "employee_id": str(row["employee_id"]),
                    "name": str(row["name"]),
                    "department": str(row["department"]),
                    "position": row.get("position"),
                    "job_title": row.get("job_title"),
                    "employment_status": row.get("employment_status"),
                    "onboarding_week": row.get("onboarding_week"),
                    "work_email": row.get("work_email"),
                    "annual_leave": _safe_json_dict(row.get("annual_leave")),
                    "employee_order": index,
                    "extra": _json_extra(row, employee_known),
                }
                for index, row in enumerate(documents["employees"]["employees"])
            ],
        )

        policy_known = {
            "id",
            "category",
            "article",
            "title",
            "content",
            "source",
            "has_template_blanks",
            "keywords",
            "authoring_notes",
            "policy_type",
            "is_demo_policy",
            "simulation_only",
            "template_original_content",
            "company_id",
        }
        _insert_rows(
            conn,
            policies,
            [
                {
                    "company_id": company_id,
                    "policy_id": str(row["id"]),
                    "category": str(row["category"]),
                    "article": str(row["article"]),
                    "title": str(row["title"]),
                    "content": str(row["content"]),
                    "source": row.get("source"),
                    "has_template_blanks": row.get("has_template_blanks"),
                    "keywords": _safe_json_list(row.get("keywords")),
                    "authoring_notes": _safe_json_list(row.get("authoring_notes")),
                    "policy_type": row.get("policy_type"),
                    "is_demo_policy": row.get("is_demo_policy"),
                    "simulation_only": row.get("simulation_only"),
                    "template_original_content": row.get("template_original_content"),
                    "policy_order": index,
                    "extra": _json_extra(row, policy_known),
                }
                for index, row in enumerate(documents["policies"]["policies"])
            ],
        )

        config = _safe_json_dict(documents["policies"].get("search_policy_config"))
        config_known = {
            "preferred_policy_type",
            "fallback_policy_type",
            "fallback_disclaimer",
            "lookup_rule",
            "personal_leave_note",
        }
        _insert_rows(
            conn,
            search_config,
            [
                {
                    "company_id": company_id,
                    **{key: config.get(key) for key in config_known},
                    "extra": _json_extra(config, config_known),
                }
            ],
        )

        location_known = {
            "location_id",
            "name",
            "aliases",
            "building",
            "floor",
            "zone",
            "guide",
            "access",
            "capacity",
        }
        _insert_rows(
            conn,
            locations,
            [
                {
                    "company_id": company_id,
                    "location_id": str(row["location_id"]),
                    "name": str(row["name"]),
                    "aliases": _safe_json_list(row.get("aliases")),
                    "building": str(row["building"]),
                    "floor": int(row["floor"]),
                    "zone": str(row["zone"]),
                    "guide": row.get("guide"),
                    "access": row.get("access"),
                    "capacity": row.get("capacity"),
                    "location_order": index,
                    "extra": _json_extra(row, location_known),
                }
                for index, row in enumerate(documents["locations"]["locations"])
            ],
        )
        _insert_rows(
            conn,
            unregistered_places,
            [
                {"company_id": company_id, "place": str(place), "place_order": index}
                for index, place in enumerate(
                    documents["locations"].get("explicitly_not_registered", [])
                )
            ],
        )

        handover_known = {
            "handover_id",
            "department",
            "title",
            "last_updated",
            "summary",
            "notes",
            "access",
        }
        _insert_rows(
            conn,
            handovers,
            [
                {
                    "company_id": company_id,
                    "handover_id": str(row["handover_id"]),
                    "department": str(row["department"]),
                    "title": str(row["title"]),
                    "last_updated": row.get("last_updated"),
                    "summary": _safe_json_dict(row.get("summary")),
                    "notes": _safe_json_list(row.get("notes")),
                    "access": row.get("access"),
                    "handover_order": index,
                    "extra": _json_extra(row, handover_known),
                }
                for index, row in enumerate(documents["handovers"]["handovers"])
            ],
        )

        equipment_known = {
            "item",
            "aliases",
            "max_quantity_per_employee",
            "policy_reference",
            "quantity_limit_defined",
        }
        _insert_rows(
            conn,
            equipment_rules,
            [
                {
                    "company_id": company_id,
                    "item": str(row["item"]),
                    "aliases": _safe_json_list(row.get("aliases")),
                    "max_quantity_per_employee": row.get("max_quantity_per_employee"),
                    "policy_reference": row.get("policy_reference"),
                    "quantity_limit_defined": row.get("quantity_limit_defined"),
                    "equipment_order": index,
                    "extra": _json_extra(row, equipment_known),
                }
                for index, row in enumerate(
                    documents["tickets"].get("allowed_equipment", [])
                )
            ],
        )

        creation_rule = _safe_json_dict(documents["tickets"].get("creation_rule"))
        ticket_rule_known = {
            "ticket_id_format",
            "status_values",
            "creation_rule",
            "unknown_equipment_message",
            "excess_quantity_message",
            "allowed_equipment",
            "tickets",
            "next_sequence",
        }
        _insert_rows(
            conn,
            ticket_rules,
            [
                {
                    "company_id": company_id,
                    "ticket_id_format": effective_format,
                    "status_values": _safe_json_list(
                        documents["tickets"].get("status_values")
                    ),
                    "creation_rule": creation_rule,
                    "unknown_equipment_message": creation_rule.get(
                        "unknown_equipment_message"
                    ),
                    "excess_quantity_message": creation_rule.get(
                        "excess_quantity_message"
                    ),
                    "extra": _json_extra(documents["tickets"], ticket_rule_known),
                }
            ],
        )

        if should_initialize_tickets:
            _insert_rows(conn, tickets, tickets_to_insert)

        initial_passwords = _sync_employee_accounts(
            conn, company_id, documents["employees"]["employees"], actor
        )
        _audit(
            conn,
            company_id,
            actor,
            audit_action,
            None,
            {
                "data_version": new_version,
                "reset_tickets": bool(reset_tickets),
                **(audit_detail or {}),
            },
        )

        return {
            "company_id": company_id,
            "company_name": company_name,
            "company_code": company_code,
            "initial_passwords": initial_passwords,
            "data_version": new_version,
            "ticket_action": "reset"
            if reset_tickets
            else "initialized"
            if current_ticket_count == 0
            else "preserved",
            "ticket_count": len(tickets_to_insert)
            if should_initialize_tickets
            else int(current_ticket_count),
            "next_ticket_sequence": next_sequence,
        }

    result = _run_db(operation, write=True, immediate=True)
    invalidate_company_cache(company_id)
    return result


def invalidate_company_cache(company_id: str | None = None) -> None:
    global _company_list_cache
    with _cache_lock:
        if company_id is None:
            _companies_cache.clear()
        else:
            _companies_cache.pop(company_id, None)
        _company_list_cache = None


def _ticket_from_row(row: dict) -> dict:
    return _record_ticket(row)


def _parse_ticket_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def create_ticket_atomic(
    *,
    company_id: str,
    employee_id: str,
    request_type: str,
    item: str,
    quantity: int,
    purpose: str,
    initial_status: str,
    now: datetime,
    duplicate_window_seconds: int,
    validate_existing: Callable[[list[dict]], dict | None],
) -> dict:
    """Serialize duplicate checks, validation and sequence allocation in one transaction."""

    def operation(conn: Connection) -> dict:
        company_query = select(companies).where(companies.c.company_id == company_id)
        if conn.dialect.name == "postgresql":
            company_query = company_query.with_for_update()
        company = conn.execute(company_query).mappings().first()
        if company is None:
            raise StorageUnavailable()

        current_rows = [
            dict(row)
            for row in conn.execute(
                select(tickets)
                .where(
                    and_(
                        tickets.c.company_id == company_id,
                        tickets.c.employee_id == employee_id,
                    )
                )
                .order_by(tickets.c.ticket_order)
            ).mappings()
        ]
        existing = [_ticket_from_row(row) for row in current_rows]

        for existing_ticket in reversed(existing):
            if (
                existing_ticket.get("item") != item
                or int(existing_ticket.get("quantity", 1)) != quantity
                or existing_ticket.get("type") != request_type
            ):
                continue
            created_at = _parse_ticket_time(existing_ticket.get("created_at"))
            if (
                created_at is not None
                and (now - created_at).total_seconds() < duplicate_window_seconds
            ):
                return {"kind": "duplicate", "ticket": existing_ticket}
            break

        rejection = validate_existing(existing)
        if rejection is not None:
            return {"kind": "rejected", "result": rejection}

        sequence = max(1, int(company["next_ticket_sequence"]))
        ticket_format = str(company["ticket_id_format"])
        ticket_id = ticket_format.format(sequence=sequence)
        existing_ids = {
            row[0]
            for row in conn.execute(
                select(tickets.c.ticket_id).where(tickets.c.company_id == company_id)
            )
        }
        while ticket_id in existing_ids:
            sequence += 1
            ticket_id = ticket_format.format(sequence=sequence)

        ticket = {
            "company_id": company_id,
            "ticket_id": ticket_id,
            "employee_id": employee_id,
            "type": request_type,
            "item": item,
            "quantity": quantity,
            "purpose": purpose,
            "status": initial_status,
            "created_at": now.isoformat(timespec="seconds"),
            "ticket_order": int(
                conn.execute(
                    select(func.coalesce(func.max(tickets.c.ticket_order), -1)).where(
                        tickets.c.company_id == company_id
                    )
                ).scalar_one()
            )
            + 1,
            "extra": {},
        }
        conn.execute(insert(tickets).values(**ticket))
        conn.execute(
            update(companies)
            .where(companies.c.company_id == company_id)
            .values(next_ticket_sequence=sequence + 1)
        )
        return {"kind": "created", "ticket": _ticket_from_row(ticket)}

    return _run_db(operation, write=True, immediate=True)


def update_ticket_status(company_id: str, ticket_id: str, status: str) -> None:
    """Small storage operation used by tests/admin integrations; tickets stay uncached."""
    changed = _run_db(
        lambda conn: (
            conn.execute(
                update(tickets)
                .where(
                    and_(
                        tickets.c.company_id == company_id,
                        tickets.c.ticket_id == ticket_id,
                    )
                )
                .values(status=status)
            ).rowcount
        ),
        write=True,
        immediate=True,
    )
    if not changed:
        raise KeyError(ticket_id)


# ---------------------------------------------------------------------------
# 회사 코드 · 계정 · 감사 로그
# 비밀번호 원문·해시·솔트는 이 모듈 밖으로 돌려주지 않는다.
# ---------------------------------------------------------------------------

COMPANY_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 0/O/1/I/L 제외
COMPANY_CODE_LENGTH = 6
ADMIN_ACCOUNT_ID = "ADMIN"
ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin"
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 10
EMPLOYEE_MIN_PASSWORD = 4
ADMIN_MIN_PASSWORD = 8
INITIAL_PASSWORD_DIGITS = 4
# scrypt 비용. 운영 값은 고정(2**14)이며 환경변수로 바꿀 수 없다.
# pytest(OFFICEGENIE_TESTING=1)에서 OFFICEGENIE_TEST_FAST_HASH=1일 때만 최소 비용을 쓴다.
# 해시 문자열에 비용 값(n, r, p)을 함께 저장하므로, 검증은 항상 그 해시를 만든 비용으로 한다.
SCRYPT_N = 2**14
SCRYPT_N_TEST_FAST = 2
_SCRYPT_R = 8
_SCRYPT_P = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def generate_company_code() -> str:
    return "".join(
        secrets.choice(COMPANY_CODE_ALPHABET) for _ in range(COMPANY_CODE_LENGTH)
    )


def normalize_company_code(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def is_valid_company_code(value: str) -> bool:
    return len(value) == COMPANY_CODE_LENGTH and all(
        char in COMPANY_CODE_ALPHABET for char in value
    )


def _new_company_code(conn: Connection) -> str:
    for _ in range(50):
        code = generate_company_code()
        taken = conn.execute(
            select(companies.c.company_id).where(companies.c.company_code == code)
        ).first()
        if not taken:
            return code
    raise RuntimeError("회사 코드를 만들지 못했습니다.")


def generate_initial_password() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(INITIAL_PASSWORD_DIGITS))


def _scrypt_cost() -> int:
    if (
        os.environ.get("OFFICEGENIE_TESTING") == "1"
        and os.environ.get("OFFICEGENIE_TEST_FAST_HASH") == "1"
    ):
        return SCRYPT_N_TEST_FAST
    return SCRYPT_N


def hash_password(password: str, *, n: int | None = None) -> tuple[str, str]:
    """Return (encoded hash, hex salt). The hash string carries its scrypt cost."""
    n = n or _scrypt_cost()
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${n}${_SCRYPT_R}${_SCRYPT_P}${digest.hex()}", salt.hex()


def verify_password(password: str, encoded: str, salt_hex: str) -> bool:
    try:
        scheme, n, r, p, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


_DUMMY_HASH: tuple[str, str] | None = None


def _burn_password_check(password: str) -> None:
    """계정이 없을 때도 해시 계산을 해서 응답 시간으로 존재 여부를 알 수 없게 한다."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    verify_password(password, *_DUMMY_HASH)


def _audit(
    conn: Connection,
    company_id: str,
    actor: str,
    action: str,
    target: str | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        insert(audit_log).values(
            company_id=company_id,
            at=_iso(_utc_now()),
            actor=str(actor),
            action=str(action),
            target=None if target is None else str(target),
            detail=detail or {},
        )
    )


def record_audit(
    company_id: str,
    actor: str,
    action: str,
    target: str | None = None,
    detail: dict | None = None,
) -> None:
    _run_db(
        lambda conn: _audit(conn, company_id, actor, action, target, detail),
        write=True,
    )


def list_audit_log(company_id: str, limit: int = 100) -> list[dict]:
    return _run_db(
        lambda conn: [
            dict(row)
            for row in conn.execute(
                select(audit_log)
                .where(audit_log.c.company_id == company_id)
                .order_by(audit_log.c.id.desc())
                .limit(limit)
            ).mappings()
        ]
    )


def _account_values(
    company_id: str,
    account_id: str,
    role: str,
    password: str,
    *,
    must_change: bool,
) -> dict:
    password_hash, salt = hash_password(password)
    return {
        "company_id": company_id,
        "account_id": account_id,
        "role": role,
        "password_hash": password_hash,
        "salt": salt,
        "must_change": must_change,
        "failed_count": 0,
        "locked_until": None,
        "disabled": False,
        "created_at": _iso(_utc_now()),
    }


def _sync_employee_accounts(
    conn: Connection, company_id: str, employee_rows: list[dict], actor: str
) -> list[dict]:
    """Create missing employee accounts; keep existing passwords; disable removed ones.

    Returns newly generated initial passwords (shown once by the caller, never stored).
    """
    existing = {
        row["account_id"]: dict(row)
        for row in conn.execute(
            select(
                accounts.c.account_id, accounts.c.role, accounts.c.disabled
            ).where(accounts.c.company_id == company_id)
        ).mappings()
    }
    present: set[str] = set()
    generated: list[dict] = []
    created = reenabled = disabled = 0
    for row in employee_rows:
        employee_id = str(row["employee_id"])
        present.add(employee_id)
        account = existing.get(employee_id)
        if account is not None:
            if account["disabled"]:
                conn.execute(
                    update(accounts)
                    .where(
                        and_(
                            accounts.c.company_id == company_id,
                            accounts.c.account_id == employee_id,
                        )
                    )
                    .values(disabled=False, failed_count=0, locked_until=None)
                )
                reenabled += 1
            continue
        password = row.get("password")
        must_change = False
        if password is None or str(password).strip() == "":
            password = generate_initial_password()
            must_change = True
            generated.append(
                {
                    "employee_id": employee_id,
                    "name": str(row.get("name", "")),
                    "initial_password": password,
                }
            )
        conn.execute(
            insert(accounts).values(
                **_account_values(
                    company_id,
                    employee_id,
                    ROLE_EMPLOYEE,
                    str(password),
                    must_change=must_change,
                )
            )
        )
        created += 1
    for account_id, account in existing.items():
        if (
            account["role"] == ROLE_EMPLOYEE
            and account_id not in present
            and not account["disabled"]
        ):
            conn.execute(
                update(accounts)
                .where(
                    and_(
                        accounts.c.company_id == company_id,
                        accounts.c.account_id == account_id,
                    )
                )
                .values(disabled=True)
            )
            disabled += 1
    if created or reenabled or disabled:
        _audit(
            conn,
            company_id,
            actor,
            "accounts_sync",
            None,
            {
                "created": created,
                "reenabled": reenabled,
                "disabled": disabled,
                "initial_passwords_generated": len(generated),
            },
        )
    return generated


def _company_public(row: dict) -> dict:
    return {
        "company_id": row["company_id"],
        "company_name": row["company_name"],
        "company_code": row.get("company_code"),
        "data_version": row.get("data_version"),
    }


def get_company(company_id: str) -> dict | None:
    row = _run_db(
        lambda conn: conn.execute(
            select(
                companies.c.company_id,
                companies.c.company_name,
                companies.c.company_code,
                companies.c.data_version,
            ).where(companies.c.company_id == company_id)
        )
        .mappings()
        .first()
    )
    return _company_public(dict(row)) if row else None


def list_companies_overview() -> list[dict]:
    """운영자용: company_id, 회사명, 회사 코드, 사원 수."""

    def operation(conn: Connection) -> list[dict]:
        counts = dict(
            conn.execute(
                select(employees.c.company_id, func.count()).group_by(
                    employees.c.company_id
                )
            ).all()
        )
        return [
            {**_company_public(dict(row)), "employee_count": int(counts.get(row["company_id"], 0))}
            for row in conn.execute(
                select(
                    companies.c.company_id,
                    companies.c.company_name,
                    companies.c.company_code,
                    companies.c.data_version,
                ).order_by(companies.c.company_id)
            ).mappings()
        ]

    return _run_db(operation)


def _account_key(company_id: str, account_id: str):
    return and_(accounts.c.company_id == company_id, accounts.c.account_id == account_id)


def _account_status(row: dict, now: datetime | None = None) -> str:
    now = now or _utc_now()
    if row.get("disabled"):
        return "disabled"
    locked_until = _parse_iso(row.get("locked_until"))
    if locked_until and locked_until > now:
        return "locked"
    if row.get("must_change"):
        return "must_change"
    return "active"


def authenticate(company_code: str, account_id: str, password: str) -> dict:
    """Check a login. Returns {"status": "ok"|"invalid"|"locked", ...}.

    "invalid" covers unknown company code, unknown/disabled account and wrong
    password alike, so callers cannot tell which part was wrong. "reason" is
    for the server log only and must never reach the UI.
    """
    code = normalize_company_code(company_code)
    raw_account = str(account_id or "").strip()
    password = str(password or "")
    if not code or not raw_account or not password:
        _burn_password_check(password)
        return {"status": "invalid", "reason": "empty_field"}

    def operation(conn: Connection) -> dict:
        company = (
            conn.execute(
                select(
                    companies.c.company_id,
                    companies.c.company_name,
                    companies.c.company_code,
                    companies.c.data_version,
                ).where(companies.c.company_code == code)
            )
            .mappings()
            .first()
        )
        if company is None:
            _burn_password_check(password)
            return {"status": "invalid", "reason": "unknown_company"}
        company_id = company["company_id"]
        candidates = (
            conn.execute(
                select(accounts)
                .where(
                    and_(
                        accounts.c.company_id == company_id,
                        func.upper(accounts.c.account_id) == raw_account.upper(),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        account = next(
            (row for row in candidates if row["account_id"] == raw_account),
            candidates[0] if len(candidates) == 1 else None,
        )
        if account is None or account["disabled"]:
            _burn_password_check(password)
            reason = "unknown_account" if account is None else "disabled_account"
            return {"status": "invalid", "reason": reason, "company_id": company_id}
        now = _utc_now()
        key = _account_key(company_id, account["account_id"])
        locked_until = _parse_iso(account["locked_until"])
        if locked_until and locked_until > now:
            return {"status": "locked", "reason": "locked", "company_id": company_id}
        if not verify_password(password, account["password_hash"], account["salt"]):
            failed = int(account["failed_count"] or 0) + 1
            if failed >= MAX_FAILED_LOGINS:
                conn.execute(
                    update(accounts)
                    .where(key)
                    .values(
                        failed_count=0,
                        locked_until=_iso(now + timedelta(minutes=LOCK_MINUTES)),
                    )
                )
                _audit(
                    conn, company_id, "system", "account_locked", account["account_id"]
                )
                return {"status": "locked", "reason": "locked_now", "company_id": company_id}
            conn.execute(update(accounts).where(key).values(failed_count=failed))
            return {"status": "invalid", "reason": "bad_password", "company_id": company_id}
        conn.execute(
            update(accounts).where(key).values(failed_count=0, locked_until=None)
        )
        return {
            "status": "ok",
            "company": _company_public(dict(company)),
            "account_id": account["account_id"],
            "role": account["role"],
            "must_change": bool(account["must_change"]),
        }

    return _run_db(operation, write=True, immediate=True)


def get_account(company_id: str, account_id: str) -> dict | None:
    """Account state without any password material."""
    row = _run_db(
        lambda conn: conn.execute(
            select(
                accounts.c.company_id,
                accounts.c.account_id,
                accounts.c.role,
                accounts.c.must_change,
                accounts.c.failed_count,
                accounts.c.locked_until,
                accounts.c.disabled,
                accounts.c.created_at,
            ).where(_account_key(company_id, account_id))
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    result = dict(row)
    result["status"] = _account_status(result)
    return result


def change_password(
    company_id: str, account_id: str, old_password: str, new_password: str
) -> str:
    """Returns "ok", "invalid" (wrong old password/no account), "locked",
    "too_short" or "same"."""
    old_password = str(old_password or "")
    new_password = str(new_password or "")

    def operation(conn: Connection) -> str:
        key = _account_key(company_id, account_id)
        account = (
            conn.execute(select(accounts).where(key).with_for_update())
            .mappings()
            .first()
        )
        if account is None or account["disabled"]:
            _burn_password_check(old_password)
            return "invalid"
        now = _utc_now()
        locked_until = _parse_iso(account["locked_until"])
        if locked_until and locked_until > now:
            return "locked"
        if not verify_password(old_password, account["password_hash"], account["salt"]):
            failed = int(account["failed_count"] or 0) + 1
            values: dict[str, Any] = {"failed_count": failed}
            if failed >= MAX_FAILED_LOGINS:
                values = {
                    "failed_count": 0,
                    "locked_until": _iso(now + timedelta(minutes=LOCK_MINUTES)),
                }
            conn.execute(update(accounts).where(key).values(**values))
            return "locked" if "locked_until" in values else "invalid"
        minimum = (
            ADMIN_MIN_PASSWORD if account["role"] == ROLE_ADMIN else EMPLOYEE_MIN_PASSWORD
        )
        if len(new_password) < minimum:
            return "too_short"
        if new_password == old_password:
            return "same"
        password_hash, salt = hash_password(new_password)
        conn.execute(
            update(accounts)
            .where(key)
            .values(
                password_hash=password_hash,
                salt=salt,
                must_change=False,
                failed_count=0,
                locked_until=None,
            )
        )
        _audit(conn, company_id, account_id, "password_changed", account_id)
        _revoke_account_sessions(conn, company_id, account_id)
        return "ok"

    return _run_db(operation, write=True, immediate=True)


def set_admin_password(company_id: str, password: str, *, actor: str = "operator") -> None:
    """Create or reset the company's ADMIN account (operator tooling)."""
    password = str(password or "")
    if len(password) < ADMIN_MIN_PASSWORD:
        raise ValueError(f"관리자 비밀번호는 {ADMIN_MIN_PASSWORD}자 이상이어야 합니다.")

    def operation(conn: Connection) -> None:
        if not conn.execute(
            select(companies.c.company_id).where(companies.c.company_id == company_id)
        ).first():
            raise KeyError(company_id)
        key = _account_key(company_id, ADMIN_ACCOUNT_ID)
        values = _account_values(
            company_id, ADMIN_ACCOUNT_ID, ROLE_ADMIN, password, must_change=False
        )
        if conn.execute(select(accounts.c.account_id).where(key)).first():
            values.pop("created_at")
            conn.execute(update(accounts).where(key).values(**values))
        else:
            conn.execute(insert(accounts).values(**values))
        _audit(conn, company_id, actor, "admin_password_set", ADMIN_ACCOUNT_ID)

    _run_db(operation, write=True, immediate=True)


def reset_employee_passwords(
    company_id: str, employee_rows: list[dict], *, actor: str = "operator"
) -> int:
    """Demo tooling: set employee accounts back to the password in employees.json.

    Only existing employee-role accounts listed with a non-empty password are
    changed; lock and failure count are cleared and must_change becomes False.
    ADMIN and other non-employee accounts are never touched. Returns the count.
    """
    wanted: dict[str, str] = {}
    for row in employee_rows:
        password = row.get("password")
        if password is None or str(password).strip() == "":
            continue
        wanted[str(row["employee_id"])] = str(password)
    # 해시는 트랜잭션 밖에서 미리 계산해 잠금 시간을 줄인다.
    hashed = {account_id: hash_password(pw) for account_id, pw in wanted.items()}

    def operation(conn: Connection) -> int:
        if not conn.execute(
            select(companies.c.company_id).where(companies.c.company_id == company_id)
        ).first():
            raise KeyError(company_id)
        employee_ids = {
            row["account_id"]
            for row in conn.execute(
                select(accounts.c.account_id).where(
                    and_(
                        accounts.c.company_id == company_id,
                        accounts.c.role == ROLE_EMPLOYEE,
                    )
                )
            ).mappings()
        }
        changed = 0
        for account_id, (password_hash, salt) in hashed.items():
            if account_id not in employee_ids or account_id == ADMIN_ACCOUNT_ID:
                continue
            conn.execute(
                update(accounts)
                .where(
                    and_(
                        _account_key(company_id, account_id),
                        accounts.c.role == ROLE_EMPLOYEE,
                    )
                )
                .values(
                    password_hash=password_hash,
                    salt=salt,
                    must_change=False,
                    failed_count=0,
                    locked_until=None,
                )
            )
            changed += 1
        _audit(
            conn,
            company_id,
            actor,
            "employee_passwords_reset",
            None,
            {"count": changed},
        )
        return changed

    return _run_db(operation, write=True, immediate=True)


COMPANY_NAME_MAX = 100


def register_company(company_name: str, admin_password: str) -> dict:
    """Self-service sign-up: new internal company_id, company code, ADMIN account.

    The company starts with no data; the admin uploads it from the admin screen.
    """
    name = re.sub(r"\s+", " ", str(company_name or "")).strip()
    if not name or len(name) > COMPANY_NAME_MAX:
        raise ValueError("company_name")
    if len(str(admin_password or "")) < ADMIN_MIN_PASSWORD:
        raise ValueError("admin_password")

    def operation(conn: Connection) -> dict:
        for _ in range(50):
            company_id = f"c_{secrets.token_hex(5)}"
            if not conn.execute(
                select(companies.c.company_id).where(companies.c.company_id == company_id)
            ).first():
                break
        else:  # pragma: no cover - 40비트 난수가 50번 연속 겹칠 일은 없다
            raise RuntimeError("company_id")
        code = _new_company_code(conn)
        conn.execute(
            insert(companies).values(
                company_id=company_id,
                company_name=name,
                data_version=1,
                next_ticket_sequence=1,
                ticket_id_format="REQ-{sequence:03d}",
                company_metadata={},
                company_code=code,
            )
        )
        conn.execute(
            insert(accounts).values(
                **_account_values(
                    company_id,
                    ADMIN_ACCOUNT_ID,
                    ROLE_ADMIN,
                    str(admin_password),
                    must_change=False,
                )
            )
        )
        _audit(
            conn,
            company_id,
            "signup",
            "company_registered",
            company_id,
            {"company_name": name},
        )
        return {
            "company_id": company_id,
            "company_name": name,
            "company_code": code,
            "account_id": ADMIN_ACCOUNT_ID,
        }

    result = _run_db(operation, write=True, immediate=True)
    invalidate_company_cache()
    return result


AUDIT_VIEW_DEDUPE_SECONDS = 300


def record_admin_view(
    company_id: str, actor: str, action: str, target: str | None = None
) -> None:
    """관리자 조회 기록. 화면이 자주 다시 그려지므로 같은 조회는 5분에 한 번만 남긴다."""

    def operation(conn: Connection) -> None:
        since = _iso(_utc_now() - timedelta(seconds=AUDIT_VIEW_DEDUPE_SECONDS))
        target_clause = (
            audit_log.c.target.is_(None) if target is None else audit_log.c.target == str(target)
        )
        recent = conn.execute(
            select(audit_log.c.id)
            .where(
                and_(
                    audit_log.c.company_id == company_id,
                    audit_log.c.actor == str(actor),
                    audit_log.c.action == action,
                    target_clause,
                    audit_log.c.at >= since,
                )
            )
            .limit(1)
        ).first()
        if not recent:
            _audit(conn, company_id, actor, action, target)

    _run_db(operation, write=True)


# ---------------------------------------------------------------------------
# 로그인 세션 (새로고침 후 로그인 유지)
# ---------------------------------------------------------------------------

SESSION_IDLE_SECONDS = 30 * 60  # 30분 무동작이면 만료(화면의 자동 로그아웃과 같은 기준)
SESSION_MAX_SECONDS = 12 * 60 * 60  # 로그인 후 최대 12시간
SESSION_TOUCH_SECONDS = 60  # 마지막 사용 시각은 1분에 한 번만 갱신한다


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def create_login_session(company_id: str, account_id: str) -> str:
    """새 세션 토큰을 만든다. 원문은 호출한 쪽에 한 번만 돌려주고 DB에는 해시만 남긴다."""
    token = secrets.token_urlsafe(32)
    now = _utc_now()

    def operation(conn: Connection) -> None:
        # 만료된 세션은 이때 같이 정리한다.
        conn.execute(delete(login_sessions).where(login_sessions.c.expires_at < _iso(now)))
        conn.execute(
            insert(login_sessions).values(
                token_hash=_token_hash(token),
                company_id=company_id,
                account_id=account_id,
                created_at=_iso(now),
                last_seen_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=SESSION_MAX_SECONDS)),
            )
        )

    _run_db(operation, write=True)
    return token


def resume_login_session(token: str) -> dict | None:
    """토큰이 유효하면 {"company_id", "account_id", "role", "must_change"}를 돌려준다.

    만료·잠금·비활성 계정·모르는 토큰은 모두 None. 무효 토큰은 지운다.
    """
    token = str(token or "")
    if not token or len(token) > 200:
        return None
    now = _utc_now()

    def operation(conn: Connection) -> dict | None:
        key = login_sessions.c.token_hash == _token_hash(token)
        row = conn.execute(select(login_sessions).where(key)).mappings().first()
        if row is None:
            return None
        expires = _parse_iso(row["expires_at"])
        last_seen = _parse_iso(row["last_seen_at"])
        account = conn.execute(
            select(accounts).where(_account_key(row["company_id"], row["account_id"]))
        ).mappings().first()
        if (
            expires is None
            or expires <= now
            or last_seen is None
            or (now - last_seen).total_seconds() > SESSION_IDLE_SECONDS
            or account is None
            or _account_status(dict(account), now) in ("disabled", "locked")
        ):
            conn.execute(delete(login_sessions).where(key))
            return None
        if (now - last_seen).total_seconds() >= SESSION_TOUCH_SECONDS:
            conn.execute(update(login_sessions).where(key).values(last_seen_at=_iso(now)))
        return {
            "company_id": row["company_id"],
            "account_id": row["account_id"],
            "role": account["role"],
            "must_change": bool(account["must_change"]),
        }

    return _run_db(operation, write=True)


def revoke_login_session(token: str) -> None:
    token = str(token or "")
    if not token:
        return
    _run_db(
        lambda conn: conn.execute(
            delete(login_sessions).where(login_sessions.c.token_hash == _token_hash(token))
        ),
        write=True,
    )


def _revoke_account_sessions(conn: Connection, company_id: str, account_id: str) -> None:
    conn.execute(
        delete(login_sessions).where(
            and_(
                login_sessions.c.company_id == company_id,
                login_sessions.c.account_id == account_id,
            )
        )
    )


def revoke_account_sessions(company_id: str, account_id: str) -> None:
    _run_db(lambda conn: _revoke_account_sessions(conn, company_id, account_id), write=True)


def company_overview(company_id: str) -> dict | None:
    """관리자 화면 상단: 회사 정보와 데이터 종류별 건수."""

    def operation(conn: Connection) -> dict | None:
        row = (
            conn.execute(
                select(
                    companies.c.company_id,
                    companies.c.company_name,
                    companies.c.company_code,
                    companies.c.data_version,
                ).where(companies.c.company_id == company_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None

        def count(table: Table) -> int:
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.company_id == company_id)
                ).scalar_one()
            )

        counts = {
            "policies": count(policies),
            "employees": count(employees),
            "locations": count(locations),
            "handovers": count(handovers),
            "equipment": count(equipment_rules),
            "tickets": count(tickets),
        }
        return {
            **_company_public(dict(row)),
            "counts": counts,
            "has_data": any(counts[k] for k in ("policies", "employees", "locations", "handovers", "equipment")),
            "cache_ttl_seconds": STATIC_CACHE_TTL_SECONDS,
        }

    return _run_db(operation)


# ---------------------------------------------------------------------------
# 관리자 화면용 조회·변경. 권한 확인은 mcp_server.py가 accounts로 먼저 한다.
# ---------------------------------------------------------------------------

TICKET_STATUS_FLOW = {
    "접수완료": ("검토중",),
    "검토중": ("승인",),
    "승인": ("지급완료",),
}
TICKET_REJECTED = "반려"
TICKET_REJECTABLE = ("접수완료", "검토중", "승인")


def allowed_ticket_transitions(status: str) -> list[str]:
    allowed = list(TICKET_STATUS_FLOW.get(status, ()))
    if status in TICKET_REJECTABLE:
        allowed.append(TICKET_REJECTED)
    return allowed


def current_company_documents(company_id: str) -> tuple[int, dict[str, dict]] | None:
    """캐시를 거치지 않은 현재 문서 5종과 data_version (업로드 비교·병합용)."""
    loaded = _run_db(lambda conn: _load_static_company(conn, company_id))
    if loaded is None:
        return None
    return int(loaded["data_version"]), loaded["documents"]


def _employee_index(conn: Connection, company_id: str) -> dict[str, dict]:
    return {
        row["employee_id"]: dict(row)
        for row in conn.execute(
            select(
                employees.c.employee_id,
                employees.c.name,
                employees.c.department,
                employees.c.position,
                employees.c.job_title,
                employees.c.employment_status,
                employees.c.annual_leave,
                employees.c.employee_order,
            ).where(employees.c.company_id == company_id)
        ).mappings()
    }


def _account_index(conn: Connection, company_id: str) -> dict[str, dict]:
    return {
        row["account_id"]: dict(row)
        for row in conn.execute(
            select(
                accounts.c.account_id,
                accounts.c.role,
                accounts.c.must_change,
                accounts.c.failed_count,
                accounts.c.locked_until,
                accounts.c.disabled,
            ).where(accounts.c.company_id == company_id)
        ).mappings()
    }


ACCOUNT_STATUS_LABELS = {
    "active": "정상",
    "locked": "잠금",
    "must_change": "변경 필요",
    "disabled": "비활성",
    "none": "계정 없음",
}


def admin_list_employees(company_id: str) -> list[dict]:
    def operation(conn: Connection) -> list[dict]:
        people = _employee_index(conn, company_id)
        account_rows = _account_index(conn, company_id)
        now = _utc_now()
        result = []
        for employee in sorted(people.values(), key=lambda r: r["employee_order"]):
            account = account_rows.get(employee["employee_id"])
            status = _account_status(account, now) if account else "none"
            leave = _safe_json_dict(employee.get("annual_leave"))
            result.append(
                {
                    "employee_id": employee["employee_id"],
                    "name": employee["name"],
                    "department": employee["department"],
                    "position": employee.get("position") or None,
                    "job_title": employee.get("job_title") or None,
                    "employment_status": employee.get("employment_status"),
                    "remaining_days": leave.get("remaining_days"),
                    "account_status": status,
                    "account_status_label": ACCOUNT_STATUS_LABELS[status],
                }
            )
        return result

    return _run_db(operation)


def admin_save_employee(
    company_id: str, employee_id: str, fields: dict, actor: str, *, create: bool
) -> dict:
    def operation(conn: Connection) -> dict:
        company_key = companies.c.company_id == company_id
        version = conn.execute(
            select(companies.c.data_version).where(company_key).with_for_update()
        ).scalar_one()
        employee_key = and_(
            employees.c.company_id == company_id,
            employees.c.employee_id == employee_id,
        )
        values = {key: fields[key] for key in ("name", "department", "position", "employment_status")}
        initial = None
        if create:
            employee_exists = conn.execute(
                select(employees.c.employee_id).where(
                    employees.c.company_id == company_id,
                    func.lower(employees.c.employee_id) == employee_id.lower(),
                )
            ).first()
            account_exists = conn.execute(
                select(accounts.c.account_id).where(
                    accounts.c.company_id == company_id,
                    func.lower(accounts.c.account_id) == employee_id.lower(),
                )
            ).first()
            if employee_exists or account_exists:
                return {"status": "duplicate"}
            last_order = conn.execute(
                select(func.max(employees.c.employee_order)).where(employees.c.company_id == company_id)
            ).scalar_one()
            conn.execute(insert(employees).values(
                company_id=company_id, employee_id=employee_id,
                employee_order=0 if last_order is None else last_order + 1,
                annual_leave={"remaining_days": fields["remaining_days"]}, **values,
            ))
            password = generate_initial_password()
            account = _account_values(
                company_id, employee_id, ROLE_EMPLOYEE, password, must_change=True
            )
            conn.execute(insert(accounts).values(**account))
            initial = {"employee_id": employee_id, "name": fields["name"], "initial_password": password}
        else:
            row = conn.execute(select(employees.c.annual_leave).where(employee_key)).first()
            if row is None:
                return {"status": "not_found"}
            leave = _safe_json_dict(row.annual_leave) | {"remaining_days": fields["remaining_days"]}
            conn.execute(update(employees).where(employee_key).values(annual_leave=leave, **values))
        conn.execute(update(companies).where(company_key).values(data_version=version + 1))
        _audit(conn, company_id, actor, "employee_added" if create else "employee_updated", employee_id, {})
        return {"status": "ok", "initial_password": initial}

    result = _run_db(operation, write=True, immediate=True)
    if result["status"] == "ok":
        invalidate_company_cache(company_id)
    return result


def list_announcements(company_id: str, *, public_only: bool = False) -> list[dict]:
    query = select(
        announcements.c.announcement_id, announcements.c.title, announcements.c.body,
        announcements.c.is_public, announcements.c.created_at, announcements.c.published_at,
    ).where(announcements.c.company_id == company_id)
    if public_only:
        query = query.where(announcements.c.is_public.is_(True))
    query = query.order_by(
        func.coalesce(announcements.c.published_at, announcements.c.created_at).desc(),
        announcements.c.announcement_id.desc(),
    )
    return _run_db(lambda conn: [dict(row) for row in conn.execute(query).mappings()])


def admin_save_announcement(
    company_id: str, actor: str, announcement_id: str | None, title: str, body: str, is_public: bool
) -> bool:
    def operation(conn: Connection) -> bool:
        now = datetime.now(KST).isoformat()
        values = {"title": title, "body": body, "is_public": is_public}
        if announcement_id is None:
            target = uuid4().hex
            conn.execute(insert(announcements).values(
                company_id=company_id, announcement_id=target, created_at=now,
                published_at=now if is_public else None, **values,
            ))
        else:
            target = announcement_id
            key = and_(
                announcements.c.company_id == company_id,
                announcements.c.announcement_id == target,
            )
            row = conn.execute(
                select(announcements.c.is_public, announcements.c.published_at)
                .where(key).with_for_update()
            ).first()
            if row is None:
                return False
            published_at = row.published_at
            if is_public and not row.is_public:
                published_at = now
            conn.execute(update(announcements).where(key).values(published_at=published_at, **values))
        action = "announcement_created" if announcement_id is None else "announcement_updated"
        _audit(conn, company_id, actor, action, target, {"is_public": is_public})
        return True

    return _run_db(operation, write=True, immediate=True)


def admin_list_accounts(company_id: str) -> list[dict]:
    """사원 계정 상태(사원 목록에 있는 사람 + JSON에서 빠져 비활성인 계정)."""

    def operation(conn: Connection) -> list[dict]:
        people = _employee_index(conn, company_id)
        now = _utc_now()
        rows = []
        for account_id, account in _account_index(conn, company_id).items():
            if account["role"] != ROLE_EMPLOYEE:
                continue
            employee = people.get(account_id) or {}
            status = _account_status(account, now)
            rows.append(
                {
                    "employee_id": account_id,
                    "name": employee.get("name") or "-",
                    "department": employee.get("department") or "-",
                    "account_status": status,
                    "account_status_label": ACCOUNT_STATUS_LABELS[status],
                    "failed_count": int(account.get("failed_count") or 0),
                    "locked_until": account.get("locked_until") if status == "locked" else None,
                    "_order": employee.get("employee_order", 10**9),
                }
            )
        rows.sort(key=lambda r: (r.pop("_order"), r["employee_id"]))
        return rows

    return _run_db(operation)


def _admin_ticket(row: dict, people: dict[str, dict]) -> dict:
    record = _record_ticket(row)
    employee = people.get(record.get("employee_id")) or {}
    return {
        "ticket_id": record.get("ticket_id"),
        "employee_id": record.get("employee_id"),
        "name": employee.get("name") or "-",
        "department": employee.get("department") or "-",
        "type": record.get("type"),
        "item": record.get("item"),
        "quantity": record.get("quantity"),
        "purpose": record.get("purpose") or "",
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "scheduled_date": record.get("scheduled_date"),
        "history": list(row.get("history") or []),
        "allowed_next": allowed_ticket_transitions(str(record.get("status"))),
    }


def admin_list_tickets(company_id: str) -> list[dict]:
    def operation(conn: Connection) -> list[dict]:
        people = _employee_index(conn, company_id)
        rows = conn.execute(
            select(tickets)
            .where(tickets.c.company_id == company_id)
            .order_by(tickets.c.ticket_order.desc())
        ).mappings()
        return [_admin_ticket(dict(row), people) for row in rows]

    return _run_db(operation)


def admin_update_ticket(
    company_id: str, ticket_id: str, to_status: str, actor: str, note: str = "",
    *, equipment_only: bool = False, scheduled_date: str | None = None,
    expected_status: str | None = None, expected_scheduled_date: str | None = None,
) -> dict:
    """Returns {"status": "ok"|"not_found"|"invalid_transition"|"note_required", "ticket"}."""
    note = str(note or "").strip()

    def operation(conn: Connection) -> dict:
        key = and_(tickets.c.company_id == company_id, tickets.c.ticket_id == ticket_id)
        row = conn.execute(select(tickets).where(key).with_for_update()).mappings().first()
        if row is None:
            return {"status": "not_found"}
        current = str(row["status"])
        changes = {}
        if equipment_only:
            if row["type"] != "비품":
                return {"status": "not_found"}
            extra = _safe_json_dict(row.get("extra")).copy()
            if current != expected_status or extra.get("scheduled_date") != expected_scheduled_date:
                return {"status": "stale"}
            flow = {"접수완료": 0, "검토중": 0, "승인": 1, "지급완료": 2}
            if to_status not in {"접수완료", "승인", "지급완료"} or current not in flow or flow[to_status] < flow[current]:
                return {"status": "invalid_transition", "from": current}
            # '검토중'은 관리 화면의 '접수'에 속하며 날짜만 바꿀 때 검토 상태를 유지한다.
            to = current if flow[current] == flow[to_status] else to_status
            if scheduled_date is None:
                extra.pop("scheduled_date", None)
            else:
                extra["scheduled_date"] = scheduled_date
            changes["extra"] = extra
        else:
            to = to_status
        if not equipment_only and to not in allowed_ticket_transitions(current):
            return {"status": "invalid_transition", "from": current}
        if to == TICKET_REJECTED and not note:
            return {"status": "note_required"}
        entry = {
            "at": datetime.now(KST).isoformat(timespec="seconds"),
            "by": str(actor),
            "from": current,
            "to": to,
            "note": note,
        }
        history = list(row.get("history") or []) + [entry]
        if equipment_only:
            entry["scheduled_date"] = scheduled_date
        conn.execute(update(tickets).where(key).values(status=to, history=history, **changes))
        _audit(
            conn,
            company_id,
            actor,
            "ticket_status_changed",
            ticket_id,
            {k: v for k, v in entry.items() if k not in ("at", "by")},
        )
        updated = dict(row) | changes | {"status": to, "history": history}
        return {
            "status": "ok",
            "ticket": _admin_ticket(updated, _employee_index(conn, company_id)),
        }

    return _run_db(operation, write=True, immediate=True)


def admin_reset_password(company_id: str, employee_id: str, actor: str) -> str | None:
    """새 4자리 초기 비밀번호를 만들어 돌려준다(원문은 저장하지 않음). 계정이 없으면 None."""
    password = generate_initial_password()

    def operation(conn: Connection) -> str | None:
        key = _account_key(company_id, employee_id)
        account = conn.execute(
            select(accounts.c.role, accounts.c.disabled).where(key)
        ).mappings().first()
        if account is None or account["role"] != ROLE_EMPLOYEE or account["disabled"]:
            return None
        password_hash, salt = hash_password(password)
        conn.execute(
            update(accounts)
            .where(key)
            .values(
                password_hash=password_hash,
                salt=salt,
                must_change=True,
                failed_count=0,
                locked_until=None,
            )
        )
        _audit(conn, company_id, actor, "password_reset", employee_id)
        _revoke_account_sessions(conn, company_id, employee_id)
        return password

    return _run_db(operation, write=True, immediate=True)


def admin_unlock_account(company_id: str, employee_id: str, actor: str) -> bool:
    def operation(conn: Connection) -> bool:
        key = _account_key(company_id, employee_id)
        account = conn.execute(select(accounts.c.role).where(key)).mappings().first()
        if account is None or account["role"] != ROLE_EMPLOYEE:
            return False
        conn.execute(update(accounts).where(key).values(failed_count=0, locked_until=None))
        _audit(conn, company_id, actor, "account_unlocked", employee_id)
        return True

    return _run_db(operation, write=True, immediate=True)


def create_postgres_test_schema(database_url: str, schema: str) -> None:
    if not re.fullmatch(r"officegenie_test_[a-f0-9]+", schema):
        raise ValueError("테스트 DB 스키마 이름이 올바르지 않습니다.")
    url = normalize_database_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("TEST_DATABASE_URL은 PostgreSQL이어야 합니다.")
    admin = _build_engine(url)
    try:
        with admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    except SQLAlchemyError:
        log.error(
            "테스트용 DB 스키마 생성 실패 (%s)",
            type(__import__("sys").exc_info()[1]).__name__,
        )
        raise StorageUnavailable() from None
    finally:
        admin.dispose()


def drop_postgres_test_schema(database_url: str, schema: str) -> None:
    if not re.fullmatch(r"officegenie_test_[a-f0-9]+", schema):
        return
    admin = _build_engine(normalize_database_url(database_url))
    try:
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except SQLAlchemyError as exc:
        log.error("테스트용 DB 스키마 정리 실패 (%s)", type(exc).__name__)
        raise StorageUnavailable() from None
    finally:
        admin.dispose()


def close_engine() -> None:
    global _engine, _engine_url, _engine_schema
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _engine_url = None
        _engine_schema = None
    invalidate_company_cache()


def recreate_tables_for_tests() -> None:
    """Drop and recreate every table. Refuses to run outside pytest."""
    if os.environ.get("OFFICEGENIE_TESTING") != "1":
        raise RuntimeError("테스트 환경에서만 사용할 수 있습니다.")

    def operation(conn: Connection) -> None:
        metadata.drop_all(conn)
        metadata.create_all(conn)

    _run_db(operation, write=True)
    invalidate_company_cache()


def clear_tables_for_tests() -> None:
    """Delete every row (tables stay). Faster than drop/create on a remote DB."""
    if os.environ.get("OFFICEGENIE_TESTING") != "1":
        raise RuntimeError("테스트 환경에서만 사용할 수 있습니다.")

    def operation(conn: Connection) -> None:
        for table in (login_sessions, audit_log, accounts, tickets, *_CLEAR_ORDER, companies):
            conn.execute(delete(table))

    _run_db(operation, write=True)
    invalidate_company_cache()


# ---------------------------------------------------------------------------
# 테스트 전용: 세션당 한 번 가져온 기준 상태를 테스트마다 빠르게 되돌린다.
# ---------------------------------------------------------------------------

# 테스트가 흔히 바꾸는 표. 회사 정적 데이터(규정·사원 등)는 data_version으로 변경을 감지한다.
_BASELINE_TABLES = (tickets, announcements, accounts, audit_log, login_sessions)


def _require_testing() -> None:
    if os.environ.get("OFFICEGENIE_TESTING") != "1":
        raise RuntimeError("테스트 환경에서만 사용할 수 있습니다.")


def snapshot_baseline_for_tests() -> dict:
    """현재 DB를 기준 상태로 기록한다(회사 행 + 티켓·계정·감사 로그)."""
    _require_testing()

    def operation(conn: Connection) -> dict:
        return {
            "companies": [dict(r) for r in conn.execute(select(companies)).mappings()],
            **{
                table.name: [dict(r) for r in conn.execute(select(table)).mappings()]
                for table in _BASELINE_TABLES
            },
        }

    return _run_db(operation)


def restore_baseline_for_tests(snapshot: dict) -> bool:
    """기준 상태로 되돌린다. 기준 회사의 정적 데이터가 바뀌었으면(data_version 변경·회사 삭제)
    아무것도 하지 않고 False를 돌려준다. 그때는 호출한 쪽이 다시 가져와야 한다."""
    _require_testing()
    baseline = {row["company_id"]: row for row in snapshot["companies"]}

    def operation(conn: Connection) -> bool:
        current = dict(
            conn.execute(select(companies.c.company_id, companies.c.data_version)).all()
        )
        if any(
            current.get(company_id) != row["data_version"]
            for company_id, row in baseline.items()
        ):
            return False
        extra_companies = [cid for cid in current if cid not in baseline]
        if extra_companies:  # 테스트가 등록한 회사와 그 데이터
            for table in _CLEAR_ORDER:
                conn.execute(delete(table).where(table.c.company_id.in_(extra_companies)))
        for table in _BASELINE_TABLES:
            conn.execute(delete(table))
            rows = snapshot[table.name]
            if rows:
                conn.execute(insert(table), rows)
        if extra_companies:
            conn.execute(delete(companies).where(companies.c.company_id.in_(extra_companies)))
        for company_id, row in baseline.items():
            conn.execute(
                update(companies)
                .where(companies.c.company_id == company_id)
                .values(next_ticket_sequence=row["next_ticket_sequence"])
            )
        return True

    restored = _run_db(operation, write=True, immediate=True)
    invalidate_company_cache()
    return restored
