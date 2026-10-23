"""Link de afiliado: validação do que é colado à mão e modelo opcional por configuração.

O Mercado Livre não tem API de link de afiliado (o painel do programa gera os links à mão, em
"Ferramentas > Gerador de link"); a Shopee devolve o link pela própria API (`offerLink`). Então:

- um link colado no painel vale por produto e nunca é apagado pela coleta;
- `ML_AFFILIATE_TEMPLATE` (opcional) monta o link do ML sozinho, se você descobrir o formato que o seu
  painel gera. Fica desligado por padrão: o formato não é documentado oficialmente.
"""

from urllib.parse import urlsplit

from achados.models import Product

# Só estes domínios (e subdomínios): impede guardar lixo, ou um link de golpe que acabaria numa mensagem.
ALLOWED_HOSTS = ("mercadolivre.com", "mercadolivre.com.br", "meli.la", "shopee.com.br", "shp.ee")
MAX_LENGTH = 500

ORIGIN_MANUAL = "manual"  # colado no painel
ORIGIN_API = "api"  # veio do coletor (Shopee)
ORIGIN_TEMPLATE = "template"  # montado por ML_AFFILIATE_TEMPLATE


def validate_affiliate_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("o link está vazio")
    if len(url) > MAX_LENGTH or any(ch.isspace() for ch in url):
        raise ValueError("o link tem espaços ou é grande demais")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("o link precisa começar com https://")
    host = (parts.hostname or "").lower()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS):
        raise ValueError(f"domínio não permitido: {host or 'sem domínio'} (aceitos: {', '.join(ALLOWED_HOSTS)})")
    return url


def _fields(product: Product) -> dict[str, str]:
    product_id = (product.raw or {}).get("product_id") or product.external_id.removeprefix("product:")
    return {
        "url": product.url,  # com ?wid=: leva ao anúncio mais barato do momento
        "product_url": f"https://www.mercadolivre.com.br/p/{product_id}",  # a página do produto, sem o anúncio
        "product_id": product_id,
        "item_id": str((product.raw or {}).get("item_id") or ""),
        "external_id": product.external_id,
    }


def validate_template(template: str) -> None:
    """Falha na subida do serviço se o modelo tiver um campo desconhecido ou der um link inválido."""
    if not template:
        return
    sample = {"url": "https://www.mercadolivre.com.br/p/MLB1?wid=MLB2", "product_url": "https://www.mercadolivre.com.br/p/MLB1",
              "product_id": "MLB1", "item_id": "MLB2", "external_id": "product:MLB1"}  # fmt: skip
    try:
        built = template.format_map(sample)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"ML_AFFILIATE_TEMPLATE inválido ({type(exc).__name__}: {exc}); campos: {', '.join(sample)}") from exc
    validate_affiliate_url(built)


def effective_link(product: Product, ml_template: str = "") -> tuple[str | None, str | None]:
    """O link que vai na mensagem e de onde ele veio: (url, origem). O que foi guardado vence o modelo."""
    if product.affiliate_url:
        return product.affiliate_url, ORIGIN_API if product.source == "shopee" else ORIGIN_MANUAL
    if ml_template and product.source == "mercadolivre":
        return ml_template.format_map(_fields(product)), ORIGIN_TEMPLATE
    return None, None
