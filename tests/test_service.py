import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from achados.config import Settings
from achados.models import CollectRun, PriceSnapshot, Product
from achados.scheduler import CollectScheduler
from achados.schemas import Offer
from achados.service import create_app

TOKEN = "segredo-de-teste"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class FakeLock:
    def __init__(self, leader=True):
        self.leader = leader

    def acquire(self):
        return self.leader

    def release(self):
        pass


class FakeCollector:
    source = "mercadolivre"

    def collect(self):
        yield Offer(
            source="mercadolivre", external_id="product:1", title="Kimono Jiu Jitsu Koral", url="https://x", price=Decimal("500")
        )


class BusyScheduler(CollectScheduler):
    busy = True


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, admin_token=TOKEN, **overrides)


def make_app(sessions, *, leader=True, enabled=True, cls=CollectScheduler, **overrides):
    scheduler = cls(
        sessions,
        FakeLock(leader),
        FakeCollector,
        "mercadolivre",
        interval=6 * 3600,
        startup_delay=3600,  # o laço não coleta sozinho durante o teste
        enabled=enabled,
    )
    return create_app(settings(**overrides), sessions=sessions, scheduler=scheduler)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def add_product(db, external_id, title, changes, *, original=None, domain=None):
    product = Product(
        source="mercadolivre",
        external_id=external_id,
        title=title,
        url=f"https://exemplo/{external_id}",
        price=Decimal(changes[-1][1]),
        original_price=Decimal(original) if original else None,
        raw={"domain_id": domain} if domain else {},
        first_seen_at=NOW - timedelta(days=changes[0][0]),
        last_seen_at=NOW,
    )
    product.snapshots = [PriceSnapshot(price=Decimal(p), collected_at=NOW - timedelta(days=d)) for d, p in changes]
    db.add(product)
    db.commit()


# --- saúde e autenticação -------------------------------------------------


def test_health_e_publico_e_confere_o_banco(sessions):
    with TestClient(make_app(sessions)) as client:
        response = client.get("/health")

    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_health_503_quando_o_banco_cai(sqlite_engine, sessions):
    class BrokenSessions:
        kw = {"bind": sqlite_engine}

        def __call__(self):
            raise RuntimeError("sem banco")

    scheduler = CollectScheduler(sessions, FakeLock(), FakeCollector, "mercadolivre", interval=3600, startup_delay=3600)
    app = create_app(settings(), sessions=BrokenSessions(), scheduler=scheduler)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503 and "RuntimeError" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/status", "/api/offers"])
