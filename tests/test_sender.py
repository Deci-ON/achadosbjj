import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from achados.models import Product, SendJob
from achados.scheduler import CollectScheduler
from achados.sender import (
    AUTO_PAUSE_AFTER,
    SenderClient,
    SenderError,
    SenderPermanentError,
    SendWorker,
    cancel_job,
    day_start,
    enqueue_offer,
    in_window,
    is_paused,
    replace_groups,
    saved_groups,
    set_paused,
)
from achados.service import create_app
from achados.workflow import recent_sends
from tests.test_service import AUTH, FakeCollector, FakeLock, add_product, settings

NOON = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)  # 12:00 em Brasília


class FakeClient:
    def __init__(self, state="ready"):
        self.state = state
        self.sent: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def status(self):
        if isinstance(self.state, Exception):
            raise self.state
        return {"state": self.state, "qr": None}

    def groups(self):
        return [{"id": "g1@g.us", "name": "Grupo 1", "participants": 50}, {"id": "g2@g.us", "name": "Grupo 2", "participants": 30}]

    def send(self, chat_id, text, image_url=None):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text))


class FakeChecker:
    def __init__(self, price="300"):
        self.price = Decimal(price) if price is not None else None
        self.error: Exception | None = None

    def current_price(self, product):
        if self.error:
            raise self.error
        return self.price


def make_product(db, price="300") -> Product:
    product = Product(
        source="mercadolivre",
        external_id="product:1",
        title="Kimono Jiu Jitsu Koral",
        url="https://exemplo/1",
        price=Decimal(price),
        raw={"product_id": "MLB1"},
    )
    db.add(product)
    db.commit()
    return product


def make_worker(sessions, client, *, clock, checker=None, cap=30, window=(8, 22)):
    return SendWorker(
        sessions,
        FakeLock(),
        client,
        daily_cap=cap,
        min_delay=60,
        max_delay=60,
        window=window,
        checker=checker,
        clock=lambda: clock[0],
        rng=random.Random(1),
    )


def queue(db, product, groups=("g1@g.us",), text="oferta"):
    replace_groups(db, [(g, g) for g in groups])
    enqueue_offer(db, product, text, None)
    db.commit()


def jobs(db):
    db.expire_all()
    return list(db.scalars(select(SendJob).order_by(SendJob.id)))


# --- fila -----------------------------------------------------------------------


def test_enfileira_um_job_por_grupo_e_nao_duplica(db):
    product = make_product(db)
    replace_groups(db, [("g1@g.us", "Grupo 1"), ("g2@g.us", "Grupo 2")])

    assert enqueue_offer(db, product, "texto", None) == 2
    with pytest.raises(ValueError, match="já está na fila"):
        enqueue_offer(db, product, "texto", None)
    assert [(j.chat_id, j.status, j.price) for j in jobs(db)] == [("g1@g.us", "queued", Decimal("300")), ("g2@g.us", "queued", Decimal("300"))]


def test_sem_grupo_escolhido_nao_enfileira(db):
    with pytest.raises(ValueError, match="nenhum grupo"):
        enqueue_offer(db, make_product(db), "texto", None)


def test_tirar_um_grupo_da_lista_cancela_o_que_estava_na_fila(db):
    product = make_product(db)
    queue(db, product, groups=("g1@g.us", "g2@g.us"))

    replace_groups(db, [("g1@g.us", "Grupo 1")])
    db.commit()

    assert [(j.chat_id, j.status) for j in jobs(db)] == [("g1@g.us", "queued"), ("g2@g.us", "cancelled")]
    assert [g.chat_id for g in saved_groups(db)] == ["g1@g.us"]


def test_cancelar_job_so_vale_para_o_que_esta_na_fila(db):
    queue(db, make_product(db))
    job = jobs(db)[0]

    assert cancel_job(db, job.id) is True
    assert cancel_job(db, job.id) is False
    assert cancel_job(db, 999) is False


def test_janela_de_horario_e_dia_sao_de_brasilia():
    assert in_window(NOON, 8, 22) and in_window(NOON.replace(hour=23), 8, 22)  # 12:00 e 20:00 em Brasília
    assert not in_window(NOON.replace(hour=1), 8, 22) and not in_window(NOON.replace(hour=10), 8, 22)  # 22:00 e 07:00
    assert day_start(NOON) == datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
    assert day_start(NOON.replace(hour=1)) == datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)  # 22:00 BRT do dia 2


# --- robô -------------------------------------------------------------------------


