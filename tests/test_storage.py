from decimal import Decimal

from sqlalchemy import func, select

from achados.models import PriceSnapshot, Product
from achados.schemas import Offer
from achados.storage import finish_run, save_offers, start_run


def offer(external_id="1", price="10.00", original="20.00", **kw) -> Offer:
    return Offer(
        source="shopee",
        external_id=external_id,
        title=f"Produto {external_id}",
        url=f"https://shopee.com.br/{external_id}",
        price=Decimal(price),
        original_price=Decimal(original) if original else None,
        discount_pct=50 if original else None,
        raw={"id": external_id},
        **kw,
    )


def test_primeira_coleta_cria_produto_e_snapshot(session):
    run = start_run(session, "shopee")
    save_offers(session, run, [offer("1"), offer("2")])
    finish_run(session, run)
    session.commit()

    assert session.scalar(select(func.count()).select_from(Product)) == 2
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 2
    assert (run.items_seen, run.items_new, run.status) == (2, 2, "ok")


def test_recoleta_sem_mudanca_de_preco_nao_gera_snapshot(session):
    save_offers(session, start_run(session, "shopee"), [offer("1")])
    run2 = start_run(session, "shopee")
    save_offers(session, run2, [offer("1")])
    session.commit()

    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1
    assert (run2.items_seen, run2.items_new) == (1, 0)


def test_mudanca_de_preco_atualiza_produto_e_guarda_historico(session):
    save_offers(session, start_run(session, "shopee"), [offer("1", price="10.00")])
    save_offers(session, start_run(session, "shopee"), [offer("1", price="7.50")])
    session.commit()

    product = session.scalars(select(Product)).one()
    prices = session.scalars(select(PriceSnapshot.price).order_by(PriceSnapshot.id)).all()
    assert product.price == Decimal("7.50")
    assert prices == [Decimal("10.00"), Decimal("7.50")]
    assert product.raw == {"id": "1"}


def test_duplicata_no_mesmo_lote_conta_uma_vez(session):
    run = start_run(session, "shopee")
    save_offers(session, run, [offer("1", price="10.00"), offer("1", price="9.00")])
    session.commit()

    assert session.scalars(select(Product)).one().price == Decimal("9.00")
    assert (run.items_seen, run.items_new) == (1, 1)


def test_mesmo_external_id_em_lojas_diferentes_nao_colide(session):
    ml = offer("1").model_copy(update={"source": "mercadolivre"})
    run = start_run(session, "shopee")
    save_offers(session, run, [offer("1")])
    save_offers(session, run, [ml])
    session.commit()

    assert session.scalar(select(func.count()).select_from(Product)) == 2
