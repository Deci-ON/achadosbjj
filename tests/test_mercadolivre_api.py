import json
from decimal import Decimal

import httpx
import pytest

from achados.collectors.base import BlockedError
from achados.collectors.mercadolivre_api import (
    MercadoLivreAPI,
    MercadoLivreApiCollector,
    MercadoLivreAPIError,
    best_offer,
)
from tests.conftest import FIXTURES


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


HIGHLIGHTS = fixture("ml_highlights_MLB1051.json")
PRODUCT = fixture("ml_product.json")
ITEMS = fixture("ml_product_items.json")
PRODUCT_ID = PRODUCT["id"]


class FakeML:
    """Servidor falso com as respostas reais gravadas em tests/fixtures."""

    def __init__(self, token_ttl: int = 21600):
        self.calls: list[str] = []
        self.token_requests = 0
        self.token_ttl = token_ttl
        self.reject_next_with_401 = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/oauth/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}", "expires_in": self.token_ttl})
        if self.reject_next_with_401:
            self.reject_next_with_401 = False
            return httpx.Response(401, json={"error": "invalid_token"})
        if path.startswith("/highlights/MLB/category/"):
            return httpx.Response(200, json=HIGHLIGHTS)
        if path == f"/products/{PRODUCT_ID}":
            return httpx.Response(200, json=PRODUCT)
        if path == f"/products/{PRODUCT_ID}/items":
            return httpx.Response(200, json=ITEMS)
        return httpx.Response(404, json={"error": "not_found"})


def make_api(handler, **kwargs) -> MercadoLivreAPI:
    return MercadoLivreAPI("cid", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def test_credenciais_obrigatorias():
    with pytest.raises(MercadoLivreAPIError):
        MercadoLivreAPI("", "")


def test_token_e_reaproveitado_entre_chamadas():
    fake = FakeML()
    api = make_api(fake)

    api.get("/products/x")
    api.get("/products/y")

    assert fake.token_requests == 1


def test_token_expirado_e_renovado():
    fake = FakeML(token_ttl=100)  # menos a margem de 60s: vence em 40s
    now = [0.0]
    api = make_api(fake, clock=lambda: now[0])

    api.get("/products/x")
    now[0] = 41.0
    api.get("/products/x")

    assert fake.token_requests == 2


def test_401_renova_o_token_e_repete_a_chamada():
    fake = FakeML()
    api = make_api(fake)
    api.get("/products/x")
    fake.reject_next_with_401 = True

    assert api.get(f"/products/{PRODUCT_ID}")["id"] == PRODUCT_ID
    assert fake.token_requests == 2


def test_404_devolve_none_e_403_vira_blocked():
    api = make_api(
        lambda r: httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if r.url.path == "/oauth/token"
        else httpx.Response(404 if r.url.path == "/a" else 403)
    )

    assert api.get("/a") is None
    with pytest.raises(BlockedError):
        api.get("/sites/MLB/search")


def test_credenciais_recusadas():
    api = make_api(lambda r: httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(MercadoLivreAPIError, match="recusadas"):
        api.get("/products/x")


def test_best_offer_escolhe_maior_desconto_e_normaliza():
    offer = best_offer(PRODUCT, ITEMS["results"])

    # Duas ofertas têm original_price 1699: a de R$ 1.392 (18%) vence a de R$ 1.499 (11%).
    assert offer.source == "mercadolivre"
    assert offer.external_id == "MLB4594882681"
    assert offer.price == Decimal("1392")
    assert offer.original_price == Decimal("1699")
    assert offer.discount_pct == 18
    assert offer.title == PRODUCT["name"]
    assert offer.url == f"https://www.mercadolivre.com.br/p/{PRODUCT_ID}?wid=MLB4594882681"
    assert offer.image_url == PRODUCT["pictures"][0]["url"]
    assert offer.affiliate_url is None
    assert offer.raw["offer"]["seller_id"] == 480263032


def test_best_offer_sem_desconto_pega_o_menor_preco_e_ignora_usados():
    results = [
        {"item_id": "MLB1", "price": 100, "condition": "new"},
        {"item_id": "MLB2", "price": 80, "condition": "new"},
        {"item_id": "MLB3", "price": 10, "condition": "used"},
        {"item_id": "MLB4", "price": None, "condition": "new"},
    ]

    offer = best_offer(PRODUCT, results)

    assert offer.external_id == "MLB2"
    assert offer.original_price is None and offer.discount_pct is None


def test_best_offer_sem_candidatas():
    assert best_offer(PRODUCT, []) is None
    assert best_offer(PRODUCT, [{"item_id": "MLB3", "price": 10, "condition": "used"}]) is None


def test_preco_original_menor_que_o_preco_e_ignorado():
    offer = best_offer(PRODUCT, [{"item_id": "MLB1", "price": 100, "original_price": 90, "condition": "new"}])
    assert offer.original_price is None and offer.discount_pct is None


def test_collector_so_usa_produtos_de_catalogo_e_nao_repete():
    fake = FakeML()
    collector = MercadoLivreApiCollector(make_api(fake), categories=["MLB1051", "MLB1000"], sleep=lambda s: None)

    offers = list(collector.collect())

    # Só o primeiro PRODUCT do destaque tem fixture de produto; os demais dão 404 e são pulados.
    assert [o.external_id for o in offers] == ["MLB4594882681"]
    assert not any("/user-products" in c for c in fake.calls)
    # A segunda categoria devolve os mesmos produtos: não são buscados de novo.
    assert fake.calls.count(f"/products/{PRODUCT_ID}") == 1


def test_collector_respeita_per_category_e_min_discount():
    fake = FakeML()

    limited = MercadoLivreApiCollector(make_api(fake), categories=["MLB1051"], per_category=1, sleep=lambda s: None)
    list(limited.collect())
    assert sum(c.startswith("/products/") and c.endswith("/items") for c in fake.calls) == 1

    strict = MercadoLivreApiCollector(make_api(FakeML()), categories=["MLB1051"], min_discount=50, sleep=lambda s: None)
    assert list(strict.collect()) == []
