from decimal import Decimal

from pydantic import BaseModel, Field


class Offer(BaseModel):
    """Oferta normalizada, independente da loja de origem."""

    source: str
    external_id: str
    title: str
    url: str
    affiliate_url: str | None = None
    image_url: str | None = None
    price: Decimal
    original_price: Decimal | None = None
    discount_pct: int | None = None
    shop_name: str | None = None
    commission_rate: Decimal | None = None
    rating: Decimal | None = None
    sales: int | None = None
    raw: dict = Field(default_factory=dict)
