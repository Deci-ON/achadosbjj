# achadosbjj

Coleta de ofertas (Shopee e Mercado Livre) para canais de achadinhos. Esta fase só **coleta e armazena** em Postgres, sem publicação.

## Como rodar

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # preencha SHOPEE_APP_ID / SHOPEE_SECRET
docker compose up -d db         # Postgres 16 em 127.0.0.1:5432
.venv/bin/achados init-db

.venv/bin/achados collect shopee -k "fone bluetooth" -k "air fryer" --pages 3
.venv/bin/achados collect mercadolivre --pages 3
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
| Mercado Livre | HTML da listagem (`/ofertas`), cards `poly-card` | **Não.** O ML bloqueou a rede de desenvolvimento ("verificação de conta"); o parser foi escrito contra uma fixture feita à mão |

Antes de confiar nos dados: rode a coleta da Shopee com credenciais reais, e salve o HTML real de `/ofertas` em `tests/fixtures/` para validar o parser do ML. Se a VPS também cair na verificação do ML, configure `ML_PROXY_URL` ou migre a coleta do ML para a API oficial (exige app registrado).
