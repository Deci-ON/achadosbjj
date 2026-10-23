from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from achados.models import PriceSnapshot, Product
from achados.scoring import BJJ_RULES, NicheRules, PricePoint, ScoringConfig, Verdict, assess


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def assess_products(
    session: Session,
    *,
    source: str | None = None,
    max_age_days: float = 3.0,
    rules: NicheRules | None = BJJ_RULES,
    config: ScoringConfig = ScoringConfig(),
    now: datetime | None = None,
) -> list[tuple[Product, Verdict]]:
    """Pontua os produtos vistos recentemente, do melhor para o pior.

    Produtos não vistos há mais de `max_age_days` ficam de fora: o último preço conhecido
    pode já não valer. A observação de cada produto termina em `last_seen_at`, não em "agora".
    """
    now = now or datetime.now(timezone.utc)
    query = select(Product).where(Product.last_seen_at >= now - timedelta(days=max_age_days))
    if source:
        query = query.where(Product.source == source)
    products = list(session.scalars(query))
    if not products:
        return []

    history: dict[int, list[PricePoint]] = defaultdict(list)
    snapshots = session.execute(
        select(PriceSnapshot.product_id, PriceSnapshot.price, PriceSnapshot.collected_at)
        .where(PriceSnapshot.product_id.in_([p.id for p in products]))
        .order_by(PriceSnapshot.collected_at)
    )
    for product_id, price, collected_at in snapshots:
        history[product_id].append(PricePoint(price, _aware(collected_at)))

    verdicts = [
        (
            product,
            assess(
                history[product.id],
                _aware(product.last_seen_at),
                product.original_price,
                title=product.title,
                domain_id=(product.raw or {}).get("domain_id"),
                rules=rules,
                config=config,
            ),
        )
        for product in products
    ]
    return sorted(verdicts, key=lambda pv: (-pv[1].score, pv[0].title))


def assess_product(
    session: Session,
    product_id: int,
    *,
    rules: NicheRules | None = BJJ_RULES,
    config: ScoringConfig = ScoringConfig(),
) -> tuple[Product, Verdict, list[tuple[Decimal, Decimal | None, datetime]]] | None:
    """Um produto só, com o histórico de preço (preço, "de" declarado, data) em ordem.

    Diferente de assess_products, não exige que o produto tenha sido visto recentemente: a página de
    detalhe também serve para ver o que parou de aparecer.
    """
    product = session.get(Product, product_id)
    if product is None:
        return None
    rows = session.execute(
        select(PriceSnapshot.price, PriceSnapshot.original_price, PriceSnapshot.collected_at)
        .where(PriceSnapshot.product_id == product_id)
        .order_by(PriceSnapshot.collected_at)
    ).all()
    history = [(price, original, _aware(at)) for price, original, at in rows]
    verdict = assess(
        [PricePoint(price, at) for price, _, at in history],
        _aware(product.last_seen_at),
        product.original_price,
        title=product.title,
        domain_id=(product.raw or {}).get("domain_id"),
        rules=rules,
        config=config,
    )
    return product, verdict, history
