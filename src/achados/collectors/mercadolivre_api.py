import logging
import time
from collections.abc import Callable, Iterator
from decimal import Decimal, InvalidOperation

import httpx

from achados.collectors.base import BlockedError, CollectorError
from achados.probes import DbProbeCache, ProbeCache
from achados.schemas import Offer
from achados.scoring import normalize

log = logging.getLogger("achados")

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

# Preset de BJJ. Categorias e domínios vêm de /sites/MLB/domain_discovery/search; rash guard e bermuda
# caem em categorias genéricas (MLB-SPORT_T_SHIRTS, MLB-SHORTS), sem produtos de catálogo úteis.
BJJ_CATEGORIES = ["MLB47731", "MLB277803"]  # Kimonos; Faixas
BJJ_DOMAINS = ("MLB-MARTIAL_ARTS_UNIFORMS", "MLB-MARTIAL_ARTS_BELTS")
# Lista fixa: as de maior rendimento num experimento com ~35 palavras (produtos novos com oferta por palavra:
# kimono pretorian 56, faixa jiu jitsu preta 52, kimono torah 43, reforçado 38, venum 27, shiroi 26...). Fica
# de fora "kimono judo" (traz judô, que o filtro de título descarta) e luva/protetor bucal (MMA, não BJJ).
# As marcas e variações que mudam com o tempo vêm de /trends (ver BJJ_TREND_LIMIT).
BJJ_KEYWORDS = [
    "kimono jiu jitsu",
    "kimono bjj",
    "kimono trancado",
    "kimono reforçado jiu jitsu",
    "kimono infantil jiu jitsu",
    "kimono pretorian",
    "kimono torah",
    "kimono venum",
    "kimono shiroi",
    "kimono koral",
    "kimono atama",
    "kimono keiko",
    "kimono hayabusa",
    "kimono dragao",
    "kimono naja",
    "faixa jiu jitsu",
    "faixa jiu jitsu preta",
    "faixa jiu jitsu azul",
    "faixa jiu jitsu roxa",
    "faixa jiu jitsu marrom",
    "faixa jiu jitsu infantil",
]
BJJ_TREND_LIMIT = 30  # palavras novas por coleta vindas de /trends

SEARCH_PAGE_SIZE = 50
SEARCH_MAX_PAGES = 3  # o ML aceita offset + limit <= 150: offsets 0, 50 e 100

# Filtro das palavras-chave de /trends (o que as pessoas mais buscam na categoria). A lista vem com
# marcas úteis (kingz, hayabusa, gracie barra...) e muito ruído (outros esportes, acessórios, lixo).
BJJ_KEYWORD_ANCHORS = ("jiu jitsu", "jiujitsu", "bjj", "kimono", "quimono", "rash guard", "rashguard", "no gi")
BJJ_KEYWORD_MARKERS = ("jiu jitsu", "jiujitsu", "bjj")  # citar o esporte salva "kimono judo jiu jitsu"
BJJ_KEYWORD_OTHER_SPORTS = ("judo", "karate", "taekwondo", "dobok", "muay", "boxe", "kendo", "capoeira")
BJJ_KEYWORD_REJECT = (
    "caneca", "boneco", "cabide", "usado", "tatame", "mochila", "bolsa", "chaveiro", "adesivo", "miniatura",
    "brinquedo", "pelucia",
)  # fmt: skip

# Esperas (s) entre tentativas quando a API responde 429; o `Retry-After`, se vier, tem prioridade.
RATE_LIMIT_BACKOFF = (3.0, 10.0, 30.0)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["Retry-After"]), 120.0)
    except (KeyError, ValueError):
        return None


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
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not client_id or not client_secret:
            raise MercadoLivreAPIError("ML_CLIENT_ID e ML_CLIENT_SECRET são obrigatórios")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=20.0)
        self._base_url = base_url
        self._clock = clock
        self._sleep = sleep
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
        """GET autenticado. Devolve None para 404; 403 vira BlockedError, e 429 também depois de
        esgotar as esperas (RATE_LIMIT_BACKOFF, ou o `Retry-After` do servidor)."""
        url = f"{self._base_url}{path}"
        renewed = False
        waits = iter(RATE_LIMIT_BACKOFF)
        while True:
            response = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._bearer()}"})
            if response.status_code == 401 and not renewed:
                renewed, self._token = True, None
                continue
            if response.status_code == 429:
                wait = next(waits, None)
                if wait is not None:
                    self._sleep(_retry_after(response) or wait)
                    continue
            break
        if response.status_code == 404:
            return None
        if response.status_code in (403, 429):
            raise BlockedError(f"Mercado Livre respondeu {response.status_code} em {path}")
        response.raise_for_status()
        return response.json()


