"""Coletor da Shopee diante do erro passageiro `10000: System Error` (visto em produção em 2026-11-12)."""

import json

import httpx
import pytest

from achados.collectors.shopee import ShopeeAPIError, ShopeeClient, ShopeeCollector

URL = "https://shopee.test/graphql"
SYSTEM_ERROR = {"errors": [{"message": "error [10000]: System Error", "extensions": {"code": 10000}}]}


def node(item_id: int) -> dict:
    return {"itemId": item_id, "shopId": 1, "productName": f"Kimono Jiu Jitsu {item_id}", "priceMin": "200", "offerLink": "https://s.shopee.com.br/x"}


def page(*item_ids: int, more: bool = False) -> dict:
    return {"data": {"productOfferV2": {"nodes": [node(i) for i in item_ids], "pageInfo": {"hasNextPage": more}}}}


class Script:
    """Servidor falso que responde conforme (palavra, página); cada resposta pode ser uma lista (uma por tentativa)."""

    def __init__(self, replies: dict):
        self.replies = replies
        self.calls: list[tuple[str | None, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        key = (variables["keyword"], variables["page"])
        self.calls.append(key)
        reply = self.replies[key]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        return httpx.Response(200, json=reply)


def collector(script: Script, keywords, max_pages=2, waits=(0.0, 0.0, 0.0)) -> ShopeeCollector:
    client = ShopeeClient("app", "secret", URL, http=httpx.Client(transport=httpx.MockTransport(script)))
    return ShopeeCollector(client, keywords=keywords, max_pages=max_pages, retry_waits=waits, sleep=lambda s: None)


def ids(offers) -> list[str]:
    return [o.external_id for o in offers]


def test_erro_passageiro_na_primeira_tentativa_e_repetido_e_a_coleta_segue():
    script = Script({("kimono", 1): [SYSTEM_ERROR, SYSTEM_ERROR, page(1, 2)]})

    offers = list(collector(script, ["kimono"]).collect())

    assert ids(offers) == ["1.1", "1.2"] and script.calls == [("kimono", 1)] * 3


def test_pagina_que_nao_passa_nas_tentativas_pula_o_resto_da_palavra_e_mantem_o_que_ja_veio():
    script = Script(
        {
            ("kimono", 1): page(1, 2, more=True),
            ("kimono", 2): SYSTEM_ERROR,  # falha sempre: esgota as 3 esperas
            ("faixa", 1): page(3),
        }
    )
    c = collector(script, ["kimono", "faixa"])

    offers = list(c.collect())

    assert ids(offers) == ["1.1", "1.2", "1.3"]  # o que veio antes do erro fica, e a próxima palavra roda
    assert script.calls.count(("kimono", 2)) == 4 and c.skipped == ["kimono (pagina 2)"]


def test_se_nenhuma_pagina_veio_a_coleta_falha_com_o_ultimo_erro():
    script = Script({("kimono", 1): SYSTEM_ERROR, ("faixa", 1): SYSTEM_ERROR})

    with pytest.raises(ShopeeAPIError, match="10000"):
        list(collector(script, ["kimono", "faixa"]).collect())


def test_erro_que_nao_e_passageiro_derruba_na_hora_sem_repetir():
    denied = {"errors": [{"message": "no access", "extensions": {"code": 10035}}]}
    script = Script({("kimono", 1): denied, ("faixa", 1): page(3)})

    with pytest.raises(ShopeeAPIError, match="10035"):
        list(collector(script, ["kimono", "faixa"]).collect())

    assert script.calls == [("kimono", 1)]  # não tentou de novo nem passou para a próxima palavra


def test_coleta_sem_erro_nao_marca_palavras_puladas():
    script = Script({("kimono", 1): page(1)})
    c = collector(script, ["kimono"])

    assert ids(c.collect()) == ["1.1"] and c.skipped == []


def test_coleta_agendada_usa_a_ordenacao_e_o_tamanho_de_pagina_que_a_shopee_aguenta():
    from achados.collectors.shopee import SORT_RELEVANCE, build_collector
    from achados.config import Settings

    c = build_collector(Settings(_env_file=None, shopee_app_id="app", shopee_secret="segredo"))

    # Medido em produção: vendas (sort 2) e comissão (sort 5) dão System Error com página grande; relevância não.
    assert c._sort_type == SORT_RELEVANCE and c._limit == 20 and c._max_pages == 5
