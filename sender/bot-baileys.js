// Motor alternativo do robô: Baileys (WebSocket direto, sem navegador). Ligado com SENDER_ENGINE=baileys.
// Mesmo contrato do bot.js (status/groups/send/fatal) e o mesmo ciclo de vida (lifecycle.js): o que decide parar,
// o marcador /data/halted.json e a regra "nunca parear de novo sozinho" valem igual.
//
// Política de reconexão (mais fina que a do whatsapp-web.js porque o Baileys exige reabrir o socket):
//  - 515 (restartRequired) logo depois de ler o QR/digitar o código é PARTE do pareamento: reabre, no máximo 3 vezes.
//  - 401 (LOGOUT), 403, 411, 440 (outro aparelho assumiu), 500: para de vez, com marcador. No 401 a sessão local já
//    morreu, então ela é apagada (retomar pede pareamento novo).
//  - Sessão válida que cai por rede (408, 428, 503...): até 3 reconexões em 15 min com espera crescente; depois para.
//  - Ainda sem sessão (QR/código não usado): para sem marcador (um reinício pode tentar de novo).

const fs = require("fs");
const path = require("path");
const { EventEmitter } = require("events");
const QRCode = require("qrcode");
const { HttpError } = require("./server");
const { createLifecycle, readHalt, QR_TIMEOUT } = require("./lifecycle");

const FATAL_CODES = { 403: "forbidden", 411: "multideviceMismatch", 440: "connectionReplaced", 500: "badSession" };
const RESTART_REQUIRED = 515;
const LOGGED_OUT = 401;
const MAX_RESTARTS = 3;
const MAX_DROPS = 3;
const DROP_WINDOW_MS = 15 * 60 * 1000;
const PAIRING_TIMEOUT_MS = 5 * 60 * 1000;

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function statusCodeOf(update) {
  return update?.lastDisconnect?.error?.output?.statusCode ?? update?.lastDisconnect?.error?.statusCode;
}

function formatPairingCode(code) {
  const text = String(code);
  return text.length === 8 ? `${text.slice(0, 4)}-${text.slice(4)}` : text;
}

// Versão do WhatsApp Web que o Baileys anuncia: a embutida na biblioteca envelhece, e uma versão velha demais
// é recusada (405). Busca a atual; qualquer falha ou resposta estranha cai na embutida.
async function latestVersion(baileys, log) {
  if (process.env.SENDER_BAILEYS_AUTO_VERSION === "0" || typeof baileys.fetchLatestBaileysVersion !== "function") return undefined;
  try {
    const result = await Promise.race([
      baileys.fetchLatestBaileysVersion(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("tempo esgotado")), 8000).unref()),
    ]);
    const v = result?.version;
    if (Array.isArray(v) && v.length === 3 && v.every((n) => Number.isInteger(n) && n >= 0)) return v;
  } catch (err) {
    log.warn("não consegui a versão atual do WhatsApp Web, usando a da biblioteca:", err.message);
  }
  return undefined;
}

