from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from achados.coupons import CouponBook
from achados.messages import coupon_line, format_offer_message
from achados.models import Coupon, Product, ProductCoupon
from tests.test_sender import FakeClient, make_sender_app, ready_offer
from tests.test_service import AUTH, add_product, make_app

BRT = timezone(timedelta(hours=-3))


def soon(days=10, *, hour=20) -> datetime:
    """Daqui a `days` dias às `hour`h de Brasília (relativo ao relógio)."""
    return (datetime.now(BRT) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def until(moment: datetime) -> str:
    return f"até {moment.astimezone(BRT):%d/%m}"


def fake(kind="code", title="BJJ10", rule="", expires_at=None, store="all", is_default=False, id=1):
    return SimpleNamespace(id=id, kind=kind, title=title, rule=rule, expires_at=expires_at or soon(), store=store, is_default=is_default)


def body(**changes):
    return {"store": "mercadolivre", "kind": "code", "title": "BJJ10", "expires_at": soon().isoformat(), **changes}


# --- a linha ----------------------------------------------------------------------


def test_linha_de_codigo_com_regra_e_validade():
    when = soon()

    assert coupon_line(fake(rule="compra mínima R$ 100, até R$ 30 off", expires_at=when), "mercadolivre") == (
        f"🎟️ Cupom: BJJ10 · compra mínima R$ 100, até R$ 30 off · {until(when)}"
    )
    assert coupon_line(fake(expires_at=when), "shopee") == f"🎟️ Cupom: BJJ10 · {until(when)}"  # sem regra, sem separador sobrando


def test_linha_de_cupom_de_pagina_diz_onde_ativar_conforme_a_loja():
    when = soon()
    coupon = fake(kind="page", title="Semana do Judô", rule="10% off", expires_at=when)

    assert coupon_line(coupon, "mercadolivre") == f"🎟️ Ative o cupom Semana do Judô em mercadolivre.com.br/cupons · 10% off · {until(when)}"
    assert coupon_line(coupon, "shopee") == f"🎟️ Ative o cupom Semana do Judô na aba Cupons do app · 10% off · {until(when)}"


def test_linha_de_frete_gratis():
    when = soon()

    assert coupon_line(fake(kind="shipping", title="acima de R$ 79", expires_at=when), "shopee") == f"🚚 Frete grátis: acima de R$ 79 · {until(when)}"


def test_validade_e_o_dia_em_brasilia_nao_o_dia_utc():
    # 01:30 UTC ainda é a noite do dia anterior em Brasília
    utc = (datetime.now(timezone.utc) + timedelta(days=10)).replace(hour=1, minute=30, second=0, microsecond=0)
    expected = (utc - timedelta(hours=3)).strftime("%d/%m")

    assert coupon_line(fake(expires_at=utc), "shopee").endswith(f"até {expected}")
    # sem fuso (o SQLite devolve assim) vale UTC
    assert coupon_line(fake(expires_at=utc.replace(tzinfo=None)), "shopee").endswith(f"até {expected}")
    # 23:59 de Brasília é o mesmo dia, mesmo já sendo o dia seguinte em UTC
    late = soon(hour=23).replace(minute=59)
    assert coupon_line(fake(expires_at=late), "shopee").endswith(f"até {late:%d/%m}")


def test_linha_sem_formatacao_do_whatsapp():
    line = coupon_line(fake(title="*BJJ_10~*", rule="até _R$ 30_ off"), "shopee")

    assert "*" not in line and "_" not in line and "~" not in line
    assert line.startswith("🎟️ Cupom: BJJ10 · até R$ 30 off · ")


def test_a_linha_fica_antes_do_link_nos_dois_formatos():
    lines = ["🎟️ Cupom: A", "🚚 Frete grátis: B"]
    plain = format_offer_message(title="Kimono", price=300, link="https://l/1", coupon_lines=lines)
    offer = format_offer_message(
        title="Kimono", price=300, link="https://l/1", is_offer=True, reference_price=500, real_discount_pct=40, coupon_lines=lines
    )

    for text in (plain, offer):
        assert text.endswith("🎟️ Cupom: A\n🚚 Frete grátis: B\n\n👉 https://l/1")
        assert text.index("🎟️ Cupom: A") < text.index("🚚 Frete grátis: B") < text.index("👉")
        assert text.index("💰") < text.index("🎟️")


def test_sem_cupom_a_mensagem_nao_muda():
    kwargs = dict(title="Kimono", price=300, link="https://l/1", is_offer=True, reference_price=500, real_discount_pct=40)

    assert format_offer_message(**kwargs) == format_offer_message(**kwargs, coupon_lines=[]) == format_offer_message(**kwargs, coupon_lines=[""])
    assert format_offer_message(title="K", price=1, link="L") == "*K*\n\n💰 R$ 1,00\n\n👉 L"


# --- quais cupons entram ----------------------------------------------------------


def test_livro_loja_certa_ou_all_padrao_ou_anexado_sem_repetir():
    ml_default = fake(id=1, store="mercadolivre", is_default=True)
    all_default = fake(id=2, store="all", is_default=True)
    shopee_default = fake(id=3, store="shopee", is_default=True)
    ml_plain = fake(id=4, store="mercadolivre")  # nem padrão nem anexado: fora
    ml_attached = fake(id=5, store="mercadolivre")
    shopee_attached = fake(id=6, store="shopee")  # anexado, mas de outra loja: fora
    both = fake(id=7, store="all", is_default=True)  # padrão E anexado: uma vez só, entre os anexados
    book = CouponBook(
        [ml_default, all_default, shopee_default, ml_plain, ml_attached, shopee_attached, both],
        {10: [5, 6, 7]},
    )

    assert [c.id for c in book.for_offer("mercadolivre", 10)] == [5, 7, 1, 2]  # anexados primeiro
    assert [c.id for c in book.for_offer("mercadolivre", 11)] == [1, 2, 7]  # outra oferta: só os padrões
    assert [c.id for c in book.for_offer("shopee", 11)] == [2, 3, 7]


# --- API: cadastro ----------------------------------------------------------------


def test_exige_token(sessions):
    with TestClient(make_app(sessions)) as client:
        assert client.get("/api/coupons").status_code == 401
        assert client.post("/api/coupons", json=body()).status_code == 401
        assert client.put("/api/coupons/1", json={}).status_code == 401
        assert client.delete("/api/coupons/1").status_code == 401
        assert client.put("/api/products/1/coupons", json={"coupon_ids": []}).status_code == 401


def test_cria_lista_ordenada_edita_e_apaga(sessions):
    with TestClient(make_app(sessions)) as client:
        later = client.post("/api/coupons", json=body(title="Depois", expires_at=soon(20).isoformat()), headers=AUTH)
        sooner = client.post(
            "/api/coupons", json=body(title="Antes", store="all", kind="shipping", rule="acima de R$ 79", expires_at=soon(5).isoformat(), is_default=True), headers=AUTH
        )
        listed = client.get("/api/coupons", headers=AUTH).json()
        edited = client.put(f"/api/coupons/{later.json()['id']}", json={"rule": "10% off", "is_default": True}, headers=AUTH)
        deleted = client.delete(f"/api/coupons/{sooner.json()['id']}", headers=AUTH)
        after = client.get("/api/coupons", headers=AUTH).json()

    assert later.status_code == sooner.status_code == 201
    assert [c["title"] for c in listed] == ["Antes", "Depois"]  # vence antes primeiro
    assert set(listed[0]) == {"id", "store", "kind", "title", "rule", "expires_at", "is_default", "active", "created_at"}
    assert listed[0]["active"] is True and listed[0]["is_default"] is True and listed[0]["rule"] == "acima de R$ 79"
    assert listed[1]["is_default"] is False and listed[1]["rule"] == ""  # padrões do corpo
    assert edited.status_code == 200
    assert edited.json()["title"] == "Depois" and edited.json()["rule"] == "10% off" and edited.json()["is_default"] is True  # o resto fica
    assert edited.json()["store"] == "mercadolivre" and edited.json()["expires_at"] == soon(20).isoformat()
    assert deleted.json() == {"deleted": sooner.json()["id"]}
    assert [c["title"] for c in after] == ["Depois"]


def test_data_volta_em_iso_com_o_fuso_de_brasilia(sessions):
    when = soon(7)
    with TestClient(make_app(sessions)) as client:
        created = client.post("/api/coupons", json=body(expires_at=when.astimezone(timezone.utc).isoformat()), headers=AUTH).json()
        listed = client.get("/api/coupons", headers=AUTH).json()[0]

    for out in (created, listed):
        assert datetime.fromisoformat(out["expires_at"]) == when
        assert out["expires_at"].endswith("-03:00")
        assert datetime.fromisoformat(out["created_at"]).tzinfo is not None


def test_vencidos_aparecem_depois_dos_ativos_e_inativos(sessions, db):
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            Coupon(store="all", kind="code", title="velho", expires_at=now - timedelta(days=5)),
            Coupon(store="all", kind="code", title="ontem", expires_at=now - timedelta(days=1)),
            Coupon(store="all", kind="code", title="vivo", expires_at=now + timedelta(days=1)),
        ]
    )
    db.commit()

    with TestClient(make_app(sessions)) as client:
        listed = client.get("/api/coupons", headers=AUTH).json()

    assert [(c["title"], c["active"]) for c in listed] == [("vivo", True), ("ontem", False), ("velho", False)]


