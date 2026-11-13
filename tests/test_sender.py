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
    set_auto,
    set_paused,
)
from achados.service import create_app
from achados.workflow import recent_sends, record_send
from tests.test_service import AUTH, FakeCollector, FakeLock, add_product, settings

# Hoje às 12:00 em Brasília. Relativo ao relógio porque `recent_sends` (carência de 7 dias) usa a hora de verdade.
NOON = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)


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
    midnight = NOON.replace(hour=3)  # meia-noite de Brasília, em UTC
    assert day_start(NOON) == midnight
    assert day_start(NOON.replace(hour=1)) == midnight - timedelta(days=1)  # 01:00 UTC ainda é o dia anterior em Brasília


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


def make_sender_app(sessions, client, shopee_client=None, **overrides):
    scheduler = CollectScheduler(sessions, FakeLock(), FakeCollector, "mercadolivre", interval=6 * 3600, startup_delay=3600, enabled=False)
    idle = SendWorker(sessions, FakeLock(), None)  # sem cliente o laço não roda: o teste manda só pelo endpoint
    return create_app(
        settings(**overrides), sessions=sessions, scheduler=scheduler, sender_client=client, send_worker=idle, shopee_client=shopee_client
    )


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


# --- envio automático ---------------------------------------------------------------------
# Pontuações com estes preços (histórico de 40 dias a R$ 500): 250 -> 100, 350 -> 82, 400 -> 65, 450 -> não é oferta.


def auto_offer(db, external_id, title, price, *, source="mercadolivre", seller=None) -> Product:
    add_product(db, external_id, title, [(40, "500"), (3, price)], original="500", seller=seller)
    product = db.scalars(select(Product).where(Product.external_id == external_id)).one()
    product.source = source
    product.affiliate_url = f"https://meli.la/{external_id}" if source == "mercadolivre" else f"https://s.shopee.com.br/{external_id}"
    db.commit()
    return product


def seed_auto(db) -> dict[str, Product]:
    """Uma oferta do ML (100), uma da Shopee (82) e uma do ML abaixo do corte padrão de 70 (65)."""
    return {
        "ml": auto_offer(db, "ml:1", "Kimono Jiu Jitsu Koral", "250"),
        "shopee": auto_offer(db, "1.2", "Kimono Jiu Jitsu Shopee", "350", source="shopee"),
        "baixa": auto_offer(db, "ml:3", "Kimono Jiu Jitsu Atama", "400"),
    }


def auto_worker(sessions, *, cap=30, candidates=None, interval=300.0):
    """Um worker com o `candidates` do serviço de verdade; o relógio é o de `clock`."""
    clock = [NOON]
    if candidates is None:
        candidates = make_sender_app(sessions, FakeClient()).state.sender_candidates
    worker = SendWorker(
        sessions,
        FakeLock(),
        FakeClient(),
        daily_cap=cap,
        window=(8, 22),
        candidates=candidates,
        auto_interval=interval,
        auto_default_min_score=70,
        clock=lambda: clock[0],
    )
    return worker, clock


def queued_products(db) -> list[int]:
    return [j.product_id for j in jobs(db) if j.status == "queued"]


def test_auto_desligado_nao_enfileira_e_e_o_padrao(sessions, db):
    seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    db.commit()
    worker, _ = auto_worker(sessions)

    assert worker.auto_enqueue() == 0
    assert jobs(db) == []


def test_auto_sem_funcao_de_candidatas_nao_faz_nada(sessions, db):
    seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True)
    db.commit()
    bare = SendWorker(sessions, FakeLock(), FakeClient(), clock=lambda: NOON)

    assert bare.auto_enqueue() == 0
    assert jobs(db) == []


def test_auto_enfileira_so_o_que_passa_do_corte_na_ordem_de_pontuacao_das_duas_fontes(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True)  # corte padrão: 70
    db.commit()
    worker, _ = auto_worker(sessions)

    assert worker.auto_enqueue() == 2

    assert queued_products(db) == [products["ml"].id, products["shopee"].id]  # 100 antes de 82; o 65 fica de fora
    first, second = jobs(db)
    assert "Kimono Jiu Jitsu Koral" in first.text and "https://meli.la/ml:1" in first.text
    assert "https://s.shopee.com.br/1.2" in second.text and first.price == Decimal("250")


def test_auto_usa_o_corte_guardado(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True, 60)
    db.commit()
    worker, _ = auto_worker(sessions)

    worker.auto_enqueue()

    assert queued_products(db) == [products["ml"].id, products["shopee"].id, products["baixa"].id]


