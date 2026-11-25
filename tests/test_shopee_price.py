"""Aviso de preço da Shopee na mensagem e conferência de preço da Shopee antes de enviar."""

import json
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from achados.collectors.shopee import ShopeeAPIError, ShopeeClient
from achados.messages import SHOPEE_PRICE_NOTE, format_offer_message
from achados.models import Product
from achados.sender import SenderError, ShopeePriceChecker

from tests.test_sender import FakeChecker, FakeClient, NOON, jobs, make_product, make_sender_app, make_worker, queue
from tests.test_service import AUTH, add_product

URL = "https://shopee.test/graphql"


def make_shopee_client(handler) -> ShopeeClient:
    return ShopeeClient("app1", "secret1", URL, http=httpx.Client(transport=httpx.MockTransport(handler)))


def shopee_product(db, price="429.38", external_id="84499012.17979995178", raw=None) -> Product:
    product = Product(
        source="shopee",
        external_id=external_id,
        title="Kimono Jiu Jitsu Bjj First Trançado Pretorian Adulto 350g",
        url="https://shopee.com.br/product/84499012/17979995178",
        affiliate_url="https://s.shopee.com.br/comum",
        price=Decimal(price),
        raw=raw if raw is not None else {},
    )
    db.add(product)
    db.commit()
    return product


# --- aviso na mensagem ----------------------------------------------------------------------


def test_aviso_vai_depois_dos_cupons_e_antes_do_link_nos_dois_formatos():
    plain = format_offer_message(title="Kimono", price=429.38, link="https://l/1", coupon_lines=["🎟️ cupom X"], price_note=SHOPEE_PRICE_NOTE)
    offer = format_offer_message(
        title="Kimono",
        price=300,
        link="https://l/1",
        is_offer=True,
        reference_price=400,
        real_discount_pct=25,
        coupon_lines=["🎟️ cupom X"],
        price_note=SHOPEE_PRICE_NOTE,
    )

    for text in (plain, offer):
        assert text.endswith(f"🎟️ cupom X\n{SHOPEE_PRICE_NOTE}\n\n👉 https://l/1")
    assert format_offer_message(title="K", price=1, link="L", price_note=SHOPEE_PRICE_NOTE) == f"*K*\n\n💰 R$ 1,00\n\n{SHOPEE_PRICE_NOTE}\n\n👉 L"


def test_sem_aviso_a_mensagem_e_a_de_sempre():
    base = dict(title="K", price=1, link="L")
    assert format_offer_message(**base) == format_offer_message(**base, price_note="") == format_offer_message(**base, price_note="  ")
    assert format_offer_message(**base) == "*K*\n\n💰 R$ 1,00\n\n👉 L"