@pytest.mark.parametrize(
    "changes",
    [
        {"store": "amazon"},
        {"kind": "link"},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 81},
        {"expires_at": (datetime.now(BRT) - timedelta(minutes=1)).isoformat()},
        {"expires_at": "ontem"},
    ],
)
def test_criar_valida(sessions, changes):
    with TestClient(make_app(sessions)) as client:
        response = client.post("/api/coupons", json=body(**changes), headers=AUTH)
        assert client.get("/api/coupons", headers=AUTH).json() == []

    assert response.status_code == 422


def test_criar_sem_campo_obrigatorio_e_422(sessions):
    with TestClient(make_app(sessions)) as client:
        for missing in ("store", "kind", "title", "expires_at"):
            payload = body()
            del payload[missing]
            assert client.post("/api/coupons", json=payload, headers=AUTH).status_code == 422, missing


def test_data_sem_fuso_vale_brasilia(sessions):
    naive = soon(3).replace(tzinfo=None)
    with TestClient(make_app(sessions)) as client:
        created = client.post("/api/coupons", json=body(expires_at=naive.isoformat()), headers=AUTH).json()

    assert datetime.fromisoformat(created["expires_at"]) == naive.replace(tzinfo=BRT)


def test_editar_valida_e_404(sessions):
    with TestClient(make_app(sessions)) as client:
        coupon_id = client.post("/api/coupons", json=body(), headers=AUTH).json()["id"]
        url = f"/api/coupons/{coupon_id}"
        statuses = [
            client.put(url, json={"store": "amazon"}, headers=AUTH).status_code,
            client.put(url, json={"kind": "x"}, headers=AUTH).status_code,
            client.put(url, json={"title": " "}, headers=AUTH).status_code,
            client.put(url, json={"expires_at": (datetime.now(BRT) - timedelta(hours=1)).isoformat()}, headers=AUTH).status_code,
        ]
        missing = client.put("/api/coupons/999", json={"title": "x"}, headers=AUTH)
        unchanged = client.get("/api/coupons", headers=AUTH).json()[0]
        empty = client.put(url, json={}, headers=AUTH)

    assert statuses == [422, 422, 422, 422]
    assert missing.status_code == 404
    assert unchanged["title"] == "BJJ10" and unchanged["store"] == "mercadolivre"
    assert empty.status_code == 200 and empty.json()["title"] == "BJJ10"


