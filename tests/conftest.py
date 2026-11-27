"""공통 테스트 DB fixture.

- pytest는 DATABASE_URL(공유 DB)을 절대 쓰지 않는다. 기본은 임시 SQLite 파일이다.
- TEST_DATABASE_URL이 있으면 `@pytest.mark.pg`가 붙은 테스트(DB 종류에 따라 결과가 달라질 수
  있는 것)만 PostgreSQL 임시 스키마에서 한 번 더 돌린다. 전부 돌리려면 `pytest --pg-all`.
  TEST_DATABASE_URL이 DATABASE_URL과 같으면 즉시 중단한다.
- 원본 data/·seed/는 읽기만 한다. DB 종류마다 세션당 한 번 data/를 가져오고 티켓을 seed로
  초기화한 "기준 상태"를 만든다(REQ-004 기대값 유지). 테스트가 끝나면 티켓·계정·감사 로그와
  테스트가 등록한 회사만 기준 상태로 되돌린다.
- 회사 정적 데이터(규정·사원·장소 등)를 바꾸는 테스트(가져오기·업로드·규정 삭제)는
  `@pytest.mark.reimport`를 붙인다. 이 테스트가 끝나면 테이블을 다시 만들고 다시 가져온다.
  표시 없이 정적 데이터를 바꾸면 그 테스트는 오류로 표시된다.
- 비밀번호 해시는 OFFICEGENIE_TEST_FAST_HASH=1로 최소 비용을 쓴다(운영 비용은 그대로).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
SEED_ROOT = ROOT / "seed"

_dotenv = dotenv_values(ROOT / ".env") if (ROOT / ".env").is_file() else {}
_SHARED_URL = os.environ.get("DATABASE_URL") or _dotenv.get("DATABASE_URL") or ""
_TEST_PG_URL = os.environ.get("TEST_DATABASE_URL") or _dotenv.get("TEST_DATABASE_URL") or ""

# storage.py는 이 표시가 있으면 .env를 읽지 않고 OFFICEGENIE_TEST_DATABASE_URL만 쓴다.
os.environ["OFFICEGENIE_TESTING"] = "1"
# 테스트에서만 scrypt 비용을 최소로 낮춘다. 해시에 비용이 함께 저장되어 검증은 정확하다.
os.environ["OFFICEGENIE_TEST_FAST_HASH"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("TEST_DATABASE_URL", None)
os.environ.pop("OFFICEGENIE_TEST_SCHEMA", None)
os.environ.pop("OFFICEGENIE_TEST_DATABASE_URL", None)
os.environ.pop("OFFICEGENIE_ENV", None)
# 화면의 'AI 앱 연결' 안내에 쓰는 주소. 하위 프로세스(MCP 서버)까지 같은 값을 보도록 고정한다.
os.environ["OFFICEGENIE_MCP_PUBLIC_URL"] = "https://officegenie-mcp.example.test"

import storage  # noqa: E402

if _TEST_PG_URL and _SHARED_URL and storage.database_url_matches(_TEST_PG_URL, _SHARED_URL):
    pytest.exit("TEST_DATABASE_URL이 DATABASE_URL과 같습니다. 공유 DB 보호를 위해 테스트를 중단합니다.", returncode=3)

BACKENDS = ["sqlite"] + (["postgresql"] if _TEST_PG_URL else [])
COMPANIES = ("bridgesoft", "nextwave")


def pytest_addoption(parser):
    parser.addoption(
        "--pg-all",
        action="store_true",
        help="TEST_DATABASE_URL이 있을 때 pg 표시가 없는 테스트도 PostgreSQL에서 실행",
    )


def pytest_collection_modifyitems(config, items):
    """PostgreSQL 회차는 pg 표시 테스트만 남긴다(--pg-all이면 전부)."""
    if config.getoption("--pg-all"):
        return
    keep, drop = [], []
    for item in items:
        callspec = getattr(item, "callspec", None)
        backend = callspec.params.get("db_backend") if callspec else None
        if backend == "postgresql" and item.get_closest_marker("pg") is None:
            drop.append(item)
        else:
            keep.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def import_all_with_seed_tickets() -> None:
    from scripts import import_company

    for company_id in COMPANIES:
        import_company.import_company_directory(DATA_ROOT / company_id, reset_tickets=True)


def _drop_cached_agents() -> None:
    """Streamlit AppTest가 캐시한 agent(와 그 MCP 하위 프로세스)를 닫고 캐시를 비운다.

    agent는 만들 때의 환경(OFFICEGENIE_TEST_DATABASE_URL)으로 MCP 서버를 띄운다. DB 종류가 바뀐 뒤에도
    캐시된 agent를 쓰면 화면 테스트가 이전 DB(예: SQLite)에 로그인해 실패하므로, DB를 바꿀 때마다 비운다.
    """
    import gc
    import sys

    agent_module = sys.modules.get("agent")
    if agent_module is not None:
        for obj in gc.get_objects():
            # 이미 닫힌 agent(이벤트 루프가 멈춘 것)는 다시 닫지 않는다(닫기 대기 시간 방지).
            if isinstance(obj, agent_module.OfficeGenieAgent) and obj._loop.is_running():
                try:
                    obj.close()
                except Exception:
                    pass
    try:
        import streamlit as st

        st.cache_resource.clear()
    except Exception:
        pass


def _build_baseline() -> dict:
    """테이블을 새로 만들고 data/ + seed 티켓을 가져온 뒤 기준 상태를 기록한다."""
    storage.recreate_tables_for_tests()
    import_all_with_seed_tickets()
    storage.invalidate_company_cache()
    return storage.snapshot_baseline_for_tests()


@pytest.fixture(scope="session", params=BACKENDS)
def db_backend(request, tmp_path_factory):
    """세션 동안 쓸 테스트 DB를 고르고 이 프로세스·하위 프로세스 환경에 연결한다."""
    schema = None
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'officegenie_test.db'}"
    else:
        url = _TEST_PG_URL
        schema = f"officegenie_test_{uuid.uuid4().hex[:12]}"
        storage.create_postgres_test_schema(url, schema)

    os.environ["OFFICEGENIE_TEST_DATABASE_URL"] = url
    if schema:
        os.environ["OFFICEGENIE_TEST_SCHEMA"] = schema
    storage.configure_database(url, schema=schema)
    _drop_cached_agents()
    state = {"name": request.param, "url": url, "schema": schema}
    state["baseline"] = _build_baseline()
    try:
        yield state
    finally:
        _drop_cached_agents()
        storage.close_engine()
        os.environ.pop("OFFICEGENIE_TEST_DATABASE_URL", None)
        os.environ.pop("OFFICEGENIE_TEST_SCHEMA", None)
        if schema:
            storage.drop_postgres_test_schema(url, schema)


def _ensure_engine(db_backend) -> None:
    same_engine = (
        storage._engine is not None
        and storage._engine_schema == db_backend["schema"]
        and storage._engine_url == storage.normalize_database_url(db_backend["url"])
    )
    if not same_engine:  # 다른 테스트가 엔진을 바꿨다면 테스트 DB로 되돌린다
        storage.configure_database(db_backend["url"], schema=db_backend["schema"])
        os.environ["OFFICEGENIE_TEST_DATABASE_URL"] = db_backend["url"]
        if db_backend["schema"]:
            os.environ["OFFICEGENIE_TEST_SCHEMA"] = db_backend["schema"]
        else:
            os.environ.pop("OFFICEGENIE_TEST_SCHEMA", None)


@pytest.fixture
def fresh_db(request, db_backend):
    """기준 상태에서 시작하고, 끝나면 기준 상태로 되돌린다."""
    _ensure_engine(db_backend)
    storage.invalidate_company_cache()
    yield db_backend
    _ensure_engine(db_backend)
    if request.node.get_closest_marker("reimport"):
        db_backend["baseline"] = _build_baseline()
        return
    if not storage.restore_baseline_for_tests(db_backend["baseline"]):
        db_backend["baseline"] = _build_baseline()
        pytest.fail(
            "이 테스트가 회사 정적 데이터를 바꿨습니다. @pytest.mark.reimport를 붙이세요.",
            pytrace=False,
        )
