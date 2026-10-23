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

ENTRYPOINT ["achados"]
CMD ["--help"]