def test_auto_um_job_por_grupo_escolhido(sessions, db):
    seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1"), ("g2@g.us", "G2")])
    set_auto(db, True)
    db.commit()
    worker, _ = auto_worker(sessions)

    assert worker.auto_enqueue() == 2

    assert [j.chat_id for j in jobs(db)] == ["g1@g.us", "g2@g.us"] * 2


def test_auto_respeita_pausa_grupos_e_janela(sessions, db):
    seed_auto(db)
    set_auto(db, True)
    db.commit()
    worker, clock = auto_worker(sessions, interval=0)

    assert worker.auto_enqueue() == 0  # sem grupos escolhidos
    replace_groups(db, [("g1@g.us", "G1")])
    set_paused(db, True, "teste")
    db.commit()
    assert worker.auto_enqueue() == 0  # pausado
    set_paused(db, False)
    db.commit()
    clock[0] = NOON.replace(hour=2)  # 23:00 em Brasília
    assert worker.auto_enqueue() == 0  # fora da janela de horário
    assert jobs(db) == []

    clock[0] = NOON
    assert worker.auto_enqueue() == 2


def test_auto_nao_duplica_e_so_procura_a_cada_intervalo(sessions, db):
    seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True)
    db.commit()
    worker, clock = auto_worker(sessions)

    assert worker.auto_enqueue() == 2
    clock[0] = NOON + timedelta(minutes=10)
    assert worker.auto_enqueue() == 0  # as duas já estão na fila
    assert len(jobs(db)) == 2

    auto_offer(db, "ml:4", "Kimono Jiu Jitsu Novo", "250")
    clock[0] += timedelta(seconds=10)
    assert worker.auto_enqueue() == 0  # procurou há 10s: espera o intervalo
    clock[0] += timedelta(minutes=6)
    assert worker.auto_enqueue() == 1


