"""SQLAlchemy Core storage for OfficeGenie.

Company JSON files are importer inputs only. Runtime callers use this module for
all company data and tickets. No password fields are stored in ``employees``.
"""

from __future__ import annotations

import copy
import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
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
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError


BASE_DIR = Path(__file__).resolve().parent
STATIC_CACHE_TTL_SECONDS = 60
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
)

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
        if url.host and url.host.endswith(".neon.tech") and "sslmode" not in query:
            query["sslmode"] = "require"
        url = url.set(query=query)
    elif url.get_backend_name() != "sqlite":
        raise ValueError("DATABASE_URL은 PostgreSQL 또는 SQLite여야 합니다.")
    return url


def database_url_from_environment() -> URL:
    if os.environ.get("OFFICEGENIE_TESTING") == "1":
        value = os.environ.get("OFFICEGENIE_TEST_DATABASE_URL")
        if not value:
            raise RuntimeError("테스트 전용 DATABASE URL이 설정되지 않았습니다.")
        return normalize_database_url(value)

    load_dotenv(BASE_DIR / ".env")
    value = os.environ.get("DATABASE_URL")
    if value:
        return normalize_database_url(value)
    return normalize_database_url("sqlite:///officegenie.db")


def _build_engine(url: URL, schema: str | None = None) -> Engine:
    connect_args: dict[str, Any] = {}
    if url.get_backend_name() == "sqlite":
        connect_args.update(timeout=CONNECTION_TIMEOUT_SECONDS, check_same_thread=False)
    else:
        connect_args["connect_timeout"] = CONNECTION_TIMEOUT_SECONDS
        if schema:
            if not re.fullmatch(r"officegenie_test_[a-f0-9]+", schema):
                raise ValueError("테스트 DB 스키마 이름이 올바르지 않습니다.")
            connect_args["options"] = f"-csearch_path={schema}"

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

    return engine


def get_engine() -> Engine:
    global _engine, _engine_url, _engine_schema
    with _engine_lock:
        if _engine is None:
            url = database_url_from_environment()
            try:
                _engine = _build_engine(url, os.environ.get("OFFICEGENIE_TEST_SCHEMA"))
            except Exception:
                log.error(
                    "DB 엔진 설정 실패 (%s)",
                    type(__import__("sys").exc_info()[1]).__name__,
                )
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


def init_db() -> None:
    """Create missing tables without altering existing company data."""
    _run_db(lambda conn: metadata.create_all(conn), write=True)


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
        ticket_rows = _run_db(
            lambda conn: [
                dict(row)
                for row in conn.execute(
                    select(tickets)
                    .where(tickets.c.company_id == company_id)
                    .order_by(tickets.c.ticket_order)
                ).mappings()
            ]
        )
        result = copy.deepcopy(company["documents"]["tickets"])
        result["tickets"] = [_record_ticket(row) for row in ticket_rows]
        result["next_sequence"] = company["company"]["next_ticket_sequence"]
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
    source_ticket_rows = _make_ticket_rows(
        company_id, source_ticket_doc.get("tickets", [])
    )
    company_name = str(
        documents["employees"].get("company_name")
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
        current_ticket_count = conn.execute(
            select(func.count())
            .select_from(tickets)
            .where(tickets.c.company_id == company_id)
        ).scalar_one()
        should_reset = reset_tickets
        should_initialize_tickets = should_reset or current_ticket_count == 0
        tickets_to_insert = selected_ticket_rows if should_initialize_tickets else []
        sequence_source = selected_ticket_doc if should_initialize_tickets else None
        next_sequence = (
            _ticket_sequence_next(sequence_source or {}, tickets_to_insert)
            if should_initialize_tickets
            else existing["next_ticket_sequence"]
        )
        new_version = int(existing["data_version"]) + 1 if existing else 1

        company_values = {
            "company_id": company_id,
            "company_name": company_name,
            "data_version": new_version,
            "next_ticket_sequence": next_sequence,
            "ticket_id_format": ticket_format,
            "company_metadata": company_metadata,
        }
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
                    "ticket_id_format": ticket_format,
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

        return {
            "company_id": company_id,
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


def database_url_for_tests_equal(left: str, right: str) -> bool:
    return database_url_matches(left, right)


def _test_seed_marker() -> None:
    """Keep table names available to schema introspection without ORM models."""
    return None
