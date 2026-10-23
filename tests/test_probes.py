from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from achados.models import EmptyCatalogProduct
from achados.probes import DbProbeCache

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def count(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(EmptyCatalogProduct))


def test_guarda_os_vazios_e_devolve_os_recentes(sessions, db):
    cache = DbProbeCache(sessions, clock=Clock(T0))

    cache.record(empty={"MLB1", "MLB2"}, with_offers=set())

    assert cache.fresh_empty_ids() == {"MLB1", "MLB2"} and count(db) == 2


def test_passado_o_prazo_o_produto_volta_a_ser_checado(sessions):
    clock = Clock(T0)
    cache = DbProbeCache(sessions, recheck_after=timedelta(hours=24), clock=clock)
    cache.record(empty={"MLB1"}, with_offers=set())

    clock.now = T0 + timedelta(hours=23)
    assert cache.fresh_empty_ids() == {"MLB1"}

    clock.now = T0 + timedelta(hours=25)
    assert cache.fresh_empty_ids() == set()


def test_quem_ganhou_oferta_sai_do_cache(sessions, db):
    cache = DbProbeCache(sessions, clock=Clock(T0))
    cache.record(empty={"MLB1", "MLB2"}, with_offers=set())

    cache.record(empty=set(), with_offers={"MLB1", "MLB-NUNCA-VISTO"})

    assert cache.fresh_empty_ids() == {"MLB2"} and count(db) == 1


def test_gravar_de_novo_atualiza_a_data_sem_duplicar(sessions, db):
    clock = Clock(T0)
    cache = DbProbeCache(sessions, clock=clock)
    cache.record(empty={"MLB1"}, with_offers=set())

    clock.now = T0 + timedelta(hours=30)  # já venceu...
    assert cache.fresh_empty_ids() == set()
    cache.record(empty={"MLB1"}, with_offers=set())  # ...e foi reconfirmado vazio

    assert cache.fresh_empty_ids() == {"MLB1"} and count(db) == 1


def test_sem_nada_para_gravar_nao_faz_nada(sessions, db):
    DbProbeCache(sessions, clock=Clock(T0)).record(empty=set(), with_offers=set())

    assert count(db) == 0


def test_lotes_grandes_passam_dos_limites_de_variaveis_do_banco(sessions, db):
    cache = DbProbeCache(sessions, clock=Clock(T0))
    ids = {f"MLB{i}" for i in range(1500)}
    cache.record(empty=ids, with_offers=set())
    assert len(cache.fresh_empty_ids()) == 1500

    cache.record(empty=set(), with_offers=ids)  # 1500 ids no DELETE, em lotes

    assert count(db) == 0
