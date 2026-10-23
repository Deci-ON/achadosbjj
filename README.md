# achadosbjj

Coleta de ofertas (Shopee e Mercado Livre) para canais de achadinhos. Esta fase só **coleta e armazena** em Postgres, sem publicação.

## Como rodar

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # preencha SHOPEE_APP_ID / SHOPEE_SECRET
docker compose up -d db         # Postgres 16 em 127.0.0.1:5432
.venv/bin/achados init-db

.venv/bin/achados collect shopee -k "fone bluetooth" -k "air fryer" --pages 3
.venv/bin/achados collect mercadolivre -c MLB1051 --per-category 10 --min-discount 20
.venv/bin/achados stats
```

Testes (precisam de um Postgres com o banco `achados_test`; sem ele os testes de storage são pulados):

```bash
.venv/bin/python -m pytest
```

## Modelo de dados

- `products`: um registro por `(source, external_id)`, com o estado atual da oferta e o JSON bruto da loja.
- `price_snapshots`: histórico, com uma linha na primeira coleta e outra a cada mudança de preço.
- `collect_runs`: uma linha por execução, com contagem de itens e erro, se houver.

As tabelas são criadas com `create_all`. Quando o esquema estabilizar, vale adotar Alembic.

## Estado das fontes

| Fonte | Como coleta | Validado contra a loja real? |
|---|---|---|
| Shopee | API GraphQL de afiliados (`productOfferV2`), com assinatura SHA256 | **Não.** Faltam credenciais; testado só com respostas simuladas |
| Mercado Livre (`collect mercadolivre`) | API oficial, fluxo `client_credentials`: `/highlights` (mais vendidos por categoria) + `/products/{id}` + `/products/{id}/items` | **Sim**, com o token real; os testes usam respostas reais em `tests/fixtures/ml_*.json` |
| Mercado Livre (`collect mercadolivre-html`) | HTML da listagem (`/ofertas`), cards `poly-card` | **Não.** O ML exige login (`account-verification`) até no navegador; o parser foi escrito contra uma fixture feita à mão |

Mercado Livre pela API: precisa de `ML_CLIENT_ID` e `ML_CLIENT_SECRET` (app criado em developers.mercadolivre.com.br/devcenter). Limites vistos com esse token: `/sites/MLB/search`, `/items/{id}`, `/user-products/*` e `/seller-promotions` respondem 403, então não há busca por palavra-chave nem título/foto por anúncio (vêm do produto de catálogo). A coleta grava, por produto, a oferta nova de maior desconto (empate: menor preço) e o link `https://www.mercadolivre.com.br/p/{produto}?wid={anúncio}`. Só uma fração das ofertas tem `original_price`, então descontos reais são minoria; use `--min-discount` para filtrar. O link de afiliado não vem da API.

Shopee: rode a coleta com credenciais reais antes de confiar nos dados.
