"""Envio das ofertas aos grupos do WhatsApp por um robô próprio (sidecar `sender/`, whatsapp-web.js).

O robô controla um WhatsApp Web logado num número dedicado (QR code). Isso é um cliente não oficial: o
WhatsApp não permite, e o número pode ser banido. O que este módulo faz para reduzir o risco:

- só manda o que você aprovou ("Enviar" na oferta): a fila nunca se enche sozinha;
- intervalo aleatório entre mensagens, limite por dia e janela de horário (Brasília);
- confere o preço no ML logo antes de mandar e cancela se subiu (ou se o anúncio sumiu);
- pausa sozinho depois de falhas seguidas; botão de pausa no painel.

Um job é uma oferta para um grupo. O texto é congelado quando entra na fila. Quando o último job de um
produto sai, a oferta é marcada como enviada (a mesma carência de 7 dias do envio manual).
"""

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from achados.collectors.mercadolivre_api import MercadoLivreAPI, SellerTrust, best_offer
from achados.models import Product, SendGroup, SendJob, SenderSetting
from achados.scheduler import LeaderLock
from achados.workflow import record_send

log = logging.getLogger("achados")

SENDER_LOCK_KEY = 875321003  # ao lado do da coleta (875321002)
BRT = timezone(timedelta(hours=-3))  # o Brasil não tem horário de verão desde 2019
MAX_ATTEMPTS = 3
AUTO_PAUSE_AFTER = 3  # falhas de envio seguidas pausam o robô
POLL_SECONDS = 30.0
RETRY_SECONDS = 60.0
PRICE_RISE_TOLERANCE = Decimal("1.03")  # cancela se o preço subiu mais de 3% desde que entrou na fila


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- sidecar ------------------------------------------------------------------


class SenderError(Exception):
    """Falha do robô que pode passar numa nova tentativa."""


class SenderPermanentError(SenderError):
    """O robô recusou o pedido (ex.: grupo não existe mais): tentar de novo não adianta."""


class SenderClient:
    """Cliente HTTP do sidecar. Respostas: GET /status, GET /groups, POST /send."""

    def __init__(self, url: str, token: str, http: httpx.Client | None = None):
        self._url = url.rstrip("/")
        self._http = http or httpx.Client(timeout=60.0)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, json: dict | None = None):
        try:
            response = self._http.request(method, f"{self._url}{path}", json=json, headers=self._headers)
        except httpx.HTTPError as exc:
            raise SenderError(f"robô indisponível ({type(exc).__name__})") from exc
        if response.status_code in (400, 404, 422):
            raise SenderPermanentError(_detail(response))
        if response.status_code >= 400:
            raise SenderError(f"robô respondeu {response.status_code}: {_detail(response)}")
        return response.json()

    def status(self) -> dict:
        """{'state': starting | qr | authenticated | ready | disconnected, 'qr': data URL ou None}."""
        return self._call("GET", "/status")

    def groups(self) -> list[dict]:
        """[{'id': '1203...@g.us', 'name': '...', 'participants': n}]"""
        return self._call("GET", "/groups")

    def send(self, chat_id: str, text: str, image_url: str | None = None) -> None:
        self._call("POST", "/send", {"chat_id": chat_id, "text": text, "image_url": image_url})


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error") or response.text)[:200]
    except Exception:
        return response.text[:200]


class PriceChecker(Protocol):
    def current_price(self, product: Product) -> Decimal | None:
        """O menor preço novo hoje; None quando o produto não tem mais anúncio novo."""


class MlPriceChecker:
    """Confere o preço no ML (mesma regra do coletor, inclusive o filtro de preço de isca)."""

    def __init__(self, api: MercadoLivreAPI):
        self._api = api
        self._trust = SellerTrust(api)  # a mesma regra do coletor: vendedor sem reputação não dá preço

    def current_price(self, product: Product) -> Decimal | None:
        product_id = (product.raw or {}).get("product_id")
        if not product_id:
            return product.price  # sem como conferir: vale o que foi coletado
        body = self._api.get(f"/products/{product_id}/items")
        offer = (
            best_offer({"id": product_id, "name": product.title}, (body or {}).get("results", []), self._trust) if body else None
        )
        return offer.price if offer else None


# --- configuração guardada ------------------------------------------------------


def _get(session: Session, key: str) -> str:
    row = session.get(SenderSetting, key)
    return row.value if row else ""


