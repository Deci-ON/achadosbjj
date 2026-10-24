"""Variações de tamanho do mesmo modelo.

No catálogo do ML cada tamanho de um kimono é um produto à parte ("... Torah Tamanho A2", "... Tamanho A3"),
com o próprio id, o próprio anúncio e o próprio preço. Para quem vai divulgar, é um modelo só: a página do ML
mostra o seletor de tamanho com os produtos irmãos, então um link serve todos os tamanhos.

Por isso o agrupamento é só de apresentação: cada produto continua sendo pontuado sozinho (o preço de um
tamanho pode cair sem os outros caírem), e a lista, a fila de links e a mensagem tratam o modelo como um item.
Modelo = mesma loja, mesmo vendedor e mesmo título sem o "Tamanho X" do fim.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from achados.models import Product
from achados.scoring import IRRELEVANT, LEARNING, NO_OFFER, OFFER, Verdict, normalize

_SIZE_SUFFIX = re.compile(r"^(?P<base>.*\S)\s+tamanho\s+(?P<size>\S+)\s*$", re.IGNORECASE)
# Vendedores também põem o código solto no fim ("... Preto A3", "... CBJJ - M2"). Só os códigos de
# tamanho de kimono/faixa vistos no catálogo: A0-A6, M0-M6 (e M00/M000, infantil), K0-K6, F0-F6.
_SIZE_CODE_SUFFIX = re.compile(r"^(?P<base>.*\S)\s+(?:[-–]\s*)?(?P<size>[AKF][0-6]|M[0-6]|M0{2,3})\s*$", re.IGNORECASE)
# "... Branco A2 E A3" lista vários tamanhos num anúncio só: não é a variação de um tamanho.
_LISTS_SIZES = re.compile(r"(?:^|\s)(?:e|ou|/|&|,)$", re.IGNORECASE)
_STATUS_RANK = {OFFER: 0, LEARNING: 1, NO_OFFER: 2, IRRELEVANT: 3}

Entry = tuple[Product, Verdict]


def split_size(title: str) -> tuple[str, str | None]:
    """('Kimono X Tamanho A2') -> ('Kimono X', 'A2'); também 'Kimono X Preto A2' e 'Faixa X - M1'.

    Sem tamanho no fim, devolve (título, None).
    """
    title = title.strip()
    match = _SIZE_SUFFIX.match(title)
    if match:
        return match.group("base"), match.group("size")
    match = _SIZE_CODE_SUFFIX.match(title)
    if match:
        base = match.group("base").rstrip(" -–")
        if base and not _LISTS_SIZES.search(base):
            return base, match.group("size")
    return title, None


def model_key(product: Product) -> tuple | None:
    """Chave do modelo, ou None quando o produto não é agrupável (outra loja, sem vendedor, sem tamanho no título)."""
    if product.source != "mercadolivre":
        return None
    seller = (product.raw or {}).get("seller_id")
    base, size = split_size(product.title)
    if seller is None or size is None:
        return None
    return (product.source, seller, normalize(base))


def _size_order(size: str) -> tuple[str, int, str]:
    digits = re.search(r"\d+", size)
    return (re.sub(r"\d+", "", size).lower(), int(digits.group()) if digits else -1, size)


@dataclass
class Group:
    """Um modelo: o produto que o representa (`members[0]`) e todos os tamanhos vistos."""

    members: list[Entry]  # o melhor primeiro: oferta, depois maior pontuação, depois menor preço

    @property
    def product(self) -> Product:
        return self.members[0][0]

    @property
    def verdict(self) -> Verdict:
        return self.members[0][1]

    @property
    def is_model(self) -> bool:
        return len(self.members) > 1

    @property
    def title(self) -> str:
        """O título do modelo: sem o tamanho quando há mais de um."""
        return split_size(self.product.title)[0] if self.is_model else self.product.title

    @property
    def link(self) -> str | None:
        """O link de afiliado do modelo: o do representante, ou o de qualquer tamanho (o link serve todos)."""
        for product, _ in self.members:
            if product.affiliate_url:
                return product.affiliate_url
        return None

    @property
    def ids(self) -> list[int]:
        return [product.id for product, _ in self.members]

    def sizes(self) -> list[Entry]:
        """Os tamanhos em ordem de leitura (A0, A1, A2...)."""
        return sorted(self.members, key=lambda e: _size_order(split_size(e[0].title)[1] or ""))

    def offer_sizes(self) -> list[tuple[str, Decimal]]:
        """(tamanho, preço) dos tamanhos que estão em oferta de verdade."""
        return [(split_size(p.title)[1] or "", p.price) for p, v in self.sizes() if v.is_offer]


def _rank(entry: Entry) -> tuple:
    product, verdict = entry
    return (_STATUS_RANK.get(verdict.status, 9), -verdict.score, product.price, product.title)


def group_models(entries: list[Entry]) -> list[Group]:
    """Junta os tamanhos de cada modelo. Os grupos saem do melhor para o pior (pontuação do representante)."""
    buckets: dict[tuple, list[Entry]] = {}
    for entry in entries:
        key = model_key(entry[0]) or ("single", entry[0].id)
        buckets.setdefault(key, []).append(entry)
    groups = [Group(sorted(members, key=_rank)) for members in buckets.values()]
    return sorted(groups, key=lambda g: (-g.verdict.score, g.title))


def find_siblings(session: Session, product: Product) -> list[Product]:
    """Os produtos do mesmo modelo (inclui o próprio). Sem tamanho no título, só ele."""
    key = model_key(product)
    if key is None:
        return [product]
    base, _ = split_size(product.title)
    escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = session.scalars(
        select(Product).where(Product.source == product.source, Product.title.like(f"{escaped}%", escape="\\"))
    )
    siblings = [p for p in candidates if model_key(p) == key]
    return siblings or [product]
