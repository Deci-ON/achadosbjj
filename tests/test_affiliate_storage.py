from decimal import Decimal

from sqlalchemy import select

from achados.models import Product
from achados.schemas import Offer
from achados.storage import finish_run, save_offers, start_run


def offer(external_id="product:1", price="100", affiliate_url=None) -> Offer:
    return Offer(
        source="mercadolivre",
        external_id=external_id,
        title="Kimono Jiu Jitsu",
        url=f"https://www.mercadolivre.com.br/p/{external_id}",
        affiliate_url=affiliate_url,
        price=Decimal(price),
    )


def collect(db, *offers):
    run = start_run(db, "mercadolivre")
    save_offers(db, run, offers)
    finish_run(db, run)
    db.commit()


def product(db) -> Product:
    db.expire_all()
    return db.scalars(select(Product)).one()


def test_a_coleta_nao_apaga_o_link_de_afiliado_colado_na_mao(db):
    collect(db, offer())
    product(db).affiliate_url = "https://mercadolivre.com/sec/abc123"
    db.commit()

    collect(db, offer(price="90"))  # o coletor do ML nunca traz link de afiliado (vem None)

    assert product(db).affiliate_url == "https://mercadolivre.com/sec/abc123"
    assert product(db).price == Decimal("90")  # o resto continua sendo atualizado


def test_link_de_afiliado_que_vem_do_coletor_continua_valendo(db):
    # Shopee devolve o link de afiliado na própria API (offerLink): esse segue atualizando.
    collect(db, offer(affiliate_url="https://s.shopee.com.br/aaa"))
    assert product(db).affiliate_url == "https://s.shopee.com.br/aaa"

    collect(db, offer(affiliate_url="https://s.shopee.com.br/bbb"))

    assert product(db).affiliate_url == "https://s.shopee.com.br/bbb"


def test_produto_novo_sem_link_fica_sem_link(db):
    collect(db, offer())

    assert product(db).affiliate_url is None
