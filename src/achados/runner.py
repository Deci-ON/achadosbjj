import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from achados.collectors.base import Collector
from achados.storage import finish_run, save_offers, start_run

log = logging.getLogger("achados")


@dataclass(frozen=True)
class RunResult:
    run_id: int
    source: str
    items_seen: int
    items_new: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def run_collection(sessions: sessionmaker[Session], collector: Collector) -> RunResult:
    """Uma coleta completa: registra o run, coleta, grava e fecha o run, mesmo se a coleta falhar.

    Bloqueante (a coleta usa httpx síncrono e `time.sleep`); o serviço chama em uma thread.
    Usado pela CLI e pelo agendador, para que os dois deixem o mesmo rastro em `collect_runs`.
    """
    with sessions() as session:
        run = start_run(session, collector.source)
        session.commit()

        offers, error = [], None
        try:
            for offer in collector.collect():
                offers.append(offer)
        except Exception as exc:  # o run precisa registrar a falha, qualquer que seja
            error = f"{type(exc).__name__}: {exc}"
            log.error("coleta falhou: %s", error)

        # Grava o que veio antes da falha (ex.: páginas 1-2 de 3).
        save_offers(session, run, offers)
        finish_run(session, run, error=error)
        session.commit()
        log.info("%s: %d vistos, %d novos", collector.source, run.items_seen, run.items_new)
        return RunResult(run.id, collector.source, run.items_seen, run.items_new, error)


def record_failure(sessions: sessionmaker[Session], source: str, error: str) -> None:
    """Registra em collect_runs uma coleta que nem chegou a começar (ex.: coletor não pôde ser montado)."""
    with sessions() as session:
        run = start_run(session, source)
        finish_run(session, run, error=error)
        session.commit()
