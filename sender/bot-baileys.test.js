const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { createBaileysBot, formatPairingCode } = require("./bot-baileys");
const { readHalt, HALT_FILE } = require("./lifecycle");

const quiet = { log() {}, warn() {}, error() {} };
const tick = () => new Promise((resolve) => setImmediate(resolve));
const close = (code) => ({ connection: "close", lastDisconnect: { error: { output: { statusCode: code } } } });

// Baileys de mentira: cada makeWASocket devolve um socket cujo `ev` o teste dirige.
function fakeBaileys({ registered = false } = {}) {
  const sockets = [];
  const state = { creds: { registered } };
  const module = {
    default: (options) => {
      const sock = {
        options,
        ev: new EventEmitter(),
        ended: 0,
        sent: [],
        pairingRequests: [],
        end() {
          this.ended += 1;
        },
        requestPairingCode: async function (phone) {
          this.pairingRequests.push(phone);
          return "ABCD1234";
        },
        groupFetchAllParticipating: async () => ({
          "2@g.us": { id: "2@g.us", subject: "Zeta", participants: [{}, {}] },
          "1@g.us": { id: "1@g.us", subject: "Alfa", participants: [{}] },
        }),
        groupMetadata: async (jid) => {
          if (jid === "gone@g.us") throw new Error("item-not-found");
          return { id: jid };
        },
        sendMessage: async function (jid, content) {
          if (content.image && this.failImage) throw new Error("download falhou");
          this.sent.push({ jid, content });
        },
      };
      sockets.push(sock);
      return sock;
    },
    useMultiFileAuthState: async () => ({ state, saveCreds: async () => {} }),
    Browsers: { ubuntu: (name) => ["Ubuntu", name, "22.04.4"] },
    fetchLatestBaileysVersion: async () => ({ version: [2, 3000, 1] }),
  };
  return { module, sockets, state };
}

function setup({ registered = false, phone = "", sleeps = [] } = {}) {
  process.env.SENDER_BAILEYS_AUTO_VERSION = "0";
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "baileys-"));
  const fake = fakeBaileys({ registered });
  const bot = createBaileysBot({
    dataDir,
    phone,
    loadBaileys: async () => fake.module,
    sleep: async (ms) => void sleeps.push(ms),
    log: quiet,
  });
  return { dataDir, fake, bot, sleeps, sock: () => fake.sockets.at(-1) };
}

async function connected(ctx) {
  await tick();
  ctx.sock().ev.emit("connection.update", { connection: "open" });
  await tick();
}

test("QR: mostra o QR, reabre no 515 do pareamento e fica pronto", async () => {
  const ctx = setup();
  await tick();
  ctx.sock().ev.emit("connection.update", { qr: "ref,abc" });
  for (let i = 0; i < 100 && ctx.bot.status().state !== "qr"; i += 1) await new Promise((r) => setTimeout(r, 10)); // o PNG é desenhado de verdade
  assert.equal(ctx.bot.status().state, "qr");
  assert.match(ctx.bot.status().qr, /^data:image\/png/);

  ctx.fake.state.creds.registered = true; // o pareamento gravou as credenciais
  ctx.sock().ev.emit("connection.update", { isNewLogin: true });
  ctx.sock().ev.emit("connection.update", close(515));
  await tick();
  assert.equal(ctx.fake.sockets.length, 2); // socket novo, o velho foi fechado
  assert.equal(ctx.fake.sockets[0].ended, 1);
  assert.equal(ctx.bot.status().state, "authenticated");

  ctx.sock().ev.emit("connection.update", { connection: "open" });
  assert.equal(ctx.bot.status().state, "ready");
  // eventos atrasados do socket velho não mexem em nada
  ctx.fake.sockets[0].ev.emit("connection.update", close(401));
  await tick();
  assert.equal(ctx.bot.status().state, "ready");
});

test("opções anti-ruído: sem histórico, sem online automático, navegador Ubuntu/Chrome", async () => {
  const ctx = setup();
  await tick();
  const o = ctx.sock().options;
  assert.equal(o.syncFullHistory, false);
  assert.equal(o.markOnlineOnConnect, false);
  assert.equal(o.shouldSyncHistoryMessage(), false);
  assert.deepEqual(o.browser, ["Ubuntu", "Chrome", "22.04.4"]);
});

test("código de pareamento: pede um único código só com dígitos e mostra no painel", async () => {
  const ctx = setup({ phone: "+55 (12) 99142-7315" });
  await tick();
  ctx.sock().ev.emit("connection.update", { qr: "ref,1" });
  ctx.sock().ev.emit("connection.update", { qr: "ref,2" });
  await tick();
  await tick();
  assert.deepEqual(ctx.sock().pairingRequests, ["5512991427315"]);
  assert.deepEqual(ctx.bot.status(), { state: "qr", qr: null, pairingCode: "ABCD-1234", reason: null });
  assert.equal(formatPairingCode("ABCD1234"), "ABCD-1234");
});

