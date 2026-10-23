from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from achados.models import CollectRun, PriceSnapshot, Product
from achados.schemas import Offer

# Campos copiados da oferta para o produto a cada coleta.
_FIELDS = (
    "title",
    "url",
    "affiliate_url",
    "image_url",
    "shop_name",
    "price",
    "original_price",
    "discount_pct",
    "commission_rate",
    "rating",
    "sales",
    "raw",
)


def start_run(session: Session, source: str) -> CollectRun:
    run = CollectRun(source=source)
    session.add(run)
    session.flush()
    return run


def finish_run(session: Session, run: CollectRun, *, error: str | None = None) -> None:
    run.finished_at = datetime.now(timezone.utc)
    run.status = "error" if error else "ok"
    run.error = error
    session.flush()


def save_offers(session: Session, run: CollectRun, offers: Iterable[Offer]) -> None:
    """Grava as ofertas: cria produtos novos, atualiza os existentes e registra
    um snapshot quando o preço muda. Atualiza os contadores do `run`."""
    # Se a mesma oferta aparecer duas vezes no lote, vale a última.
    batch = {o.external_id: o for o in offers}
    if not batch:
        return

    source = next(iter(batch.values())).source
    existing = {
        p.external_id: p
        for p in session.scalars(
            select(Product).where(Product.source == source, Product.external_id.in_(batch))
        )
    }

    now = datetime.now(timezone.utc)
    for external_id, offer in batch.items():
        product = existing.get(external_id)
        if product is None:
            product = Product(source=offer.source, external_id=external_id, first_seen_at=now)
            session.add(product)
            run.items_new += 1
            price_changed = True
        else:
            price_changed = (product.price, product.original_price) != (offer.price, offer.original_price)

        for field in _FIELDS:
            setattr(product, field, getattr(offer, field))
        product.last_seen_at = now

        if price_changed:
            product.snapshots.append(
                PriceSnapshot(
                    run_id=run.id,
                    price=offer.price,
                    original_price=offer.original_price,
                    discount_pct=offer.discount_pct,
                    collected_at=now,
                )
            )

    run.items_seen += len(batch)
    session.flush()
