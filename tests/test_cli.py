import httpx

from achados.cli import run_check_ml
from achados.collectors.mercadolivre_api import MercadoLivreAPI


def api_with(handler) -> MercadoLivreAPI:
    return MercadoLivreAPI("cid", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/oauth/token":
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    return httpx.Response(200, json={"content": [{"id": "MLB1"}], "results": [{"id": "MLB1"}]})


def test_check_ml_ok(capsys):
    assert run_check_ml(api_with(ok_handler)) == 0
    assert capsys.readouterr().out.count("ok ") == 2


def test_check_ml_ip_bloqueado(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(403)

    assert run_check_ml(api_with(handler)) == 1
    assert "BLOQUEADO" in capsys.readouterr().out


def test_check_ml_credenciais_recusadas(capsys):
    assert run_check_ml(api_with(lambda r: httpx.Response(401, json={"error": "invalid_client"}))) == 2
    assert "recusadas" in capsys.readouterr().out


def test_check_ml_resposta_vazia(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(404)

    assert run_check_ml(api_with(handler)) == 1
    assert "FALHA" in capsys.readouterr().out


def test_check_ml_sem_rede(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rota")

    assert run_check_ml(api_with(handler)) == 1
    assert "ERRO DE REDE" in capsys.readouterr().out
