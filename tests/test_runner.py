from decimal import Decimal

from sqlalchemy import func, select

from achados.models import CollectRun, Product
from achados.runner import run_collection
from achados.schemas import Offer


def offer(external_id: str, price: str = "100.00") -> Offer:
    return Offer(
        source="mercadolivre",
        external_id=external_id,
        title=f"Kimono Jiu Jitsu {external_id}",
        url=f"https://exemplo/{external_id}",
        price=Decimal(price),
    )


class FakeCollector:
    source = "mercadolivre"

    def __init__(self, offers, fail_after=None):
        self._offers = offers
        self._fail_after = fail_after

    def collect(self):
        for i, o in enumerate(self._offers):
            if self._fail_after is not None and i == self._fail_after:
                raise RuntimeError("API caiu")
            yield o


def test_coleta_ok_grava_produtos_e_fecha_o_run(sessions, db):
    result = run_collection(sessions, FakeCollector([offer("a"), offer("b")]))

    assert result.ok and (result.items_seen, result.items_new) == (2, 2)
    run = db.get(CollectRun, result.run_id)
    assert (run.status, run.error, run.finished_at is not None) == ("ok", None, True)
    assert db.scalar(select(func.count()).select_from(Product)) == 2


def test_falha_no_meio_grava_o_que_veio_antes_e_registra_o_erro(sessions, db):
    result = run_collection(sessions, FakeCollector([offer("a"), offer("b"), offer("c")], fail_after=2))

    assert not result.ok and result.error == "RuntimeError: API caiu"
    assert (result.items_seen, result.items_new) == (2, 2)  # a e b ficaram
    run = db.get(CollectRun, result.run_id)
    assert (run.status, run.error) == ("error", "RuntimeError: API caiu")
    assert db.scalar(select(func.count()).select_from(Product)) == 2


def test_falha_logo_no_inicio_nao_grava_nada_mas_o_run_fecha(sessions, db):
    result = run_collection(sessions, FakeCollector([offer("a")], fail_after=0))

    assert not result.ok and result.items_seen == 0
    assert db.get(CollectRun, result.run_id).status == "error"
    assert db.scalar(select(func.count()).select_from(Product)) == 0


def test_segunda_coleta_nao_duplica_produtos(sessions, db):
    run_collection(sessions, FakeCollector([offer("a")]))
    second = run_collection(sessions, FakeCollector([offer("a")]))

    assert (second.items_seen, second.items_new) == (1, 0)
    assert db.scalar(select(func.count()).select_from(Product)) == 1
    assert db.scalar(select(func.count()).select_from(CollectRun)) == 2