def test_apagar_inexistente_e_404(sessions):
    with TestClient(make_app(sessions)) as client:
        assert client.delete("/api/coupons/999", headers=AUTH).status_code == 404


# --- API: anexar a uma oferta -----------------------------------------------------


def test_anexar_substitui_e_valida(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    product = db.scalars(select(Product)).one()

    with TestClient(make_app(sessions)) as client:
        ids = [client.post("/api/coupons", json=body(title=t), headers=AUTH).json()["id"] for t in ("A", "B", "C")]
        url = f"/api/products/{product.id}/coupons"
        first = client.put(url, json={"coupon_ids": [ids[0], ids[1], ids[0]]}, headers=AUTH)  # repetido conta uma vez
        second = client.put(url, json={"coupon_ids": [ids[2], ids[1]]}, headers=AUTH)
        unknown = client.put(url, json={"coupon_ids": [ids[0], 999]}, headers=AUTH)
        no_product = client.put("/api/products/999/coupons", json={"coupon_ids": []}, headers=AUTH)
        kept = client.get(f"/api/products/{product.id}", headers=AUTH).json()["attached_coupon_ids"]
        cleared = client.put(url, json={"coupon_ids": []}, headers=AUTH)

    assert first.json() == {"coupon_ids": [ids[0], ids[1]]}
    assert second.json() == {"coupon_ids": [ids[1], ids[2]]}
    assert unknown.status_code == 422 and no_product.status_code == 404
    assert sorted(kept) == [ids[1], ids[2]]  # o 422 não mexeu em nada
    assert cleared.json() == {"coupon_ids": []}
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ProductCoupon)) == 0


