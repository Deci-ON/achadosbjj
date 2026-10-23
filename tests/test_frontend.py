from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from achados.config import Settings
from achados.scheduler import CollectScheduler
from achados.service import create_app

TOKEN = "t"


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html><body>painel</body></html>", encoding="utf-8")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path.parent / "segredo.txt").write_text("fora da pasta", encoding="utf-8")
    return tmp_path


def make_app(sessions, static_dir):
    scheduler = CollectScheduler(sessions, None, lambda: None, "mercadolivre", interval=3600, enabled=False)
    settings = Settings(_env_file=None, admin_token=TOKEN, static_dir=str(static_dir))
    return create_app(settings, sessions=sessions, scheduler=scheduler)


def test_raiz_e_rotas_do_react_router_devolvem_o_index(sessions, dist):
    with TestClient(make_app(sessions, dist)) as client:
        for path in ("/", "/ofertas", "/produto/12", "/qualquer/coisa/profunda"):
            response = client.get(path)
            assert response.status_code == 200 and "painel" in response.text, path
            assert response.headers["cache-control"] == "no-cache"


def test_arquivos_com_hash_tem_cache_longo_e_os_demais_nao(sessions, dist):
    with TestClient(make_app(sessions, dist)) as client:
        asset = client.get("/assets/app-abc123.js")
        icone = client.get("/favicon.svg")

    assert asset.status_code == 200 and "console.log" in asset.text
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert icone.status_code == 200 and icone.headers["cache-control"] == "no-cache"


def test_a_api_continua_na_frente_do_coringa(sessions, dist):
    with TestClient(make_app(sessions, dist)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert client.get("/api/status").status_code == 401  # exige token, não vira o index
        assert client.get("/docs").status_code == 200


def test_rota_inexistente_da_api_e_404_e_nao_o_index(sessions, dist):
    with TestClient(make_app(sessions, dist)) as client:
        for path in ("/api/nao-existe", "/api"):
            response = client.get(path)
            assert response.status_code == 404 and "painel" not in response.text, path


def test_nao_sai_da_pasta_do_front(sessions, dist):
    with TestClient(make_app(sessions, dist)) as client:
        for path in ("/../segredo.txt", "/%2e%2e/segredo.txt", "/assets/../../segredo.txt", "/..%2fsegredo.txt"):
            response = client.get(path)
            assert "fora da pasta" not in response.text, path


def test_sem_static_dir_so_a_api(sessions):
    scheduler = CollectScheduler(sessions, None, lambda: None, "mercadolivre", interval=3600, enabled=False)
    app = create_app(Settings(_env_file=None, admin_token=TOKEN), sessions=sessions, scheduler=scheduler)

    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/health").status_code == 200


def test_pasta_sem_index_nao_derruba_o_servico(sessions, tmp_path, caplog):
    with caplog.at_level("WARNING", logger="achados"):
        with TestClient(make_app(sessions, tmp_path)) as client:
            assert client.get("/").status_code == 404
            assert client.get("/health").status_code == 200

    assert any("não tem index.html" in r.message for r in caplog.records)


def test_a_protecao_contra_sair_da_pasta_funciona_no_proprio_tratador(sessions, dist):
    # O cliente HTTP normaliza "../" antes de chegar aqui; chamar o tratador direto prova a guarda em si.
    app = make_app(sessions, dist)
    handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/{path:path}")

    fora = handler("../segredo.txt")
    dentro = handler("favicon.svg")

    assert Path(fora.path) == dist / "index.html"  # cai no index, não serve o arquivo de fora
    assert Path(dentro.path) == (dist / "favicon.svg").resolve()
