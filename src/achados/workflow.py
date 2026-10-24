"""Do "virou oferta" ao "pronta para enviar".

1. O produto vira oferta (histórico próprio, ver `scoring`).
2. Oferta do ML sem link de afiliado e com pontuação >= `LINK_MIN_SCORE` entra na fila de links pendentes
   (`needs_link`); as abaixo do corte seguem como oferta, só não entram na fila (`below_cut`).
3. Com o link guardado, a oferta fica pronta para enviar (`is_ready`), com a mensagem montada.
4. Ao marcar como enviada, não volta à lista por 7 dias, a menos que o preço caia mais de 3%.

A geração do link do ML continua com você: o `createLink` do ML vem com reCAPTCHA Enterprise e
verificação de navegador, então nada aqui o chama sozinho. O que este módulo faz é deixar a sua parte
em um passo só, para qualquer número de ofertas (ver `pair_links`).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from achados.affiliate import validate_affiliate_url
from achados.models import Product, SentOffer
from achados.scoring import Verdict

SENT_COOLDOWN = timedelta(days=7)
REPRICE_FACTOR = Decimal("0.97")  # só repete a oferta se o preço caiu mais de 3% desde o envio
MAX_BATCH = 50


@dataclass(frozen=True)
class Sent:
    at: datetime
    price: Decimal


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recent_sends(session: Session, now: datetime | None = None) -> dict[int, Sent]:
    """O último envio de cada produto dentro da carência."""
    cutoff = (now or _now()) - SENT_COOLDOWN
    rows = session.execute(
        select(SentOffer.product_id, SentOffer.sent_at, SentOffer.price).order_by(SentOffer.sent_at)
    ).all()
    latest: dict[int, Sent] = {}
    for product_id, sent_at, price in rows:
        if _aware(sent_at) >= cutoff:
            latest[product_id] = Sent(_aware(sent_at), price)  # ordenado: o último vence
    return latest


def record_send(session: Session, product: Product, now: datetime | None = None) -> Sent:
    sent_at = now or _now()
    session.add(SentOffer(product_id=product.id, sent_at=sent_at, price=product.price))
    session.flush()
    return Sent(sent_at, product.price)


def undo_send(session: Session, product_id: int | list[int]) -> bool:
    """Desfaz o envio mais recente (marcou sem querer). Com vários ids, o mais recente entre os tamanhos."""
    ids = [product_id] if isinstance(product_id, int) else product_id
    last = session.scalars(
        select(SentOffer).where(SentOffer.product_id.in_(ids)).order_by(SentOffer.sent_at.desc(), SentOffer.id.desc())
    ).first()
    if last is None:
        return False
    session.execute(delete(SentOffer).where(SentOffer.id == last.id))
    return True


def already_sent(product: Product, sent: Sent | None) -> bool:
    return sent is not None and product.price >= sent.price * REPRICE_FACTOR


def needs_link(product: Product, verdict: Verdict, min_score: int = 0, *, link: str | None = None) -> bool:
    """Oferta do ML ainda sem link de afiliado e com pontuação para valer a geração (`min_score`).

    A Shopee traz o link pela API, então não entra na fila. O corte só decide quem entra na fila: a
    oferta abaixo dele continua sendo oferta e aparece na lista, e dá para colar o link à mão.
    `link`: o link do modelo quando vem de outro tamanho (ver `variants`); vale como link do produto.
    """
    has_link = bool(link or product.affiliate_url)
    return verdict.is_offer and product.source == "mercadolivre" and not has_link and verdict.score >= min_score


def below_cut(product: Product, verdict: Verdict, min_score: int, *, link: str | None = None) -> bool:
    """Oferta do ML sem link que ficou de fora da fila só por causa do corte de pontuação."""
    return needs_link(product, verdict, link=link) and not needs_link(product, verdict, min_score, link=link)


def is_ready(product: Product, verdict: Verdict, sent: Sent | None, *, link: str | None = None) -> bool:
    return verdict.is_offer and bool(link or product.affiliate_url) and not already_sent(product, sent)


def latest_sent(sends: dict[int, Sent], ids: list[int]) -> Sent | None:
    """O envio mais recente entre os tamanhos de um modelo."""
    found = [sends[i] for i in ids if i in sends]
    return max(found, key=lambda s: s.at) if found else None


def pair_links(session: Session, pairs: list[tuple[int, str]]) -> int:
    """Guarda, de uma vez, os links do gerador do ML, pareados com os produtos pela ordem.

    O gerador aceita várias URLs e devolve os links na mesma ordem (verificado com dois produtos: o
    código de um produto já gerado voltou na posição certa, e o outro abriu o produto esperado). Daí o
    pareamento por posição. Como o servidor não tem como saber qual link é de qual produto, confere o
    que dá: tudo ou nada, links válidos, sem repetir (produtos diferentes têm links diferentes; um
    repetido indica desalinhamento) e sem tomar o link de outro produto.
    """
    if not pairs:
        raise ValueError("nenhum link para salvar")
    if len(pairs) > MAX_BATCH:
        raise ValueError(f"no máximo {MAX_BATCH} links por vez")
    ids = [pid for pid, _ in pairs]
    if len(set(ids)) != len(ids):
        raise ValueError("produto repetido na lista")
    links = [validate_affiliate_url(url) for _, url in pairs]
    if len(set(links)) != len(links):
        raise ValueError("link repetido na lista: produtos diferentes têm links diferentes, confira o pareamento")

    products = {p.id: p for p in session.scalars(select(Product).where(Product.id.in_(ids)))}
    missing = [pid for pid in ids if pid not in products]
    if missing:
        raise LookupError(f"produto não encontrado: {missing[0]}")
    taken = session.scalars(select(Product).where(Product.affiliate_url.in_(links), Product.id.not_in(ids))).first()
    if taken is not None:
        raise ValueError(f"este link já é de outro produto: {taken.affiliate_url}")

    for pid, link in zip(ids, links):
        products[pid].affiliate_url = link
    session.flush()
    return len(ids)
