import pytest

from achados.affiliate import (
    ORIGIN_API,
    ORIGIN_MANUAL,
    effective_link,
    validate_affiliate_url,
)
from achados.models import Product


def product(source="mercadolivre", affiliate_url=None, raw=None, external_id="product:MLB123") -> Product:
    return Product(
        source=source,
        external_id=external_id,
        title="Kimono",
        url="https://www.mercadolivre.com.br/p/MLB123?wid=MLB999",
        affiliate_url=affiliate_url,
        raw=raw if raw is not None else {"product_id": "MLB123", "item_id": "MLB999"},
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://mercadolivre.com/sec/abc123",
        "https://www.mercadolivre.com.br/social/meuperfil?matt_tool=1",
        "https://meli.la/1AbCdE",
        "https://s.shopee.com.br/xyz",
        "https://shp.ee/abc",
        "  https://mercadolivre.com/sec/abc123  ",  # espaços nas pontas são aparados
    ],
)
def test_links_validos(url):
    assert validate_affiliate_url(url) == url.strip()


@pytest.mark.parametrize(
    ("url", "motivo"),
    [
        ("", "vazio"),
        ("   ", "vazio"),
        ("http://mercadolivre.com/sec/abc", "https"),
        ("mercadolivre.com/sec/abc", "https"),
        ("javascript:alert(1)", "https"),
        ("https://exemplo.com/sec/abc", "domínio não permitido"),
        ("https://mercadolivre.com.evil.com/x", "domínio não permitido"),  # termina parecido, domínio é outro
        ("https://evilmercadolivre.com/x", "domínio não permitido"),  # sem o ponto: não é subdomínio
        ("https://mercadolivre.com@evil.com/x", "domínio não permitido"),  # o host de verdade é evil.com
        ("https://mercadolivre.com/a b", "espaços"),
        ("https://mercadolivre.com/" + "a" * 1100, "grande"),
    ],
)
def test_links_invalidos(url, motivo):
    with pytest.raises(ValueError, match=motivo):
        validate_affiliate_url(url)


def test_o_link_guardado_e_o_que_vale():
    link, origin = effective_link(product(affiliate_url="https://mercadolivre.com/sec/abc"))

    assert (link, origin) == ("https://mercadolivre.com/sec/abc", ORIGIN_MANUAL)


def test_link_da_shopee_vem_da_api():
    assert effective_link(product(source="shopee", affiliate_url="https://s.shopee.com.br/a")) == (
        "https://s.shopee.com.br/a",
        ORIGIN_API,
    )


def test_sem_link_guardado_nao_ha_link():
    assert effective_link(product()) == (None, None)


def test_o_link_curto_e_o_completo_do_gerador_do_ml_sao_aceitos():
    curto = "https://meli.la/1JhdESJ"
    completo = "https://www.mercadolivre.com.br/social/on_lancer?matt_word=onlancer&matt_tool=27345668&forceInApp=true&ref=" + "Ab%2B" * 80
    assert validate_affiliate_url(curto) == curto and validate_affiliate_url(completo) == completo
