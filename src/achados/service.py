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
from pathlib import Path
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from achados.affiliate import effective_link, validate_affiliate_url
from achados.collectors.mercadolivre_api import PRESETS, build_collector
from achados.config import Settings, get_settings
from achados.db import make_engine, make_session_factory
from achados.frontend import mount_frontend
from achados.logs import configure_logging
from achados.models import Base, CollectRun, Product
from achados.messages import format_offer_message
from achados.offers import assess_product, assess_products
from achados.scheduler import CollectScheduler, LeaderLock
from achados.scoring import IRRELEVANT, LEARNING, NO_OFFER, OFFER, ScoringConfig, Verdict, normalize
from achados.variants import Group, find_siblings, group_models, split_size
from achados.workflow import (
    MAX_BATCH,
    Sent,
    below_cut,
    is_ready,
    latest_sent,
    needs_link,
    pair_links,
    recent_sends,
    record_send,
    undo_send,
)

log = logging.getLogger("achados")

STATUSES = (OFFER, NO_OFFER, LEARNING, IRRELEVANT)


class VariantOut(BaseModel):
    """Um tamanho do modelo (ver `variants`)."""

    id: int
    size: str
    price: float
    status: str
    score: int


class OfferOut(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    url: str
    affiliate_url: str | None  # o que vai na mensagem
    affiliate_origin: str | None  # manual | api
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
    ready: bool  # oferta com link de afiliado e ainda não enviada: pronta para mandar
    needs_link: bool  # oferta do ML ainda sem link de afiliado e acima do corte: está na fila de links
    sent_at: datetime | None  # último envio dentro da carência de 7 dias
    variants: list[VariantOut] = []  # os tamanhos do modelo (vazio quando o produto não tem tamanhos)


class PricePointOut(BaseModel):
    price: float
    original_price: float | None  # "de" declarado pelo vendedor naquele momento (não é confiável)
    collected_at: datetime


class ProductDetailOut(OfferOut):
    history: list[PricePointOut]


class AffiliateLinkIn(BaseModel):
    url: str | None = None  # vazio ou null remove o link guardado


class OffersResponse(BaseModel):
    counts: dict[str, int]
    ready: int  # ofertas prontas para enviar (com o filtro de busca aplicado)
    pending_link: int  # ofertas do ML aguardando link de afiliado (já acima do corte de pontuação)
    below_cut: int  # ofertas do ML sem link que ficaram de fora da fila pelo corte de pontuação
    items: list[OfferOut]


class PendingItem(BaseModel):
    id: int
    title: str
    url: str  # a URL a colar no gerador do ML
    price: float
    score: int


class PendingResponse(BaseModel):
    total: int
    min_score: int  # corte de pontuação da fila (LINK_MIN_SCORE)
    below_cut: int
    items: list[PendingItem]


class BulkLinkItem(BaseModel):
    id: int
    url: str


class BulkLinksIn(BaseModel):
    items: list[BulkLinkItem]  # na ordem em que as URLs foram coladas no gerador


def offer_out(
    product: Product,
    verdict: Verdict,
    affiliate_url: str | None = None,
    origin: str | None = None,
    sent: Sent | None = None,
    min_link_score: int = 0,
    group: Group | None = None,
) -> OfferOut:
    """`product`/`verdict` são os campos próprios; com `group` de vários tamanhos, o título, a mensagem, o
    link e os sinais de fila/pronta são do modelo (do representante), e `variants` traz cada tamanho."""
    model = group if group is not None and group.is_model else None
    head, head_verdict = (model.product, model.verdict) if model else (product, verdict)
    link = affiliate_url or head.url
    return OfferOut(
        id=product.id,
        source=product.source,
        external_id=product.external_id,
        title=model.title if model else product.title,
        url=product.url,
        affiliate_url=affiliate_url,
        affiliate_origin=origin,
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
            title=model.title if model else product.title,
            price=head.price,
            link=link,
            is_offer=head_verdict.is_offer,
            reference_price=head_verdict.reference_price,
            real_discount_pct=head_verdict.real_discount_pct,
            window_days=ScoringConfig.window_days,
            sizes=model.offer_sizes() if model else None,
        ),
        ready=is_ready(head, head_verdict, sent, link=affiliate_url),
        needs_link=needs_link(head, head_verdict, min_link_score, link=affiliate_url),
        sent_at=sent.at if sent else None,
        variants=[
            VariantOut(id=p.id, size=split_size(p.title)[1] or "", price=float(p.price), status=v.status, score=v.score)
            for p, v in model.sizes()
        ]
        if model
        else [],
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

    def present(product: Product, verdict: Verdict, sent: Sent | None = None, group: Group | None = None) -> OfferOut:
        # O link do modelo vale para todos os tamanhos: pode estar guardado em qualquer um deles.
        holder = next((p for p, _ in group.members if p.affiliate_url), product) if group else product
        link, origin = effective_link(holder)
        return offer_out(product, verdict, link, origin, sent, settings.link_min_score, group)

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
        ready: bool = Query(False, description="só as prontas para enviar (ignora status)"),
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
            sends = recent_sends(session)
        words = normalize(q).split() if q else []
        counts = {s: 0 for s in STATUSES}
        items = []
        ready_count = pending_count = cut_count = 0
        for group in group_models(verdicts):  # um item por modelo: os tamanhos de um kimono entram juntos
            product, verdict = group.product, group.verdict
            if words and not any(all(word in normalize(p.title) for word in words) for p, _ in group.members):
                continue  # a busca vale também para as contagens: elas descrevem o que o filtro mostra
            counts[verdict.status] += 1
            sent = latest_sent(sends, group.ids)
            is_rdy = is_ready(product, verdict, sent, link=group.link)
            ready_count += is_rdy
            pending_count += needs_link(product, verdict, settings.link_min_score, link=group.link)
            cut_count += below_cut(product, verdict, settings.link_min_score, link=group.link)
            chosen = is_rdy if ready else verdict.status == status
            if chosen and verdict.score >= min_score and len(items) < limit:
                items.append(present(product, verdict, sent, group))
        return OffersResponse(counts=counts, ready=ready_count, pending_link=pending_count, below_cut=cut_count, items=items)

    @app.get("/api/products/{product_id}", response_model=ProductDetailOut, dependencies=[Depends(require_admin)])
    def product_detail(product_id: int):
        with sessions() as session:
            found = assess_product(session, product_id)
            if found is None:
                raise HTTPException(404, "produto não encontrado")
            product, verdict, history = found
            entries = [(product, verdict)]
            for sibling in find_siblings(session, product):
                if sibling.id != product_id and (other := assess_product(session, sibling.id)):
                    entries.append(other[:2])
            group = group_models(entries)[0]  # todos têm a mesma chave de modelo: um grupo só
            sent = latest_sent(recent_sends(session), group.ids)
        return ProductDetailOut(
            **present(product, verdict, sent, group).model_dump(),
            history=[
                PricePointOut(price=float(price), original_price=float(original) if original else None, collected_at=at)
                for price, original, at in history
            ],
        )

    @app.put("/api/products/{product_id}/affiliate-link", response_model=ProductDetailOut, dependencies=[Depends(require_admin)])
    def set_affiliate_link(product_id: int, body: AffiliateLinkIn):
        """Guarda (ou, com url vazia, remove) o link de afiliado do produto. A coleta não o apaga."""
        try:
            url = validate_affiliate_url(body.url) if body.url and body.url.strip() else None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with sessions() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise HTTPException(404, "produto não encontrado")
            product.affiliate_url = url
            session.commit()
        return product_detail(product_id)

    @app.get("/api/affiliate/pending", response_model=PendingResponse, dependencies=[Depends(require_admin)])
    def affiliate_pending(limit: int = Query(20, ge=1, le=MAX_BATCH)):
        """Ofertas do ML sem link de afiliado acima do corte, da melhor para a pior: as URLs a colar no gerador."""
        with sessions() as session:
            verdicts = assess_products(session)
        groups = group_models(verdicts)  # um link por modelo, não um por tamanho
        pending = [g for g in groups if needs_link(g.product, g.verdict, settings.link_min_score, link=g.link)]
        return PendingResponse(
            total=len(pending),
            min_score=settings.link_min_score,
            below_cut=sum(below_cut(g.product, g.verdict, settings.link_min_score, link=g.link) for g in groups),
            items=[
                PendingItem(id=g.product.id, title=g.title, url=g.product.url, price=float(g.product.price), score=g.verdict.score)
                for g in pending[:limit]
            ],
        )

    @app.post("/api/affiliate/links", dependencies=[Depends(require_admin)])
    def affiliate_links(body: BulkLinksIn):
        """Guarda os links do gerador do ML pareados pela ordem. Tudo ou nada."""
        try:
            with sessions() as session:
                saved = pair_links(session, [(item.id, item.url) for item in body.items])
                session.commit()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"saved": saved}

    @app.post("/api/products/{product_id}/sent", response_model=ProductDetailOut, dependencies=[Depends(require_admin)])
    def mark_sent(product_id: int):
        """Marca a oferta como enviada: sai de "prontas" por 7 dias, a menos que o preço caia mais de 3%."""
        with sessions() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise HTTPException(404, "produto não encontrado")
            record_send(session, product)
            session.commit()
        return product_detail(product_id)

    @app.delete("/api/products/{product_id}/sent", response_model=ProductDetailOut, dependencies=[Depends(require_admin)])
    def unmark_sent(product_id: int):
        """Desfaz o envio mais recente (marcou sem querer)."""
        with sessions() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise HTTPException(404, "produto não encontrado")
            undo_send(session, [p.id for p in find_siblings(session, product)])  # o envio pode ser de outro tamanho
            session.commit()
        return product_detail(product_id)

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

    if settings.static_dir and not mount_frontend(app, Path(settings.static_dir)):
        log.warning("STATIC_DIR=%s não tem index.html: servindo só a API", settings.static_dir)

    return app