def test_manda_o_job_marca_enviado_e_registra_a_oferta_no_ultimo_grupo(sessions, db):
    product = make_product(db)
    queue(db, product, groups=("g1@g.us", "g2@g.us"), text="*Kimono*")
    client, clock = FakeClient(), [NOON]
    worker = make_worker(sessions, client, clock=clock, checker=FakeChecker("300"))

    worker.step()
    assert client.sent == [("g1@g.us", "*Kimono*")]
    assert [j.status for j in jobs(db)] == ["sent", "queued"]
    assert recent_sends(db) == {}  # ainda falta um grupo: a oferta não conta como enviada

    clock[0] = NOON + timedelta(seconds=61)
    worker.step()
    assert [c for c, _ in client.sent] == ["g1@g.us", "g2@g.us"]
    assert [j.status for j in jobs(db)] == ["sent", "sent"]
    assert list(recent_sends(db)) == [product.id]


def test_respeita_o_intervalo_entre_mensagens(sessions, db):
    queue(db, make_product(db), groups=("g1@g.us", "g2@g.us"))
    client, clock = FakeClient(), [NOON]
    worker = make_worker(sessions, client, clock=clock)

    worker.step()
    clock[0] = NOON + timedelta(seconds=30)
    worker.step()

    assert len(client.sent) == 1  # os 60s ainda não passaram


def test_nao_manda_pausado_fora_da_janela_nem_acima_do_limite(sessions, db):
    queue(db, make_product(db), groups=("g1@g.us", "g2@g.us"))
    client, clock = FakeClient(), [NOON]
    worker = make_worker(sessions, client, clock=clock, cap=1)

    set_paused(db, True, "teste")
    db.commit()
    worker.step()
    assert client.sent == [] and is_paused(db)

    set_paused(db, False)
    db.commit()
    clock[0] = NOON.replace(hour=2)  # 23:00 BRT do dia anterior
    worker.step()
    assert client.sent == []

    clock[0] = NOON
    worker.step()
    assert len(client.sent) == 1
    clock[0] = NOON + timedelta(minutes=5)
    worker.step()
    assert len(client.sent) == 1  # limite de 1 por dia


def test_nao_manda_com_o_whatsapp_desconectado(sessions, db):
    queue(db, make_product(db))
    for state in ("qr", SenderError("fora do ar")):
        client = FakeClient(state)
        make_worker(sessions, client, clock=[NOON]).step()
        assert client.sent == []
    assert [j.status for j in jobs(db)] == ["queued"]


def test_cancela_se_o_preco_subiu_desde_que_entrou_na_fila(sessions, db):
    queue(db, make_product(db, "300"), groups=("g1@g.us", "g2@g.us"))
    client = FakeClient()

    make_worker(sessions, client, clock=[NOON], checker=FakeChecker("350")).step()

    assert client.sent == []
    assert [(j.status, j.error) for j in jobs(db)] == [("cancelled", "preço subiu de R$ 300.00 para R$ 350")] * 2


def test_preco_estavel_ou_menor_ainda_manda(sessions, db):
    queue(db, make_product(db, "300"))
    client = FakeClient()

    make_worker(sessions, client, clock=[NOON], checker=FakeChecker("305")).step()  # +1.6%, dentro da tolerância

    assert len(client.sent) == 1


def test_cancela_se_o_produto_nao_tem_mais_anuncio_novo(sessions, db):
    queue(db, make_product(db))

    make_worker(sessions, FakeClient(), clock=[NOON], checker=FakeChecker(None)).step()

    assert [j.status for j in jobs(db)] == ["cancelled"]


def test_nunca_manda_sem_conseguir_conferir_o_preco(sessions, db):
    queue(db, make_product(db))
    checker = FakeChecker()
    checker.error = RuntimeError("ML fora do ar")
    client = FakeClient()
    worker = make_worker(sessions, client, clock=[NOON], checker=checker)

    worker.step()

    assert client.sent == [] and [j.status for j in jobs(db)] == ["queued"]
    assert "conferência de preço falhou" in worker.state.last_error


def test_falhas_seguidas_pausam_o_robo_e_tres_tentativas_falham_o_job(sessions, db):
    queue(db, make_product(db))
    client = FakeClient()
    client.error = SenderError("timeout")
    clock = [NOON]
    worker = make_worker(sessions, client, clock=clock)

    for _ in range(AUTO_PAUSE_AFTER):
        worker.step()
        clock[0] += timedelta(minutes=2)

    (job,) = jobs(db)
    assert (job.status, job.attempts, job.error) == ("failed", 3, "timeout")
    assert is_paused(db)


def test_erro_permanente_falha_o_job_sem_pausar(sessions, db):
    queue(db, make_product(db))
    client = FakeClient()
    client.error = SenderPermanentError("grupo não encontrado")

    make_worker(sessions, client, clock=[NOON]).step()

    (job,) = jobs(db)
    assert (job.status, job.error) == ("failed", "grupo não encontrado")
    assert not is_paused(db)


def test_depois_de_reiniciar_conta_o_intervalo_do_ultimo_envio(sessions, db):
    queue(db, make_product(db), groups=("g1@g.us", "g2@g.us"))
    client, clock = FakeClient(), [NOON]
    make_worker(sessions, client, clock=clock).step()

    clock[0] = NOON + timedelta(seconds=10)
    make_worker(sessions, client, clock=clock).step()  # processo novo, sem memória do último envio

    assert len(client.sent) == 1


