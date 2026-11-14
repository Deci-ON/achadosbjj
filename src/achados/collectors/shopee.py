import hashlib
import json
import logging
import time
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

import httpx

from achados.collectors.base import BlockedError, CollectorError
from achados.schemas import Offer

log = logging.getLogger("achados")

# sortType da Shopee Affiliate Open API (productOfferV2).
SORT_RELEVANCE = 1
SORT_SALES = 2
SORT_PRICE_DESC = 3
SORT_PRICE_ASC = 4
SORT_COMMISSION = 5

PRODUCT_OFFER_QUERY = """
query ProductOffers($keyword: String, $page: Int, $limit: Int, $sortType: Int) {
  productOfferV2(keyword: $keyword, page: $page, limit: $limit, sortType: $sortType) {
    nodes {
      itemId
      shopId
      productName
      shopName
      imageUrl
      priceMin
      priceMax
      priceDiscountRate
      commissionRate
      sales
      ratingStar
      productLink
      offerLink
    }
    pageInfo { page limit hasNextPage }
  }
}
""".strip()


CONVERSION_QUERY = """
query Conversions($start: Int, $end: Int, $limit: Int, $scrollId: String) {
  conversionReport(purchaseTimeStart: $start, purchaseTimeEnd: $end, limit: $limit, scrollId: $scrollId) {
    nodes {
      conversionId
      purchaseTime
      clickTime
      utmContent
      buyerType
      totalCommission
      orders { orderId orderStatus items { itemId itemName qty actualAmount itemTotalCommission displayItemStatus } }
    }
    pageInfo { limit hasNextPage scrollId }
  }
}
""".strip()


# Palavras do nicho. A busca da Shopee mistura judô e infantil; a regra de nicho (`scoring.BJJ_RULES`) exige
# "jiu jitsu"/BJJ/rash guard no título, então o resto é descartado na pontuação, não aqui.
BJJ_KEYWORDS = [
    "kimono jiu jitsu",
    "kimono bjj",
    "kimono trancado jiu jitsu",
    "faixa jiu jitsu",
    "rash guard jiu jitsu",
    "bermuda jiu jitsu",
    "tatame jiu jitsu",
    "tatame luta",
]


class ShopeeAPIError(CollectorError):
    pass


class ShopeeClient:
    """Cliente GraphQL da Shopee Affiliate Open API.

    A assinatura é SHA256(app_id + timestamp + payload + secret), e o payload
    assinado precisa ser exatamente o corpo enviado, por isso o corpo é
    serializado uma única vez e enviado como bytes.
    """

    def __init__(self, app_id: str, secret: str, url: str, http: httpx.Client | None = None):
        if not app_id or not secret:
            raise ShopeeAPIError("SHOPEE_APP_ID e SHOPEE_SECRET são obrigatórios")
        self._app_id = app_id
        self._secret = secret
        self._url = url
        self._http = http or httpx.Client(timeout=20.0)

    def authorization(self, payload: str, timestamp: int) -> str:
        raw = f"{self._app_id}{timestamp}{payload}{self._secret}"
        signature = hashlib.sha256(raw.encode()).hexdigest()
        return f"SHA256 Credential={self._app_id}, Timestamp={timestamp}, Signature={signature}"

    def query(self, query: str, variables: dict) -> dict:
        payload = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))
        response = self._http.post(
            self._url,
            content=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": self.authorization(payload, int(time.time())),
            },
        )
        if response.status_code in (403, 429):
            raise BlockedError(f"Shopee respondeu {response.status_code}")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise ShopeeAPIError(json.dumps(body["errors"], ensure_ascii=False))
        return body["data"]

    def generate_short_link(self, origin_url: str, sub_ids: list[str]) -> str:
        """Link curto de afiliado com `subIds` (até 5): eles voltam em `utmContent` no relatório de conversão.

        A mutação documentada leva o `input` inline; `json.dumps` produz strings e listas válidas em GraphQL.
        """
        query = (
            "mutation { generateShortLink(input: {"
            f"originUrl: {json.dumps(origin_url)}, subIds: {json.dumps(sub_ids)}"
            "}) { shortLink } }"
        )
        link = ((self.query(query, {}) or {}).get("generateShortLink") or {}).get("shortLink")
        if not link:
            raise ShopeeAPIError("a Shopee não devolveu o link curto")
        return link

    def conversion_report(self, start: int, end: int, limit: int = 500, max_pages: int = 20) -> list[dict]:
        """Conversões (pedidos pelos seus links) com compra entre `start` e `end` (unix). Só vale 3 meses para trás.

        A paginação é por `scrollId`, que vale 30 s; a primeira consulta de um período também só pode repetir
        depois de 30 s (documentação da API), por isso quem chama guarda o resultado em cache.
        """
        nodes: list[dict] = []
        scroll_id = None
        for _ in range(max_pages):
            variables = {"start": start, "end": end, "limit": limit, "scrollId": scroll_id}
            data = self.query(CONVERSION_QUERY, variables)["conversionReport"]
            nodes += data.get("nodes") or []
            info = data.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("scrollId"):
                break
            scroll_id = info["scrollId"]
        return nodes


