import re
from decimal import ROUND_HALF_UP, Decimal

MAX_TITLE = 100


def brl(value: Decimal | float | int | str) -> str:
    """1289.9 -> 'R$ 1.289,90'."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, cents = f"{amount:.2f}".split(".")
    groups = []
    while len(whole) > 3:
        whole, tail = whole[:-3], whole[-3:]
        groups.insert(0, tail)
    groups.insert(0, whole)
    return f"R$ {'.'.join(groups)},{cents}"


def clean_title(title: str) -> str:
    # Asteriscos, sublinhados e til são formatação do WhatsApp: no meio do título quebrariam o negrito.
    title = re.sub(r"[*_~]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1].rstrip() + "…"


def format_offer_message(
    *,
    title: str,
    price: Decimal | float,
    link: str,
    is_offer: bool = False,
    reference_price: Decimal | float | None = None,
    real_discount_pct: int | None = None,
    window_days: int = 30,
    sizes: list[tuple[str, Decimal | float]] | None = None,
) -> str:
    """Texto pronto para colar no WhatsApp (negrito com *...*).

    O preço "de" informado pelo vendedor não entra: ele pode ser inflado. A comparação é com o preço
    normal medido no nosso histórico (mediana de `window_days` dias), e só aparece quando é oferta de verdade.

    `sizes`: (tamanho, preço) dos tamanhos em oferta do mesmo modelo. Com mais de um, a mensagem lista os
    tamanhos; se os preços diferem, mostra "a partir de" e o preço de cada tamanho, e o desconto vira "até".
    """
    name = clean_title(title)
    if not (is_offer and reference_price and real_discount_pct):
        return f"*{name}*\n\n💰 {brl(price)}\n\n👉 {link}"

    sizes = sizes or []
    prices = {Decimal(str(p)) for _, p in sizes}
    varied = len(sizes) > 1 and len(prices) > 1
    headline = f"💰 *a partir de {brl(min(prices))}*" if varied else f"💰 *{brl(price)}*"
    drop = (
        f"📉 até {real_discount_pct}% abaixo do preço normal"
        if varied
        else f"📉 {real_discount_pct}% abaixo do preço normal ({brl(reference_price)}, mediana de {window_days} dias)"
    )
    if len(sizes) > 1:
        listing = (
            " · ".join(f"{size} {brl(p)}" for size, p in sizes) if varied else ", ".join(size for size, _ in sizes)
        )
        size_line = f"📏 Tamanhos: {listing}\n"
    else:
        size_line = ""
    return f"🔥 *{name}*\n\n{headline}\n{drop}\n{size_line}\n👉 {link}"
