from collections.abc import Iterator
from typing import Protocol

from achados.schemas import Offer


class CollectorError(RuntimeError):
    pass


class BlockedError(CollectorError):
    """A loja bloqueou a requisição (captcha, verificação de conta, 403/429)."""


class Collector(Protocol):
    source: str

    def collect(self) -> Iterator[Offer]: ...