# `10000: System Error` vem do servidor da Shopee e passa sozinho (visto em 2026-11-12: a página 1 de 50 produtos
# entrou e a seguinte falhou, depois a rodada seguinte falhou na primeira chamada). Os demais erros (assinatura,
# acesso negado, conta congelada) não passam com nova tentativa e derrubam a coleta de uma vez.
TRANSIENT_CODES = ("10000",)
RETRY_WAITS = (2.0, 6.0, 20.0)


class ShopeeCollector:
    source = "shopee"

    def __init__(
        self,
        client: ShopeeClient,
        keywords: list[str | None] | None = None,
        max_pages: int = 3,
        limit: int = 50,
        sort_type: int = SORT_SALES,
        delay: float = 1.0,
        retry_waits: tuple[float, ...] = RETRY_WAITS,
        sleep=time.sleep,
    ):
        # `None` = sem palavra-chave (ofertas gerais).
        self._client = client
        self._keywords = keywords or [None]
        self._max_pages = max_pages
        self._limit = limit
        self._sort_type = sort_type
        self._delay = delay
        self._retry_waits = retry_waits
        self._sleep = sleep
        self.skipped: list[str] = []  # palavras que pararam por erro passageiro na última coleta

    def _fetch_page(self, keyword: str | None, page: int) -> dict:
        variables = {"keyword": keyword, "page": page, "limit": self._limit, "sortType": self._sort_type}
        waits = iter(self._retry_waits)
        while True:
            try:
                return self._client.query(PRODUCT_OFFER_QUERY, variables)["productOfferV2"]
            except ShopeeAPIError as exc:
                wait = next(waits, None)
                if wait is None or not any(code in str(exc) for code in TRANSIENT_CODES):
                    raise
                log.info("Shopee: erro passageiro em %r pagina %d, nova tentativa em %.0fs", keyword, page, wait)
                self._sleep(wait)

    def collect(self) -> Iterator[Offer]:
        """Coleta por palavra. Um erro passageiro que não passa nas tentativas pula o resto daquela palavra e segue
        para a próxima; só falha a coleta se nenhuma página veio (ou se o erro não é passageiro)."""
        self.skipped = []
        pages_ok = 0
        last_error: ShopeeAPIError | None = None
        for keyword in self._keywords:
            for page in range(1, self._max_pages + 1):
                try:
                    data = self._fetch_page(keyword, page)
                except ShopeeAPIError as exc:
                    if not any(code in str(exc) for code in TRANSIENT_CODES):
                        raise
                    last_error = exc
                    self.skipped.append(f"{keyword} (pagina {page})")
                    log.warning("Shopee: %r pagina %d falhou depois das tentativas, seguindo para a proxima palavra", keyword, page)
                    break
                pages_ok += 1
                for node in data["nodes"]:
                    offer = parse_node(node)
                    if offer:
                        yield offer
                if not data["pageInfo"]["hasNextPage"]:
                    break
                self._sleep(self._delay)
        if pages_ok == 0 and last_error is not None:
            raise last_error
        if self.skipped:
            log.warning("Shopee: coleta parcial, palavras que pararam: %s", "; ".join(self.skipped))


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def parse_node(node: dict) -> Offer | None:
    price = _decimal(node.get("priceMin"))
    if price is None or not node.get("itemId"):
        return None

    discount = int(node.get("priceDiscountRate") or 0)
    original = None
    if 0 < discount < 100:
        # A API só informa o percentual; o preço "de" é estimado a partir dele.
        original = (price / (1 - Decimal(discount) / 100)).quantize(Decimal("0.01"))

    affiliate_url = node.get("offerLink")
    return Offer(
        source="shopee",
        external_id=f"{node.get('shopId')}.{node['itemId']}",
        title=node["productName"],
        url=node.get("productLink") or affiliate_url or "",
        affiliate_url=affiliate_url,
        image_url=node.get("imageUrl"),
        price=price,
        original_price=original,
        discount_pct=discount or None,
        shop_name=node.get("shopName"),
        commission_rate=_decimal(node.get("commissionRate")),
        rating=_decimal(node.get("ratingStar")),
        sales=int(node["sales"]) if node.get("sales") is not None else None,
        raw=node,
    )


def build_collector(settings, keywords: list[str | None] | None = None) -> ShopeeCollector:
    """Coletor da Shopee pronto para o agendamento (preset de jiu-jitsu), a partir de `SHOPEE_*`."""
    http = httpx.Client(timeout=settings.request_timeout)
    client = ShopeeClient(settings.shopee_app_id, settings.shopee_secret, settings.shopee_api_url, http=http)
    # Medido em 2026-11-12 com `check-shopee`: a relevância é a ordenação que quase nunca dá `10000: System Error`
    # (vendas falhou com 10+ por página, comissão com 50), e páginas menores falham menos. 20 por página x 5 páginas
    # cobre 100 produtos por palavra, e o retry do coletor cobre o que ainda falhar.
    return ShopeeCollector(client, keywords=keywords or list(BJJ_KEYWORDS), max_pages=5, limit=20, sort_type=SORT_RELEVANCE)

