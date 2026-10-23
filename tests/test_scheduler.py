import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from achados.models import CollectRun
from achados.scheduler import (
    LEADER_RETRY_SECONDS,
    MAX_SLEEP_SECONDS,
    CollectScheduler,
    LeaderLock,
    last_success_at,
    next_due,
    recover_interrupted,
    retry_delay,
)
from achados.schemas import Offer

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
HOUR = 3600.0
INTERVAL = 6 * HOUR


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


# --- cálculo do próximo horário (puro) -----------------------------------


def test_sem_nenhum_sucesso_espera_o_atraso_de_partida():
    assert next_due(None, INTERVAL, started_at=T0, startup_delay=30) == T0 + timedelta(seconds=30)


def test_com_sucesso_recente_vence_um_intervalo_depois_dele():
    last_ok = T0 - timedelta(hours=1)
    assert next_due(last_ok, INTERVAL, started_at=T0, startup_delay=30) == last_ok + timedelta(hours=6)


def test_coleta_atrasada_nao_dispara_no_mesmo_segundo_da_partida():
    last_ok = T0 - timedelta(days=2)
    assert next_due(last_ok, INTERVAL, started_at=T0, startup_delay=30) == T0 + timedelta(seconds=30)


def test_retry_at_adia_o_vencimento():
    retry_at = T0 + minutes(15)
    assert next_due(None, INTERVAL, started_at=T0, startup_delay=30, retry_at=retry_at) == retry_at
    # um retry_at que já passou não adianta nada
    assert next_due(None, INTERVAL, started_at=T0, startup_delay=30, retry_at=T0 - minutes(1)) == T0 + timedelta(seconds=30)


def test_data_sem_fuso_vinda_do_banco_e_aceita():
    naive = (T0 - timedelta(hours=1)).replace(tzinfo=None)
    assert next_due(naive, INTERVAL, started_at=T0, startup_delay=30) == T0 + timedelta(hours=5)


def test_espera_depois_de_falhas_cresce_ate_o_intervalo():
    assert [retry_delay(n, INTERVAL) for n in (1, 2, 3, 4, 5, 6, 7)] == [900, 1800, 3600, 7200, 14400, 21600, 21600]
    assert retry_delay(3, 1000) == 1000  # nunca passa do intervalo normal


# --- lock de líder --------------------------------------------------------


def test_fora_do_postgres_todo_mundo_e_lider(sqlite_engine):
    lock = LeaderLock(sqlite_engine)
    assert lock.acquire() and lock.verify()
    lock.release()


class FakeConn:
    def __init__(self, got=True, fail_on=None):
        self.got, self.fail_on, self.closed, self.statements = got, fail_on, False, []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("conexão perdida")
        return type("R", (), {"scalar": lambda self_: self.got})()

    def commit(self):
        pass

    def close(self):
        self.closed = True


class FakePgEngine:
    class dialect:
        name = "postgresql"

    def __init__(self, *conns):
        self._conns = list(conns)
        self.made = []

    def connect(self):
        conn = self._conns.pop(0)
        self.made.append(conn)
        return conn


def test_lock_postgres_assume_e_mantem_a_conexao_aberta():
    engine = FakePgEngine(FakeConn())
    lock = LeaderLock(engine)

    assert lock.acquire() is True
    assert lock.acquire() is True  # já líder: só confere a conexão
    assert len(engine.made) == 1 and not engine.made[0].closed
    assert any("pg_try_advisory_lock" in s for s in engine.made[0].statements)


def test_lock_ocupado_por_outro_worker_nao_assume_e_fecha_a_conexao():
    engine = FakePgEngine(FakeConn(got=False))
    lock = LeaderLock(engine)

    assert lock.acquire() is False
    assert engine.made[0].closed


def test_conexao_do_lock_caiu_reassume_com_conexao_nova():
    broken, fresh = FakeConn(fail_on="SELECT 1"), FakeConn()
    engine = FakePgEngine(broken, fresh)
    lock = LeaderLock(engine)
    assert lock.acquire() is True

    assert lock.verify() is True

    assert broken.closed and not fresh.closed and len(engine.made) == 2


