import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from achados.db import make_engine
from achados.models import Base

FIXTURES = Path(__file__).parent / "fixtures"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://achados:achados@localhost:5432/achados_test"
)


@pytest.fixture
def session():
    """Sessão em um Postgres descartável; os testes são pulados se ele não estiver acessível."""
    engine = make_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except OperationalError:
        pytest.skip(f"Postgres de teste indisponível ({TEST_DATABASE_URL})")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    # Só para os testes: o esquema real usa JSONB do Postgres.
    return "JSON"


@pytest.fixture
def sqlite_engine():
    """SQLite em memória, uma única conexão compartilhada (serve a threads e a várias sessões)."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(sqlite_engine):
    return sessionmaker(sqlite_engine, expire_on_commit=False)


@pytest.fixture
def db(sessions):
    with sessions() as s:
        yield s
