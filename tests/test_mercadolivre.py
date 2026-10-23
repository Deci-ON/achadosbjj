from decimal import Decimal

import httpx
import pytest

from achados.collectors.base import BlockedError
from achados.collectors.mercadolivre import MercadoLivreCollector, parse_listing
from tests.conftest import FIXTURES

HTML = (FIXTURES / "ml_ofertas.html").read_text()


def test_parse_card_completo():
    fone = parse_listing(HTML)[0]

    assert fone.external_id == "MLB3456789012"
    assert fone.title == "Fone Bluetooth Sem Fio"
    assert fone.url == "https://produto.mercadolivre.com.br/MLB-3456789012-fone-bluetooth-_JM"
    assert fone.price == Decimal("899.00")  # não confunde com o valor da parcela
    assert fone.original_price == Decimal("1299.90")
    assert fone.discount_pct == 30
    assert fone.image_url == "https://http2.mlstatic.com/D_NQ_NP_111-MLA.webp"  # ignora o placeholder data:
    assert fone.shop_name == "Loja Oficial XYZ"
    assert fone.rating == Decimal("4.8")


def test_parse_card_com_link_de_clique_e_catalogo():
    cabos = parse_listing(HTML)[1]

    assert cabos.external_id == "MLB4567890123"  # wid, não o id do catálogo
    assert cabos.url == "https://www.mercadolivre.com.br/kit-cabos/p/MLB19999999?wid=MLB4567890123"
    assert cabos.price == Decimal("29.00")
    assert cabos.original_price is None and cabos.discount_pct is None


def test_cards_sem_link_sao_ignorados():
    assert len(parse_listing(HTML)) == 2


def test_pagina_de_verificacao_levanta_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": "https://www.mercadolivre.com.br/gz/account-verification?go=x"}
        ) if request.url.path == "/ofertas" else httpx.Response(200, text="<html>suspicious traffic</html>")

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    collector = MercadoLivreCollector(http, max_pages=1, sleep=lambda s: None)

    with pytest.raises(BlockedError):
        list(collector.collect())


def test_collector_para_quando_a_pagina_nao_tem_cards():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        requested.append(page)
        return httpx.Response(200, text=HTML if page == "1" else "<html></html>")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    collector = MercadoLivreCollector(http, max_pages=5, sleep=lambda s: None)

    assert len(list(collector.collect())) == 2
    assert requested == ["1", "2"]