def test_release_libera_o_lock_e_fecha():
    engine = FakePgEngine(FakeConn())
    lock = LeaderLock(engine)
    lock.acquire()
    conn = engine.made[0]

    lock.release()

    assert conn.closed and any("pg_advisory_unlock" in s for s in conn.statements)
    assert lock.verify() is False


# --- banco ----------------------------------------------------------------


def add_run(db, status, finished_at=None, source="mercadolivre"):
    db.add(CollectRun(source=source, status=status, finished_at=finished_at))
    db.commit()


def test_recover_interrupted_fecha_so_as_que_estavam_rodando(sessions, db):
    add_run(db, "running")
    add_run(db, "ok", T0)

    assert recover_interrupted(sessions, now=T0) == 1

    statuses = sorted(db.scalars(select(CollectRun.status)).all())
    assert statuses == ["error", "ok"]
    erro = db.scalars(select(CollectRun).where(CollectRun.status == "error")).one()
    assert "reiniciou" in erro.error and erro.finished_at is not None


def test_last_success_at_ignora_erros_e_outras_lojas(sessions, db):
    add_run(db, "ok", T0 - timedelta(hours=9))
    add_run(db, "ok", T0 - timedelta(hours=2))
    add_run(db, "error", T0 - timedelta(hours=1))
    add_run(db, "ok", T0, source="shopee")

    assert last_success_at(sessions, "mercadolivre") == T0 - timedelta(hours=2)
    assert last_success_at(sessions, "outra") is None


# --- o agendador ----------------------------------------------------------


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeLock:
    def __init__(self, leader=True):
        self.leader, self.released = leader, 0

    def acquire(self):
        return self.leader

    def release(self):
        self.released += 1


class FakeCollector:
    source = "mercadolivre"

    def __init__(self, fail=False):
        self.fail = fail

    def collect(self):
        if self.fail:
            raise RuntimeError("API caiu")
        yield Offer(source="mercadolivre", external_id="product:1", title="Kimono Jiu Jitsu", url="https://x", price=Decimal("1"))


def scheduler(sessions, clock, *, lock=None, collectors=None, **kw):
    queue = list(collectors or [FakeCollector()])
    made = []

    def make():
        made.append(1)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    sched = CollectScheduler(sessions, lock or FakeLock(), make, "mercadolivre", interval=INTERVAL, clock=clock, **kw)
    sched.made = made
    return sched


def run(coro):
    return asyncio.run(coro)


def count_runs(db):
    db.expire_all()
    return len(db.scalars(select(CollectRun)).all())


def test_antes_do_atraso_de_partida_nao_coleta(sessions, db):
    clock = Clock(T0)
    sched = scheduler(sessions, clock)

    wait = run(sched.tick())

    assert wait == pytest.approx(30) and count_runs(db) == 0


def test_vencida_a_partida_coleta_e_dorme_ate_o_proximo_ciclo(sessions, db):
    clock = Clock(T0)
    sched = scheduler(sessions, clock)
    clock.now = T0 + timedelta(seconds=31)

    wait = run(sched.tick())

    assert count_runs(db) == 1 and sched.state.consecutive_failures == 0
    assert wait == MAX_SLEEP_SECONDS  # o próximo ciclo está horas à frente; acorda de 5 em 5 min
    assert sched.state.next_due is not None


def test_reinicio_com_coleta_recente_nao_coleta_de_novo(sessions, db):
    add_run(db, "ok", T0 - timedelta(hours=1))
    clock = Clock(T0)
    sched = scheduler(sessions, clock)
    clock.now = T0 + timedelta(minutes=10)

    run(sched.tick())

    assert sched.made == [] and count_runs(db) == 1


