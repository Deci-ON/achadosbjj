"""Biblioteca de cupons: quais cupons entram na mensagem de cada oferta.

Cupom é só texto na mensagem (o comprador aplica no app/checkout): nada aqui mexe em preço nem em pontuação.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from achados.messages import BRT
from achados.models import Coupon, ProductCoupon


def aware(moment: datetime) -> datetime:
    """O SQLite devolve datas sem fuso (são UTC); o Postgres devolve com fuso."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_active(coupon: Coupon, now: datetime) -> bool:
    return aware(coupon.expires_at) > now


def in_brt(moment: datetime) -> datetime:
    return aware(moment).astimezone(BRT)


@dataclass
class CouponBook:
    """Os cupons ativos e os anexos de todos os produtos, lidos de uma vez (a biblioteca é pequena e uma
    listagem de ofertas consultaria um por um)."""

    active: list[Coupon] = field(default_factory=list)  # por validade crescente
    attached: dict[int, list[int]] = field(default_factory=dict)  # product_id -> coupon_ids (inclui vencidos)

    def for_offer(self, store: str, product_id: int) -> list[Coupon]:
        """Os anexados à oferta primeiro, depois os padrões da loja; ativos, da loja ou "all", sem repetir."""
        mine = set(self.attached.get(product_id, ()))
        fits = [c for c in self.active if c.store in (store, "all")]
        return [c for c in fits if c.id in mine] + [c for c in fits if c.id not in mine and c.is_default]


def load_book(session: Session, now: datetime | None = None) -> CouponBook:
    now = now or datetime.now(timezone.utc)
    coupons = sorted((c for c in session.scalars(select(Coupon)) if is_active(c, now)), key=lambda c: (aware(c.expires_at), c.id))
    attached: dict[int, list[int]] = {}
    for product_id, coupon_id in session.execute(select(ProductCoupon.product_id, ProductCoupon.coupon_id)):
        attached.setdefault(product_id, []).append(coupon_id)
    return CouponBook(coupons, attached)