def test_api_exige_token(sessions, path):
    with TestClient(make_app(sessions)) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer errado"}).status_code == 401
        assert client.get(path, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
        assert client.get(path, headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200  # esquema sem caixa


def test_post_collect_tambem_exige_token(sessions):
    with TestClient(make_app(sessions)) as client:
        assert client.post("/api/collect").status_code == 401


def test_sem_admin_token_configurado_a_api_fica_fechada(sessions):
    scheduler = CollectScheduler(sessions, FakeLock(), FakeCollector, "mercadolivre", interval=3600, startup_delay=3600)
    app = create_app(Settings(_env_file=None, admin_token=""), sessions=sessions, scheduler=scheduler)

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 503
        assert client.get("/api/status", headers=AUTH).status_code == 503  # mesmo "acertando" qualquer token
        assert client.get("/health").status_code == 200


def test_preset_desconhecido_falha_na_subida(sessions):
    with pytest.raises(ValueError, match="COLLECT_PRESET"):
        create_app(settings(collect_preset="xyz"), sessions=sessions)


def test_a_subida_cria_as_tabelas():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    sessions = sessionmaker(engine, expire_on_commit=False)
    assert inspect(engine).get_table_names() == []

    with TestClient(make_app(sessions)):
        pass

    assert {"products", "collect_runs", "price_snapshots"} <= set(inspect(engine).get_table_names())


# --- status ---------------------------------------------------------------


def test_status_mostra_agendador_produtos_e_ultimas_coletas(sessions, db):
    add_product(db, "product:1", "Kimono Jiu Jitsu", [(40, "100")])
    db.add(CollectRun(source="mercadolivre", status="ok", items_seen=360, items_new=360, finished_at=NOW))
    db.commit()

    with TestClient(make_app(sessions)) as client:
        assert wait_until(lambda: client.get("/api/status", headers=AUTH).json()["scheduler"]["is_leader"])
        body = client.get("/api/status", headers=AUTH).json()

    assert body["scheduler"]["enabled"] is True and body["scheduler"]["running"] is False
    assert body["scheduler"]["consecutive_failures"] == 0 and body["scheduler"]["last_error"] is None
    assert body["products"] == {"mercadolivre": 1}
    assert [(r["status"], r["items_seen"]) for r in body["recent_runs"]] == [("ok", 360)]


# --- ofertas --------------------------------------------------------------


def seed_offers(db):
    add_product(db, "product:oferta", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    add_product(db, "product:grande", "Kimono Jiu Jitsu Atama", [(40, "500"), (3, "250")])
    add_product(db, "product:normal", "Kimono Jiu Jitsu Keiko", [(40, "500")], original="900")
    add_product(db, "product:novo", "Faixa Jiu Jitsu Azul", [(1, "40")])
    add_product(db, "product:judo", "Judo Gi Infantil", [(40, "100"), (3, "50")])


OFFERS = {"max_age_days": 100000}


def test_offers_lista_so_ofertas_ordenadas_por_score_com_contagens(sessions, db):
    seed_offers(db)

    with TestClient(make_app(sessions)) as client:
        body = client.get("/api/offers", params=OFFERS, headers=AUTH).json()

    assert body["counts"] == {"offer": 2, "no_offer": 1, "learning": 1, "irrelevant": 1}
    assert [i["external_id"] for i in body["items"]] == ["product:grande", "product:oferta"]
    top = body["items"][0]
    assert top["status"] == "offer" and top["price"] == 250.0 and top["reference_price"] == 500.0
    assert top["real_discount_pct"] == 50 and top["score"] > body["items"][1]["score"]
    assert top["url"] == "https://exemplo/product:grande" and top["reasons"]


def test_offers_filtra_por_status_score_e_limite(sessions, db):
    seed_offers(db)

    with TestClient(make_app(sessions)) as client:
        get = lambda **p: client.get("/api/offers", params={**OFFERS, **p}, headers=AUTH).json()["items"]

        assert [i["external_id"] for i in get(status="learning")] == ["product:novo"]
        assert [i["external_id"] for i in get(status="irrelevant")] == ["product:judo"]
        assert [i["external_id"] for i in get(status="no_offer")] == ["product:normal"]
        assert len(get(limit=1)) == 1
        assert [i["external_id"] for i in get(min_score=85)] == ["product:grande"]
        assert get(min_score=100) == []


def test_offers_valida_os_parametros(sessions):
    with TestClient(make_app(sessions)) as client:
        for params in ({"status": "qualquer"}, {"limit": 0}, {"limit": 500}, {"min_score": 101}, {"source": "amazon"}):
            assert client.get("/api/offers", params=params, headers=AUTH).status_code == 422, params


def test_offers_sem_produtos_recentes(sessions, db):
    seed_offers(db)  # last_seen_at fixo em 2026-10-23: fora da janela padrão de 3 dias, hoje

    with TestClient(make_app(sessions)) as client:
        body = client.get("/api/offers", params={"max_age_days": 0.001}, headers=AUTH).json()

    assert body == {"counts": {"offer": 0, "no_offer": 0, "learning": 0, "irrelevant": 0}, "items": []}


# --- disparo manual -------------------------------------------------------


def test_post_collect_dispara_uma_coleta_em_segundo_plano(sessions, db):
    with TestClient(make_app(sessions)) as client:
        assert wait_until(lambda: client.get("/api/status", headers=AUTH).json()["scheduler"]["is_leader"])

        response = client.post("/api/collect", headers=AUTH)
        assert response.status_code == 202 and response.json() == {"status": "started"}

        assert wait_until(lambda: client.get("/api/status", headers=AUTH).json()["products"] == {"mercadolivre": 1})
        runs = client.get("/api/status", headers=AUTH).json()["recent_runs"]

    assert [(r["status"], r["items_new"]) for r in runs] == [("ok", 1)]


def test_post_collect_recusa_quando_o_agendador_esta_desligado(sessions):
    with TestClient(make_app(sessions, enabled=False)) as client:
        response = client.post("/api/collect", headers=AUTH)

    assert response.status_code == 409 and "desligado" in response.json()["detail"]


def test_post_collect_recusa_quando_nao_e_lider(sessions):
    with TestClient(make_app(sessions, leader=False)) as client:
        time.sleep(0.2)  # dá tempo de o laço tentar assumir e falhar
        response = client.post("/api/collect", headers=AUTH)

    assert response.status_code == 409 and "líder" in response.json()["detail"]


def test_post_collect_recusa_quando_ja_ha_coleta(sessions):
    with TestClient(make_app(sessions, cls=BusyScheduler)) as client:
        assert wait_until(lambda: client.get("/api/status", headers=AUTH).json()["scheduler"]["is_leader"])
        response = client.post("/api/collect", headers=AUTH)

    assert response.status_code == 409 and "andamento" in response.json()["detail"]
