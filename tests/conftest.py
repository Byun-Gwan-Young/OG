"""공통 테스트 DB fixture.

- pytest는 DATABASE_URL(공유 DB)을 절대 쓰지 않는다. 기본은 임시 SQLite 파일이다.
- TEST_DATABASE_URL이 있으면 같은 테스트를 PostgreSQL 임시 스키마에서도 한 번 더 돌린다.
  TEST_DATABASE_URL이 DATABASE_URL과 같으면 즉시 중단한다.
- 원본 data/·seed/는 읽기만 한다. 매 테스트마다 테이블을 새로 만들고 data/를 가져온 뒤
  티켓을 seed로 초기화한다(REQ-004 기대값 유지).
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
os.environ.pop("DATABASE_URL", None)
os.environ.pop("TEST_DATABASE_URL", None)
os.environ.pop("OFFICEGENIE_TEST_SCHEMA", None)
os.environ.pop("OFFICEGENIE_TEST_DATABASE_URL", None)

import storage  # noqa: E402

if _TEST_PG_URL and _SHARED_URL and storage.database_url_matches(_TEST_PG_URL, _SHARED_URL):
    pytest.exit("TEST_DATABASE_URL이 DATABASE_URL과 같습니다. 공유 DB 보호를 위해 테스트를 중단합니다.", returncode=3)

BACKENDS = ["sqlite"] + (["postgresql"] if _TEST_PG_URL else [])
COMPANIES = ("bridgesoft", "nextwave")


def import_all_with_seed_tickets() -> None:
    from scripts import import_company

    for company_id in COMPANIES:
        import_company.import_company_directory(DATA_ROOT / company_id, reset_tickets=True)


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
    storage.recreate_tables_for_tests()
    try:
        yield {"name": request.param, "url": url, "schema": schema}
    finally:
        storage.close_engine()
        os.environ.pop("OFFICEGENIE_TEST_DATABASE_URL", None)
        os.environ.pop("OFFICEGENIE_TEST_SCHEMA", None)
        if schema:
            storage.drop_postgres_test_schema(url, schema)


@pytest.fixture
def fresh_db(db_backend):
    """테스트마다 빈 테이블 → data/ 가져오기 → 티켓은 seed 상태."""
    same_engine = (
        storage._engine is not None
        and storage._engine_schema == db_backend["schema"]
        and storage._engine_url == storage.normalize_database_url(db_backend["url"])
    )
    if not same_engine:  # 다른 테스트가 엔진을 바꿨다면 테스트 DB로 되돌린다
        storage.configure_database(db_backend["url"], schema=db_backend["schema"])
    storage.clear_tables_for_tests()
    import_all_with_seed_tickets()
    storage.invalidate_company_cache()
    yield db_backend
