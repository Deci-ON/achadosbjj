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
) -> str:
    """Texto pronto para colar no WhatsApp (negrito com *...*).

    O preço "de" informado pelo vendedor não entra: ele pode ser inflado. A comparação é com o preço
    normal medido no nosso histórico (mediana de `window_days` dias), e só aparece quando é oferta de verdade.
    """
    name = clean_title(title)
    if not (is_offer and reference_price and real_discount_pct):
        return f"*{name}*\n\n💰 {brl(price)}\n\n👉 {link}"
    return (
        f"🔥 *{name}*\n\n"
        f"💰 *{brl(price)}*\n"
        f"📉 {real_discount_pct}% abaixo do preço normal ({brl(reference_price)}, mediana de {window_days} dias)\n\n"
        f"👉 {link}"
    )