def test_anexo_some_com_o_produto_e_com_o_cupom(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    product = db.scalars(select(Product)).one()
    coupons = [Coupon(store="all", kind="code", title=t, expires_at=soon()) for t in ("A", "B")]
    db.add_all(coupons)
    db.commit()
    db.add_all([ProductCoupon(product_id=product.id, coupon_id=c.id) for c in coupons])
    db.commit()

    with TestClient(make_app(sessions)) as client:
        client.delete(f"/api/coupons/{coupons[0].id}", headers=AUTH)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ProductCoupon)) == 1  # saiu o do cupom apagado

    db.delete(db.get(Product, product.id))
    db.commit()

    assert db.scalar(select(func.count()).select_from(ProductCoupon)) == 0  # e o do produto apagado
    assert db.scalar(select(func.count()).select_from(Coupon)) == 1  # o cupom continua na biblioteca


# --- API: na oferta e na mensagem -------------------------------------------------


def offer_of(client, product_id):
    return client.get(f"/api/products/{product_id}", headers=AUTH).json()


def test_oferta_traz_so_cupom_ativo_da_loja_certa_padrao_ou_anexado(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")  # mercadolivre
    product = db.scalars(select(Product)).one()
    now = datetime.now(timezone.utc)
    rows = {
        "padrao_ml": Coupon(store="mercadolivre", kind="code", title="PADRAO", expires_at=now + timedelta(days=9), is_default=True),
        "padrao_all": Coupon(store="all", kind="shipping", title="acima de R$ 99", expires_at=now + timedelta(days=8), is_default=True),
        "anexado": Coupon(store="mercadolivre", kind="page", title="PAGINA", expires_at=now + timedelta(days=12)),
        "avulso": Coupon(store="mercadolivre", kind="code", title="AVULSO", expires_at=now + timedelta(days=5)),  # nem padrão nem anexado
        "vencido": Coupon(store="mercadolivre", kind="code", title="VENCIDO", expires_at=now - timedelta(minutes=1), is_default=True),
        "shopee": Coupon(store="shopee", kind="code", title="SHOPEEPADRAO", expires_at=now + timedelta(days=5), is_default=True),
        "dois": Coupon(store="all", kind="code", title="DOIS", expires_at=now + timedelta(days=7), is_default=True),  # padrão E anexado
    }
    db.add_all(rows.values())
    db.commit()
    db.add_all([ProductCoupon(product_id=product.id, coupon_id=rows[k].id) for k in ("anexado", "dois", "vencido")])
    db.commit()

    with TestClient(make_app(sessions)) as client:
        detail = offer_of(client, product.id)
        listed = client.get("/api/offers", headers=AUTH).json()["items"][0]

    for offer in (detail, listed):
        # anexados primeiro (o que vence antes na frente), depois os padrões
        assert [c["title"] for c in offer["coupons"]] == ["DOIS", "PAGINA", "acima de R$ 99", "PADRAO"]
        assert {"id", "title", "kind"} == set(offer["coupons"][0])
        assert sorted(offer["attached_coupon_ids"]) == sorted(rows[k].id for k in ("anexado", "dois", "vencido"))  # inclui o vencido
        message = offer["message"]
        for gone in ("AVULSO", "VENCIDO", "SHOPEEPADRAO"):
            assert gone not in message
        assert message.count("DOIS") == 1
        assert "mercadolivre.com.br/cupons" in message
        assert message.index("DOIS") < message.index("PAGINA") < message.index("👉")  # anexados primeiro, linhas antes do link
        assert message.endswith("👉 https://exemplo/product:1")


def test_oferta_da_shopee_so_recebe_cupom_da_shopee_ou_all(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    product = db.scalars(select(Product)).one()
    product.source = "shopee"
    db.add_all(
        [
            Coupon(store="mercadolivre", kind="code", title="SOML", expires_at=soon(), is_default=True),
            Coupon(store="shopee", kind="page", title="SHOP", expires_at=soon(), is_default=True),
            Coupon(store="all", kind="page", title="TODAS", expires_at=soon(), is_default=True),
        ]
    )
    db.commit()

    with TestClient(make_app(sessions)) as client:
        offer = offer_of(client, product.id)

    assert sorted(c["title"] for c in offer["coupons"]) == ["SHOP", "TODAS"]
    assert "SOML" not in offer["message"]
    assert offer["message"].count("na aba Cupons do app") == 2  # o "all" também diz onde ativar na loja da oferta
    assert "mercadolivre.com.br/cupons" not in offer["message"]


def test_sem_cupom_os_campos_novos_vem_vazios(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")

    with TestClient(make_app(sessions)) as client:
        offer = client.get("/api/offers", headers=AUTH).json()["items"][0]

    assert offer["coupons"] == [] and offer["attached_coupon_ids"] == []
    assert "🎟️" not in offer["message"] and "🚚" not in offer["message"]


def test_cupom_nao_mexe_no_preco_nem_na_pontuacao(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")

    with TestClient(make_app(sessions)) as client:
        before = client.get("/api/offers", headers=AUTH).json()["items"][0]
        client.post("/api/coupons", json=body(store="all", is_default=True, rule="R$ 100 off"), headers=AUTH)
        after = client.get("/api/offers", headers=AUTH).json()["items"][0]

    assert after["coupons"] and not before["coupons"]
    for field in ("price", "score", "status", "reference_price", "real_discount_pct", "reasons", "ready"):
        assert after[field] == before[field], field


def test_cupom_vencido_deixa_de_aparecer_sem_apagar(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    coupon = Coupon(store="all", kind="code", title="CURTO", expires_at=datetime.now(timezone.utc) + timedelta(days=1), is_default=True)
    db.add(coupon)
    db.commit()

    with TestClient(make_app(sessions)) as client:
        live = client.get("/api/offers", headers=AUTH).json()["items"][0]
        coupon.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        gone = client.get("/api/offers", headers=AUTH).json()["items"][0]
        listed = client.get("/api/coupons", headers=AUTH).json()

    assert "CURTO" in live["message"] and "CURTO" not in gone["message"] and gone["coupons"] == []
    assert [(c["title"], c["active"]) for c in listed] == [("CURTO", False)]


def test_o_texto_enfileirado_leva_a_linha_do_cupom(sessions, db):
    product = ready_offer(db)  # mercadolivre, com link de afiliado
    when = soon(6)

    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        client.put("/api/sender/groups", json={"groups": [{"id": "g1@g.us", "name": "Grupo 1"}]}, headers=AUTH)
        coupon = client.post("/api/coupons", json=body(title="BJJ10", rule="compra mínima R$ 100", expires_at=when.isoformat()), headers=AUTH).json()
        client.put(f"/api/products/{product.id}/coupons", json={"coupon_ids": [coupon["id"]]}, headers=AUTH)
        queued = client.post(f"/api/products/{product.id}/send", headers=AUTH)
        job = client.get("/api/sender/jobs", headers=AUTH).json()[0]

    assert queued.json() == {"queued": 1}
    line = f"🎟️ Cupom: BJJ10 · compra mínima R$ 100 · {until(when)}"
    assert line in job["text"]
    assert job["text"].index(line) < job["text"].index("👉 https://meli.la/AAA")
    assert job["text"].endswith("👉 https://meli.la/AAA")
