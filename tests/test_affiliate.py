import pytest

from achados.affiliate import (
    ORIGIN_API,
    ORIGIN_MANUAL,
    ORIGIN_TEMPLATE,
    effective_link,
    validate_affiliate_url,
    validate_template,
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
        ("https://mercadolivre.com/" + "a" * 600, "grande"),
    ],
)
def test_links_invalidos(url, motivo):
    with pytest.raises(ValueError, match=motivo):
        validate_affiliate_url(url)


def test_o_link_guardado_vence_o_modelo():
    link, origin = effective_link(product(affiliate_url="https://mercadolivre.com/sec/abc"), "{product_url}?x=1")

    assert (link, origin) == ("https://mercadolivre.com/sec/abc", ORIGIN_MANUAL)


def test_link_da_shopee_vem_da_api():
    assert effective_link(product(source="shopee", affiliate_url="https://s.shopee.com.br/a"), "") == (
        "https://s.shopee.com.br/a",
        ORIGIN_API,
    )


def test_sem_link_e_sem_modelo_nao_ha_link():
    assert effective_link(product()) == (None, None)
    assert effective_link(product(), "") == (None, None)


def test_modelo_monta_o_link_do_ml_com_os_campos_do_produto():
    template = "{product_url}?matt_tool=ABC&matt_word=meuuser&item={item_id}"

    link, origin = effective_link(product(), template)

    assert link == "https://www.mercadolivre.com.br/p/MLB123?matt_tool=ABC&matt_word=meuuser&item=MLB999"
    assert origin == ORIGIN_TEMPLATE


def test_modelo_sem_raw_usa_o_id_do_external_id():
    link, _ = effective_link(product(raw={}), "{product_url}?t=1")

    assert link == "https://www.mercadolivre.com.br/p/MLB123?t=1"


def test_modelo_so_vale_para_o_ml_nao_para_a_shopee():
    assert effective_link(product(source="shopee"), "{product_url}?t=1") == (None, None)


def test_modelo_valido_e_vazio_passam():
    validate_template("")
    validate_template("{product_url}?matt_tool=ABC&matt_word=user")
    validate_template("{url}&matt_tool=ABC")


@pytest.mark.parametrize(
    "template",
    [
        "{campo_que_nao_existe}",  # campo desconhecido
        "{url",  # chave sem fechar
        "{}",  # campo posicional
        "http://mercadolivre.com/p?x={product_id}",  # montaria um link http
        "https://outro.com/{product_id}",  # montaria um link de outro domínio
    ],
)
def test_modelo_invalido_falha_na_hora(template):
    with pytest.raises(ValueError):
        validate_template(template)