test("LOGOUT (401): para com marcador, apaga a sessão local e não reabre", async () => {
  const ctx = setup({ registered: true });
  await connected(ctx);
  const authDir = path.join(ctx.dataDir, "baileys-auth");
  fs.mkdirSync(authDir, { recursive: true });
  fs.writeFileSync(path.join(authDir, "creds.json"), "{}");

  ctx.sock().ev.emit("connection.update", close(401));
  await tick();
  assert.equal(ctx.bot.status().state, "stopped");
  assert.match(ctx.bot.status().reason, /LOGOUT/);
  assert.equal(fs.existsSync(authDir), false);
  assert.match(readHalt(ctx.dataDir).reason, /LOGOUT/);
  assert.equal(ctx.fake.sockets.length, 1);
});

test("outro aparelho assumiu (440) também para com marcador", async () => {
  const ctx = setup({ registered: true });
  await connected(ctx);
  ctx.sock().ev.emit("connection.update", close(440));
  await tick();
  assert.equal(ctx.bot.status().state, "stopped");
  assert.match(ctx.bot.status().reason, /440/);
  assert.notEqual(readHalt(ctx.dataDir), null);
});

test("QR que expirou sem ninguém ler: para sem marcador", async () => {
  const ctx = setup();
  await tick();
  ctx.sock().ev.emit("connection.update", close(408));
  await tick();
  assert.equal(ctx.bot.status().state, "stopped");
  assert.equal(fs.existsSync(path.join(ctx.dataDir, HALT_FILE)), false);
});

test("sessão válida que cai por rede reconecta com espera crescente e para na 4ª queda", async () => {
  const sleeps = [];
  const ctx = setup({ registered: true, sleeps });
  await connected(ctx);
  for (let i = 0; i < 3; i += 1) {
    ctx.sock().ev.emit("connection.update", close(428));
    await tick();
    await tick();
    assert.equal(ctx.fake.sockets.length, i + 2);
  }
  assert.deepEqual(sleeps, [5000, 15000, 45000]);
  ctx.sock().ev.emit("connection.update", close(428));
  await tick();
  assert.equal(ctx.bot.status().state, "stopped");
  assert.match(ctx.bot.status().reason, /caiu 4 vezes/);
  assert.equal(ctx.fake.sockets.length, 4);
});

test("515 em excesso (mais de 3 seguidos) para", async () => {
  const ctx = setup();
  await tick();
  for (let i = 0; i < 4; i += 1) {
    ctx.sock().ev.emit("connection.update", close(515));
    await tick();
    await tick();
  }
  assert.equal(ctx.bot.status().state, "stopped");
});

test("grupos em ordem, envio de texto e de imagem (com queda para texto)", async () => {
  const ctx = setup({ registered: true });
  await connected(ctx);
  assert.deepEqual(await ctx.bot.groups(), [
    { id: "1@g.us", name: "Alfa", participants: 1 },
    { id: "2@g.us", name: "Zeta", participants: 2 },
  ]);

  await ctx.bot.send({ chatId: "1@g.us", text: "oi", imageUrl: "https://img/x.jpg" });
  assert.deepEqual(ctx.sock().sent[0], { jid: "1@g.us", content: { image: { url: "https://img/x.jpg" }, caption: "oi" } });

  ctx.sock().failImage = true;
  await ctx.bot.send({ chatId: "1@g.us", text: "só texto", imageUrl: "https://img/ruim.jpg" });
  assert.deepEqual(ctx.sock().sent[1], { jid: "1@g.us", content: { text: "só texto" } });

  await assert.rejects(ctx.bot.send({ chatId: "gone@g.us", text: "x" }), (e) => e.status === 404);
});

test("antes de conectar, groups/send respondem 503", async () => {
  const ctx = setup();
  await tick();
  await assert.rejects(ctx.bot.groups(), (e) => e.status === 503);
  await assert.rejects(ctx.bot.send({ chatId: "1@g.us", text: "x" }), (e) => e.status === 503);
});

test("com marcador de parada nem abre o socket", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "baileys-"));
  fs.writeFileSync(path.join(dataDir, HALT_FILE), JSON.stringify({ reason: "LOGOUT", at: "2026-11-18T00:00:00Z" }));
  const fake = fakeBaileys();
  const bot = createBaileysBot({ dataDir, loadBaileys: async () => fake.module, log: quiet });
  await tick();
  assert.equal(fake.sockets.length, 0);
  assert.equal(bot.status().state, "stopped");
});

test("falha ao carregar a biblioteca para sem marcador e sem derrubar o processo", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "baileys-"));
  const bot = createBaileysBot({
    dataDir,
    loadBaileys: async () => {
      throw new Error("módulo ausente");
    },
    log: quiet,
  });
  await tick();
  await tick();
  assert.equal(bot.status().state, "stopped");
  assert.match(bot.status().reason, /módulo ausente/);
  assert.equal(readHalt(dataDir), null);
});

test("versão atual do WhatsApp Web é usada quando válida", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "baileys-"));
  const fake = fakeBaileys();
  delete process.env.SENDER_BAILEYS_AUTO_VERSION;
  createBaileysBot({ dataDir, loadBaileys: async () => fake.module, log: quiet });
  await new Promise((r) => setTimeout(r, 30));
  assert.deepEqual(fake.sockets[0].options.version, [2, 3000, 1]);
});