class MercadoLivreApiCollector:
    """Coleta produtos de catálogo e grava, de cada um, o menor preço do mercado.

    O item acompanhado é o **produto de catálogo**, não o anúncio de um vendedor: quando o
    vendedor mais barato muda, o histórico de preço do produto continua contínuo, e a queda
    do menor preço é o sinal de oferta que `achados.scoring` procura.

    Os produtos vêm de duas fontes, combináveis: os mais vendidos de `categories` e a busca
    de catálogo por `keywords` (filtrada por `domains`, quando informados).
    """

    source = "mercadolivre"

    def __init__(
        self,
        api: MercadoLivreAPI,
        categories: list[str] | None = None,
        keywords: list[str] | None = None,
        domains: tuple[str, ...] | None = None,
        per_category: int = 20,
        search_pages: int = SEARCH_MAX_PAGES,
        min_discount: int = 0,
        delay: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        probe_cache: ProbeCache | None = None,
        trend_categories: list[str] | None = None,
        trend_limit: int = 30,
    ):
        self._api = api
        self._trend_categories = trend_categories or []
        self._trend_limit = trend_limit
        self.discovered_keywords: list[str] = []  # palavras vindas de /trends na última coleta
        self._probe_cache = probe_cache
        self.skipped_empty = 0  # produtos pulados pelo cache na última coleta
        self._categories = categories if categories is not None else ([] if keywords else list(DEFAULT_CATEGORIES))
        self._keywords = keywords or []
        self._domains = domains
        self._per_category = per_category
        self._search_pages = search_pages
        self._min_discount = min_discount
        self._delay = delay
        self._sleep = sleep

    def collect(self) -> Iterator[Offer]:
        seen: set[str] = set()
        known_empty = self._probe_cache.fresh_empty_ids() if self._probe_cache else set()
        empty: set[str] = set()
        with_offers: set[str] = set()
        self.skipped_empty = 0
        try:
            for product in self._candidates(seen):
                if product["id"] in seen:
                    continue
                seen.add(product["id"])
                if product["id"] in known_empty:
                    self.skipped_empty += 1
                    continue
                offer = self._offer_for(product)
                self._sleep(self._delay)
                (with_offers if offer else empty).add(product["id"])
                # `discount_pct` é o desconto declarado pelo vendedor; a oferta de verdade sai do histórico.
                if offer and (offer.discount_pct or 0) >= self._min_discount:
                    yield offer
        finally:
            # Também numa coleta que falhou no meio: o que já foi checado não precisa ser refeito.
            if self._probe_cache:
                self._probe_cache.record(empty, with_offers)
            if self.skipped_empty:
                log.info("cache: %d produtos sem oferta pulados (revalidados após 24h)", self.skipped_empty)

    def _candidates(self, seen: set[str]) -> Iterator[dict]:
        for category in self._categories:
            for product_id in self._highlight_ids(category):
                if product_id in seen:  # já coletado por outra categoria: poupa a chamada de detalhe
                    continue
                product = self._api.get(f"/products/{product_id}")
                if product:
                    yield product
        self.discovered_keywords = (
            discover_keywords(self._api, self._trend_categories, known=self._keywords, limit=self._trend_limit)
            if self._trend_categories
            else []
        )
        if self.discovered_keywords:
            log.info("trends: %d palavras-chave novas: %s", len(self.discovered_keywords), self.discovered_keywords)
        for keyword in [*self._keywords, *self.discovered_keywords]:
            yield from self._search(keyword)

    def _highlight_ids(self, category: str) -> list[str]:
        body = self._api.get(f"/highlights/MLB/category/{category}")
        if not body:
            return []
        # `USER_PRODUCT` e `ITEM` não abrem com este token (403), então só os produtos de catálogo servem.
        ids = [entry["id"] for entry in body.get("content", []) if entry.get("type") == "PRODUCT"]
        return ids[: self._per_category]

    def _search(self, keyword: str) -> Iterator[dict]:
        for page in range(self._search_pages):
            offset = page * SEARCH_PAGE_SIZE
            try:
                body = self._api.get(
                    "/products/search",
                    {"status": "active", "site_id": "MLB", "q": keyword, "limit": SEARCH_PAGE_SIZE, "offset": offset},
                )
            except httpx.HTTPStatusError as exc:
                # O ML só pagina a busca de catálogo até uns 150 resultados (offset=200 já dá 400).
                # Depois da primeira página, 400 quer dizer "acabou"; na primeira é erro de verdade.
                if exc.response.status_code == 400 and offset > 0:
                    log.info("busca %r: o ML não pagina além de offset=%d; seguindo com o que veio", keyword, offset)
                    break
                raise
            results = (body or {}).get("results", [])
            for product in results:
                if self._domains is None or product.get("domain_id") in self._domains:
                    yield product
            if len(results) < SEARCH_PAGE_SIZE:
                break

    def _offer_for(self, product: dict) -> Offer | None:
        items = self._api.get(f"/products/{product['id']}/items")
        if not items:
            return None
        return best_offer(product, items.get("results", []))


