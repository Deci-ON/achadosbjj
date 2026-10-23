import argparse
import logging
import sys
from collections import Counter

import httpx
from sqlalchemy import func, select

from achados.collectors.base import BlockedError, Collector, CollectorError
from achados.collectors.mercadolivre import OFFERS_URL, MercadoLivreCollector, make_http_client
from achados.collectors.mercadolivre_api import (
    BJJ_CATEGORIES,
    BJJ_DOMAINS,
    BJJ_KEYWORDS,
    MercadoLivreAPI,
    MercadoLivreAPIError,
    MercadoLivreApiCollector,
)
from achados.collectors.shopee import SORT_SALES, ShopeeClient, ShopeeCollector
from achados.config import get_settings
from achados.db import make_engine, make_session_factory
from achados.models import Base, Product
from achados.offers import assess_products
from achados.scoring import BJJ_RULES, IRRELEVANT, LEARNING, NO_OFFER, OFFER, ScoringConfig
from achados.storage import finish_run, save_offers, start_run

log = logging.getLogger("achados")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="achados")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="cria as tabelas")
    sub.add_parser("stats", help="resumo do que já foi coletado")
    sub.add_parser("check-ml", help="testa em 2s se a API do Mercado Livre responde deste IP, sem gravar nada")

    offers = sub.add_parser("offers", help="lista o que está em oferta de verdade, pelo histórico de preços")
    offers.add_argument("--source", choices=["shopee", "mercadolivre"])
    offers.add_argument("--status", choices=[OFFER, NO_OFFER, LEARNING, IRRELEVANT], default=OFFER)
    offers.add_argument("--limit", type=int, default=30)
    offers.add_argument("--max-age-days", type=float, default=3.0, help="ignora produtos não vistos há mais tempo")
    offers.add_argument("--min-drop", type=float, default=ScoringConfig.min_drop_pct, help="queda mínima (%%) sobre a mediana")
    offers.add_argument(
        "--min-history", type=float, default=ScoringConfig.min_history_days, help="dias de histórico antes de opinar"
    )
    offers.add_argument("--any-niche", action="store_true", help="não filtra por nicho (BJJ)")

    collect = sub.add_parser("collect", help="coleta ofertas de uma loja")
    stores = collect.add_subparsers(dest="store", required=True)

    shopee = stores.add_parser("shopee")
    shopee.add_argument("-k", "--keyword", action="append", help="palavra-chave (repetível); sem ela, ofertas gerais")
    shopee.add_argument("--pages", type=int, default=3)
    shopee.add_argument("--sort", type=int, default=SORT_SALES, help="sortType da API (1=relevância, 2=vendas, 5=comissão)")

    ml = stores.add_parser("mercadolivre", help="API oficial: mais vendidos por categoria + melhor oferta")
    ml.add_argument(
        "-c", "--category", action="append", help="id de categoria, ex. MLB1051 (repetível); sem ela, as categorias padrão"
    )
    ml.add_argument("-k", "--keyword", action="append", help="busca no catálogo por palavra-chave (repetível)")
    ml.add_argument("--domain", action="append", help="só produtos deste domain_id nas buscas (repetível)")
    ml.add_argument("--preset", choices=["bjj"], help="bjj: categorias, palavras-chave e domínios de jiu-jitsu")
    ml.add_argument("--search-pages", type=int, default=2, help="páginas de 50 resultados por palavra-chave")
    ml.add_argument("--per-category", type=int, default=20, help="produtos por categoria (máx. 20)")
    ml.add_argument("--min-discount", type=int, default=0, help="só grava ofertas com este desconto mínimo (%%)")

    ml_html = stores.add_parser("mercadolivre-html", help="scraping do HTML de uma listagem (o ML costuma bloquear)")
    ml_html.add_argument("--url", default=OFFERS_URL, help="página de listagem a coletar")
    ml_html.add_argument("--pages", type=int, default=3)

    return parser


def _make_collector(args: argparse.Namespace) -> Collector:
    settings = get_settings()
    if args.store == "shopee":
        client = ShopeeClient(settings.shopee_app_id, settings.shopee_secret, settings.shopee_api_url)
        return ShopeeCollector(client, keywords=args.keyword, max_pages=args.pages, sort_type=args.sort)
    if args.store == "mercadolivre":
        http = httpx.Client(timeout=settings.request_timeout, proxy=settings.ml_proxy_url)
        api = MercadoLivreAPI(settings.ml_client_id, settings.ml_client_secret, http=http)
        categories, keywords, domains = args.category, args.keyword, tuple(args.domain or ()) or None
        if args.preset == "bjj":
            categories = categories or BJJ_CATEGORIES
            keywords = keywords or BJJ_KEYWORDS
            domains = domains or BJJ_DOMAINS
        return MercadoLivreApiCollector(
            api,
            categories=categories,
            keywords=keywords,
            domains=domains,
            per_category=args.per_category,
            search_pages=args.search_pages,
            min_discount=args.min_discount,
        )
    http = make_http_client(settings.ml_proxy_url, settings.request_timeout)
    return MercadoLivreCollector(http, listing_url=args.url, max_pages=args.pages)


