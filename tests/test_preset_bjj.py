from achados.cli import build_parser
from achados.collectors.mercadolivre_api import (
    BJJ_CATEGORIES,
    BJJ_DOMAINS,
    BJJ_KEYWORDS,
    BJJ_TREND_LIMIT,
    SEARCH_MAX_PAGES,
    _keyword_ok,
    build_collector,
)
from achados.config import Settings
from achados.probes import DbProbeCache
from achados.scoring import BJJ_RULES, check_niche, normalize


def test_toda_palavra_fixa_passa_no_proprio_filtro_de_palavras():
    assert all(_keyword_ok(normalize(k)) for k in BJJ_KEYWORDS), [k for k in BJJ_KEYWORDS if not _keyword_ok(normalize(k))]


def test_a_lista_fixa_nao_tem_duplicata_nem_judo_sozinho():
    normalized = [normalize(k) for k in BJJ_KEYWORDS]
    assert len(normalized) == len(set(normalized))
    # judô sem jiu jitsu traz produto que a regra de nicho descarta depois: gasto de chamada à toa
    assert not any("judo" in k and "jiu jitsu" not in k and "bjj" not in k for k in normalized)


def test_nenhuma_palavra_fixa_e_de_mma():
    assert not any(w in normalize(k) for k in BJJ_KEYWORDS for w in ("luva", "protetor bucal", "mma"))


def test_o_titulo_tipico_de_cada_palavra_fixa_passa_na_regra_de_nicho():
    # Se a palavra buscada produzisse títulos que a pontuação joga fora, a busca seria desperdício.
    for keyword in BJJ_KEYWORDS:
        title = keyword if any(m in keyword for m in ("jiu jitsu", "bjj")) else f"{keyword} jiu jitsu adulto"
        assert check_niche(title, None, BJJ_RULES) is None, title


def test_a_paginacao_padrao_respeita_o_limite_de_150_do_ml():
    assert SEARCH_MAX_PAGES * 50 == 150  # offset + limit <= 150 (offset=140 já dá 400)


def test_preset_liga_trends_cache_e_tres_paginas(sessions):
    settings = Settings(_env_file=None, ml_client_id="id", ml_client_secret="secret")

    collector = build_collector(settings, "bjj", sessions)

    assert collector._trend_categories == BJJ_CATEGORIES and collector._trend_limit == BJJ_TREND_LIMIT
    assert collector._search_pages == 3 and collector._keywords == BJJ_KEYWORDS and collector._domains == BJJ_DOMAINS
    assert isinstance(collector._probe_cache, DbProbeCache)


def test_preset_sem_sessoes_nao_usa_cache():
    collector = build_collector(Settings(_env_file=None, ml_client_id="id", ml_client_secret="secret"), "bjj")

    assert collector._probe_cache is None


def test_cli_padroes_do_ml():
    args = build_parser().parse_args(["collect", "mercadolivre", "--preset", "bjj"])
    assert args.search_pages == SEARCH_MAX_PAGES and args.no_trends is False

    args = build_parser().parse_args(["collect", "mercadolivre", "--preset", "bjj", "--no-trends", "--search-pages", "1"])
    assert args.no_trends is True and args.search_pages == 1
