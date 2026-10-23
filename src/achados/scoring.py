"""Decide se um produto está em oferta de verdade.

O `original_price` ("de R$ X") é informado pelo vendedor e pode ser inflado, então não
prova nada. O preço de referência vem do histórico que nós mesmos coletamos: a mediana,
ponderada pelo tempo, do que o item custou na janela recente. Também descartamos itens
fora do nicho (canecas, brindes etc.).

Módulo puro, sem banco: recebe o histórico e devolve um veredito.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

OFFER = "offer"
NO_OFFER = "no_offer"
LEARNING = "learning"  # histórico curto demais para opinar
IRRELEVANT = "irrelevant"  # fora do nicho


@dataclass(frozen=True)
class PricePoint:
    price: Decimal
    at: datetime


@dataclass(frozen=True)
class NicheRules:
    """Palavras e categorias que definem o nicho. Comparação sem acento e sem caixa."""

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()


BJJ_RULES = NicheRules(
    include=(
        "kimono",
        "jiu jitsu",
        "jiu-jitsu",
        "jiujitsu",
        "bjj",
        "rash guard",
        "rashguard",
        "no-gi",
        "no gi",
        "judo",
    ),
    exclude=(
        "caneca",
        "chaveiro",
        "adesivo",
        "quadro",
        "miniatura",
        "boneco",
        "brinquedo",
        "almofada",
        "copo",
        "garrafa",
        "protecao solar",
        "fpu",
        "bebe",
        "pelucia",
        "decoracao",
        "cardigan",
        "praia",
    ),
    allowed_domains=("MLB-MARTIAL_ARTS_UNIFORMS", "MLB-MARTIAL_ARTS_BELTS"),
    excluded_domains=("MLB-MUGS", "MLB-BABY_SWIMWEAR"),
)


@dataclass(frozen=True)
class ScoringConfig:
    window_days: int = 30
    min_history_days: float = 7.0  # observação mínima para opinar
    min_drop_pct: float = 15.0  # queda mínima sobre a mediana para chamar de oferta
    lowest_tolerance: float = 0.01  # "menor preço da janela" aceita 1% de folga
    original_tolerance: float = 0.05  # o preço "de" conta como praticado se algum preço chegou a 95% dele
    inflated_ratio: float = 1.3  # "de" acima de 130% da referência é suspeito


@dataclass
class Verdict:
    status: str
    score: int = 0
    reference_price: Decimal | None = None
    real_discount_pct: int | None = None
    claimed_discount_pct: int | None = None
    observed_days: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_offer(self) -> bool:
        return self.status == OFFER


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped)


def _contains(text: str, words: Sequence[str]) -> str | None:
    for word in words:
        # Palavra inteira (com plural opcional): "gi" não casa com "original", nem "copo" com "scopo".
        if re.search(rf"(?<![a-z0-9]){re.escape(normalize(word))}s?(?![a-z0-9])", text):
            return word
    return None


def check_niche(title: str, domain_id: str | None, rules: NicheRules = BJJ_RULES) -> str | None:
    """Devolve o motivo da exclusão, ou None se o item pertence ao nicho."""
    if domain_id and domain_id in rules.excluded_domains:
        return f"categoria fora do nicho ({domain_id})"
    text = normalize(title)
    word = _contains(text, rules.exclude)
    if word:
        return f"palavra de exclusão: {word!r}"
    if domain_id and domain_id in rules.allowed_domains:
        return None
    if _contains(text, rules.include) is None:
        return "nenhuma palavra do nicho no título"
    return None


def _segments(history: Sequence[PricePoint], start: datetime, end: datetime) -> list[tuple[Decimal, float]]:
    """(preço, segundos) dentro de [start, end]: cada preço vale até a próxima mudança."""
    points = sorted(history, key=lambda p: p.at)
    segments: list[tuple[Decimal, float]] = []
    for i, point in enumerate(points):
        seg_start = max(point.at, start)
        seg_end = min(points[i + 1].at if i + 1 < len(points) else end, end)
        if seg_end > seg_start:
            segments.append((point.price, (seg_end - seg_start).total_seconds()))
    return segments


def time_weighted_median(segments: Sequence[tuple[Decimal, float]]) -> Decimal | None:
    total = sum(seconds for _, seconds in segments)
    if total <= 0:
        return None
    accumulated = 0.0
    for price, seconds in sorted(segments):
        accumulated += seconds
        if accumulated >= total / 2:
            return price
    return None


def _pct(numerator: Decimal, denominator: Decimal) -> int:
    return int((numerator / denominator) * 100)


def assess(
    history: Sequence[PricePoint],
    observed_until: datetime,
    claimed_original: Decimal | None = None,
    *,
    title: str = "",
    domain_id: str | None = None,
    rules: NicheRules | None = BJJ_RULES,
    config: ScoringConfig = ScoringConfig(),
) -> Verdict:
    """`history`: mudanças de preço em ordem; o último ponto é o preço atual, válido até `observed_until`."""
    if rules is not None:
        reason = check_niche(title, domain_id, rules)
        if reason:
            return Verdict(IRRELEVANT, reasons=[reason])
    if not history:
        return Verdict(LEARNING, reasons=["sem histórico de preço"])

    current = max(history, key=lambda p: p.at).price
    claimed = _pct(claimed_original - current, claimed_original) if claimed_original and claimed_original > current else None

    first_seen = min(p.at for p in history)
    window_start = max(observed_until - timedelta(days=config.window_days), first_seen)
    observed_days = (observed_until - window_start).total_seconds() / 86400
    verdict = Verdict(NO_OFFER, claimed_discount_pct=claimed, observed_days=round(observed_days, 1))

    if observed_days < config.min_history_days:
        verdict.status = LEARNING
        verdict.reasons.append(f"só {observed_days:.1f} dias de histórico (mínimo {config.min_history_days:g})")
        if claimed:
            verdict.reasons.append(f"desconto de {claimed}% informado pelo vendedor, ainda não verificado")
        return verdict

    segments = _segments(history, window_start, observed_until)
    reference = time_weighted_median(segments)
    if reference is None:
        verdict.status = LEARNING
        verdict.reasons.append("histórico sem duração mensurável")
        return verdict

    verdict.reference_price = reference
    drop = _pct(reference - current, reference) if current < reference else 0
    verdict.real_discount_pct = drop
    prices = [price for price, _ in segments]
    lowest = current <= min(prices) * (1 + Decimal(str(config.lowest_tolerance)))

    original_practiced = None
    if claimed_original:
        original_practiced = max(prices) >= claimed_original * (1 - Decimal(str(config.original_tolerance)))
        if not original_practiced and claimed_original > reference * Decimal(str(config.inflated_ratio)):
            verdict.reasons.append(
                f"preço 'de' R$ {claimed_original} nunca foi praticado (mediana R$ {reference}): desconto inflado"
            )

    if drop >= config.min_drop_pct and lowest:
        verdict.status = OFFER
        verdict.reasons.append(f"{drop}% abaixo da mediana de {config.window_days} dias (R$ {reference}) e no menor preço do período")
    elif drop >= config.min_drop_pct:
        verdict.reasons.append(f"{drop}% abaixo da mediana, mas já esteve mais barato no período")
    else:
        verdict.reasons.append(f"preço dentro do normal (queda de {drop}% sobre a mediana R$ {reference})")

    verdict.score = _score(verdict, lowest, original_practiced, observed_days, config)
    return verdict


def _score(verdict: Verdict, lowest: bool, original_practiced: bool | None, observed_days: float, config: ScoringConfig) -> int:
    """0-100: queda real sobre a mediana (até 70, satura em 40%), menor preço do período (15),
    "de" confirmado pelo histórico (10), profundidade do histórico (5). "De" inflado tira 20."""
    score = min((verdict.real_discount_pct or 0) / 40, 1) * 70
    score += 15 if lowest else 0
    score += 10 if original_practiced else 0
    score += min(observed_days / config.window_days, 1) * 5
    if original_practiced is False:
        score -= 20
    return max(0, min(100, round(score)))
