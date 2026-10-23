import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

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
