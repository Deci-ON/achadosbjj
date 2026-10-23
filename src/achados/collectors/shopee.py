import hashlib
import json
import time
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

import httpx

from achados.collectors.base import BlockedError, CollectorError
from achados.schemas import Offer

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
    ):
        # `None` = sem palavra-chave (ofertas gerais).
        self._client = client
        self._keywords = keywords or [None]
        self._max_pages = max_pages
        self._limit = limit
        self._sort_type = sort_type
        self._delay = delay

    def collect(self) -> Iterator[Offer]:
        for keyword in self._keywords:
            for page in range(1, self._max_pages + 1):
                data = self._client.query(
                    PRODUCT_OFFER_QUERY,
                    {"keyword": keyword, "page": page, "limit": self._limit, "sortType": self._sort_type},
                )["productOfferV2"]
                for node in data["nodes"]:
                    offer = parse_node(node)
                    if offer:
                        yield offer
                if not data["pageInfo"]["hasNextPage"]:
                    break
                time.sleep(self._delay)


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
