"""Link da Shopee por grupo (generateShortLink + sub-ids) e vendas por grupo (conversionReport)."""

import hashlib
import json
import re
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from achados.collectors.shopee import ShopeeAPIError, ShopeeClient
from achados.models import Product, SendGroup
from achados.sender import (
    ShopeeGroupLinks,
    enqueue_offer,
    group_tag,
    replace_groups,
    saved_groups,
    summarize_conversions,
)
from tests.test_sender import FakeClient, jobs, make_product, make_sender_app
from tests.test_service import AUTH

URL = "https://shopee.test/graphql"


def make_client(handler) -> ShopeeClient:
    return ShopeeClient("app1", "secret1", URL, http=httpx.Client(transport=httpx.MockTransport(handler)))


# --- cliente da Shopee ------------------------------------------------------------------


def test_generate_short_link_leva_a_url_e_os_sub_ids_e_assina_o_corpo():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"data": {"generateShortLink": {"shortLink": "https://s.shopee.com.br/abc"}}})

    link = make_client(handler).generate_short_link("https://shopee.com.br/product/1/2", ["wa", "grupoa1b2"])

    assert link == "https://s.shopee.com.br/abc"
    query = json.loads(seen["body"])["query"]
    assert 'originUrl: "https://shopee.com.br/product/1/2"' in query and 'subIds: ["wa", "grupoa1b2"]' in query
    timestamp = seen["auth"].split("Timestamp=")[1].split(",")[0]
    signature = seen["auth"].rsplit("Signature=", 1)[1]
    assert signature == hashlib.sha256(f"app1{timestamp}{seen['body']}secret1".encode()).hexdigest()


def test_generate_short_link_sem_link_na_resposta_vira_erro():
    with pytest.raises(ShopeeAPIError, match="link curto"):
        make_client(lambda r: httpx.Response(200, json={"data": {"generateShortLink": None}})).generate_short_link("https://x", [])


def test_conversion_report_pagina_por_scroll_id_ate_acabar():
    sent = []

    def handler(request):
        variables = json.loads(request.content)["variables"]
        sent.append(variables)
        if variables["scrollId"] is None:
            page = {"nodes": [{"conversionId": 1}], "pageInfo": {"hasNextPage": True, "scrollId": "tok1"}}
        else:
            page = {"nodes": [{"conversionId": 2}], "pageInfo": {"hasNextPage": False, "scrollId": None}}
        return httpx.Response(200, json={"data": {"conversionReport": page}})

    nodes = make_client(handler).conversion_report(100, 200)

    assert [n["conversionId"] for n in nodes] == [1, 2]
    assert [v["scrollId"] for v in sent] == [None, "tok1"] and sent[0]["start"] == 100 and sent[0]["limit"] == 500


# --- link por grupo na fila ------------------------------------------------------------------


class FakeShortLinks:
    def __init__(self, fail=False):
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail

    def generate_short_link(self, origin_url, sub_ids):
        if self.fail:
            raise RuntimeError("sem rede")
        self.calls.append((origin_url, sub_ids))
        return f"https://s.shopee.com.br/{sub_ids[1]}"


def shopee_product(db) -> Product:
    product = Product(
        source="shopee",
        external_id="1.2",
        title="Kimono Jiu Jitsu",
        url="https://shopee.com.br/product/1/2",
        affiliate_url="https://s.shopee.com.br/comum",
        price=Decimal("200"),
    )
    db.add(product)
    db.commit()
    return product


def test_group_tag_so_letras_e_numeros_e_os_quatro_ultimos_seguem_o_id_do_grupo():
    a = SendGroup(chat_id="111@g.us", name="Kimonos & Faixas BJJ 🥋 São Paulo")
    b = SendGroup(chat_id="222@g.us", name="Kimonos & Faixas BJJ 🥋 São Paulo")
    renamed = SendGroup(chat_id="111@g.us", name="Outro nome")

    assert re.fullmatch(r"[a-z0-9]{5,16}", group_tag(a))
    assert group_tag(a).startswith("kimonosfaixa") and group_tag(a) != group_tag(b)  # 12 letras do nome + 4 hex
    assert group_tag(a)[-4:] == group_tag(renamed)[-4:]


def test_shopee_cada_grupo_recebe_o_proprio_link_no_texto(db):
    product = shopee_product(db)
    replace_groups(db, [("g1@g.us", "Grupo Um"), ("g2@g.us", "Grupo Dois")])
    api = FakeShortLinks()
    text = "#publi\n\n*Kimono*\n\n👉 https://s.shopee.com.br/comum"

    assert enqueue_offer(db, product, text, None, ShopeeGroupLinks(api)) == 2
    db.commit()

    texts = {j.chat_id: j.text for j in jobs(db)}
    assert all("comum" not in t for t in texts.values()) and texts["g1@g.us"] != texts["g2@g.us"]
    assert [c[1][0] for c in api.calls] == ["wa", "wa"]
    assert {c[0] for c in api.calls} == {"https://shopee.com.br/product/1/2"}


