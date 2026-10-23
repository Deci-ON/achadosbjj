import argparse
import logging
import sys

from sqlalchemy import func, select

from achados.collectors.base import Collector, CollectorError
from achados.collectors.mercadolivre import OFFERS_URL, MercadoLivreCollector, make_http_client
from achados.collectors.shopee import SORT_SALES, ShopeeClient, ShopeeCollector
from achados.config import get_settings
from achados.db import make_engine, make_session_factory
from achados.models import Base, Product
from achados.storage import finish_run, save_offers, start_run

log = logging.getLogger("achados")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="achados")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="cria as tabelas")
    sub.add_parser("stats", help="resumo do que já foi coletado")

    collect = sub.add_parser("collect", help="coleta ofertas de uma loja")
    stores = collect.add_subparsers(dest="store", required=True)

    shopee = stores.add_parser("shopee")
    shopee.add_argument("-k", "--keyword", action="append", help="palavra-chave (repetível); sem ela, ofertas gerais")
    shopee.add_argument("--pages", type=int, default=3)
    shopee.add_argument("--sort", type=int, default=SORT_SALES, help="sortType da API (1=relevância, 2=vendas, 5=comissão)")

    ml = stores.add_parser("mercadolivre")
    ml.add_argument("--url", default=OFFERS_URL, help="página de listagem a coletar")
    ml.add_argument("--pages", type=int, default=3)

    return parser


def _make_collector(args: argparse.Namespace) -> Collector:
    settings = get_settings()
    if args.store == "shopee":
        client = ShopeeClient(settings.shopee_app_id, settings.shopee_secret, settings.shopee_api_url)
        return ShopeeCollector(client, keywords=args.keyword, max_pages=args.pages, sort_type=args.sort)
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    if args.command == "init-db":
        Base.metadata.create_all(make_engine())
        print("tabelas criadas")
        return 0
    if args.command == "stats":
        return run_stats()
    return run_collect(args)


if __name__ == "__main__":
    sys.exit(main())