def _set(session: Session, key: str, value: str) -> None:
    row = session.get(SenderSetting, key)
    if row is None:
        session.add(SenderSetting(key=key, value=value))
    else:
        row.value = value


def is_paused(session: Session) -> bool:
    return _get(session, "paused") == "1"


def pause_reason(session: Session) -> str:
    return _get(session, "pause_reason")


def set_paused(session: Session, paused: bool, reason: str = "") -> None:
    _set(session, "paused", "1" if paused else "0")
    _set(session, "pause_reason", reason if paused else "")


def saved_groups(session: Session) -> list[SendGroup]:
    return list(session.scalars(select(SendGroup).order_by(SendGroup.name)))


def replace_groups(session: Session, groups: list[tuple[str, str]]) -> None:
    """Os grupos que recebem as ofertas: o que não está na lista sai. Jobs na fila de grupo removido são cancelados."""
    wanted = dict(groups)
    for group in saved_groups(session):
        if group.chat_id not in wanted:
            session.delete(group)
    for chat_id, name in wanted.items():
        group = session.get(SendGroup, chat_id)
        if group is None:
            session.add(SendGroup(chat_id=chat_id, name=name))
        else:
            group.name = name
    session.execute(
        update(SendJob)
        .where(SendJob.status == "queued", SendJob.chat_id.not_in(list(wanted)) if wanted else True)
        .values(status="cancelled", error="grupo removido da lista")
    )


# --- fila -------------------------------------------------------------------------


def enqueue_offer(session: Session, product: Product, text: str, image_url: str | None) -> int:
    """Põe a oferta na fila de cada grupo escolhido. Devolve quantos jobs criou."""
    groups = saved_groups(session)
    if not groups:
        raise ValueError("nenhum grupo escolhido: marque os grupos na aba Envio")
    queued = session.scalar(select(func.count()).select_from(SendJob).where(SendJob.product_id == product.id, SendJob.status == "queued"))
    if queued:
        raise ValueError("esta oferta já está na fila de envio")
    for group in groups:
        session.add(
            SendJob(
                product_id=product.id,
                chat_id=group.chat_id,
                group_name=group.name,
                text=text,
                image_url=image_url,
                price=product.price,
            )
        )
    session.flush()
    return len(groups)


def cancel_job(session: Session, job_id: int, reason: str = "cancelado no painel") -> bool:
    job = session.get(SendJob, job_id)
    if job is None or job.status != "queued":
        return False
    job.status, job.error = "cancelled", reason
    return True


def day_start(now: datetime) -> datetime:
    """Meia-noite de Brasília do dia de `now`, em UTC."""
    local = now.astimezone(BRT)
    return datetime.combine(local.date(), dtime.min, tzinfo=BRT).astimezone(timezone.utc)


def sent_today(session: Session, now: datetime) -> int:
    return session.scalar(select(func.count()).select_from(SendJob).where(SendJob.status == "sent", SendJob.sent_at >= day_start(now))) or 0


def in_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    return start_hour <= now.astimezone(BRT).hour < end_hour


# --- laço -------------------------------------------------------------------------


@dataclass
class WorkerState:
    enabled: bool
    is_leader: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    next_send_at: datetime | None = None


