import httpx

from achados.cli import run_check_ml, run_check_shopee
from achados.collectors.shopee import ShopeeClient
from achados.collectors.mercadolivre_api import MercadoLivreAPI


def api_with(handler) -> MercadoLivreAPI:
    return MercadoLivreAPI("cid", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/oauth/token":
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    return httpx.Response(200, json={"content": [{"id": "MLB1"}], "results": [{"id": "MLB1"}]})


def test_check_ml_ok(capsys):
    assert run_check_ml(api_with(ok_handler)) == 0
    assert capsys.readouterr().out.count("ok ") == 3  # token+destaques, busca e ofertas do produto


POLICY_DENIAL = {
    "code": "PA_UNAUTHORIZED_RESULT_FROM_POLICIES",
    "blocked_by": "PolicyAgent",
    "message": "At least one policy returned UNAUTHORIZED.",
    "status": 403,
}


def policy_handler(request: httpx.Request) -> httpx.Response:
    """Token, destaques, busca e detalhe passam; só as ofertas do produto são negadas (o caso de 2026-11-27)."""
    if request.url.path == "/oauth/token":
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    if request.url.path.endswith("/items"):
        return httpx.Response(403, json=POLICY_DENIAL)
    return httpx.Response(200, json={"content": [{"id": "MLB1"}], "results": [{"id": "MLB1"}]})


def test_check_ml_app_sem_permissao_funcional_diz_o_que_fazer(capsys):
    assert run_check_ml(api_with(policy_handler)) == 1
    out = capsys.readouterr().out
    assert "BLOQUEADO" in out and "permissão funcional" in out and "Publicação e sincronização" in out
    assert out.count("ok ") == 2  # os dois primeiros passos passaram: não é o IP nem o token


def test_check_ml_produtos_sem_oferta_nao_confirmam_mas_nao_falham(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(404)
        return ok_handler(request)

    assert run_check_ml(api_with(handler)) == 0
    assert "AVISO" in capsys.readouterr().out


def test_check_ml_acha_oferta_no_segundo_produto(capsys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/products/search":
            return httpx.Response(200, json={"results": [{"id": "MLB1"}, {"id": "MLB2"}]})
        if request.url.path.endswith("/items"):
            calls.append(request.url.path)
            return httpx.Response(404 if "MLB1" in request.url.path else 200, json={"results": []})
        return ok_handler(request)

    assert run_check_ml(api_with(handler)) == 0
    assert calls == ["/products/MLB1/items", "/products/MLB2/items"]
    assert "AVISO" not in capsys.readouterr().out


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


# --- check-shopee ---------------------------------------------------------------


def shopee_with(handler) -> ShopeeClient:
    return ShopeeClient("app1", "secret1", "https://shopee.test/graphql", http=httpx.Client(transport=httpx.MockTransport(handler)))


def shopee_ok(request: httpx.Request) -> httpx.Response:
    node = {
        "itemId": 1,
        "shopId": 2,
        "productName": "Kimono Jiu Jitsu Trancado",
        "priceMin": "250.5",
        "priceDiscountRate": 10,
        "commissionRate": "0.12",
        "offerLink": "https://s.shopee.com.br/abc",
        "productLink": "https://shopee.com.br/x",
    }
    return httpx.Response(200, json={"data": {"productOfferV2": {"nodes": [node, {"productName": "sem preco"}], "pageInfo": {}}}})


def test_check_shopee_ok_mostra_preco_comissao_e_link_sem_expor_credenciais(capsys):
    assert run_check_shopee(shopee_with(shopee_ok)) == 0
    out = capsys.readouterr().out
    assert "2 produtos" in out and "R$    250.5" in out and "12.0%" in out and "com link" in out
    assert "ignorado" in out and "app1" not in out and "secret1" not in out


def test_check_shopee_sem_acesso_explica_o_codigo_10035(capsys):
    body = {"errors": [{"message": "no access", "extensions": {"code": 10035}}]}

    assert run_check_shopee(shopee_with(lambda r: httpx.Response(200, json=body))) == 2
    out = capsys.readouterr().out
    assert "ERRO" in out and "ainda não tem acesso" in out


def test_check_shopee_assinatura_invalida_explica_o_codigo_10020(capsys):
    body = {"errors": [{"message": "Invalid Signature", "extensions": {"code": 10020}}]}

    assert run_check_shopee(shopee_with(lambda r: httpx.Response(200, json=body))) == 2
    assert "SHOPEE_APP_ID" in capsys.readouterr().out


def test_check_shopee_bloqueado_sem_rede_e_lista_vazia(capsys):
    assert run_check_shopee(shopee_with(lambda r: httpx.Response(429))) == 1
    assert "BLOQUEADO" in capsys.readouterr().out

    def boom(request):
        raise httpx.ConnectError("sem rota")

    assert run_check_shopee(shopee_with(boom)) == 1
    assert "ERRO DE REDE" in capsys.readouterr().out

    empty = {"data": {"productOfferV2": {"nodes": [], "pageInfo": {}}}}
    assert run_check_shopee(shopee_with(lambda r: httpx.Response(200, json=empty))) == 1


def shopee_with_link(link_response):
    def handler(request: httpx.Request) -> httpx.Response:
        if b"generateShortLink" in request.content:
            return link_response
        return shopee_ok(request)

    return shopee_with(handler)


def test_check_shopee_short_link_gera_o_link_com_sub_ids(capsys):
    ok = httpx.Response(200, json={"data": {"generateShortLink": {"shortLink": "https://s.shopee.com.br/xyz"}}})

    assert run_check_shopee(shopee_with_link(ok), short_link=True) == 0
    assert "https://s.shopee.com.br/xyz" in capsys.readouterr().out


def test_check_shopee_short_link_mostra_o_erro_da_api(capsys):
    erro = httpx.Response(200, json={"errors": [{"message": "bad origin"}]})

    assert run_check_shopee(shopee_with_link(erro), short_link=True) == 2
    assert "link curto" in capsys.readouterr().out


def test_check_shopee_sem_a_flag_nao_gera_link(capsys):
    assert run_check_shopee(shopee_with_link(httpx.Response(500))) == 0
    assert "link curto" not in capsys.readouterr().out
