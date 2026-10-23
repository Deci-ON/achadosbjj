"""Agendador da coleta, no padrão do bazaar_sync_loop do tibiascope.

- Só o worker que segura o lock de líder (`pg_try_advisory_lock`) coleta.
- O próximo horário sai do banco (último sucesso em `collect_runs`), não de "agora + intervalo":
  um deploy ou reinício não dispara coleta nova se a última é recente.
- Coletas que ficaram `running` por uma queda são marcadas como erro ao assumir a liderança.
- Falhas repetem com espera crescente (15 min, 30 min... até o intervalo).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from achados.collectors.base import Collector
from achados.models import CollectRun
from achados.runner import RunResult, record_failure, run_collection

log = logging.getLogger("achados")

LEADER_LOCK_KEY = 875321002  # fixo e arbitrário; o do tibiascope (875321001) vive em outro banco
RETRY_BASE_SECONDS = 900.0
LEADER_RETRY_SECONDS = 60.0
MAX_SLEEP_SECONDS = 300.0  # acorda de tempos em tempos para notar coleta manual e perda de liderança


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def retry_delay(failures: int, interval: float, base: float = RETRY_BASE_SECONDS) -> float:
    """Espera depois de `failures` falhas seguidas: base, 2x base... limitada ao intervalo normal."""
    return min(interval, base * 2 ** max(failures - 1, 0))


def next_due(
    last_ok: datetime | None,
    interval: float,
    *,
    started_at: datetime,
    startup_delay: float,
    retry_at: datetime | None = None,
) -> datetime:
    """Quando a próxima coleta vence.

    Sem nenhum sucesso registrado: `startup_delay` depois de o processo subir. Com sucesso: último
    sucesso + intervalo, mas nunca antes de `startup_delay` desde a subida (coleta atrasada por
    uma parada longa não dispara no mesmo segundo). Depois de falha, vale o `retry_at`.
    """
    earliest = started_at + timedelta(seconds=startup_delay)
    due = earliest if last_ok is None else max(_aware(last_ok) + timedelta(seconds=interval), earliest)
    return max(due, retry_at) if retry_at else due


class LeaderLock:
    """Lock de líder por advisory lock do Postgres, numa conexão que fica aberta enquanto o processo viver.

    Fora do Postgres (SQLite nos testes) todo mundo é líder.
    """

    def __init__(self, engine: Engine, key: int = LEADER_LOCK_KEY):
        self._engine = engine
        self._key = key
        self._conn = None

    @property
    def _enabled(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    def acquire(self) -> bool:
        if not self._enabled:
            return True
        if self._conn is not None:
            return self.verify()
        conn = self._engine.connect()
        try:
            got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}).scalar())
            conn.commit()  # o lock é da sessão; não deixa transação aberta
        except Exception:
            conn.close()
            raise
        if got:
            self._conn = conn
        else:
            conn.close()
        return got

    def verify(self) -> bool:
        """Confere se a conexão (e portanto o lock) segue viva; se caiu, descarta e tenta de novo."""
        if not self._enabled:
            return True
        if self._conn is None:
            return False
        try:
            self._conn.execute(text("SELECT 1"))
            self._conn.commit()
            return True
        except Exception as exc:
            log.warning("conexão do lock de líder caiu (%s); tentando reassumir", type(exc).__name__)
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            return self.acquire()

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
            self._conn.commit()
        except Exception:
            pass
        finally:
            try:
                self._conn.close()
            finally:
                self._conn = None


def recover_interrupted(sessions: sessionmaker[Session], now: datetime | None = None) -> int:
    """Fecha como erro as coletas que ficaram `running` (processo morreu no meio).

    Só o líder pode chamar: em outro worker isso mataria uma coleta que está de fato rodando.
    """
    with sessions() as session:
        result = session.execute(
            update(CollectRun)
            .where(CollectRun.status == "running")
            .values(status="error", error="interrompida: o processo reiniciou", finished_at=now or utcnow())
        )
        session.commit()
        return result.rowcount or 0


def last_success_at(sessions: sessionmaker[Session], source: str) -> datetime | None:
    with sessions() as session:
        value = session.scalar(
            select(func.max(CollectRun.finished_at)).where(CollectRun.source == source, CollectRun.status == "ok")
        )
    return _aware(value) if value else None


@dataclass
class SchedulerState:
    enabled: bool
    is_leader: bool = False
    running: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    next_due: datetime | None = None


class CollectScheduler:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        lock: LeaderLock,
        make_collector: Callable[[], Collector],
        source: str,
        *,
        interval: float,
        startup_delay: float = 30.0,
        enabled: bool = True,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep,
    ):
        self._sessions = sessions
        self._lock = lock
        self._make_collector = make_collector
        self._source = source
        self._interval = interval
        self._startup_delay = startup_delay
        self._clock = clock
        self._sleep = sleep
        self._started_at = clock()
        self._retry_at: datetime | None = None
        self._cycle_lock = asyncio.Lock()
        self.state = SchedulerState(enabled=enabled)

    # --- laço ---------------------------------------------------------------

    async def run(self) -> None:
        if not self.state.enabled:
            log.info("coleta agendada desligada (COLLECT_ENABLED)")
            return
        log.info("agendador de coleta iniciado (a cada %.0fs)", self._interval)
        try:
            while True:
                await self._sleep(await self.tick())
        except asyncio.CancelledError:
            raise
        finally:
            self._lock.release()
            self.state.is_leader = False

    async def tick(self) -> float:
        """Um passo: garante a liderança, roda a coleta se venceu, e devolve quanto dormir."""
        was_leader = self.state.is_leader
        self.state.is_leader = await asyncio.to_thread(self._lock.acquire)
        if not self.state.is_leader:
            return LEADER_RETRY_SECONDS
        if not was_leader:
            fixed = await asyncio.to_thread(recover_interrupted, self._sessions)
            if fixed:
                log.warning("%d coleta(s) interrompida(s) marcadas como erro", fixed)

        due = await asyncio.to_thread(self._due)
        self.state.next_due = due
        wait = (due - self._clock()).total_seconds()
        if wait > 0:
            return min(wait, MAX_SLEEP_SECONDS)

        await self.collect_now()
        due = await asyncio.to_thread(self._due)
        self.state.next_due = due
        return min(max((due - self._clock()).total_seconds(), 1.0), MAX_SLEEP_SECONDS)

    def _due(self) -> datetime:
        return next_due(
            last_success_at(self._sessions, self._source),
            self._interval,
            started_at=self._started_at,
            startup_delay=self._startup_delay,
            retry_at=self._retry_at,
        )

    # --- coleta -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._cycle_lock.locked()

    async def collect_now(self) -> RunResult | None:
        """Roda uma coleta agora (laço ou disparo manual). Devolve None se já há uma em andamento."""
        if self._cycle_lock.locked():
            return None
        async with self._cycle_lock:
            self.state.running = True
            try:
                try:
                    collector = self._make_collector()
                except Exception as exc:  # ex.: credenciais ausentes; precisa aparecer em collect_runs
                    error = f"{type(exc).__name__}: {exc}"
                    log.error("não foi possível montar o coletor: %s", error)
                    await asyncio.to_thread(record_failure, self._sessions, self._source, error)
                    self._failed(error)
                    return None
                result = await asyncio.to_thread(run_collection, self._sessions, collector)
                if result.ok:
                    self.state.consecutive_failures, self.state.last_error, self._retry_at = 0, None, None
                else:
                    self._failed(result.error)
                return result
            finally:
                self.state.running = False

    def _failed(self, error: str | None) -> None:
        self.state.consecutive_failures += 1
        self.state.last_error = error
        delay = retry_delay(self.state.consecutive_failures, self._interval)
        self._retry_at = self._clock() + timedelta(seconds=delay)
        log.warning("coleta falhou (%d seguidas); nova tentativa em %.0fs", self.state.consecutive_failures, delay)
