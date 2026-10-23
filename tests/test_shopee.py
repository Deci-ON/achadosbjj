import hashlib
import json
from decimal import Decimal

import httpx
import pytest

from achados.collectors.base import BlockedError
from achados.collectors.shopee import ShopeeAPIError, ShopeeClient, ShopeeCollector, parse_node
from tests.conftest import FIXTURES

URL = "https://open-api.affiliate.shopee.com.br/graphql"


def page1():
    return json.loads((FIXTURES / "shopee_page1.json").read_text())


def make_client(handler) -> ShopeeClient:
    return ShopeeClient("app1", "secret1", URL, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_assinatura_cobre_exatamente_o_corpo_enviado():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json=page1())

    make_client(handler).query("query { x }", {"page": 1})

    timestamp = seen["auth"].split("Timestamp=")[1].split(",")[0]
    expected = hashlib.sha256(f"app1{timestamp}{seen['body']}secret1".encode()).hexdigest()
    assert seen["auth"] == f"SHA256 Credential=app1, Timestamp={timestamp}, Signature={expected}"


def test_parse_node_normaliza_precos():
    nodes = page1()["data"]["productOfferV2"]["nodes"]

    fone = parse_node(nodes[0])
    assert fone.external_id == "9.111"
    assert fone.price == Decimal("49.90")
    assert fone.discount_pct == 50
    assert fone.original_price == Decimal("99.80")
    assert fone.affiliate_url == "https://s.shopee.com.br/abc123"
    assert fone.commission_rate == Decimal("0.0825")
    assert fone.sales == 1520

    cabo = parse_node(nodes[1])
    assert cabo.original_price is None and cabo.discount_pct is None
    assert cabo.url == "https://s.shopee.com.br/def456"  # sem productLink, cai no link de afiliado

    assert parse_node(nodes[2]) is None  # sem preço


def test_collector_pagina_ate_hasnextpage_falso():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        pages.append(variables["page"])
        body = page1()
        body["data"]["productOfferV2"]["pageInfo"]["hasNextPage"] = variables["page"] < 2
        return httpx.Response(200, json=body)

    collector = ShopeeCollector(make_client(handler), keywords=["fone"], max_pages=5, delay=0)
    offers = list(collector.collect())

    assert pages == [1, 2]
    assert len(offers) == 4  # 2 ofertas válidas por página


def test_erros_da_api_viram_excecao():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Invalid Signature", "extensions": {"code": 10020}}]})

    with pytest.raises(ShopeeAPIError, match="Invalid Signature"):
        make_client(handler).query("query { x }", {})


def test_429_vira_blocked():
    with pytest.raises(BlockedError):
        make_client(lambda request: httpx.Response(429)).query("query { x }", {})


def test_credenciais_obrigatorias():
    with pytest.raises(ShopeeAPIError):
        ShopeeClient("", "", URL)
