"""Envio das ofertas aos grupos do WhatsApp por um robô próprio (sidecar `sender/`, whatsapp-web.js).

O robô controla um WhatsApp Web logado num número dedicado (QR code). Isso é um cliente não oficial: o
WhatsApp não permite, e o número pode ser banido. O que este módulo faz para reduzir o risco:

- só manda o que você aprovou ("Enviar" na oferta): a fila só se enche sozinha se você ligar o envio
  automático (desligado por padrão, nunca liga sozinho; ver `SendWorker.auto_enqueue`);
- intervalo aleatório entre mensagens, limite por dia e janela de horário (Brasília);
- confere o preço no ML logo antes de mandar e cancela se subiu (ou se o anúncio sumiu);
- pausa sozinho depois de falhas seguidas; botão de pausa no painel.

Um job é uma oferta para um grupo. O texto é congelado quando entra na fila. Quando o último job de um
produto sai, a oferta é marcada como enviada (a mesma carência de 7 dias do envio manual).
"""

import asyncio
import hashlib
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from achados.collectors.mercadolivre_api import MercadoLivreAPI, SellerTrust, best_offer
from achados.collectors.shopee import ShopeeClient
from achados.models import Product, SendGroup, SendJob, SenderSetting
from achados.scheduler import LeaderLock
from achados.scoring import normalize
from achados.workflow import record_send

log = logging.getLogger("achados")

SENDER_LOCK_KEY = 875321003  # ao lado do da coleta (875321002)
BRT = timezone(timedelta(hours=-3))  # o Brasil não tem horário de verão desde 2019
MAX_ATTEMPTS = 3
AUTO_PAUSE_AFTER = 3  # falhas de envio seguidas pausam o robô
POLL_SECONDS = 30.0
RETRY_SECONDS = 60.0
PRICE_RISE_TOLERANCE = Decimal("1.03")  # cancela se o preço subiu mais de 3% desde que entrou na fila
# O automático não repõe na fila, por este tempo, a oferta cujo job foi cancelado ou falhou: sem isso, o
# preço que subiu (cancelado na conferência) voltaria a cada ciclo e seria cancelado de novo, e o que você
# cancelou no painel voltaria sozinho.
AUTO_RETRY_COOLDOWN = timedelta(hours=24)

# O que o automático enfileira: (produto representante do modelo, texto, imagem), da melhor para a pior.
# Recebe o corte de pontuação. Vem do serviço, que reusa a lógica de /api/offers e `present`.
Candidates = Callable[[int], list[tuple[Product, str, str | None]]]


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


def auto_enabled(session: Session) -> bool:
    return _get(session, "auto_send") == "1"  # ausente = desligado: só liga por ação explícita no painel


def auto_min_score(session: Session, default: int) -> int:
    try:
        return min(max(int(_get(session, "auto_min_score")), 0), 100)
    except ValueError:
        return default


def set_auto(session: Session, enabled: bool, min_score: int | None = None) -> None:
    """`min_score` None mantém o corte guardado."""
    _set(session, "auto_send", "1" if enabled else "0")
    if min_score is not None:
        _set(session, "auto_min_score", str(min_score))


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


def group_tag(group: SendGroup) -> str:
    """Identificador do grupo para o sub-id do link da Shopee: nome só com letras e números (até 12) + 4 hex do id.

    Só [a-z0-9] porque a documentação não diz quais caracteres o sub-id aceita. Os 4 hex separam dois grupos de
    nome parecido e não mudam se o grupo for renomeado.
    """
    name = re.sub(r"[^a-z0-9]", "", normalize(group.name))[:12]
    return f"{name}{hashlib.sha1(group.chat_id.encode()).hexdigest()[:4]}"


class GroupLinks(Protocol):
    def link_for(self, product: Product, group: SendGroup) -> str | None:
        """Link de afiliado próprio deste grupo para o produto, ou None (usa o link comum da oferta)."""


class ShopeeGroupLinks:
    """Um link curto da Shopee por grupo (`generateShortLink` com sub-ids), para o relatório de vendas dizer qual grupo vendeu.

    Falhou? Devolve None e a oferta sai com o link comum: perder o rastreio de um envio é melhor do que não enviar.
    """

    def __init__(self, client: ShopeeClient):
        self._client = client
        self._cache: dict[tuple[int, str], str] = {}

    def link_for(self, product: Product, group: SendGroup) -> str | None:
        if product.source != "shopee" or not product.url:
            return None
        key = (product.id, group.chat_id)
        if key not in self._cache:
            try:
                self._cache[key] = self._client.generate_short_link(product.url, ["wa", group_tag(group)])
            except Exception as exc:
                log.warning("link por grupo indisponível (produto %s): %s", product.id, type(exc).__name__)
                return None
        return self._cache[key]