def _keyword_ok(normalized: str) -> bool:
    words = normalized.split()
    if len(words) < 2 or len(words) > 6 or any(sum(ch.isdigit() for ch in w) >= 3 for w in words):
        # Uma palavra só ("kimono") traz 150 resultados genéricos, quase todos de judô e caratê;
        # frases longas e números ("terreno 5x25 plano valor 28 000 00") são lixo.
        return False
    if not any(anchor in normalized for anchor in BJJ_KEYWORD_ANCHORS):
        return False
    if any(word in normalized for word in BJJ_KEYWORD_REJECT):
        return False
    has_marker = any(marker in normalized for marker in BJJ_KEYWORD_MARKERS)
    return has_marker or not any(sport in normalized for sport in BJJ_KEYWORD_OTHER_SPORTS)


def discover_keywords(
    api: "MercadoLivreAPI", categories: list[str], *, known: list[str] = (), limit: int = 30
) -> list[str]:
    """Palavras-chave em alta nas categorias (GET /trends/MLB/{categoria}) que servem ao nicho.

    Mantém a ordem de popularidade, descarta o que já está em `known` e para em `limit`. Um erro no
    /trends só registra aviso: é enriquecimento, a coleta segue com a lista fixa.
    """
    seen = {normalize(k) for k in known}
    found: list[str] = []
    for category in categories:
        try:
            body = api.get(f"/trends/MLB/{category}")
        except (BlockedError, httpx.HTTPError) as exc:
            log.warning("trends %s indisponível (%s); seguindo sem elas", category, type(exc).__name__)
            continue
        for entry in body or []:
            keyword = " ".join(str(entry.get("keyword", "")).split())
            normalized = normalize(keyword)
            if normalized in seen or not _keyword_ok(normalized):
                continue
            seen.add(normalized)
            found.append(keyword)
            if len(found) >= limit:
                return found
    return found


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
    """Resume o produto pela oferta nova de menor preço (empate: a primeira da lista).

    `external_id` é o produto, não o anúncio: o anúncio mais barato pode trocar de vendedor
    entre coletas sem quebrar o histórico. O link aponta para o anúncio mais barato do momento.
    """
    candidates = []
    for result in results:
        price = _decimal(result.get("price"))
        if price is None or not result.get("item_id") or result.get("condition") != "new":
            continue
        candidates.append((price, result))
    if not candidates:
        return None

    price, result = min(candidates, key=lambda c: c[0])
    original = _decimal(result.get("original_price"))
    pictures = product.get("pictures") or []
    return Offer(
        source="mercadolivre",
        external_id=f"product:{product['id']}",
        title=product["name"],
        url=f"{SITE_URL}/p/{product['id']}?wid={result['item_id']}",
        image_url=pictures[0].get("url") if pictures else None,
        price=price,
        original_price=original if original and original > price else None,
        discount_pct=_discount(price, original),
        raw={
            "product_id": product["id"],
            "domain_id": product.get("domain_id"),
            "item_id": result["item_id"],
            "seller_id": result.get("seller_id"),
            "offers_count": len(candidates),
            "offer": result,
        },
    )


PRESETS = ("bjj",)


def build_collector(settings, preset: str, sessions=None) -> MercadoLivreApiCollector:
    """Coletor do ML pronto para uso a partir das configurações (`ML_CLIENT_*`, proxy, timeout).

    Com `sessions`, usa o cache de produtos sem oferta (tabela `empty_catalog_products`).
    """
    if preset not in PRESETS:
        raise MercadoLivreAPIError(f"preset desconhecido: {preset!r} (disponíveis: {', '.join(PRESETS)})")
    http = httpx.Client(timeout=settings.request_timeout, proxy=settings.ml_proxy_url)
    api = MercadoLivreAPI(settings.ml_client_id, settings.ml_client_secret, http=http)
    return MercadoLivreApiCollector(
        api,
        categories=BJJ_CATEGORIES,
        keywords=BJJ_KEYWORDS,
        domains=BJJ_DOMAINS,
        probe_cache=DbProbeCache(sessions) if sessions is not None else None,
        trend_categories=BJJ_CATEGORIES,
        trend_limit=BJJ_TREND_LIMIT,
    )
