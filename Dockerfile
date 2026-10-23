FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Sem root: o contêiner só chama APIs e grava no Postgres.
RUN useradd --create-home achados
USER achados

EXPOSE 8000
# Um único worker: o disparo manual (POST /api/collect) só funciona no worker líder da coleta.
# A CLI segue disponível: docker compose exec app achados stats
CMD ["uvicorn", "achados.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
