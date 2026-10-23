from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from achados.models import PriceSnapshot, Product
from achados.offers import assess_products
from achados.scoring import IRRELEVANT, LEARNING, NO_OFFER, OFFER

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def add_product(db, external_id, title, changes, *, original=None, seen_days_ago=0.0, domain=None, source="mercadolivre"):
    """changes: (dias atrás, preço) em que o preço mudou; o último vale até last_seen_at."""
    product = Product(
        source=source,
        external_id=external_id,
        title=title,
        url=f"https://exemplo/{external_id}",
        price=Decimal(changes[-1][1]),
        original_price=Decimal(original) if original else None,
        raw={"domain_id": domain} if domain else {},
        first_seen_at=NOW - timedelta(days=changes[0][0]),
        last_seen_at=NOW - timedelta(days=seen_days_ago),
    )
    product.snapshots = [PriceSnapshot(price=Decimal(price), collected_at=NOW - timedelta(days=days)) for days, price in changes]
    db.add(product)
    db.commit()
    return product


def by_id(results):
    return {product.external_id: verdict for product, verdict in results}


def test_classifica_oferta_normal_aprendizado_e_fora_do_nicho(db):
    add_product(db, "oferta", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500", seen_days_ago=0.1)
    add_product(db, "normal", "Kimono Jiu Jitsu Atama", [(40, "500")], original="900", seen_days_ago=0.1)
    add_product(db, "novo", "Rash Guard Vulkan No-Gi", [(1, "100")], original="200", seen_days_ago=0.1)
    add_product(db, "caneca", "Caneca Kimono Jiu Jitsu", [(40, "40"), (3, "20")], seen_days_ago=0.1)

    verdicts = by_id(assess_products(db, now=NOW))

    assert verdicts["oferta"].status == OFFER and verdicts["oferta"].real_discount_pct == 30
    assert verdicts["normal"].status == NO_OFFER  # "de R$ 900" nunca foi praticado
    assert verdicts["novo"].status == LEARNING
    assert verdicts["caneca"].status == IRRELEVANT


def test_ordena_do_maior_para_o_menor_score(db):
    add_product(db, "pequena", "Kimono Jiu Jitsu A", [(40, "100"), (3, "80")], seen_days_ago=0.1)
    add_product(db, "grande", "Kimono Jiu Jitsu B", [(40, "100"), (3, "60")], seen_days_ago=0.1)

    results = assess_products(db, now=NOW)

    assert [p.external_id for p, _ in results] == ["grande", "pequena"]


def test_produto_nao_visto_recentemente_fica_de_fora(db):
    add_product(db, "velho", "Kimono Jiu Jitsu Antigo", [(40, "100"), (10, "60")], seen_days_ago=9)
    add_product(db, "atual", "Kimono Jiu Jitsu Atual", [(40, "100"), (3, "60")], seen_days_ago=0.5)

    assert [p.external_id for p, _ in assess_products(db, now=NOW)] == ["atual"]
    assert len(assess_products(db, now=NOW, max_age_days=30)) == 2


def test_observacao_termina_em_last_seen_e_nao_em_agora(db):
    # Visto pela última vez há 2 dias, com o preço que tinha caído há 4: histórico até lá.
    add_product(db, "p", "Kimono Jiu Jitsu", [(40, "100"), (6, "70")], seen_days_ago=2)

    verdict = by_id(assess_products(db, now=NOW))["p"]

    assert verdict.status == OFFER
    assert verdict.observed_days == pytest.approx(30, abs=0.1)


def test_filtra_por_loja_e_aceita_dominio_do_nicho(db):
    add_product(db, "ml", "Faixa Jiu Jitsu Azul 3,10m", [(40, "100"), (3, "70")], domain="MLB-MARTIAL_ARTS_BELTS", seen_days_ago=0.1)
    add_product(db, "sp", "Kimono Jiu Jitsu Shopee", [(40, "100"), (3, "70")], source="shopee", seen_days_ago=0.1)

    assert [p.external_id for p, _ in assess_products(db, now=NOW, source="mercadolivre")] == ["ml"]
    assert by_id(assess_products(db, now=NOW, source="mercadolivre"))["ml"].status == OFFER
    assert by_id(assess_products(db, now=NOW))["sp"].status == OFFER


def test_sem_produtos(db):
    assert assess_products(db, now=NOW) == []
