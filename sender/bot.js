// Liga o whatsapp-web.js ao contrato que o server.js espera. Número dedicado: o login (QR) fica em /data.

const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const { HttpError } = require("./server");

function createBot({ dataDir = "/data", executablePath } = {}) {
  let state = "starting"; // starting | qr | authenticated | ready | disconnected
  let qr = null;

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
