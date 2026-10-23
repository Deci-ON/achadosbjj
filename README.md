# achadosbjj

Coleta de ofertas (Shopee e Mercado Livre) para canais de achadinhos. Esta fase **coleta, armazena** em Postgres e **expõe** as ofertas por uma API protegida; ainda não publica nos canais.

## Como rodar

O achadosbjj é um **serviço**: um app FastAPI que coleta sozinho a cada 6 horas (laço no `lifespan`, no padrão do `bazaar_sync_loop` do tibiascope) e expõe uma API protegida por token. Sem cron.

```bash
cp .env.example .env     # ML_CLIENT_ID, ML_CLIENT_SECRET, ADMIN_TOKEN, POSTGRES_PASSWORD (descomente); SHOPEE_* quando tiver
docker compose up -d --build
docker compose exec app achados check-ml     # o ML aceita este IP/credencial? (2s, não grava)
```

A primeira coleta começa 30s depois de o serviço subir, e só se a última coleta bem-sucedida for mais velha que o intervalo: reiniciar ou fazer deploy não recoleta à toa. Acompanhe:

```bash
docker compose logs -f app
curl -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8001/api/status
```

### API

Escuta em `127.0.0.1:8001` do host. Tudo em `/api/*` exige `Authorization: Bearer $ADMIN_TOKEN`; sem `ADMIN_TOKEN` no `.env` responde 503 (nunca fica aberto por esquecimento).

| Rota | O que faz |
|---|---|
| `GET /health` | público; confere o banco (usado pelo healthcheck do compose) |
| `GET /api/status` | estado do agendador (líder, rodando, falhas seguidas, próximo horário), produtos por loja, últimas 5 coletas |
| `GET /api/offers` | produtos pela pontuação por histórico: `status` (`offer` padrão, `no_offer`, `learning`, `irrelevant`), `min_score`, `limit`, `source`, `max_age_days`, `min_drop`, `min_history`; devolve também as contagens por situação |
| `POST /api/collect` | dispara uma coleta agora, em segundo plano (202). 409 se o agendador está desligado, se este worker não é o líder, ou se já há uma coleta |

Configuração (`.env`): `COLLECT_ENABLED` (true), `COLLECT_INTERVAL_SECONDS` (21600), `COLLECT_PRESET` (bjj), `COLLECT_STARTUP_DELAY_SECONDS` (30). Falha de coleta repete em 15 min, 30 min... até o intervalo normal, e fica registrada em `collect_runs` (visível em `/api/status`).

### CLI (continua disponível)

```bash
docker compose exec app achados offers                    # o que está em oferta de verdade
docker compose exec app achados stats
docker compose exec app achados collect mercadolivre --preset bjj     # coleta manual, fora do agendamento
docker compose exec app achados collect shopee -k "kimono bjj" --pages 2
docker compose exec db psql -U achados -c "select title, price from products order by last_seen_at desc limit 10"
```

