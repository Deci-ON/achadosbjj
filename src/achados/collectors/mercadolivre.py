import random
import re
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from achados.collectors.base import BlockedError
from achados.schemas import Offer

OFFERS_URL = "https://www.mercadolivre.com.br/ofertas"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
}

_ITEM_ID = re.compile(r"MLB-?(\d{6,})", re.IGNORECASE)


def make_http_client(proxy: str | None = None, timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(headers=HEADERS, proxy=proxy, timeout=timeout, follow_redirects=True)


class MercadoLivreCollector:
    """Coleta cards de listagem (ex.: /ofertas) do Mercado Livre por HTML.

    O ML bloqueia IPs de datacenter com uma página de "verificação de conta";
    nesse caso levanta BlockedError em vez de devolver lista vazia.
    """

    source = "mercadolivre"

    def __init__(
        self,
        http: httpx.Client,
        listing_url: str = OFFERS_URL,
        max_pages: int = 3,
        delay: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = http
        self._listing_url = listing_url
        self._max_pages = max_pages
        self._delay = delay
        self._sleep = sleep

    def collect(self) -> Iterator[Offer]:
        for page in range(1, self._max_pages + 1):
            html = self._fetch(self._listing_url, page)
            offers = parse_listing(html)
            if not offers:
                break
            yield from offers
            if page < self._max_pages:
                self._sleep(self._delay + random.uniform(0, 1.5))

    def _fetch(self, url: str, page: int) -> str:
        response = self._http.get(url, params={"page": page} if page > 1 else None)
        if response.status_code in (403, 429) or "account-verification" in str(response.url):
            raise BlockedError(f"Mercado Livre bloqueou a requisição ({response.status_code}, {response.url})")
        response.raise_for_status()
        return response.text


def parse_listing(html: str) -> list[Offer]:
    offers = []
    for card in HTMLParser(html).css(".poly-card"):
        offer = _parse_card(card)
        if offer:
            offers.append(offer)
    return offers


def _parse_card(card: Node) -> Offer | None:
    link = card.css_first("a.poly-component__title") or card.css_first(".poly-component__title-wrapper a")
    if link is None or not link.attributes.get("href"):
        return None

    href = link.attributes["href"]
    external_id = _external_id(href)
    price = _money(card.css_first(".poly-price__current .andes-money-amount") or _first_current_amount(card))
    if external_id is None or price is None:
        return None

    original = _money(card.css_first(".andes-money-amount--previous"))
    discount = card.css_first(".andes-money-amount__discount, .poly-price__disc_label")
    seller = card.css_first(".poly-component__seller")
    rating = card.css_first(".poly-reviews__rating")

    return Offer(
        source="mercadolivre",
        external_id=external_id,
        title=link.text(strip=True),
        url=_clean_url(href),
        image_url=_image(card.css_first("img.poly-component__picture, .poly-component__picture img")),
        price=price,
        original_price=original,
        discount_pct=_percent(discount.text()) if discount else _derive_discount(price, original),
        shop_name=_strip_seller(seller.text(strip=True)) if seller else None,
        rating=Decimal(rating.text(strip=True).replace(",", ".")) if rating and _is_number(rating.text()) else None,
    )


def _first_current_amount(card: Node) -> Node | None:
    for amount in card.css(".andes-money-amount"):
        classes = amount.attributes.get("class") or ""
        if "andes-money-amount--previous" not in classes:
            return amount
    return None


def _money(node: Node | None) -> Decimal | None:
    if node is None:
        return None
    fraction = node.css_first(".andes-money-amount__fraction")
    if fraction is None:
        return None
    digits = re.sub(r"\D", "", fraction.text())
    if not digits:
        return None
    cents_node = node.css_first(".andes-money-amount__cents")
    cents = re.sub(r"\D", "", cents_node.text()) if cents_node else ""
    return Decimal(f"{digits}.{cents.ljust(2, '0')[:2]}")


def _percent(text: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*%", text)
    return int(match.group(1)) if match else None


def _derive_discount(price: Decimal, original: Decimal | None) -> int | None:
    if original and original > price:
        return int((1 - price / original) * 100)
    return None


def _is_number(text: str) -> bool:
    return bool(re.fullmatch(r"\s*\d+([.,]\d+)?\s*", text))


def _strip_seller(text: str) -> str:
    return re.sub(r"^\s*(Vendido por|Por)\s+", "", text, flags=re.IGNORECASE)


def _image(img: Node | None) -> str | None:
    if img is None:
        return None
    src = img.attributes.get("src") or ""
    if not src or src.startswith("data:"):
        src = img.attributes.get("data-src") or ""
    return src or None


def _target_url(href: str) -> str:
    """Links de oferta podem passar por um redirecionador de clique (`?url=...`)."""
    parsed = urlparse(href)
    if "click" in parsed.netloc:
        target = parse_qs(parsed.query).get("url")
        if target:
            return unquote(target[0])
    return href


def _external_id(href: str) -> str | None:
    target = urlparse(_target_url(href))
    # `wid` identifica o anúncio específico quando a URL aponta para o catálogo (/p/MLB...).
    wid = parse_qs(target.query).get("wid")
    match = _ITEM_ID.search(wid[0]) if wid else _ITEM_ID.search(target.path)
    return f"MLB{match.group(1)}" if match else None


def _clean_url(href: str) -> str:
    target = urlparse(_target_url(href))
    url = f"{target.scheme}://{target.netloc}{target.path}"
    wid = parse_qs(target.query).get("wid")
    return f"{url}?wid={wid[0]}" if wid else url