def run_collect(args: argparse.Namespace) -> int:
    try:
        collector = _make_collector(args)
    except CollectorError as exc:
        log.error("%s", exc)
        return 2
    sessions = make_session_factory(make_engine())

    with sessions() as session:
        run = start_run(session, collector.source)
        session.commit()

        offers, error = [], None
        try:
            for offer in collector.collect():
                offers.append(offer)
        except Exception as exc:  # o run precisa registrar a falha, qualquer que seja
            error = f"{type(exc).__name__}: {exc}"
            log.error("coleta falhou: %s", error)

        # Grava o que veio antes da falha (ex.: páginas 1-2 de 3).
        save_offers(session, run, offers)
        finish_run(session, run, error=error)
        session.commit()
        log.info("%s: %d vistos, %d novos", collector.source, run.items_seen, run.items_new)
        return 1 if error else 0


def run_check_ml(api: MercadoLivreAPI | None = None) -> int:
    """Chamadas mínimas aos endpoints que a coleta usa. Serve para validar credenciais e o IP do servidor."""
    try:
        if api is None:
            settings = get_settings()
            http = httpx.Client(timeout=settings.request_timeout, proxy=settings.ml_proxy_url)
            api = MercadoLivreAPI(settings.ml_client_id, settings.ml_client_secret, http=http)
        steps = [
            ("token + destaques da categoria Kimonos", "/highlights/MLB/category/MLB47731", None),
            (
                "busca no catálogo",
                "/products/search",
                {"status": "active", "site_id": "MLB", "q": "kimono jiu jitsu", "limit": 1},
            ),
        ]
        for label, path, params in steps:
            body = api.get(path, params)
            if not body:
                print(f"FALHA  {label}: resposta vazia ou 404")
                return 1
            print(f"ok     {label}")
        return 0
    except BlockedError as exc:
        print(f"BLOQUEADO  {exc}")
        return 1
    except MercadoLivreAPIError as exc:
        print(f"ERRO  {exc}")
        return 2
    except httpx.HTTPError as exc:
        print(f"ERRO DE REDE  {type(exc).__name__}: {exc}")
        return 1


def run_stats() -> int:
    sessions = make_session_factory(make_engine())
    with sessions() as session:
        rows = session.execute(
            select(Product.source, func.count(), func.max(Product.last_seen_at)).group_by(Product.source)
        ).all()
        for source, count, last_seen in rows:
            print(f"{source}: {count} produtos, última coleta {last_seen:%Y-%m-%d %H:%M}")
        if not rows:
            print("nenhum produto coletado ainda")
    return 0


def run_offers(args: argparse.Namespace) -> int:
    config = ScoringConfig(min_drop_pct=args.min_drop, min_history_days=args.min_history)
    sessions = make_session_factory(make_engine())
    with sessions() as session:
        verdicts = assess_products(
            session,
            source=args.source,
            max_age_days=args.max_age_days,
            rules=None if args.any_niche else BJJ_RULES,
            config=config,
        )

    counts = Counter(v.status for _, v in verdicts)
    print("; ".join(f"{status}: {counts.get(status, 0)}" for status in (OFFER, NO_OFFER, LEARNING, IRRELEVANT)))
    for product, verdict in [pv for pv in verdicts if pv[1].status == args.status][: args.limit]:
        drop = f"-{verdict.real_discount_pct}%" if verdict.real_discount_pct else "  -"
        claimed = f"{verdict.claimed_discount_pct}%" if verdict.claimed_discount_pct else "-"
        print(f"[{verdict.score:3d}] R$ {product.price:>9} real {drop:>5} (vendedor diz {claimed:>4}) {product.title[:60]}")
        print(f"      {product.url}")
        for reason in verdict.reasons:
            print(f"      - {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    if args.command == "init-db":
        Base.metadata.create_all(make_engine())
        print("tabelas criadas")
        return 0
    if args.command == "stats":
        return run_stats()
    if args.command == "check-ml":
        return run_check_ml()
    if args.command == "offers":
        return run_offers(args)
    return run_collect(args)


if __name__ == "__main__":
    sys.exit(main())
