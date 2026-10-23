"""Serviço FastAPI: coleta agendada no lifespan e API de consulta.

Subir com:  uvicorn achados.service:create_app --factory --host 0.0.0.0 --port 8000
Use um único worker: o lock de líder (Postgres) evita coleta duplicada se houver mais, mas o disparo
manual (`POST /api/collect`) só funciona no worker que é líder.
"""

import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from achados.collectors.mercadolivre_api import PRESETS, build_collector
from achados.config import Settings, get_settings
from achados.db import make_engine, make_session_factory
from achados.logs import configure_logging
from achados.models import Base, CollectRun, Product
from achados.messages import format_offer_message
from achados.offers import assess_product, assess_products
from achados.scheduler import CollectScheduler, LeaderLock
from achados.scoring import IRRELEVANT, LEARNING, NO_OFFER, OFFER, ScoringConfig, Verdict, normalize

log = logging.getLogger("achados")

STATUSES = (OFFER, NO_OFFER, LEARNING, IRRELEVANT)


class OfferOut(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    url: str
    affiliate_url: str | None
    image_url: str | None
    price: float
    original_price: float | None
    status: str
    score: int
    reference_price: float | None
    real_discount_pct: int | None
    claimed_discount_pct: int | None
    observed_days: float
    reasons: list[str]
    last_seen_at: datetime
    message: str  # texto pronto para colar no WhatsApp


class PricePointOut(BaseModel):
    price: float
    original_price: float | None  # "de" declarado pelo vendedor naquele momento (não é confiável)
    collected_at: datetime


class ProductDetailOut(OfferOut):
    history: list[PricePointOut]


class OffersResponse(BaseModel):
    counts: dict[str, int]
    items: list[OfferOut]


def offer_out(product: Product, verdict: Verdict) -> OfferOut:
    link = product.affiliate_url or product.url
    return OfferOut(
        id=product.id,
        source=product.source,
        external_id=product.external_id,
        title=product.title,
        url=product.url,
        affiliate_url=product.affiliate_url,
        image_url=product.image_url,
        price=float(product.price),
        original_price=float(product.original_price) if product.original_price else None,
        status=verdict.status,
        score=verdict.score,
        reference_price=float(verdict.reference_price) if verdict.reference_price else None,
        real_discount_pct=verdict.real_discount_pct,
        claimed_discount_pct=verdict.claimed_discount_pct,
        observed_days=verdict.observed_days,
        reasons=verdict.reasons,
        last_seen_at=product.last_seen_at,
        message=format_offer_message(
            title=product.title,
            price=product.price,
            link=link,
            is_offer=verdict.is_offer,
            reference_price=verdict.reference_price,
            real_discount_pct=verdict.real_discount_pct,
            window_days=ScoringConfig.window_days,
        ),
    )


def create_app(
    settings: Settings | None = None,
    *,
    sessions: sessionmaker[Session] | None = None,
    scheduler: CollectScheduler | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    if sessions is None:
        sessions = make_session_factory(make_engine())
    engine = sessions.kw["bind"]
    if scheduler is None:
        if settings.collect_preset not in PRESETS:  # falha na subida, não só na primeira coleta
            raise ValueError(f"COLLECT_PRESET desconhecido: {settings.collect_preset!r} (disponíveis: {PRESETS})")
        scheduler = CollectScheduler(
            sessions,
            LeaderLock(engine),
            lambda: build_collector(settings, settings.collect_preset, sessions),
            "mercadolivre",
            interval=settings.collect_interval_seconds,
            startup_delay=settings.collect_startup_delay_seconds,
            enabled=settings.collect_enabled,
        )

    manual_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)  # idempotente; poupa um `init-db` manual
        if not settings.admin_token:
            log.warning("ADMIN_TOKEN não definido: os endpoints /api/* responderão 503")
        loop_task = asyncio.create_task(scheduler.run(), name="collect-scheduler")
        try:
            yield
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    app = FastAPI(title="achadosbjj", lifespan=lifespan)

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        if not settings.admin_token:
            raise HTTPException(503, "ADMIN_TOKEN não configurado")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), settings.admin_token.encode()):
            raise HTTPException(401, "token inválido", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    def health():
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(503, f"banco indisponível: {type(exc).__name__}") from exc
        return {"status": "ok"}

    @app.get("/api/status", dependencies=[Depends(require_admin)])
    def status():
        state = scheduler.state
        with sessions() as session:
            runs = session.scalars(select(CollectRun).order_by(CollectRun.id.desc()).limit(5)).all()
            products = dict(session.execute(select(Product.source, func.count()).group_by(Product.source)).all())
        return {
            "scheduler": {
                "enabled": state.enabled,
                "is_leader": state.is_leader,
                "running": state.running,
                "consecutive_failures": state.consecutive_failures,
                "last_error": state.last_error,
                "next_due": state.next_due,
            },
            "products": products,
            "recent_runs": [
                {
                    "id": r.id,
                    "source": r.source,
                    "status": r.status,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "items_seen": r.items_seen,
                    "items_new": r.items_new,
                    "error": r.error,
                }
                for r in runs
            ],
        }

    @app.get("/api/offers", response_model=OffersResponse, dependencies=[Depends(require_admin)])
    def offers(
        status: str = Query(OFFER, pattern="^(" + "|".join(STATUSES) + ")$"),
        source: str | None = Query(None, pattern="^(shopee|mercadolivre)$"),
        min_score: int = Query(0, ge=0, le=100),
        limit: int = Query(30, ge=1, le=200),
        q: str | None = Query(None, max_length=80, description="palavras que o título deve conter"),
        max_age_days: float = Query(3.0, gt=0),
        min_drop: float = Query(ScoringConfig.min_drop_pct, ge=0, le=100),
        min_history: float = Query(ScoringConfig.min_history_days, ge=0),
    ):
        with sessions() as session:
            verdicts = assess_products(
                session,
                source=source,
                max_age_days=max_age_days,
                config=ScoringConfig(min_drop_pct=min_drop, min_history_days=min_history),
            )
        words = normalize(q).split() if q else []
        counts = {s: 0 for s in STATUSES}
        items = []
        for product, verdict in verdicts:
            title = normalize(product.title)
            if words and not all(word in title for word in words):
                continue  # a busca vale também para as contagens: elas descrevem o que o filtro mostra
            counts[verdict.status] += 1
            if verdict.status == status and verdict.score >= min_score and len(items) < limit:
                items.append(offer_out(product, verdict))
        return OffersResponse(counts=counts, items=items)

    @app.get("/api/products/{product_id}", response_model=ProductDetailOut, dependencies=[Depends(require_admin)])
    def product_detail(product_id: int):
        with sessions() as session:
            found = assess_product(session, product_id)
        if found is None:
            raise HTTPException(404, "produto não encontrado")
        product, verdict, history = found
        return ProductDetailOut(
            **offer_out(product, verdict).model_dump(),
            history=[
                PricePointOut(price=float(price), original_price=float(original) if original else None, collected_at=at)
                for price, original, at in history
            ],
        )

    @app.post("/api/collect", status_code=202, dependencies=[Depends(require_admin)])
    async def collect():
        state = scheduler.state
        if not state.enabled:
            raise HTTPException(409, "agendador desligado (COLLECT_ENABLED=false)")
        if not state.is_leader:
            raise HTTPException(409, "este worker não é o líder da coleta")
        if scheduler.busy:
            raise HTTPException(409, "já existe uma coleta em andamento")
        task = asyncio.create_task(scheduler.collect_now(), name="manual-collect")
        manual_tasks.add(task)  # referência forte: o loop só guarda referência fraca
        task.add_done_callback(manual_tasks.discard)
        return {"status": "started"}

    return app
