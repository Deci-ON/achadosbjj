#!/usr/bin/env bash
# Atualiza o serviço no servidor: puxa o código, reconstrói a imagem e sobe. Uso: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose up -d --build db app

echo "aguardando o serviço ficar saudável..."
for _ in $(seq 1 30); do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || true)
  if [ "$status" = "healthy" ]; then
    docker compose ps
    echo "ok"
    exit 0
  fi
  sleep 2
done

echo "o serviço não ficou saudável em 60s; últimas linhas do log:" >&2
docker compose logs --tail 30 app >&2
exit 1
