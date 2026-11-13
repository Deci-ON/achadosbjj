import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

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
def sqlite_engine(tmp_path):
    """SQLite em arquivo temporário: uma conexão por thread, como o Postgres.

    Antes era em memória com uma conexão só (StaticPool), e dois agendadores ou uma coleta mais uma consulta
    usando essa conexão ao mesmo tempo davam `sqlite3.InterfaceError` de vez em quando.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'teste.db'}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _fast(dbapi_connection, _record):  # banco descartável: sem fsync, o teste fica perto do tempo em memória
        dbapi_connection.execute("PRAGMA synchronous=OFF")
        dbapi_connection.execute("PRAGMA journal_mode=MEMORY")

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
