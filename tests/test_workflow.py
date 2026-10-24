from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from achados.models import Product, SentOffer
from achados.scoring import LEARNING, NO_OFFER, OFFER, Verdict
from achados.workflow import (
    MAX_BATCH,
    already_sent,
    below_cut,
    is_ready,
    needs_link,
    pair_links,
    recent_sends,
    record_send,
    undo_send,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def make(db, external_id, price="100", source="mercadolivre", affiliate_url=None) -> Product:
    product = Product(
        source=source,
        external_id=external_id,
        title=f"Kimono Jiu Jitsu {external_id}",
        url=f"https://www.mercadolivre.com.br/p/{external_id}",
        affiliate_url=affiliate_url,
        price=Decimal(price),
    )
    db.add(product)
    db.commit()
    return product


def verdict(status=OFFER) -> Verdict:
    return Verdict(status=status, score=80)


# --- quem entra na fila de links e quem está pronto ----------------------


def test_so_oferta_do_ml_sem_link_entra_na_fila(db):
    sem_link = make(db, "a")
    com_link = make(db, "b", affiliate_url="https://meli.la/x")
    shopee = make(db, "c", source="shopee")

    assert needs_link(sem_link, verdict()) is True
    assert needs_link(com_link, verdict()) is False  # já tem
    assert needs_link(shopee, verdict()) is False  # a Shopee traz o link pela API
    assert needs_link(sem_link, verdict(LEARNING)) is False  # ainda não é oferta
    assert needs_link(sem_link, verdict(NO_OFFER)) is False


def test_corte_de_score_decide_quem_entra_na_fila(db):
    sem_link = make(db, "a")
    com_link = make(db, "b", affiliate_url="https://meli.la/x")
    fraca = Verdict(status=OFFER, score=55)

    assert needs_link(sem_link, fraca) is True  # sem corte (padrão 0) entra
    assert needs_link(sem_link, fraca, 60) is False
    assert needs_link(sem_link, Verdict(status=OFFER, score=60), 60) is True  # o corte é inclusivo
    assert below_cut(sem_link, fraca, 60) is True
    assert below_cut(sem_link, verdict(), 60) is False  # score 80: entra na fila
    assert below_cut(com_link, fraca, 60) is False  # já tem link: não é fila nem "abaixo do corte"
    assert below_cut(sem_link, Verdict(status=NO_OFFER, score=10), 60) is False  # nem é oferta


def test_pronta_e_oferta_com_link_que_nao_foi_enviada_agora(db):
    com_link = make(db, "a", affiliate_url="https://meli.la/x")
    sem_link = make(db, "b")

    assert is_ready(com_link, verdict(), None) is True
    assert is_ready(sem_link, verdict(), None) is False  # falta o link
    assert is_ready(com_link, verdict(LEARNING), None) is False  # deixou de ser oferta


def test_enviada_nao_volta_a_menos_que_o_preco_caia_mais_de_3_por_cento(db):
    produto = make(db, "a", price="100", affiliate_url="https://meli.la/x")
    sent = record_send(db, produto, NOW)
    db.commit()

    assert already_sent(produto, sent) is True
    produto.price = Decimal("98")  # caiu só 2%
    assert is_ready(produto, verdict(), sent) is False
    produto.price = Decimal("96")  # caiu 4%: vale reenviar
    assert is_ready(produto, verdict(), sent) is True
    produto.price = Decimal("120")  # subiu
    assert is_ready(produto, verdict(), sent) is False


def test_nunca_enviada_nao_conta_como_enviada(db):
    assert already_sent(make(db, "a"), None) is False


# --- registro de envios ---------------------------------------------------


def test_envio_guarda_o_preco_do_momento_e_vale_por_sete_dias(db):
    produto = make(db, "a", price="150")
    record_send(db, produto, NOW)
    db.commit()

    assert recent_sends(db, NOW + timedelta(days=6))[produto.id].price == Decimal("150")
    assert recent_sends(db, NOW + timedelta(days=8)) == {}  # passou da carência


def test_vale_o_ultimo_envio_do_produto(db):
    produto = make(db, "a", price="150")
    record_send(db, produto, NOW)
    produto.price = Decimal("120")
    record_send(db, produto, NOW + timedelta(days=1))
    db.commit()

    assert recent_sends(db, NOW + timedelta(days=2))[produto.id].price == Decimal("120")


def test_desfazer_remove_so_o_envio_mais_recente(db):
    produto = make(db, "a", price="150")
    record_send(db, produto, NOW)
    produto.price = Decimal("120")
    record_send(db, produto, NOW + timedelta(days=1))
    db.commit()

    assert undo_send(db, produto.id) is True
    db.commit()

    assert recent_sends(db, NOW + timedelta(days=2))[produto.id].price == Decimal("150")
    assert undo_send(db, 999999) is False


# --- pareamento em lote ---------------------------------------------------


def links_of(db):
    db.expire_all()
    return {p.external_id: p.affiliate_url for p in db.scalars(select(Product))}


def test_pareia_pela_ordem(db):
    a, b, c = make(db, "a"), make(db, "b"), make(db, "c")

    saved = pair_links(db, [(c.id, "https://meli.la/ccc"), (a.id, "https://meli.la/aaa"), (b.id, "https://meli.la/bbb")])
    db.commit()

    assert saved == 3
    assert links_of(db) == {"a": "https://meli.la/aaa", "b": "https://meli.la/bbb", "c": "https://meli.la/ccc"}


def test_tudo_ou_nada_um_link_invalido_nao_salva_nenhum(db):
    a, b = make(db, "a"), make(db, "b")

    with pytest.raises(ValueError, match="domínio não permitido"):
        pair_links(db, [(a.id, "https://meli.la/aaa"), (b.id, "https://golpe.com/bbb")])

    assert links_of(db) == {"a": None, "b": None}


def test_link_repetido_indica_desalinhamento_e_nao_salva(db):
    a, b = make(db, "a"), make(db, "b")

    with pytest.raises(ValueError, match="repetido"):
        pair_links(db, [(a.id, "https://meli.la/igual"), (b.id, "https://meli.la/igual")])

    assert links_of(db) == {"a": None, "b": None}


def test_nao_toma_o_link_de_outro_produto(db):
    make(db, "dono", affiliate_url="https://meli.la/dele")
    novo = make(db, "novo")

    with pytest.raises(ValueError, match="já é de outro produto"):
        pair_links(db, [(novo.id, "https://meli.la/dele")])

    assert links_of(db)["novo"] is None


def test_regravar_o_proprio_link_do_produto_e_permitido(db):
    a = make(db, "a", affiliate_url="https://meli.la/aaa")

    assert pair_links(db, [(a.id, "https://meli.la/aaa")]) == 1  # o ML devolve o mesmo código para o mesmo produto


def test_produto_repetido_ou_inexistente_ou_lista_vazia_ou_grande_demais(db):
    a = make(db, "a")

    with pytest.raises(ValueError, match="produto repetido"):
        pair_links(db, [(a.id, "https://meli.la/1"), (a.id, "https://meli.la/2")])
    with pytest.raises(LookupError):
        pair_links(db, [(999999, "https://meli.la/1")])
    with pytest.raises(ValueError, match="nenhum link"):
        pair_links(db, [])
    with pytest.raises(ValueError, match="no máximo"):
        pair_links(db, [(i, f"https://meli.la/{i}") for i in range(MAX_BATCH + 1)])


def test_o_envio_some_junto_com_o_produto_no_banco_real_via_fk():
    # A FK com ondelete=CASCADE existe no modelo (no SQLite dos testes ela não é imposta por padrão).
    fk = next(iter(SentOffer.__table__.c.product_id.foreign_keys))
    assert fk.ondelete == "CASCADE"
