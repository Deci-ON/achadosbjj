// Liga o whatsapp-web.js ao contrato que o server.js espera. Número dedicado: o login (QR) fica em /data.

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const { HttpError } = require("./server");
const { createLifecycle, readHalt } = require("./lifecycle");
const { watchPage } = require("./pagewatch");

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

// O whatsapp-web.js se apresenta ao WhatsApp como "Chrome 101 no Mac" (um User-Agent de 2022 fixo na biblioteca),
// mas o navegador de verdade é um Chromium atual: o WhatsApp vê um Chrome que não existe mais. Usa-se o User-Agent
// de um Chrome comum com a MESMA versão do Chromium instalado. SENDER_USER_AGENT no .env sobrescreve.
function chromiumUserAgent(executablePath) {
  try {
    const out = execFileSync(executablePath || "chromium", ["--version"], { encoding: "utf8", timeout: 15000 });
    const major = (out.match(/(\d+)\.\d+\.\d+\.\d+/) || [])[1];
    if (major) return `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`;
  } catch (err) {
    console.warn("não consegui ler a versão do Chromium:", err.message);
  }
  return undefined; // cai no padrão da biblioteca
}

function createBot({ dataDir = "/data", executablePath } = {}) {
  const life = createLifecycle({ dataDir, renderQr: (raw) => QRCode.toDataURL(raw, { margin: 1, width: 280 }) });

  // Parou antes (LOGOUT, conflito, erro): nem abre o navegador. Só o dono retoma (ver lifecycle.js).
  let client = null;
  const previousHalt = readHalt(dataDir);
  if (previousHalt) {
    life.startStopped(previousHalt);
  } else {
    const stale = clearStaleLocks(dataDir);
    if (stale) console.log(`removi ${stale} trava(s) antiga(s) do Chromium em ${dataDir}`);
    console.log("iniciando o navegador do WhatsApp...");
    setTimeout(() => {
      if (life.getState() === "starting") console.warn("ainda iniciando depois de 2 min: o Chromium não respondeu (veja se há erro acima)");
    }, 120000).unref();

    const userAgent = process.env.SENDER_USER_AGENT || chromiumUserAgent(executablePath);
    console.log(`User-Agent do navegador: ${userAgent || "(padrão da biblioteca: Chrome 101 no Mac)"}`);

    client = new Client({
      userAgent,
      authStrategy: new LocalAuth({ dataPath: dataDir }),
      // Sem cache local da página do WhatsApp Web: o padrão grava em ./.wwebjs_cache (a pasta do app, que o usuário
      // sem root não pode escrever) e essa escrita falhava no meio do login. Sempre a versão atual, direto da web.
      webVersionCache: { type: "none" },
      // O QR troca a cada ~20 s; sem limite o robô ficaria gerando QR para sempre se ninguém lesse. ~4 min.
      qrMaxRetries: 10,
      puppeteer: {
        executablePath,
        headless: true,
        // SENDER_DEBUG=1 no .env do sender: despeja no log tudo o que o Chromium escreve (erros de rede, crash, etc.).
        dumpio: process.env.SENDER_DEBUG === "1",
        args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"],
      },
    });
    life.attach(client);
    watchPage(client, life);
    client.initialize().catch((err) => life.halt(`não deu para iniciar o WhatsApp: ${err?.message || err}`, { persist: false }));
  }

  const requireReady = () => {
    if (!life.isReady()) throw new HttpError(503, `WhatsApp não está pronto (${life.getState()})`);
  };

  return {
    status: () => life.status(),
    // Erro que ninguém tratou (index.js liga isto a unhandledRejection/uncaughtException).
    fatal: (err) => life.fatal(err),

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
