import time
from collections.abc import Callable, Iterator
from decimal import Decimal, InvalidOperation

import httpx

from achados.collectors.base import BlockedError, CollectorError
from achados.schemas import Offer

API_URL = "https://api.mercadolibre.com"
SITE_URL = "https://www.mercadolivre.com.br"

# Categorias de topo (GET /sites/MLB/categories) com bom volume de achadinhos.
DEFAULT_CATEGORIES = {
    "MLB1051": "Celulares e Telefones",
    "MLB5726": "Eletrodomésticos",
    "MLB1000": "Eletrônicos, Áudio e Vídeo",
    "MLB1648": "Informática",
    "MLB1574": "Casa, Móveis e Decoração",
    "MLB1246": "Beleza e Cuidado Pessoal",
    "MLB1276": "Esportes e Fitness",
    "MLB1144": "Games",
    "MLB263532": "Ferramentas",
    "MLB1384": "Bebês",
}


class MercadoLivreAPIError(CollectorError):
    pass


class MercadoLivreAPI:
    """Cliente da API oficial do Mercado Livre (fluxo client_credentials).

    O token dura 6h e é renovado sozinho, inclusive se a API responder 401 no meio da coleta.
    Endpoints de busca (`/sites/MLB/search`) e de anúncio (`/items/{id}`) respondem 403 para
    esse tipo de token, por isso a coleta parte de `/highlights` e de `/products`.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.Client | None = None,
        base_url: str = API_URL,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not client_id or not client_secret:
            raise MercadoLivreAPIError("ML_CLIENT_ID e ML_CLIENT_SECRET são obrigatórios")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=20.0)
        self._base_url = base_url
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def _authenticate(self) -> str:
        response = self._http.post(
            f"{self._base_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code in (400, 401):
            raise MercadoLivreAPIError(f"credenciais do Mercado Livre recusadas ({response.status_code})")
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        # Margem de 60s para não usar um token prestes a expirar.
        self._expires_at = self._clock() + int(body.get("expires_in", 21600)) - 60
        return self._token

    def _bearer(self) -> str:
        if self._token is None or self._clock() >= self._expires_at:
            return self._authenticate()
        return self._token

    def get(self, path: str, params: dict | None = None) -> dict | list | None:
        """GET autenticado. Devolve None para 404; 403/429 viram BlockedError."""
        url = f"{self._base_url}{path}"
        for attempt in (1, 2):
            response = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._bearer()}"})
            if response.status_code == 401 and attempt == 1:
                self._token = None
                continue
            break
        if response.status_code == 404:
            return None
        if response.status_code in (403, 429):
            raise BlockedError(f"Mercado Livre respondeu {response.status_code} em {path}")
        response.raise_for_status()
        return response.json()


class MercadoLivreApiCollector:
    """Coleta os mais vendidos de cada categoria e, de cada produto, a melhor oferta."""

    source = "mercadolivre"

    def __init__(
        self,
        api: MercadoLivreAPI,
        categories: list[str] | None = None,
        per_category: int = 20,
        min_discount: int = 0,
        delay: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api = api
        self._categories = categories or list(DEFAULT_CATEGORIES)
        self._per_category = per_category
        self._min_discount = min_discount
        self._delay = delay
        self._sleep = sleep

    def collect(self) -> Iterator[Offer]:
        seen: set[str] = set()
        for category in self._categories:
            for product_id in self._highlight_ids(category):
                if product_id in seen:
                    continue
                seen.add(product_id)
                offer = self._offer_for(product_id)
                self._sleep(self._delay)
                if offer and (offer.discount_pct or 0) >= self._min_discount:
                    yield offer

    def _highlight_ids(self, category: str) -> list[str]:
        body = self._api.get(f"/highlights/MLB/category/{category}")
        if not body:
            return []
        # `USER_PRODUCT` responde 403 em /user-products, então só os produtos de catálogo servem.
        ids = [entry["id"] for entry in body.get("content", []) if entry.get("type") == "PRODUCT"]
        return ids[: self._per_category]

    def _offer_for(self, product_id: str) -> Offer | None:
        product = self._api.get(f"/products/{product_id}")
        items = self._api.get(f"/products/{product_id}/items")
        if not product or not items:
            return None
        return best_offer(product, items.get("results", []))


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _discount(price: Decimal, original: Decimal | None) -> int | None:
    if original and original > price:
        return int((1 - price / original) * 100)
    return None


def best_offer(product: dict, results: list[dict]) -> Offer | None:
    """Escolhe, entre as ofertas novas do produto, a de maior desconto (empate: menor preço)."""
    candidates = []
    for result in results:
        price = _decimal(result.get("price"))
        if price is None or not result.get("item_id") or result.get("condition") != "new":
            continue
        original = _decimal(result.get("original_price"))
        candidates.append((-(_discount(price, original) or 0), price, result, original))
    if not candidates:
        return None

    _, price, result, original = min(candidates, key=lambda c: (c[0], c[1]))
    pictures = product.get("pictures") or []
    return Offer(
        source="mercadolivre",
        external_id=result["item_id"],
        title=product["name"],
        url=f"{SITE_URL}/p/{product['id']}?wid={result['item_id']}",
        image_url=pictures[0].get("url") if pictures else None,
        price=price,
        original_price=original if original and original > price else None,
        discount_pct=_discount(price, original),
        raw={"product_id": product["id"], "domain_id": product.get("domain_id"), "offer": result},
    )
