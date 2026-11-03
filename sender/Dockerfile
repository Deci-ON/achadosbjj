# Robô de envio (whatsapp-web.js). Usa o Chromium do sistema: sem baixar o do Puppeteer.
FROM node:22-slim

ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    SENDER_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund
COPY *.js ./

# Sem root. A sessão do WhatsApp (o QR lido) fica no volume /data.
RUN useradd --create-home sender && mkdir -p /data && chown sender /data
USER sender

EXPOSE 3000
CMD ["node", "index.js"]
