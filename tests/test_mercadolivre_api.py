import json
from decimal import Decimal

import httpx
import pytest

from achados.collectors.base import BlockedError
from achados.collectors import mercadolivre_api
from achados.collectors.mercadolivre_api import (
    BJJ_DOMAINS,
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
SEARCH = fixture("ml_products_search.json")
PRODUCT_ID = PRODUCT["id"]
# Produtos do fixture de busca que têm ofertas no servidor falso: os 4 uniformes e a faixa.
SEARCH_WITH_OFFERS = {p["id"] for p in SEARCH["results"] if p["domain_id"] != "MLB-MUGS"}


class FakeML:
    """Servidor falso com as respostas reais gravadas em tests/fixtures."""

    def __init__(self, token_ttl: int = 21600):
        self.calls: list[str] = []
        self.search_params: list[dict] = []
        self.token_requests = 0
        self.token_ttl = token_ttl
        self.reject_next_with_401 = False
        self.with_offers = {PRODUCT_ID}
        self.search_400_from_offset: int | None = None  # como o ML: offset=200 devolve 400

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
        if path == "/products/search":
            params = request.url.params
            offset, limit = int(params["offset"]), int(params["limit"])
            self.search_params.append(dict(params))
            if self.search_400_from_offset is not None and offset >= self.search_400_from_offset:
                return httpx.Response(400, json={"message": "offset out of range", "status": 400})
            return httpx.Response(200, json={**SEARCH, "results": SEARCH["results"][offset : offset + limit]})
        if path == f"/products/{PRODUCT_ID}":
            return httpx.Response(200, json=PRODUCT)
        if path.startswith("/products/") and path.endswith("/items"):
            if path.split("/")[2] in self.with_offers:
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


def test_best_offer_resume_o_produto_pelo_menor_preco():
    offer = best_offer(PRODUCT, ITEMS["results"])

    # O item acompanhado é o produto de catálogo; o anúncio mais barato do momento só dá o link.
    assert offer.source == "mercadolivre"
    assert offer.external_id == f"product:{PRODUCT_ID}"
    assert offer.price == Decimal("1289.9")
    assert offer.original_price is None and offer.discount_pct is None
    assert offer.title == PRODUCT["name"]
    assert offer.url == f"https://www.mercadolivre.com.br/p/{PRODUCT_ID}?wid=MLB4669565403"
    assert offer.image_url == PRODUCT["pictures"][0]["url"]
    assert offer.affiliate_url is None
    assert offer.raw["item_id"] == "MLB4669565403"
    assert offer.raw["seller_id"] == 147445712
    assert offer.raw["offers_count"] == len(ITEMS["results"])
    assert offer.raw["domain_id"] == PRODUCT["domain_id"]


def test_o_anuncio_mais_barato_pode_trocar_sem_quebrar_a_identidade_do_produto():
    new = lambda item_id, price: {"item_id": item_id, "price": price, "condition": "new"}
    antes = best_offer(PRODUCT, [new("MLB1", 100), new("MLB2", 120)])
    depois = best_offer(PRODUCT, [new("MLB1", 100), new("MLB2", 90)])

    assert antes.external_id == depois.external_id  # mesmo histórico de preço
    assert (antes.raw["item_id"], depois.raw["item_id"]) == ("MLB1", "MLB2")
    assert depois.price == Decimal("90")


def test_desconto_declarado_vem_do_anuncio_mais_barato():
    results = [
        {"item_id": "MLB1", "price": 70, "original_price": 100, "condition": "new"},
        {"item_id": "MLB2", "price": 80, "original_price": 200, "condition": "new"},
    ]

    offer = best_offer(PRODUCT, results)

    assert (offer.raw["item_id"], offer.discount_pct, offer.original_price) == ("MLB1", 30, Decimal("100"))


def test_best_offer_ignora_usados_e_sem_preco():
    results = [
        {"item_id": "MLB1", "price": 100, "condition": "new"},
        {"item_id": "MLB2", "price": 80, "condition": "new"},
        {"item_id": "MLB3", "price": 10, "condition": "used"},
        {"item_id": "MLB4", "price": None, "condition": "new"},
    ]

    offer = best_offer(PRODUCT, results)

    assert offer.raw["item_id"] == "MLB2" and offer.raw["offers_count"] == 2


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
    assert [o.external_id for o in offers] == [f"product:{PRODUCT_ID}"]
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


# --- busca por palavra-chave ----------------------------------------------


def search_collector(fake, **kwargs):
    fake.with_offers = set(SEARCH_WITH_OFFERS)
    return MercadoLivreApiCollector(make_api(fake), sleep=lambda s: None, **kwargs)


def test_busca_filtra_por_dominio_do_nicho():
    fake = FakeML()
    collector = search_collector(fake, keywords=["kimono jiu jitsu"], domains=BJJ_DOMAINS)

    offers = list(collector.collect())

    # 4 uniformes + 1 faixa; a caneca (MLB-MUGS) nem chega a ter as ofertas consultadas.
    assert len(offers) == 5
    assert "/products/MLB60127635/items" not in fake.calls
    assert {o.raw["domain_id"] for o in offers} == set(BJJ_DOMAINS)
    assert fake.search_params[0]["q"] == "kimono jiu jitsu" and fake.search_params[0]["status"] == "active"


def test_busca_sem_dominio_traz_tudo():
    fake = FakeML()
    fake.with_offers = {p["id"] for p in SEARCH["results"]}

    offers = list(MercadoLivreApiCollector(make_api(fake), keywords=["kimono"], sleep=lambda s: None).collect())

    assert len(offers) == len(SEARCH["results"])


def test_busca_pagina_ate_vir_menos_que_o_tamanho_da_pagina(monkeypatch):
    monkeypatch.setattr(mercadolivre_api, "SEARCH_PAGE_SIZE", 3)
    fake = FakeML()

    list(search_collector(fake, keywords=["kimono"], search_pages=5).collect())

    # 6 resultados em páginas de 3: a 3ª página vem vazia e encerra a busca.
    assert [p["offset"] for p in fake.search_params] == ["0", "3", "6"]


def test_busca_respeita_search_pages(monkeypatch):
    monkeypatch.setattr(mercadolivre_api, "SEARCH_PAGE_SIZE", 3)
    fake = FakeML()

    list(search_collector(fake, keywords=["kimono"], search_pages=1).collect())

    assert len(fake.search_params) == 1


def test_mesmo_produto_em_duas_palavras_chave_e_coletado_uma_vez():
    fake = FakeML()
    collector = search_collector(fake, keywords=["kimono jiu jitsu", "kimono bjj"], domains=BJJ_DOMAINS)

    assert len(list(collector.collect())) == 5
    assert fake.calls.count("/products/MLB79731026/items") == 1
    assert len(fake.search_params) == 2


def test_categorias_e_palavras_chave_se_combinam_sem_repetir():
    fake = FakeML()
    fake.with_offers = set(SEARCH_WITH_OFFERS) | {PRODUCT_ID}
    collector = MercadoLivreApiCollector(
        make_api(fake), categories=["MLB47731"], keywords=["kimono"], domains=BJJ_DOMAINS, sleep=lambda s: None
    )

    ids = [o.raw["product_id"] for o in collector.collect()]

    assert PRODUCT_ID in ids and "MLB79731026" in ids and len(ids) == len(set(ids))


def test_so_palavras_chave_nao_usa_as_categorias_padrao():
    fake = FakeML()

    list(search_collector(fake, keywords=["kimono"]).collect())

    assert not any(c.startswith("/highlights") for c in fake.calls)


# --- limite de taxa (429) -------------------------------------------------


def rate_limited_api(responses: list[httpx.Response], sleeps: list[float]) -> MercadoLivreAPI:
    queue = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return next(queue)

    return make_api(handler, sleep=sleeps.append)


def test_429_espera_e_tenta_de_novo():
    sleeps: list[float] = []
    api = rate_limited_api([httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"ok": True})], sleeps)

    assert api.get("/products/search") == {"ok": True}
    assert sleeps == [3.0, 10.0]