def test_so_a_oferta_da_shopee_leva_o_aviso_na_api(sessions, db):
    add_product(db, "84499012.1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    add_product(db, "product:ml", "Kimono Jiu Jitsu Atama", [(40, "500"), (3, "350")], original="500")
    db.scalars(select(Product).where(Product.external_id == "84499012.1")).one().source = "shopee"  # o helper só cria produtos do ML
    db.commit()

    with TestClient(make_sender_app(sessions, None)) as client:
        items = client.get("/api/offers", headers=AUTH).json()["items"]

    by_source = {i["source"]: i["message"] for i in items}
    assert SHOPEE_PRICE_NOTE in by_source["shopee"] and SHOPEE_PRICE_NOTE not in by_source["mercadolivre"]


# --- cliente: product_offer -------------------------------------------------------------------


def test_product_offer_consulta_por_loja_e_item_com_argumentos_inline():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        node = {"itemId": "17979995178", "shopId": "84499012", "priceMin": "429.38", "priceMax": "459.90"}
        return httpx.Response(200, json={"data": {"productOfferV2": {"nodes": [node]}}})

    node = make_shopee_client(handler).product_offer(84499012, 17979995178)

    assert node["priceMin"] == "429.38"
    assert seen["body"]["variables"] == {}
    assert "shopId: 84499012, itemId: 17979995178, limit: 1" in seen["body"]["query"]


def test_product_offer_sem_resultado_devolve_none():
    handler = lambda request: httpx.Response(200, json={"data": {"productOfferV2": {"nodes": []}}})  # noqa: E731
    assert make_shopee_client(handler).product_offer(1, 2) is None


# --- ShopeePriceChecker -----------------------------------------------------------------------


class FakeShopeeApi:
    def __init__(self, node=None, error=None):
        self.node, self.error, self.calls = node, error, []

    def product_offer(self, shop_id, item_id):
        self.calls.append((shop_id, item_id))
        if self.error:
            raise self.error
        return self.node


def test_conferencia_le_loja_e_item_do_external_id_ou_do_raw(db):
    api = FakeShopeeApi({"priceMin": "399.90"})
    checker = ShopeePriceChecker(api)

    assert checker.current_price(shopee_product(db)) == Decimal("399.90")
    assert checker.current_price(shopee_product(db, external_id="x", raw={"shopId": "7", "itemId": "8"})) == Decimal("399.90")
    assert api.calls == [(84499012, 17979995178), (7, 8)]


def test_sem_ids_vale_o_preco_coletado_e_sem_anuncio_e_none(db):
    product = shopee_product(db, external_id="sem-ids")
    assert ShopeePriceChecker(FakeShopeeApi()).current_price(product) == Decimal("429.38")
    assert ShopeePriceChecker(FakeShopeeApi(node=None)).current_price(shopee_product(db)) is None


def test_resposta_sem_preco_vira_erro_para_tentar_de_novo(db):
    with pytest.raises(SenderError, match="não devolveu o preço"):
        ShopeePriceChecker(FakeShopeeApi({"itemId": "1"})).current_price(shopee_product(db))


# --- worker ---------------------------------------------------------------------------------


def worker_with_shopee(sessions, client, checker, ml_checker=None):
    worker = make_worker(sessions, client, clock=[NOON], checker=ml_checker)
    worker._shopee_checker = checker
    return worker


def test_cancela_se_o_preco_da_shopee_subiu(sessions, db):
    queue(db, shopee_product(db, "300"), groups=("g1@g.us", "g2@g.us"))
    client = FakeClient()

    worker_with_shopee(sessions, client, FakeChecker("350")).step()

    assert client.sent == []
    assert [(j.status, j.error) for j in jobs(db)] == [("cancelled", "preço subiu de R$ 300.00 para R$ 350")] * 2


def test_preco_da_shopee_estavel_ou_menor_ainda_manda(sessions, db):
    queue(db, shopee_product(db, "300"))
    client = FakeClient()

    worker_with_shopee(sessions, client, FakeChecker("250")).step()

    assert len(client.sent) == 1


def test_cancela_se_o_produto_saiu_da_shopee(sessions, db):
    queue(db, shopee_product(db))

    worker_with_shopee(sessions, FakeClient(), FakeChecker(None)).step()

    assert [(j.status, j.error) for j in jobs(db)] == [("cancelled", "o produto não está mais na Shopee")]


def test_nunca_manda_da_shopee_sem_conseguir_conferir(sessions, db):
    queue(db, shopee_product(db))
    checker = FakeChecker()
    checker.error = ShopeeAPIError('[{"extensions": {"code": 10000}}]')
    client = FakeClient()
    worker = worker_with_shopee(sessions, client, checker)

    worker.step()

    assert client.sent == [] and [j.status for j in jobs(db)] == ["queued"]
    assert "conferência de preço falhou" in worker.state.last_error


def test_sem_conferidor_da_shopee_o_comportamento_antigo_continua(sessions, db):
    queue(db, shopee_product(db, "300"))
    client = FakeClient()

    make_worker(sessions, client, clock=[NOON]).step()  # sem checker nenhum

    assert len(client.sent) == 1


def test_ml_continua_usando_o_proprio_conferidor(sessions, db):
    queue(db, make_product(db, "300"))
    client = FakeClient()

    worker_with_shopee(sessions, client, FakeChecker("999"), ml_checker=FakeChecker("300")).step()  # o da Shopee não é consultado

    assert len(client.sent) == 1
