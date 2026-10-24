from decimal import Decimal

import pytest

from achados.messages import MAX_TITLE, brl, clean_title, format_offer_message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "R$ 0,00"),
        (5, "R$ 5,00"),
        (49.9, "R$ 49,90"),
        (1289.9, "R$ 1.289,90"),
        (Decimal("1234567.891"), "R$ 1.234.567,89"),
        ("999.995", "R$ 1.000,00"),  # arredonda para cima na metade
        (100, "R$ 100,00"),
        (1000, "R$ 1.000,00"),
    ],
)
def test_brl(value, expected):
    assert brl(value) == expected


def test_titulo_sem_formatacao_do_whatsapp_e_com_espacos_normalizados():
    assert clean_title("  Kimono *Jiu*_Jitsu~  Koral \n A2 ") == "Kimono JiuJitsu Koral A2"


def test_titulo_longo_e_cortado_com_reticencias():
    title = clean_title("Kimono " + "Jiu Jitsu " * 30)

    assert len(title) == MAX_TITLE and title.endswith("…")


def test_oferta_mostra_a_queda_sobre_o_preco_normal_medido_e_nao_o_de_do_vendedor():
    message = format_offer_message(
        title="Kimono Jiu Jitsu Koral Branco A2",
        price=Decimal("349.25"),
        link="https://exemplo/p/MLB1",
        is_offer=True,
        reference_price=Decimal("499"),
        real_discount_pct=30,
    )

    assert message == (
        "🔥 *Kimono Jiu Jitsu Koral Branco A2*\n\n"
        "💰 *R$ 349,25*\n"
        "📉 30% abaixo do preço normal (R$ 499,00, mediana de 30 dias)\n\n"
        "👉 https://exemplo/p/MLB1"
    )


def test_sem_oferta_a_mensagem_e_so_titulo_preco_e_link_sem_prometer_desconto():
    message = format_offer_message(
        title="Faixa Jiu Jitsu Azul",
        price=39.9,
        link="https://exemplo/p/MLB2",
        is_offer=False,
        reference_price=Decimal("39.9"),
        real_discount_pct=0,
    )

    assert message == "*Faixa Jiu Jitsu Azul*\n\n💰 R$ 39,90\n\n👉 https://exemplo/p/MLB2"
    assert "abaixo" not in message and "🔥" not in message


def test_oferta_sem_dados_de_referencia_cai_no_formato_simples():
    message = format_offer_message(title="X", price=10, link="l", is_offer=True, reference_price=None, real_discount_pct=20)

    assert "abaixo" not in message


def test_o_periodo_da_mediana_e_configuravel():
    message = format_offer_message(
        title="X", price=70, link="l", is_offer=True, reference_price=100, real_discount_pct=30, window_days=14
    )

    assert "mediana de 14 dias" in message


# --- vários tamanhos do mesmo modelo -----------------------------------------


def test_varios_tamanhos_com_o_mesmo_preco_listam_os_tamanhos_e_mantem_o_resto():
    message = format_offer_message(
        title="Kimono Torah Azul",
        price=Decimal("250"),
        link="https://meli.la/AAA",
        is_offer=True,
        reference_price=Decimal("500"),
        real_discount_pct=50,
        sizes=[("A1", Decimal("250")), ("A2", Decimal("250")), ("A3", Decimal("250"))],
    )

    assert message == (
        "🔥 *Kimono Torah Azul*\n\n"
        "💰 *R$ 250,00*\n"
        "📉 50% abaixo do preço normal (R$ 500,00, mediana de 30 dias)\n"
        "📏 Tamanhos: A1, A2, A3\n\n"
        "👉 https://meli.la/AAA"
    )


def test_tamanhos_com_precos_diferentes_mostram_a_partir_de_e_o_preco_de_cada_um():
    message = format_offer_message(
        title="Kimono Torah Azul",
        price=Decimal("270"),
        link="https://meli.la/AAA",
        is_offer=True,
        reference_price=Decimal("500"),
        real_discount_pct=46,
        sizes=[("A1", Decimal("270")), ("A2", Decimal("250"))],
    )

    assert "💰 *a partir de R$ 250,00*" in message
    assert "📉 até 46% abaixo do preço normal\n" in message
    assert "mediana" not in message  # o preço normal é do representante: não vale para todos os tamanhos
    assert "📏 Tamanhos: A1 R$ 270,00 · A2 R$ 250,00" in message


def test_um_tamanho_so_ou_sem_lista_fica_igual_ao_formato_de_sempre():
    base = dict(
        title="Kimono Torah Azul",
        price=Decimal("250"),
        link="https://meli.la/AAA",
        is_offer=True,
        reference_price=Decimal("500"),
        real_discount_pct=50,
    )

    assert format_offer_message(**base) == format_offer_message(**base, sizes=[("A1", Decimal("250"))])
    assert "📏" not in format_offer_message(**base)
