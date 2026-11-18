const { createServer } = require("./server");

const token = process.env.SENDER_TOKEN || "";
if (!token) {
  console.error("SENDER_TOKEN é obrigatório");
  process.exit(1);
}

// SENDER_ENGINE: "wwebjs" (padrão: whatsapp-web.js + Chromium) ou "baileys" (WebSocket direto, sem navegador).
// Só o motor escolhido é carregado: o Baileys não puxa o Puppeteer e vice-versa.
const engine = (process.env.SENDER_ENGINE || "wwebjs").toLowerCase();
const dataDir = process.env.SENDER_DATA_DIR || "/data";
let bot;
if (engine === "baileys") {
  bot = require("./bot-baileys").createBaileysBot({ dataDir });
} else if (engine === "wwebjs") {
  bot = require("./bot").createBot({ dataDir, executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined });
} else {
  console.error(`SENDER_ENGINE desconhecido: ${engine} (use wwebjs ou baileys)`);
  process.exit(1);
}
console.log(`motor de envio: ${engine}`);

// Nunca sair por erro solto: o Docker reiniciaria o contêiner e, sem sessão, ele pediria um QR novo em ciclo (o que
// o WhatsApp vê como aparelho ligando e desligando). O bot decide: antes de conectar, para; conectado, só registra.
process.on("unhandledRejection", (err) => bot.fatal(err));
process.on("uncaughtException", (err) => bot.fatal(err));

const port = Number(process.env.PORT || 3000);
// HOST: em um PC fora do Docker, ligue só ao endereço privado (ex.: o IP do Tailscale) em vez de 0.0.0.0.
const host = process.env.HOST || "0.0.0.0";
createServer(bot, token).listen(port, host, () => console.log(`robô de envio em ${host}:${port}`));