# --- cliente HTTP -------------------------------------------------------------------


def http_client(handler):
    return SenderClient("http://sender:3000/", "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_cliente_manda_o_token_e_o_corpo():
    seen = {}

    def handler(request):
        seen["auth"], seen["body"], seen["url"] = request.headers["authorization"], request.content, str(request.url)
        return httpx.Response(200, json={"ok": True})

    http_client(handler).send("g1@g.us", "oi", "https://img")

    assert seen["auth"] == "Bearer tok" and seen["url"] == "http://sender:3000/send"
    assert b'"chat_id":"g1@g.us"' in seen["body"].replace(b" ", b"")


def test_cliente_separa_erro_permanente_de_erro_que_passa():
    with pytest.raises(SenderPermanentError, match="sem grupo"):
        http_client(lambda r: httpx.Response(404, json={"error": "sem grupo"})).send("x", "oi")
    with pytest.raises(SenderError) as caught:
        http_client(lambda r: httpx.Response(503, json={"error": "iniciando"})).status()
    assert not isinstance(caught.value, SenderPermanentError)

    def boom(request):
        raise httpx.ConnectError("recusou")

    with pytest.raises(SenderError, match="indisponível"):
        http_client(boom).groups()


# --- API ------------------------------------------------------------------------------


def make_sender_app(sessions, client):
    scheduler = CollectScheduler(sessions, FakeLock(), FakeCollector, "mercadolivre", interval=6 * 3600, startup_delay=3600, enabled=False)
    idle = SendWorker(sessions, FakeLock(), None)  # sem cliente o laço não roda: o teste manda só pelo endpoint
    return create_app(settings(), sessions=sessions, scheduler=scheduler, sender_client=client, send_worker=idle)


def ready_offer(db):
    add_product(db, "product:oferta", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")
    product = db.scalars(select(Product)).one()
    product.affiliate_url = "https://meli.la/AAA"
    db.commit()
    return product


def test_api_sem_robo_configurado_responde_desligado_e_409(sessions, db):
    with TestClient(make_sender_app(sessions, None)) as client:
        overview = client.get("/api/sender", headers=AUTH).json()
        response = client.put("/api/sender/groups", json={"groups": []}, headers=AUTH)

    assert overview["configured"] is False and overview["connection"]["state"] == "off"
    assert response.status_code == 409


def test_api_escolhe_grupos_enfileira_e_cancela(sessions, db):
    product = ready_offer(db)

    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        live = client.get("/api/sender/groups", headers=AUTH).json()
        assert [g["selected"] for g in live] == [False, False]

        assert client.post(f"/api/products/{product.id}/send", headers=AUTH).status_code == 409  # sem grupo escolhido
        client.put("/api/sender/groups", json={"groups": [{"id": "g1@g.us", "name": "Grupo 1"}]}, headers=AUTH)
        assert [g["selected"] for g in client.get("/api/sender/groups", headers=AUTH).json()] == [True, False]

        assert client.post(f"/api/products/{product.id}/send", headers=AUTH).json() == {"queued": 1}
        assert client.post(f"/api/products/{product.id}/send", headers=AUTH).status_code == 409  # já na fila

        overview = client.get("/api/sender", headers=AUTH).json()
        job = client.get("/api/sender/jobs", headers=AUTH).json()[0]
        cancelled = client.delete(f"/api/sender/jobs/{job['id']}", headers=AUTH)

    assert overview["queued"] == 1 and overview["connection"]["state"] == "ready" and overview["groups"] == [{"id": "g1@g.us", "name": "Grupo 1"}]
    assert job["status"] == "queued" and "Kimono Jiu Jitsu Koral" in job["text"] and "https://meli.la/AAA" in job["text"]
    assert cancelled.status_code == 200


def test_api_so_enfileira_oferta_pronta(sessions, db):
    add_product(db, "product:oferta", "Kimono Jiu Jitsu Koral", [(40, "500"), (3, "350")], original="500")  # sem link
    product = db.scalars(select(Product)).one()

    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        client.put("/api/sender/groups", json={"groups": [{"id": "g1@g.us", "name": "G"}]}, headers=AUTH)
        response = client.post(f"/api/products/{product.id}/send", headers=AUTH)

    assert response.status_code == 409 and "não está pronta" in response.json()["detail"]


def test_api_pausa_e_retoma(sessions, db):
    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        assert client.post("/api/sender/pause", headers=AUTH).json() == {"paused": True}
        paused = client.get("/api/sender", headers=AUTH).json()
        assert client.post("/api/sender/resume", headers=AUTH).json() == {"paused": False}
        resumed = client.get("/api/sender", headers=AUTH).json()

    assert paused["paused"] is True and paused["pause_reason"] == "pausado no painel"
    assert resumed["paused"] is False and resumed["pause_reason"] == ""