def enqueue_offer(
    session: Session, product: Product, text: str, image_url: str | None, group_links: GroupLinks | None = None
) -> int:
    """Põe a oferta na fila de cada grupo escolhido. Devolve quantos jobs criou.

    Com `group_links`, o link de afiliado do texto é trocado, em cada job, pelo link próprio do grupo.
    """
    groups = saved_groups(session)
    if not groups:
        raise ValueError("nenhum grupo escolhido: marque os grupos na aba Envio")
    queued = session.scalar(select(func.count()).select_from(SendJob).where(SendJob.product_id == product.id, SendJob.status == "queued"))
    if queued:
        raise ValueError("esta oferta já está na fila de envio")
    common_link = product.affiliate_url
    for group in groups:
        group_link = group_links.link_for(product, group) if group_links and common_link else None
        session.add(
            SendJob(
                product_id=product.id,
                chat_id=group.chat_id,
                group_name=group.name,
                text=text.replace(common_link, group_link) if group_link else text,
                image_url=image_url,
                price=product.price,
            )
        )
    session.flush()
    return len(groups)


def summarize_conversions(nodes: list[dict], groups: list[SendGroup]) -> dict:
    """Vendas da Shopee por grupo: agrupa as conversões pelo sub-id (`utmContent`) que o `ShopeeGroupLinks` pôs no link.

    O formato do `utmContent` não está na documentação (só "valor dos seus sub-ids"), então o texto é quebrado em
    qualquer separador e basta uma das partes ser o identificador do grupo. O que não casa com nenhum grupo
    (links de antes, grupo tirado da lista) fica em `unattributed`.
    """

    def blank(**extra) -> dict:
        return {**extra, "conversions": 0, "commission": 0.0, "completed": 0, "pending": 0, "cancelled": 0}

    rows = {group_tag(g): blank(id=g.chat_id, name=g.name, tag=group_tag(g)) for g in groups}
    other = blank()
    for node in nodes:
        tokens = {t.lower() for t in re.split(r"[^A-Za-z0-9]+", node.get("utmContent") or "") if t}
        row = next((rows[tag] for tag in rows if tag in tokens), other)
        row["conversions"] += 1
        row["commission"] = round(row["commission"] + float(node.get("totalCommission") or 0), 2)
        for order in node.get("orders") or []:
            status = (order.get("orderStatus") or "").upper()
            bucket = {"COMPLETED": "completed", "CANCELLED": "cancelled"}.get(status, "pending")  # UNPAID e PENDING
            row[bucket] += 1
    return {"groups": sorted(rows.values(), key=lambda r: (-r["commission"], r["name"])), "unattributed": other}


def cancel_job(session: Session, job_id: int, reason: str = "cancelado no painel") -> bool:
    job = session.get(SendJob, job_id)
    if job is None or job.status != "queued":
        return False
    job.status, job.error = "cancelled", reason
    return True


def queued_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(SendJob).where(SendJob.status == "queued")) or 0


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
        candidates: Candidates | None = None,
        group_links: GroupLinks | None = None,
        auto_interval: float = 300.0,
        auto_default_min_score: int = 70,
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
        self._candidates = candidates  # sem isso o envio automático não faz nada
        self._group_links = group_links
        self._auto_interval = auto_interval
        self._auto_default_min_score = auto_default_min_score
        self._next_auto: datetime | None = None
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
        return await asyncio.to_thread(self._leader_pass)

    def _leader_pass(self) -> float:
        self.auto_enqueue()
        return self.step()

    def _delay(self) -> float:
        return self._rng.uniform(self._min_delay, self._max_delay)

    def auto_enqueue(self) -> int:
        """Envio automático: põe na fila as ofertas prontas (o painel liga e desliga). Devolve quantas ofertas entraram.

        Só com o modo ligado, robô não pausado, grupos escolhidos e dentro da janela de horário: fora dela o
        texto ficaria congelado por horas antes de sair, e o limite do dia que se conta seria o do dia errado
        (o envio também respeita a janela, isto só evita fila velha). Não passa do que cabe no limite diário
        (limite - enviadas hoje - já na fila; cada grupo é uma mensagem). Os jobs seguem pela conferência de
        preço do ML, pelo intervalo e pelos limites de sempre. Procura candidatas a cada `auto_interval`.
        """
        if self._candidates is None:
            return 0
        now = self._clock()
        try:
            with self._sessions() as session:
                if not auto_enabled(session) or is_paused(session) or not in_window(now, *self._window):
                    return 0
                groups = saved_groups(session)
                budget = self._daily_cap - sent_today(session, now) - queued_count(session)
                if not groups or budget < len(groups):
                    return 0
                if self._next_auto is not None and now < self._next_auto:
                    return 0
                self._next_auto = now + timedelta(seconds=self._auto_interval)

                min_score = auto_min_score(session, self._auto_default_min_score)
                enqueued = 0
                for product, text, image_url in self._candidates(min_score):
                    if budget < len(groups):
                        break
                    try:
                        enqueue_offer(session, product, text, image_url, self._group_links)
                    except ValueError:  # já está na fila (entrou à mão enquanto procurávamos)
                        continue
                    budget -= len(groups)
                    enqueued += 1
                    log.info("envio automático: produto %s na fila para %d grupo(s) (R$ %s)", product.id, len(groups), product.price)
                session.commit()
                return enqueued
        except Exception:  # nunca derruba o laço do robô: o próximo ciclo tenta de novo
            log.exception("envio automático falhou ao montar a fila")
            return 0

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
