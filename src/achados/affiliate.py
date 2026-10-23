"""Link de afiliado: validação do que é colado à mão.

O Mercado Livre não tem API de link de afiliado. O gerador do painel de afiliados
(https://www.mercadolivre.com.br/afiliados/linkbuilder) devolve links curtos `https://meli.la/<código>`
cujo código é opaco, e a versão completa (`/social/<perfil>?matt_word=…&matt_tool=…&ref=<token>`)
leva o produto dentro de um `ref` gerado pelo servidor do ML. Não há como montar o link por modelo:
o link vale o que o gerador devolveu, colado à mão por produto. A Shopee devolve o link pela própria API.
Um link guardado nunca é apagado pela coleta.
"""

from urllib.parse import urlsplit

from achados.models import Product

# Só estes domínios (e subdomínios): impede guardar lixo, ou um link de golpe que acabaria numa mensagem.
ALLOWED_HOSTS = ("mercadolivre.com", "mercadolivre.com.br", "meli.la", "shopee.com.br", "shp.ee")
MAX_LENGTH = 1000  # o link completo do ML leva um token longo; o curto (meli.la) é o recomendado

ORIGIN_MANUAL = "manual"  # colado no painel
ORIGIN_API = "api"  # veio do coletor (Shopee)


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


def effective_link(product: Product) -> tuple[str | None, str | None]:
    """O link que vai na mensagem e de onde ele veio: (url, origem)."""
    if not product.affiliate_url:
        return None, None
    return product.affiliate_url, ORIGIN_API if product.source == "shopee" else ORIGIN_MANUAL