Sem Docker, com um Postgres seu (aponte `DATABASE_URL` no `.env`):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/achados check-ml
.venv/bin/uvicorn achados.service:create_app --factory --port 8001
```

### Deploy no servidor

```bash
git clone https://github.com/Deci-ON/achadosbjj.git ~/achadosbjj && cd ~/achadosbjj
cp .env.example .env && nano .env      # edite no servidor; não passe o secret por chat
./deploy.sh                            # git pull + build + up + espera ficar saudável
```

Para atualizar depois, `./deploy.sh` (reconstrói a imagem, o que `docker compose run` sozinho não faz). O banco não publica porta no host, então não conflita com outro Postgres do servidor. Se houver uma coleta em andamento no `docker compose stop`, o processo pode demorar a sair; se o prazo de 20s estourar, o Docker mata o contêiner, e o próximo start fecha a coleta interrompida como erro (a coleta nova só começa quando vencer o intervalo).

Testes (só os de storage precisam de um Postgres com o banco `achados_test`; os demais usam SQLite em memória):

```bash
.venv/bin/python -m pytest
```

## Modelo de dados

- `products`: um registro por `(source, external_id)`, com o estado atual da oferta e o JSON bruto da loja.
- `price_snapshots`: histórico, com uma linha na primeira coleta e outra a cada mudança de preço.
- `collect_runs`: uma linha por execução, com contagem de itens e erro, se houver.

As tabelas são criadas com `create_all`. Quando o esquema estabilizar, vale adotar Alembic.

## O que é oferta de verdade

`achados offers` lista o que está em oferta pelo **nosso histórico de preços**, não pelo "de R$ X" do vendedor (que ele mesmo define e pode inflar). Regras em `src/achados/scoring.py`:

- **Referência:** mediana, ponderada pelo tempo, do preço nos últimos 30 dias (o preço vale até a próxima mudança, já que `price_snapshots` só grava mudanças).
- **Oferta (`offer`):** preço pelo menos 15% abaixo da mediana **e** no menor valor do período. Um preço baixo há semanas é só o preço normal do item.
- **Aprendendo (`learning`):** menos de 7 dias de histórico; o desconto do vendedor aparece como "não verificado". Por isso a coleta precisa rodar várias vezes (ex.: a cada poucas horas) antes de a lista ser confiável.
- **"De" inflado:** se o preço original declarado nunca foi praticado no histórico, a oferta perde pontos e o motivo aparece.
- **Nicho (`irrelevant`):** regras BJJ (`BJJ_RULES`). O título precisa citar jiu-jitsu, BJJ, rash guard ou no-gi; "kimono" ou "judô" sozinhos não bastam, e a categoria do ML não é atalho, porque `MLB-MARTIAL_ARTS_UNIFORMS` mistura jiu-jitsu, judô, caratê e taekwondo (medido: 135 de 361 produtos, 37%, eram de outros esportes). Títulos multiuso como "Jiu-Jitsu / Judô / Caratê" passam. Exclusões: caneca, chaveiro, infantil de proteção solar, decoração, taekwondo/dobok etc.; categorias `MLB-MUGS` e `MLB-BABY_SWIMWEAR` barradas. Ajuste a lista conforme aparecerem falsos positivos.
- **Score 0-100:** queda real sobre a mediana (até 70), menor preço do período (15), "de" confirmado (10), profundidade do histórico (5); "de" inflado tira 20.

Limite atual: a API do ML com token `client_credentials` só enxerga produtos de catálogo, e a maioria dos kimonos de BJJ é anúncio comum fora dele (ver "Estado das fontes"). Os limiares são configuráveis (`--min-drop`, `--min-history`, `--max-age-days`, `--any-niche`).

## Estado das fontes

| Fonte | Como coleta | Validado contra a loja real? |
|---|---|---|
| Shopee | API GraphQL de afiliados (`productOfferV2`), com assinatura SHA256 | **Não.** Faltam credenciais; testado só com respostas simuladas |
| Mercado Livre (`collect mercadolivre`) | API oficial, fluxo `client_credentials`: `/highlights` (mais vendidos por categoria) + `/products/{id}` + `/products/{id}/items` | **Sim**, com o token real; os testes usam respostas reais em `tests/fixtures/ml_*.json` |
| Mercado Livre (`collect mercadolivre-html`) | HTML da listagem (`/ofertas`), cards `poly-card` | **Não.** O ML exige login (`account-verification`) até no navegador; o parser foi escrito contra uma fixture feita à mão |

Mercado Livre pela API: precisa de `ML_CLIENT_ID` e `ML_CLIENT_SECRET` (app criado em developers.mercadolivre.com.br/devcenter). Limites vistos com esse app (também com token de usuário): `/sites/MLB/search`, `/items/{id}`, `/user-products/*` e `/seller-promotions` respondem 403, então não há busca de anúncios nem título/foto por anúncio. O que abre: `/highlights` (mais vendidos por categoria), `/products/search` (catálogo, 50 por página), `/products/{id}` e `/products/{id}/items` (ofertas ativas do produto). A API responde 429 se chamada rápido; o cliente espera (3s, 10s, 30s ou o `Retry-After`) e tenta de novo.

O item acompanhado é o **produto de catálogo** (`external_id = product:MLB123`), com o **menor preço novo** entre os vendedores. Assim o histórico não quebra quando o vendedor mais barato muda, e a queda desse preço é o sinal de oferta usado por `achados offers`. O link aponta para o anúncio mais barato do momento. O `original_price` do vendedor é guardado, mas não é confiável (ver "O que é oferta de verdade").

`--preset bjj` busca por 10 palavras-chave de jiu-jitsu no catálogo, filtrando os domínios `MLB-MARTIAL_ARTS_UNIFORMS` e `MLB-MARTIAL_ARTS_BELTS`, mais os destaques das categorias Kimonos (MLB47731) e Faixas (MLB277803). Medido em 2026-10-23: 361 produtos com oferta ativa (294 uniformes, 67 faixas), em cerca de 7 minutos; 70% têm um único vendedor, e só ~10% trazem desconto declarado. Rash guards e bermudas caem em categorias genéricas (`MLB-SPORT_T_SHIRTS`, `MLB-SHORTS`) sem produtos de catálogo úteis, então não entram. Por isso a comparação entre vendedores quase não existe e o histórico próprio é o sinal principal. Combine com `-k`, `--domain`, `--search-pages` para outros nichos.

Shopee: rode a coleta com credenciais reais antes de confiar nos dados.