def test_429_respeita_retry_after():
    sleeps: list[float] = []
    api = rate_limited_api([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})], sleeps)

    api.get("/products/search")

    assert sleeps == [7.0]


def test_429_persistente_vira_blocked_depois_das_esperas():
    sleeps: list[float] = []
    api = rate_limited_api([httpx.Response(429)] * 4, sleeps)

    with pytest.raises(BlockedError, match="429"):
        api.get("/products/search")
    assert sleeps == [3.0, 10.0, 30.0]


# --- limite de paginação da busca (400) ----------------------------------


def test_400_de_paginacao_encerra_a_busca_sem_derrubar_a_coleta(monkeypatch):
    monkeypatch.setattr(mercadolivre_api, "SEARCH_PAGE_SIZE", 3)
    fake = FakeML()
    fake.search_400_from_offset = 6  # a 3ª página (offset=6) já dá 400, como o offset=200 do ML

    offers = list(search_collector(fake, keywords=["kimono"], search_pages=10).collect())

    assert [p["offset"] for p in fake.search_params] == ["0", "3", "6"]  # parou no primeiro 400
    assert len(offers) == len(SEARCH_WITH_OFFERS)  # o que veio nas páginas anteriores foi coletado


def test_400_nao_impede_as_demais_palavras_chave(monkeypatch):
    monkeypatch.setattr(mercadolivre_api, "SEARCH_PAGE_SIZE", 3)
    fake = FakeML()
    fake.search_400_from_offset = 6

    list(search_collector(fake, keywords=["kimono", "faixa"], search_pages=10).collect())

    assert [p["q"] for p in fake.search_params].count("faixa") == 3  # a 2ª palavra também foi buscada


def test_400_na_primeira_pagina_e_erro_de_verdade():
    fake = FakeML()
    fake.search_400_from_offset = 0  # consulta inválida, não fim de resultados

    with pytest.raises(httpx.HTTPStatusError):
        list(search_collector(fake, keywords=["kimono"]).collect())


def test_outros_erros_de_busca_continuam_subindo(monkeypatch):
    monkeypatch.setattr(mercadolivre_api, "SEARCH_PAGE_SIZE", 3)

    class Boom(FakeML):
        def __call__(self, request):
            if request.url.path == "/products/search" and request.url.params["offset"] != "0":
                return httpx.Response(500)
            return super().__call__(request)

    # 500 depois da primeira página não é "fim de resultados": a falha precisa aparecer
    with pytest.raises(httpx.HTTPStatusError):
        list(search_collector(Boom(), keywords=["kimono"], search_pages=5).collect())
