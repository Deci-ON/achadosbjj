from datetime import datetime, timedelta, timezone
from decimal import Decimal

from achados.scoring import (
    IRRELEVANT,
    LEARNING,
    NO_OFFER,
    OFFER,
    PricePoint,
    assess,
    NicheRules,
    check_niche,
    time_weighted_median,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
KIMONO = "Kimono Jiu Jitsu Trançado Koral Branco A2"


def history(*changes: tuple[float, str]) -> list[PricePoint]:
    """(dias atrás em que o preço mudou, novo preço)"""
    return [PricePoint(Decimal(price), NOW - timedelta(days=days)) for days, price in changes]


def run(changes, original=None, title=KIMONO, **kw):
    return assess(history(*changes), NOW, Decimal(original) if original else None, title=title, **kw)


# --- nicho ---------------------------------------------------------------


def test_nicho_aceita_kimono_faixa_e_rash_guard():
    assert check_niche("Kimono Jiu Jitsu Trançado Koral", None) is None
    assert check_niche("Kimonos Jiu-Jitsu Adulto Atama", None) is None  # plural
    assert check_niche("Rash Guard Manga Longa Vulkan No-Gi", None) is None
    assert check_niche("Faixa Jiu-Jitsu Atama Adulto 6 Costuras Azul A2", "MLB-MARTIAL_ARTS_BELTS") is None
    assert check_niche("Kimono Infantil Naja Jiu-Jitsu Judô Karatê M1", "MLB-MARTIAL_ARTS_UNIFORMS") is None  # multiuso


def test_nicho_exige_o_esporte_no_titulo_mesmo_em_categoria_de_artes_marciais():
    sem_bjj = "nenhuma palavra do nicho no título"
    uniformes = "MLB-MARTIAL_ARTS_UNIFORMS"
    assert check_niche("Judo Gi Infantil Reforçado Com Faixa Branco M2", uniformes) == sem_bjj
    assert check_niche("Kimono Shiroi Karatê Microfibra Adulto Branco", uniformes) == sem_bjj
    assert check_niche("Kimono Trançado adidas Judo Training J500", uniformes) == sem_bjj
    assert check_niche("Faixa De Kimono Judo Karate Grossa Marrom", "MLB-MARTIAL_ARTS_BELTS") == sem_bjj
    assert "taekwondo" in check_niche("Dobok Taekwondo Infantil Shiroi Start", uniformes)


def test_regras_com_categoria_permitida_ainda_funcionam_para_outros_nichos():
    rules = NicheRules(include=("boxe",), allowed_domains=("MLB-BOXING_GLOVES",))
    assert check_niche("Luva Everlast Pro", "MLB-BOXING_GLOVES", rules) is None
    assert check_niche("Luva Everlast Pro", None, rules) == "nenhuma palavra do nicho no título"


def test_nicho_rejeita_brindes_e_categorias_erradas():
    assert "caneca" in check_niche("Caneca Kimono Jiu Jitsu Faixa Preta", None)
    assert "MUGS" in check_niche("Kimono Jiu Jitsu", "MLB-MUGS")
    assert check_niche("Camisa Infantil Proteção Solar Fpu 50+ Rash Guard", None)  # proteção solar
    assert check_niche("Tênis Esportivo Corrida", None) == "nenhuma palavra do nicho no título"


def test_nicho_compara_palavra_inteira_e_ignora_acento():
    assert check_niche("Kimono de Praia Feminino", None)  # moda, não jiu-jitsu
    assert check_niche("Kimono BJJ Pelúcia Decoração", None)  # acento: Pelúcia/Decoração
    assert check_niche("Original Gi Training", None) == "nenhuma palavra do nicho no título"  # "gi" não é do nicho


def test_item_fora_do_nicho_nao_chega_a_ser_pontuado():
    verdict = run([(40, "100"), (3, "50")], title="Caneca Kimono Jiu Jitsu")
    assert verdict.status == IRRELEVANT and verdict.score == 0


# --- mediana ponderada pelo tempo -----------------------------------------


def test_mediana_pondera_pela_duracao_e_nao_pelo_numero_de_linhas():
    # Três mudanças curtas e baratas não derrubam a mediana de quem passou 25 dias a R$ 100.
    segments = [(Decimal("100"), 25 * 86400.0), (Decimal("40"), 86400.0), (Decimal("45"), 86400.0), (Decimal("50"), 86400.0)]
    assert time_weighted_median(segments) == Decimal("100")
    assert time_weighted_median([]) is None


# --- veredito -------------------------------------------------------------


def test_historico_curto_fica_em_aprendizado_mesmo_com_desconto_declarado():
    verdict = run([(3, "200"), (0.5, "100")], original="200")

    assert verdict.status == LEARNING
    assert verdict.claimed_discount_pct == 50
    assert not verdict.is_offer
    assert any("não verificado" in r for r in verdict.reasons)


def test_sem_historico():
    assert assess([], NOW, title=KIMONO).status == LEARNING


def test_queda_recente_para_o_menor_preco_e_oferta():
    verdict = run([(40, "100"), (3, "70")], original="100")

    assert verdict.status == OFFER and verdict.is_offer
    assert verdict.reference_price == Decimal("100")
    assert verdict.real_discount_pct == 30
    assert verdict.claimed_discount_pct == 30
    assert 70 <= verdict.score <= 90  # queda real + menor preço + "de" confirmado


def test_de_inflado_nao_vira_oferta():
    # O vendedor diz "de R$ 200 por R$ 100", mas o item custou R$ 100 durante todo o período.
    verdict = run([(40, "100")], original="200")

    assert verdict.status == NO_OFFER
    assert verdict.claimed_discount_pct == 50
    assert verdict.real_discount_pct == 0
    assert any("inflado" in r for r in verdict.reasons)
    assert verdict.score == 0


def test_de_confirmado_pelo_historico_pontua_mais_que_de_inflado():
    praticado = run([(40, "100"), (3, "70")], original="100")
    inflado = run([(40, "100"), (3, "70")], original="300")

    assert inflado.status == OFFER  # a queda real existe; só o "de" é falso
    assert any("inflado" in r for r in inflado.reasons)
    assert praticado.score - inflado.score >= 25


def test_preco_baixo_ha_semanas_e_o_normal_do_item_nao_oferta():
    verdict = run([(60, "100"), (25, "60")])  # baixou há 25 dias: a mediana já é R$ 60

    assert verdict.status == NO_OFFER
    assert verdict.real_discount_pct == 0


def test_queda_que_nao_e_o_menor_preco_do_periodo_nao_e_oferta():
    # 20 dias a R$ 100, 2 dias a R$ 50, e agora R$ 80: cai 20% sobre a mediana, mas já foi a 50.
    verdict = run([(40, "100"), (10, "50"), (8, "80")])

    assert verdict.status == NO_OFFER
    assert verdict.real_discount_pct == 20
    assert any("já esteve mais barato" in r for r in verdict.reasons)


def test_queda_pequena_nao_e_oferta():
    verdict = run([(40, "100"), (3, "90")])

    assert verdict.status == NO_OFFER and verdict.real_discount_pct == 10


def test_preco_estavel_ou_em_alta_nao_e_oferta():
    assert run([(40, "100"), (3, "130")]).real_discount_pct == 0


def test_sem_regras_de_nicho_o_titulo_nao_importa():
    assert run([(40, "100"), (3, "70")], title="Qualquer coisa", rules=None).status == OFFER