function createBaileysBot({
  dataDir = "/data",
  phone = process.env.SENDER_PAIRING_PHONE || "",
  loadBaileys = () => import("@whiskeysockets/baileys"),
  makeLogger,
  sleep = realSleep,
  now = () => Date.now(),
  log = console,
} = {}) {
  const life = createLifecycle({ dataDir, renderQr: (raw) => QRCode.toDataURL(raw, { margin: 1, width: 280 }), log });
  const authDir = path.join(dataDir, "baileys-auth");
  const digits = String(phone).replace(/\D/g, "");
  const client = new EventEmitter(); // só para o lifecycle.attach (qr / authenticated / ready)

  let sock = null;
  let currentState = null;
  let generation = 0; // eventos de um socket antigo (já trocado ou fechado) são ignorados
  let everOpen = false;
  let pairingRequested = false;
  let restarts = 0;
  let drops = [];
  let pairingTimer = null;

  const requireReady = () => {
    if (!life.isReady() || !sock) throw new HttpError(503, `WhatsApp não está pronto (${life.getState()})`);
  };

  function closeSocket() {
    generation += 1;
    const old = sock;
    sock = null;
    if (!old) return;
    try {
      old.end(undefined);
    } catch {
      // já fechado
    }
  }
  client.destroy = async () => {
    clearTimeout(pairingTimer);
    closeSocket();
  };

  function wipeAuth() {
    try {
      fs.rmSync(authDir, { recursive: true, force: true });
    } catch (err) {
      log.error("não consegui apagar a sessão local:", err.message);
    }
  }

  const registered = () => Boolean(currentState?.creds?.registered);

  async function start() {
    const baileys = await loadBaileys();
    const makeWASocket = baileys.default || baileys.makeWASocket;
    const { state, saveCreds } = await baileys.useMultiFileAuthState(authDir);
    currentState = state;
    const gen = ++generation;
    const options = {
      auth: state,
      browser: baileys.Browsers.ubuntu("Chrome"),
      printQRInTerminal: false,
      // Menos barulho e menos tráfego: o robô só manda mensagens, não precisa do histórico.
      markOnlineOnConnect: false,
      syncFullHistory: false,
      shouldSyncHistoryMessage: () => false,
      generateHighQualityLinkPreview: false,
    };
    if (makeLogger) options.logger = makeLogger();
    const version = await latestVersion(baileys, log);
    if (version) options.version = version;
    if (gen !== generation || life.isStopped()) return;

    sock = makeWASocket(options);
    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("connection.update", (update) => onUpdate(gen, update));

    if (!registered() && !pairingTimer) {
      pairingTimer = setTimeout(() => {
        if (!everOpen && !life.isStopped()) life.halt("ninguém usou o QR/código a tempo", { persist: false });
      }, PAIRING_TIMEOUT_MS);
      pairingTimer.unref?.();
    }
  }

  function onUpdate(gen, update) {
    if (gen !== generation || life.isStopped()) return;
    const { connection, qr, isNewLogin } = update;

    if (qr) {
      if (digits) {
        // Pareamento por código: pede UM código por pareamento (o QR continua girando, mas não é usado).
        if (!pairingRequested && !registered()) {
          pairingRequested = true;
          Promise.resolve(sock.requestPairingCode(digits))
            .then((code) => life.showPairingCode(formatPairingCode(code)))
            .catch((err) => life.halt(`não consegui pedir o código de pareamento: ${err?.message || err}`, { persist: false }));
        }
      } else {
        client.emit("qr", qr);
      }
    }
    if (isNewLogin) client.emit("authenticated");
    if (connection === "open") {
      everOpen = true;
      clearTimeout(pairingTimer);
      client.emit("ready");
    }
    if (connection === "close") void onClose(statusCodeOf(update));
  }

  async function onClose(code) {
    if (code === LOGGED_OUT) {
      wipeAuth();
      return life.halt("o WhatsApp encerrou a sessão deste aparelho (LOGOUT)");
    }
    if (FATAL_CODES[code]) return life.halt(`conexão encerrada pelo WhatsApp (código ${code}: ${FATAL_CODES[code]})`);

    if (code === RESTART_REQUIRED) {
      // Passo normal do pareamento: o servidor pede para reabrir o socket com as credenciais novas.
      restarts += 1;
      if (restarts > MAX_RESTARTS) return life.halt(`o WhatsApp pediu para reabrir a conexão ${restarts} vezes seguidas`);
      log.log(`reabrindo a conexão depois do pareamento (${restarts}/${MAX_RESTARTS})`);
      closeSocket();
      return start().catch((err) => life.halt(`não deu para reabrir: ${err?.message || err}`, { persist: false }));
    }

    if (!everOpen && !registered()) return life.halt(`${QR_TIMEOUT} (código ${code ?? "?"})`, { persist: false });

    // Sessão válida que caiu (rede, servidor): reconecta com espera crescente, no máximo MAX_DROPS em 15 min.
    const t = now();
    drops = drops.filter((x) => t - x < DROP_WINDOW_MS);
    drops.push(t);
    if (drops.length > MAX_DROPS) return life.halt(`a conexão caiu ${drops.length} vezes em 15 min (último código: ${code ?? "?"})`);
    const wait = 5000 * 3 ** (drops.length - 1);
    log.warn(`conexão caiu (código ${code ?? "?"}); reconectando em ${wait / 1000}s (${drops.length}/${MAX_DROPS})`);
    closeSocket();
    await sleep(wait);
    if (life.isStopped()) return;
    return start().catch((err) => life.halt(`não deu para reconectar: ${err?.message || err}`));
  }

  // Parou antes: nem abre a conexão (só o dono retoma, ver lifecycle.js).
  const previousHalt = readHalt(dataDir);
  if (previousHalt) {
    life.startStopped(previousHalt);
  } else {
    life.attach(client);
    log.log(digits ? "iniciando o Baileys (pareamento por código)" : "iniciando o Baileys (pareamento por QR)");
    start().catch((err) => life.halt(`não deu para iniciar o WhatsApp: ${err?.message || err}`, { persist: false }));
  }

  return {
    status: () => life.status(),
    fatal: (err) => life.fatal(err),

    async groups() {
      requireReady();
      const all = await sock.groupFetchAllParticipating();
      return Object.values(all || {})
        .map((g) => ({ id: g.id, name: g.subject || "", participants: g.participants?.length ?? 0 }))
        .sort((a, b) => a.name.localeCompare(b.name));
    },

    async send({ chatId, text, imageUrl }) {
      requireReady();
      try {
        await sock.groupMetadata(chatId);
      } catch {
        throw new HttpError(404, "grupo não encontrado (o número saiu dele?)");
      }
      if (imageUrl) {
        try {
          await sock.sendMessage(chatId, { image: { url: imageUrl }, caption: text });
          return;
        } catch (err) {
          log.warn("imagem indisponível, mandando só o texto:", err.message);
        }
      }
      await sock.sendMessage(chatId, { text });
    },
  };
}

module.exports = { createBaileysBot, formatPairingCode };
