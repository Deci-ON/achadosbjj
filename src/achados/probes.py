from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from achados.models import EmptyCatalogProduct

# SQLite limita variáveis por consulta (999 nas versões antigas); em lotes cabe nos dois bancos.
_CHUNK = 500


class ProbeCache(Protocol):
    def fresh_empty_ids(self) -> set[str]:
        """Produtos checados sem oferta há menos do prazo de revalidação: não vale consultar de novo."""

    def record(self, empty: Iterable[str], with_offers: Iterable[str]) -> None:
        """Guarda quem veio sem oferta agora e esquece quem ganhou oferta."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DbProbeCache:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        recheck_after: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] = utcnow,
    ):
        self._sessions = sessions
        self._recheck_after = recheck_after
        self._clock = clock

    def fresh_empty_ids(self) -> set[str]:
        cutoff = self._clock() - self._recheck_after
        with self._sessions() as session:
            rows = session.execute(select(EmptyCatalogProduct.product_id, EmptyCatalogProduct.checked_at)).all()
        # O corte é aplicado em Python: o SQLite devolve datas sem fuso, o Postgres com fuso.
        return {pid for pid, checked_at in rows if _aware(checked_at) > cutoff}

    def record(self, empty: Iterable[str], with_offers: Iterable[str]) -> None:
        empty, with_offers = set(empty), set(with_offers)
        if not empty and not with_offers:
            return
        now = self._clock()
        with self._sessions() as session:
            for product_id in empty:
                session.merge(EmptyCatalogProduct(product_id=product_id, checked_at=now))
            ids = list(with_offers)
            for start in range(0, len(ids), _CHUNK):
                session.execute(
                    delete(EmptyCatalogProduct).where(EmptyCatalogProduct.product_id.in_(ids[start : start + _CHUNK]))
                )
            session.commit()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
