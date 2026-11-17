const { createBot } = require("./bot");
const { createServer } = require("./server");

const token = process.env.SENDER_TOKEN || "";
if (!token) {
  console.error("SENDER_TOKEN é obrigatório");
  process.exit(1);
}

const bot = createBot({
  dataDir: process.env.SENDER_DATA_DIR || "/data",
  executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
});

// Nunca sair por erro solto: o Docker reiniciaria o contêiner e, sem sessão, ele pediria um QR novo em ciclo (o que
// o WhatsApp vê como aparelho ligando e desligando). O bot decide: antes de conectar, para; conectado, só registra.
process.on("unhandledRejection", (err) => bot.fatal(err));
process.on("uncaughtException", (err) => bot.fatal(err));

const port = Number(process.env.PORT || 3000);
createServer(bot, token).listen(port, "0.0.0.0", () => console.log(`robô de envio na porta ${port}`));
