from decimal import Decimal

from achados.models import Product
from achados.scoring import IRRELEVANT, LEARNING, NO_OFFER, OFFER, Verdict
from achados.variants import group_models, model_key, split_size


def make(id_, title, price="100", *, seller=1, source="mercadolivre", link=None) -> Product:
    product = Product(
        id=id_,
        source=source,
        external_id=f"product:{id_}",
        title=title,
        url=f"https://exemplo/{id_}",
        affiliate_url=link,
        price=Decimal(price),
        raw={"seller_id": seller} if seller is not None else {},
    )
    return product


def verdict(status=OFFER, score=80) -> Verdict:
    return Verdict(status=status, score=score)


def test_split_size_tira_o_tamanho_do_fim_do_titulo():
    assert split_size("Kimono Combate Bjj Azul Com Faixa Torah Tamanho A2") == ("Kimono Combate Bjj Azul Com Faixa Torah", "A2")
    assert split_size("Kimono Adidas J500 Branco tamanho 180 ") == ("Kimono Adidas J500 Branco", "180")
    assert split_size("Kimono Shiroi Cor Branco Tamanho M000") == ("Kimono Shiroi Cor Branco", "M000")


def test_split_size_aceita_o_codigo_solto_no_fim_com_ou_sem_hifen():
    assert split_size("Kimono Jiu Jitsu Vouk Tyg All Black Trançado Preto A2") == ("Kimono Jiu Jitsu Vouk Tyg All Black Trançado Preto", "A2")
    assert split_size("FAIXA BRANCA DE JIU-JITSU BRAZIL COMBAT GRADUAÇÃO BRASIL BJJF/CBJJ - A1") == (
        "FAIXA BRANCA DE JIU-JITSU BRAZIL COMBAT GRADUAÇÃO BRASIL BJJF/CBJJ",
        "A1",
    )
    assert split_size("Kimono Venum Infantil Competitor Azul Azul K1") == ("Kimono Venum Infantil Competitor Azul Azul", "K1")
    assert split_size("Kimono Infantil Cor Azul M00")[1] == "M00"
    assert split_size("Faixa Jiu-jitsu Fita Preta Cinza m3")[1] == "m3"


def test_split_size_nao_confunde_modelo_com_tamanho_nem_junta_titulos_que_listam_tamanhos():
    assert split_size("Kimono Atama Mundial Branco Jiu-jitsu A2 E A3")[1] is None  # um anúncio, vários tamanhos
    assert split_size("Kimono Jiu Jitsu A1 / A2")[1] is None
    assert split_size("Camiseta Jiu Jitsu Gola V")[1] is None
    assert split_size("Kimono Jiu Jitsu Modelo A7")[1] is None  # A7 não é tamanho de kimono
    assert split_size("A2")[1] is None  # só o código, sem título: não há modelo


def test_split_size_sem_tamanho_no_fim_devolve_o_titulo():
    assert split_size("Rash Guard Jiu Jitsu Preta") == ("Rash Guard Jiu Jitsu Preta", None)
    assert split_size("Kimono Tamanho A2 Azul") == ("Kimono Tamanho A2 Azul", None)  # só vale no fim


def test_chave_do_modelo_exige_mesmo_vendedor_e_mesmo_titulo_sem_tamanho():
    a2 = make(1, "Kimono Torah Azul Tamanho A2")
    a3 = make(2, "Kimono Torah Azul TAMANHO A3")
    outro_vendedor = make(3, "Kimono Torah Azul Tamanho A2", seller=2)
    outra_cor = make(4, "Kimono Torah Preto Tamanho A2")

    assert model_key(a2) == model_key(a3)
    assert model_key(a2) != model_key(outro_vendedor)
    assert model_key(a2) != model_key(outra_cor)


def test_codigo_solto_e_tamanho_x_do_mesmo_modelo_agrupam_juntos():
    solto = make(1, "Faixa Jiu-jitsu Fita Preta Cinza A2")
    outro = make(2, "Faixa Jiu-jitsu Fita Preta Cinza M1")
    hifen = make(3, "Faixa Jiu-jitsu Fita Preta Cinza - A0")

    assert model_key(solto) == model_key(outro) == model_key(hifen)


def test_o_que_nao_e_agrupavel_nao_tem_chave():
    assert model_key(make(1, "Kimono Torah Azul")) is None  # sem tamanho
    assert model_key(make(2, "Kimono Torah Azul Tamanho A2", seller=None)) is None  # sem vendedor
    assert model_key(make(3, "Kimono Torah Azul Tamanho A2", source="shopee")) is None  # só o ML


def test_tamanhos_do_mesmo_modelo_viram_um_grupo_com_o_melhor_como_representante():
    entries = [
        (make(1, "Kimono Torah Azul Tamanho A1", "300"), verdict(NO_OFFER, 10)),
        (make(2, "Kimono Torah Azul Tamanho A2", "250"), verdict(OFFER, 70)),
        (make(3, "Kimono Torah Azul Tamanho A3", "240"), verdict(OFFER, 85)),
        (make(4, "Kimono Torah Azul Tamanho A0", "260"), verdict(LEARNING, 0)),
    ]

    (group,) = group_models(entries)

    assert group.is_model and [p.id for p, _ in group.members] == [3, 2, 4, 1]  # oferta > aprendendo > sem oferta
    assert group.product.id == 3 and group.verdict.score == 85
    assert group.title == "Kimono Torah Azul"
    assert [p.id for p, _ in group.sizes()] == [4, 1, 2, 3]  # A0, A1, A2, A3
    assert group.offer_sizes() == [("A2", Decimal("250")), ("A3", Decimal("240"))]


def test_empate_de_pontuacao_fica_com_o_menor_preco():
    entries = [
        (make(1, "Kimono Torah Tamanho A1", "260"), verdict(OFFER, 80)),
        (make(2, "Kimono Torah Tamanho A2", "240"), verdict(OFFER, 80)),
    ]

    assert group_models(entries)[0].product.id == 2


def test_produto_sem_tamanho_ou_de_outro_vendedor_fica_sozinho_e_a_ordem_e_por_pontuacao():
    entries = [
        (make(1, "Kimono Torah Tamanho A1"), verdict(OFFER, 60)),
        (make(2, "Kimono Torah Tamanho A2"), verdict(OFFER, 50)),
        (make(3, "Kimono Torah Tamanho A2", seller=9), verdict(OFFER, 90)),
        (make(4, "Rash Guard Jiu Jitsu"), verdict(OFFER, 70)),
        (make(5, "Judo Gi Tamanho 2"), verdict(IRRELEVANT, 0)),
    ]

    groups = group_models(entries)

    assert [(g.product.id, g.is_model) for g in groups] == [(3, False), (4, False), (1, True), (5, False)]
    assert groups[0].title == "Kimono Torah Tamanho A2"  # sozinho: o título fica como veio


def test_o_link_do_modelo_pode_estar_em_qualquer_tamanho():
    a1 = make(1, "Kimono Torah Tamanho A1")
    a2 = make(2, "Kimono Torah Tamanho A2", link="https://meli.la/AAA")
    group = group_models([(a1, verdict(OFFER, 90)), (a2, verdict(OFFER, 50))])[0]

    assert group.product.id == 1 and group.product.affiliate_url is None
    assert group.link == "https://meli.la/AAA"
    assert group_models([(a1, verdict())])[0].link is None