def test_reinicio_com_coleta_antiga_coleta_depois_do_atraso(sessions, db):
    add_run(db, "ok", T0 - timedelta(days=2))
    clock = Clock(T0)
    sched = scheduler(sessions, clock)
    run(sched.tick())
    assert sched.made == []
    clock.now = T0 + timedelta(seconds=31)

    run(sched.tick())

    assert sched.made == [1] and count_runs(db) == 2


def test_falha_registra_erro_e_repete_depois_de_15_minutos_com_sucesso_zerando(sessions, db):
    clock = Clock(T0)
    sched = scheduler(sessions, clock, collectors=[FakeCollector(fail=True), FakeCollector()])
    clock.now = T0 + timedelta(seconds=31)

    run(sched.tick())
    assert sched.state.consecutive_failures == 1 and "API caiu" in sched.state.last_error

    clock.now += minutes(10)
    run(sched.tick())  # ainda dentro da espera
    assert sched.made == [1]

    clock.now += minutes(6)
    run(sched.tick())
    assert sched.made == [1, 1]
    assert sched.state.consecutive_failures == 0 and sched.state.last_error is None
    db.expire_all()
    assert sorted(db.scalars(select(CollectRun.status)).all()) == ["error", "ok"]


def test_coletor_que_nao_monta_vira_linha_de_erro_e_nao_derruba_o_laco(sessions, db):
    clock = Clock(T0)
    sched = scheduler(sessions, clock, collectors=[ValueError("ML_CLIENT_ID ausente")])
    clock.now = T0 + timedelta(seconds=31)

    run(sched.tick())

    run_row = db.scalars(select(CollectRun)).one()
    assert run_row.status == "error" and "ML_CLIENT_ID ausente" in run_row.error
    assert sched.state.consecutive_failures == 1 and not sched.state.running


def test_quem_nao_e_lider_nao_coleta_nem_mexe_nos_runs(sessions, db):
    add_run(db, "running")  # coleta do líder de verdade
    clock = Clock(T0)
    sched = scheduler(sessions, clock, lock=FakeLock(leader=False))
    clock.now = T0 + timedelta(hours=10)

    wait = run(sched.tick())

    assert wait == LEADER_RETRY_SECONDS and sched.made == [] and not sched.state.is_leader
    db.expire_all()
    assert db.scalars(select(CollectRun.status)).all() == ["running"]


def test_ao_virar_lider_fecha_as_coletas_interrompidas(sessions, db):
    add_run(db, "running")
    clock = Clock(T0)
    sched = scheduler(sessions, clock)

    run(sched.tick())

    db.expire_all()
    assert db.scalars(select(CollectRun.status)).all() == ["error"]


def test_disparo_manual_recusa_quando_ja_ha_coleta_em_andamento(sessions, db):
    clock = Clock(T0)
    sched = scheduler(sessions, clock)

    async def scenario():
        await sched._cycle_lock.acquire()
        try:
            return sched.busy, await sched.collect_now()
        finally:
            sched._cycle_lock.release()

    busy, result = run(scenario())

    assert busy is True and result is None and count_runs(db) == 0


def test_disparo_manual_coleta_e_reinicia_a_contagem_do_intervalo(sessions, db):
    clock = Clock(T0)
    sched = scheduler(sessions, clock)
    clock.now = T0 + timedelta(hours=1)

    result = run(sched.collect_now())
    assert result.ok
    # a coleta manual conta como sucesso: o laço não coleta de novo logo em seguida
    clock.now += minutes(5)
    run(sched.tick())

    assert sched.made == [1]


def test_desligado_o_laco_nao_faz_nada(sessions):
    sched = scheduler(sessions, Clock(T0), enabled=False)

    run(sched.run())

    assert sched.made == []


def test_cancelar_o_laco_solta_o_lock_de_lider(sessions):
    lock = FakeLock()
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise asyncio.CancelledError

    sched = scheduler(sessions, Clock(T0), lock=lock, sleep=fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        run(sched.run())

    assert len(calls) == 2 and lock.released == 1 and sched.state.is_leader is False
