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
const port = Number(process.env.PORT || 3000);
createServer(bot, token).listen(port, "0.0.0.0", () => console.log(`robô de envio na porta ${port}`));