class SendWorker:
    """Manda, um por vez, os jobs da fila, respeitando pausa, janela, limite diário e intervalo."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        lock: LeaderLock,
        client: SenderClient | None,
        *,
        daily_cap: int = 30,
        min_delay: float = 60.0,
        max_delay: float = 180.0,
        window: tuple[int, int] = (8, 22),
        checker: PriceChecker | None = None,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep,
    ):
        self._sessions = sessions
        self._lock = lock
        self._client = client
        self._daily_cap = daily_cap
        self._min_delay = min_delay
        self._max_delay = max(max_delay, min_delay)
        self._window = window
        self._checker = checker
        self._clock = clock
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._not_before: datetime | None = None
        self.state = WorkerState(enabled=client is not None)

    async def run(self) -> None:
        if not self.state.enabled:
            log.info("envio ao WhatsApp desligado (SENDER_URL vazio)")
            return
        log.info("robô de envio iniciado (limite %d/dia, intervalo %.0f-%.0fs)", self._daily_cap, self._min_delay, self._max_delay)
        try:
            while True:
                await self._sleep(await self.tick())
        except asyncio.CancelledError:
            raise
        finally:
            self._lock.release()
            self.state.is_leader = False

    async def tick(self) -> float:
        self.state.is_leader = await asyncio.to_thread(self._lock.acquire)
        if not self.state.is_leader:
            return RETRY_SECONDS
        return await asyncio.to_thread(self.step)

    def _delay(self) -> float:
        return self._rng.uniform(self._min_delay, self._max_delay)

    def step(self) -> float:
        """Um passo: manda no máximo uma mensagem. Devolve quanto dormir."""
        now = self._clock()
        with self._sessions() as session:
            job = session.scalars(select(SendJob).where(SendJob.status == "queued").order_by(SendJob.id)).first()
            if job is None or is_paused(session):
                return POLL_SECONDS
            if not in_window(now, *self._window) or sent_today(session, now) >= self._daily_cap:
                return POLL_SECONDS * 10  # fora do horário ou no limite do dia: confere de novo em 5 minutos

            if self._not_before is None:  # depois de um reinício, conta o intervalo a partir do último envio
                last = session.scalar(select(func.max(SendJob.sent_at)).where(SendJob.status == "sent"))
                self._not_before = _aware(last) + timedelta(seconds=self._delay()) if last else now
            wait = (self._not_before - now).total_seconds()
            self.state.next_send_at = self._not_before
            if wait > 0:
                return min(wait, POLL_SECONDS)

            try:
                status = self._client.status()
            except SenderError as exc:
                self.state.last_error = str(exc)
                return RETRY_SECONDS
            if status.get("state") != "ready":
                self.state.last_error = f"WhatsApp não está conectado ({status.get('state')})"
                return RETRY_SECONDS

            product = session.get(Product, job.product_id)
            reason = self._price_problem(job, product)
            if reason == "retry":
                return RETRY_SECONDS
            if reason:
                self._cancel_product(session, job.product_id, reason)
                session.commit()
                return 1.0

            return self._send(session, job, product, now)

    def _price_problem(self, job: SendJob, product: Product | None) -> str | None:
        """Motivo para não mandar (texto), 'retry' se não deu para conferir, ou None."""
        if product is None:
            return "produto removido"
        if self._checker is None or product.source != "mercadolivre":
            return None
        try:
            price = self._checker.current_price(product)
        except Exception as exc:  # nunca manda sem conferir: tenta de novo daqui a pouco
            log.warning("não deu para conferir o preço de %s (%s)", product.id, type(exc).__name__)
            self.state.last_error = f"conferência de preço falhou ({type(exc).__name__})"
            return "retry"
        if price is None:
            return "o produto não tem mais anúncio novo no ML"
        if price > job.price * PRICE_RISE_TOLERANCE:
            return f"preço subiu de R$ {job.price} para R$ {price}"
        return None

    def _cancel_product(self, session: Session, product_id: int, reason: str) -> None:
        log.info("envio cancelado (produto %s): %s", product_id, reason)
        session.execute(
            update(SendJob).where(SendJob.product_id == product_id, SendJob.status == "queued").values(status="cancelled", error=reason)
        )

    def _send(self, session: Session, job: SendJob, product: Product, now: datetime) -> float:
        job.attempts += 1
        try:
            self._client.send(job.chat_id, job.text, job.image_url)
        except SenderPermanentError as exc:
            job.status, job.error = "failed", str(exc)
            session.commit()
            return 1.0
        except SenderError as exc:
            job.error = str(exc)
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "failed"
            self.state.consecutive_failures += 1
            self.state.last_error = str(exc)
            if self.state.consecutive_failures >= AUTO_PAUSE_AFTER:
                set_paused(session, True, f"{AUTO_PAUSE_AFTER} falhas seguidas: {exc}")
                log.error("robô de envio pausado: %d falhas seguidas (%s)", self.state.consecutive_failures, exc)
            session.commit()
            return RETRY_SECONDS

        job.status, job.sent_at, job.error = "sent", now, None
        self.state.consecutive_failures, self.state.last_error = 0, None
        session.flush()
        remaining = session.scalar(select(func.count()).select_from(SendJob).where(SendJob.product_id == job.product_id, SendJob.status == "queued"))
        if not remaining:
            record_send(session, product, now)  # último grupo: a oferta conta como enviada
        session.commit()
        delay = self._delay()
        self._not_before = now + timedelta(seconds=delay)
        self.state.next_send_at = self._not_before
        return min(delay, POLL_SECONDS)
