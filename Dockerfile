# Estágio 1: compila o painel (React + Vite). Só o resultado (dist/) segue para a imagem final.
FROM node:22-slim AS front
WORKDIR /front
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Estágio 2: o serviço, que também serve o painel.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATIC_DIR=/app/static

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=front /front/dist /app/static

# Sem root: o contêiner só chama APIs e grava no Postgres.
RUN useradd --create-home achados
USER achados

EXPOSE 8000
# Um único worker: o disparo manual (POST /api/collect) só funciona no worker líder da coleta.
# A CLI segue disponível: docker compose exec app achados stats
CMD ["uvicorn", "achados.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
