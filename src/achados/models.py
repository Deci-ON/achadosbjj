from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CollectRun(Base):
    __tablename__ = "collect_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_products_source_external_id"),
        Index("ix_products_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    affiliate_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    shop_name: Mapped[str | None] = mapped_column(String(255))

    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_pct: Mapped[int | None] = mapped_column(Integer)
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    sales: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    # Cascata também no ORM: o SQLite dos testes não aplica o ON DELETE CASCADE.
    coupon_links: Mapped[list["ProductCoupon"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class PriceSnapshot(Base):
    """Uma linha por mudança de preço (e uma na primeira coleta), para histórico."""

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_price_snapshots_product_collected", "product_id", "collected_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collect_runs.id", ondelete="SET NULL"))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_pct: Mapped[int | None] = mapped_column(Integer)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="snapshots")


class EmptyCatalogProduct(Base):
    """Produto de catálogo que, na última checagem, não tinha oferta ativa.

    Cerca de 77% dos produtos achados na busca do ML respondem 404 em /items; sem este cache cada
    coleta repetiria essas chamadas. Quem ganhar oferta volta a ser checado depois do prazo de revalidação.
    """

    __tablename__ = "empty_catalog_products"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SentOffer(Base):
    """Uma oferta que foi mandada (colada no WhatsApp). Serve para não repetir a mesma oferta à toa."""

    __tablename__ = "sent_offers"
    __table_args__ = (Index("ix_sent_offers_product_sent", "product_id", "sent_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # o preço no momento do envio


class SendGroup(Base):
    """Um grupo do WhatsApp (id do wwebjs) que recebe as ofertas enviadas pelo robô."""

    __tablename__ = "send_groups"

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")


class SendJob(Base):
    """Uma mensagem na fila do robô: uma oferta para um grupo. O texto é congelado ao entrar na fila."""

    __tablename__ = "send_jobs"
    __table_args__ = (Index("ix_send_jobs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(String(64))
    group_name: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # preço ao entrar na fila; o robô confere de novo antes de enviar
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | sent | failed | cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SenderSetting(Base):
    """Chave/valor do robô de envio (ex.: `paused`), para sobreviver a reinício."""

    __tablename__ = "sender_settings"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Coupon(Base):
    """Cupom cadastrado à mão (nenhuma das lojas tem API de cupom). Só vira uma linha na mensagem da oferta:
    o comprador aplica no app/checkout, o cupom não vai no link de afiliado e nunca entra no preço nem na pontuação."""

    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[str] = mapped_column(String(16))  # mercadolivre | shopee | all
    kind: Mapped[str] = mapped_column(String(16))  # code | page | shipping
    title: Mapped[str] = mapped_column(String(80))  # o código digitado ou o nome do cupom
    rule: Mapped[str] = mapped_column(Text, default="")  # texto livre curto: "compra mínima R$ 100, até R$ 30 off"
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # entra em toda mensagem da loja enquanto valer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product_links: Mapped[list["ProductCoupon"]] = relationship(back_populates="coupon", cascade="all, delete-orphan")


class ProductCoupon(Base):
    """Cupom anexado a uma oferta específica (o produto representante do modelo, o id que o painel usa)."""

    __tablename__ = "product_coupons"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id", ondelete="CASCADE"), primary_key=True)

    product: Mapped[Product] = relationship(back_populates="coupon_links")
    coupon: Mapped[Coupon] = relationship(back_populates="product_links")
