import re
from datetime import timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

MAX_TITLE = 100
BRT = timezone(timedelta(hours=-3))  # o Brasil não tem horário de verão desde 2019


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


def _clean(text: str, limit: int) -> str:
    # Asteriscos, sublinhados e til são formatação do WhatsApp: no meio do texto quebrariam o negrito.
    text = re.sub(r"[*_~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean_title(title: str) -> str:
    return _clean(title, MAX_TITLE)


# Onde ativar um cupom de página, por loja.
_ACTIVATE_AT = {"mercadolivre": "em mercadolivre.com.br/cupons", "shopee": "na aba Cupons do app"}


def coupon_line(coupon, store: str) -> str:
    """Linha de cupom da mensagem, sempre com a validade no fim ("até DD/MM", horário de Brasília).

    `coupon` tem kind, title, rule e expires_at; `store` é a loja da oferta (diz onde ativar um cupom "all").
    Devolve "" se o título ficar vazio depois de limpo."""
    title = _clean(coupon.title, 80)
    if not title:
        return ""
    rule = _clean(coupon.rule or "", 120)
    expires = coupon.expires_at if coupon.expires_at.tzinfo else coupon.expires_at.replace(tzinfo=timezone.utc)
    until = f"até {expires.astimezone(BRT):%d/%m}"
    if coupon.kind == "shipping":
        head = f"🚚 Frete grátis: {title}"
    elif coupon.kind == "page":
        where = _ACTIVATE_AT.get(store, "")
        head = f"🎟️ Ative o cupom {title}" + (f" {where}" if where else "")
    else:
        head = f"🎟️ Cupom: {title}"
    return " · ".join(part for part in (head, rule, until) if part)


def _with_label(ad_label: str, text: str) -> str:
    return f"{ad_label.strip()}\n\n{text}" if ad_label.strip() else text


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
    ad_label: str = "",
    coupon_lines: list[str] | None = None,
) -> str:
    """Texto pronto para colar no WhatsApp (negrito com *...*).

    `ad_label`: identificação de publicidade, na primeira linha (o guia do CONAR pede que apareça em primeiro
    plano, e os termos do Mercado Afiliados exigem). Vazio = sem a linha.

    O preço "de" informado pelo vendedor não entra: ele pode ser inflado. A comparação é com o preço
    normal medido no nosso histórico (mediana de `window_days` dias), e só aparece quando é oferta de verdade.

    `sizes`: (tamanho, preço) dos tamanhos em oferta do mesmo modelo. Com mais de um, a mensagem lista os
    tamanhos; se os preços diferem, mostra "a partir de" e o preço de cada tamanho, e o desconto vira "até".

    `coupon_lines`: linhas de `coupon_line`, uma por cupom, logo antes da linha do link (nos dois formatos).
    """
    name = clean_title(title)
    coupons = "\n".join(line for line in (coupon_lines or []) if line)
    block = f"{coupons}\n\n" if coupons else ""
    if not (is_offer and reference_price and real_discount_pct):
        return _with_label(ad_label, f"*{name}*\n\n💰 {brl(price)}\n\n{block}👉 {link}")

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
    return _with_label(ad_label, f"🔥 *{name}*\n\n{headline}\n{drop}\n{size_line}\n{block}👉 {link}")
