# achadosbjj

Coleta de ofertas (Shopee e Mercado Livre) para canais de achadinhos. Esta fase só **coleta e armazena** em Postgres, sem publicação.

## Como rodar

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # preencha SHOPEE_APP_ID / SHOPEE_SECRET
docker compose up -d db         # Postgres 16 em 127.0.0.1:5432
.venv/bin/achados init-db

.venv/bin/achados collect shopee -k "fone bluetooth" -k "air fryer" --pages 3
.venv/bin/achados collect mercadolivre --preset bjj          # kimonos e faixas (catálogo do ML)
.venv/bin/achados collect mercadolivre -c MLB1051 --per-category 10
.venv/bin/achados offers                                      # o que está em oferta de verdade
.venv/bin/achados stats
```

Testes (precisam de um Postgres com o banco `achados_test`; sem ele os testes de storage são pulados):

```bash
.venv/bin/python -m pytest
```

## Rodar em um servidor com Docker

O `docker-compose.yml` tem o banco (`db`) e a CLI (`app`, no profile `tools`, que o `up -d` não sobe). O projeto se chama `achadosbjj`, então rede e volume não se misturam com outros compose do servidor.

```bash
# 1. Enviar o código (o repositório não tem remoto): da sua máquina, na raiz do projeto
git archive --format=tar HEAD | ssh USUARIO@VPS "mkdir -p /opt/achadosbjj && tar -x -C /opt/achadosbjj"

# 2. Na VPS: criar o .env (edite no servidor; não passe o secret por chat)
cd /opt/achadosbjj && cp .env.example .env && nano .env
#    ML_CLIENT_ID, ML_CLIENT_SECRET, POSTGRES_PASSWORD, ACHADOS_DB_PORT=5433

# 3. Banco, e o teste que importa primeiro: o ML aceita o IP deste servidor?
docker compose up -d db
docker compose run --rm app check-ml          # 2s; "BLOQUEADO" = IP recusado, precisa de proxy

# 4. Só se o passo 3 deu "ok"
docker compose run --rm app init-db
docker compose run --rm app collect mercadolivre --preset bjj     # ~7 min
docker compose run --rm app stats
```

Agendar a cada 6 horas (`-T` é necessário no cron, que não tem terminal; `nice` para não competir com outros serviços do servidor):

```cron
0 */6 * * * cd /opt/achadosbjj && nice docker compose run --rm -T app collect mercadolivre --preset bjj >> /var/log/achadosbjj.log 2>&1
```

Depois de ~7 dias de coletas, `docker compose run --rm app offers` passa a listar ofertas de verdade. `check-ml` também serve em qualquer ambiente para validar credenciais.

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