def test_se_a_shopee_falha_o_envio_sai_com_o_link_comum(db):
    product = shopee_product(db)
    replace_groups(db, [("g1@g.us", "Grupo Um")])
    text = "👉 https://s.shopee.com.br/comum"

    enqueue_offer(db, product, text, None, ShopeeGroupLinks(FakeShortLinks(fail=True)))
    db.commit()

    assert [j.text for j in jobs(db)] == [text]


def test_produto_do_ml_nao_gera_link_por_grupo(db):
    product = make_product(db)  # mercadolivre
    product.affiliate_url = "https://meli.la/AAA"
    replace_groups(db, [("g1@g.us", "Grupo Um")])
    api = FakeShortLinks()

    enqueue_offer(db, product, "👉 https://meli.la/AAA", None, ShopeeGroupLinks(api))
    db.commit()

    assert api.calls == [] and [j.text for j in jobs(db)] == ["👉 https://meli.la/AAA"]


# --- vendas por grupo ------------------------------------------------------------------------


def conversion(tag, commission, *statuses):
    return {"utmContent": tag, "totalCommission": commission, "orders": [{"orderStatus": s} for s in statuses]}


def test_vendas_por_grupo_agrupam_pelo_sub_id_e_contam_status_e_comissao(db):
    replace_groups(db, [("g1@g.us", "Grupo Um"), ("g2@g.us", "Grupo Dois")])
    g1, g2 = sorted(saved_groups(db), key=lambda g: g.chat_id)
    nodes = [
        conversion(f"wa-{group_tag(g1)}-----", "12.50", "COMPLETED"),
        conversion(f"wa-{group_tag(g1)}", "7.50", "PENDING", "CANCELLED"),
        conversion(f"wa_{group_tag(g2).upper()}", "3", "UNPAID"),  # outro separador e outra caixa: ainda casa
        conversion("outro-link-antigo", "100", "COMPLETED"),
        conversion(None, "1", "COMPLETED"),
    ]

    summary = summarize_conversions(nodes, [g1, g2])

    by_id = {r["id"]: r for r in summary["groups"]}
    um = by_id["g1@g.us"]
    assert (um["conversions"], um["commission"], um["completed"], um["pending"], um["cancelled"]) == (2, 20.0, 1, 1, 1)
    assert (by_id["g2@g.us"]["conversions"], by_id["g2@g.us"]["pending"]) == (1, 1)
    assert (summary["unattributed"]["conversions"], summary["unattributed"]["commission"]) == (2, 101.0)
    assert summary["groups"][0]["id"] == "g1@g.us"  # o que mais rendeu vem primeiro


class FakeConversions:
    def __init__(self, nodes=None, error=None):
        self.nodes, self.error, self.calls = nodes or [], error, 0

    def conversion_report(self, start, end):
        self.calls += 1
        if self.error:
            raise self.error
        return self.nodes


def test_api_vendas_por_grupo_sem_shopee_configurada(sessions, db):
    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        body = client.get("/api/sender/performance", headers=AUTH).json()

    assert body["configured"] is False and body["groups"] == []


def test_api_vendas_por_grupo_usa_cache_e_traduz_erro_da_shopee(sessions, db):
    replace_groups(db, [("g1@g.us", "Grupo Um")])
    db.commit()
    tag = group_tag(saved_groups(db)[0])
    api = FakeConversions([conversion(f"wa-{tag}", "5", "COMPLETED")])

    with TestClient(make_sender_app(sessions, FakeClient(), shopee_client=api)) as client:
        first = client.get("/api/sender/performance", params={"days": 7}, headers=AUTH).json()
        client.get("/api/sender/performance", params={"days": 7}, headers=AUTH)

    assert first["configured"] is True and first["conversions"] == 1 and first["groups"][0]["commission"] == 5.0
    assert api.calls == 1  # a segunda chamada veio do cache: a API só deixa repetir o período a cada 30 s

    erro = FakeConversions(error=ShopeeAPIError('[{"extensions": {"code": 10035}}]'))
    with TestClient(make_sender_app(sessions, FakeClient(), shopee_client=erro)) as client:
        response = client.get("/api/sender/performance", headers=AUTH)

    assert response.status_code == 502 and "10035" in response.json()["detail"]
