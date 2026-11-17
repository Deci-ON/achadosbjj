// Liga o whatsapp-web.js ao contrato que o server.js espera. Número dedicado: o login (QR) fica em /data.

const fs = require("fs");
const path = require("path");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const { HttpError } = require("./server");

// O Chromium deixa SingletonLock/SingletonCookie/SingletonSocket no perfil. Se o contêiner reinicia sem fechar o
// navegador, o lock sobra e o Chromium novo acha que o perfil está em uso: o robô fica em "iniciando" para sempre.
// Só existe um Chromium por contêiner, então ao subir qualquer lock que sobrou é lixo.
function clearStaleLocks(dir) {
  let removed = 0;
  const walk = (d, depth) => {
    let entries;
    try {
      entries = fs.readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(d, entry.name);
      if (/^Singleton(Lock|Cookie|Socket)$/.test(entry.name)) {
        try {
          fs.rmSync(full, { force: true });
          removed += 1;
        } catch {
          // sem permissão: o Chromium vai reclamar no log
        }
      } else if (entry.isDirectory() && depth < 4) {
        walk(full, depth + 1);
      }
    }
  };
  walk(dir, 0);
  return removed;
}

function createBot({ dataDir = "/data", executablePath } = {}) {
  let state = "starting"; // starting | qr | authenticated | ready | disconnected
  let qr = null;

  const stale = clearStaleLocks(dataDir);
  if (stale) console.log(`removi ${stale} trava(s) antiga(s) do Chromium em ${dataDir}`);
  console.log("iniciando o navegador do WhatsApp...");
  setTimeout(() => {
    if (state === "starting") console.warn("ainda iniciando depois de 2 min: o Chromium não respondeu (veja se há erro acima)");
  }, 120000).unref();

  const client = new Client({
    authStrategy: new LocalAuth({ dataPath: dataDir }),
    puppeteer: {
      executablePath,
      headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"],
    },
  });

  client.on("qr", async (raw) => {
    state = "qr";
    qr = await QRCode.toDataURL(raw, { margin: 1, width: 280 });
    console.log("QR code novo: abra o painel (aba Envio) e leia com o WhatsApp do número do robô");
  });
  client.on("authenticated", () => ((state = "authenticated"), (qr = null)));
  client.on("ready", () => ((state = "ready"), (qr = null), console.log("WhatsApp conectado")));
  client.on("auth_failure", (msg) => ((state = "disconnected"), console.error("falha de autenticação:", msg)));
  client.on("disconnected", (reason) => ((state = "disconnected"), console.error("desconectado:", reason)));

  client.initialize().catch((err) => {
    state = "disconnected";
    console.error("não deu para iniciar o WhatsApp:", err.message);
  });

  const requireReady = () => {
    if (state !== "ready") throw new HttpError(503, `WhatsApp não está pronto (${state})`);
  };

  return {
    status: () => ({ state, qr }),

    async groups() {
      requireReady();
      const chats = await client.getChats();
      return chats
        .filter((chat) => chat.isGroup)
        .map((chat) => ({ id: chat.id._serialized, name: chat.name, participants: chat.participants?.length ?? 0 }))
        .sort((a, b) => a.name.localeCompare(b.name));
    },

    async send({ chatId, text, imageUrl }) {
      requireReady();
      let chat;
      try {
        chat = await client.getChatById(chatId);
      } catch {
        throw new HttpError(404, "grupo não encontrado (o número saiu dele?)");
      }
      if (!chat.isGroup) throw new HttpError(422, "não é um grupo");
      if (imageUrl) {
        try {
          const media = await MessageMedia.fromUrl(imageUrl, { unsafeMime: true });
          await client.sendMessage(chatId, media, { caption: text });
          return;
        } catch (err) {
          console.warn("imagem indisponível, mandando só o texto:", err.message);
        }
      }
      await client.sendMessage(chatId, text);
    },
  };
}

module.exports = { createBot };