def test_auto_pula_o_que_ja_entrou_na_fila_a_mao_ou_ja_foi_enviado(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    enqueue_offer(db, products["ml"], "manual", None)
    record_send(db, products["shopee"])  # enviada agora: dentro da carência de 7 dias
    set_auto(db, True, 0)
    db.commit()
    worker, _ = auto_worker(sessions)

    assert worker.auto_enqueue() == 1

    assert queued_products(db) == [products["ml"].id, products["baixa"].id]


def test_auto_pula_o_modelo_que_ja_tem_outro_tamanho_na_fila(sessions, db):
    a2 = auto_offer(db, "ml:a2", "Kimono Jiu Jitsu Torah Tamanho A2", "350", seller=7)
    a3 = auto_offer(db, "ml:a3", "Kimono Jiu Jitsu Torah Tamanho A3", "300", seller=7)  # o representante do modelo
    replace_groups(db, [("g1@g.us", "G1")])
    enqueue_offer(db, a2, "manual", None)
    set_auto(db, True)
    db.commit()
    worker, _ = auto_worker(sessions)

    assert worker.auto_enqueue() == 0
    assert queued_products(db) == [a2.id] and a3.id not in queued_products(db)


def test_auto_nao_repoe_o_que_foi_cancelado_ou_falhou_ha_pouco(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True)
    db.commit()
    worker, clock = auto_worker(sessions, interval=0)
    worker.auto_enqueue()
    cancelled, failed = jobs(db)
    cancelled.status, cancelled.error = "cancelled", "preço subiu"
    failed.status = "failed"
    db.commit()

    clock[0] += timedelta(minutes=10)
    assert worker.auto_enqueue() == 0  # preço que subiu / cancelado no painel não volta a cada ciclo

    for job in jobs(db):  # passou a carência
        job.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()
    clock[0] += timedelta(minutes=10)
    assert worker.auto_enqueue() == 2
    assert queued_products(db) == [products["ml"].id, products["shopee"].id]


def test_auto_nao_passa_do_limite_diario_contando_grupos_enviadas_e_fila(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1"), ("g2@g.us", "G2")])  # uma oferta custa 2 mensagens
    set_auto(db, True, 0)
    db.commit()
    worker, clock = auto_worker(sessions, cap=5, interval=0)

    assert worker.auto_enqueue() == 2  # 5 -> 3 -> 1: a terceira não cabe
    assert queued_products(db) == [products["ml"].id] * 2 + [products["shopee"].id] * 2

    # mensagens já enviadas hoje também contam; a fila cheia fecha o orçamento
    db.add(SendJob(product_id=products["ml"].id, chat_id="g1@g.us", text="x", price=Decimal("1"), status="sent", sent_at=NOON))
    db.commit()
    clock[0] += timedelta(minutes=10)
    assert worker.auto_enqueue() == 0  # 5 - 1 enviada - 4 na fila = 0


def test_auto_com_limite_so_para_uma_oferta(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    db.add(SendJob(product_id=products["baixa"].id, chat_id="g1@g.us", text="x", price=Decimal("1"), status="sent", sent_at=NOON))
    set_auto(db, True, 0)
    db.commit()
    worker, _ = auto_worker(sessions, cap=2)  # 1 enviada hoje: sobra uma mensagem

    assert worker.auto_enqueue() == 1
    assert queued_products(db) == [products["ml"].id]


def test_auto_erro_ao_buscar_candidatas_nao_derruba_o_robo(sessions, db):
    seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True)
    db.commit()

    def boom(min_score):
        raise RuntimeError("banco lento")

    worker, _ = auto_worker(sessions, candidates=boom)

    assert worker.auto_enqueue() == 0
    assert jobs(db) == []


def test_auto_o_job_automatico_passa_pela_conferencia_de_preco_do_ml(sessions, db):
    products = seed_auto(db)
    replace_groups(db, [("g1@g.us", "G1")])
    set_auto(db, True)
    db.commit()
    worker, clock = auto_worker(sessions)
    worker._checker = FakeChecker("999")  # o ML subiu o preço desde a coleta
    worker.auto_enqueue()

    worker.step()  # o primeiro job é o do ML (100)

    ml_job = jobs(db)[0]
    assert (ml_job.product_id, ml_job.status) == (products["ml"].id, "cancelled") and "preço subiu" in ml_job.error


# --- API do envio automático ---------------------------------------------------------------


def test_api_auto_comeca_desligado_e_o_get_do_painel_traz_o_estado(sessions, db):
    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        auto = client.get("/api/sender/auto", headers=AUTH)
        overview = client.get("/api/sender", headers=AUTH).json()
        anonymous = client.get("/api/sender/auto")

    assert auto.json() == {"enabled": False, "min_score": 70, "default_min_score": 70}
    assert overview["auto"] == {"enabled": False, "min_score": 70}
    assert anonymous.status_code == 401


def test_api_liga_muda_o_corte_e_null_mantem(sessions, db):
    with TestClient(make_sender_app(sessions, FakeClient(), sender_auto_min_score=55)) as client:
        assert client.get("/api/sender/auto", headers=AUTH).json()["min_score"] == 55  # o padrão vem da configuração
        on = client.put("/api/sender/auto", json={"enabled": True, "min_score": 80}, headers=AUTH)
        keep = client.put("/api/sender/auto", json={"enabled": True, "min_score": None}, headers=AUTH)
        off = client.put("/api/sender/auto", json={"enabled": False}, headers=AUTH)
        overview = client.get("/api/sender", headers=AUTH).json()

    assert on.json() == {"enabled": True, "min_score": 80, "default_min_score": 55}
    assert keep.json() == {"enabled": True, "min_score": 80, "default_min_score": 55}
    assert off.json() == {"enabled": False, "min_score": 80, "default_min_score": 55}
    assert overview["auto"] == {"enabled": False, "min_score": 80}


@pytest.mark.parametrize("body", [{"enabled": True, "min_score": 101}, {"enabled": True, "min_score": -1}, {"min_score": 70}, {"enabled": "talvez"}])
def test_api_auto_recusa_corpo_invalido(sessions, body):
    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        response = client.put("/api/sender/auto", json=body, headers=AUTH)
        state = client.get("/api/sender/auto", headers=AUTH).json()

    assert response.status_code == 422 and state["enabled"] is False


def test_api_auto_exige_token(sessions):
    with TestClient(make_sender_app(sessions, FakeClient())) as client:
        assert client.put("/api/sender/auto", json={"enabled": True}).status_code == 401


def test_api_o_corte_guardado_vale_para_as_candidatas_do_servico(sessions, db):
    products = seed_auto(db)
    app = make_sender_app(sessions, FakeClient())

    with TestClient(app) as client:
        client.put("/api/sender/auto", json={"enabled": True, "min_score": 90}, headers=AUTH)
        client.put("/api/sender/groups", json={"groups": [{"id": "g1@g.us", "name": "G1"}]}, headers=AUTH)
    worker, _ = auto_worker(sessions, candidates=app.state.sender_candidates)

    assert worker.auto_enqueue() == 1
    assert queued_products(db) == [products["ml"].id]  # só o de 100
