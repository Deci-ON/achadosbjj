from collections import defaultdict
from datetime import datetime, timedelta, timezone

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
