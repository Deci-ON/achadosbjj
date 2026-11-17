// Ciclo de vida da conexão com o WhatsApp, separado do navegador para poder ser testado com um cliente de mentira.
//
// Regra central: o robô NUNCA tenta se reconectar ou parear de novo sozinho. Cada pareamento/reconexão que falha
// parece, para o WhatsApp, um aparelho que liga e desliga em ciclo, e foi isso que bloqueou o número. Qualquer
// desconexão (LOGOUT, conflito, falha de autenticação, erro interno antes de conectar) leva ao estado "stopped" e
// fica nele: o navegador fecha, o processo continua vivo (sair faria o Docker reiniciar e pedir QR de novo) e,
// quando o motivo é grave, grava-se um marcador no volume para que nem um reinício do contêiner (deploy, queda da
// VPS) volte a abrir o WhatsApp. Retomar é decisão do dono (ver `resumeHint`).

const fs = require("fs");
const path = require("path");

const HALT_FILE = "halted.json";
// A biblioteca emite isto quando o QR expirou N vezes sem ninguém ler: é só um robô ocioso, não vale marcador.
const QR_TIMEOUT = "Max qrcode retries reached";
const RESUME_HINT = "para tentar de novo: docker compose exec sender rm /data/halted.json && docker compose restart sender";

function readHalt(dataDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dataDir, HALT_FILE), "utf8"));
  } catch {
    return null;
  }
}

function createLifecycle({ dataDir, renderQr, log = console }) {
  let state = "starting"; // starting | qr | authenticated | ready | stopped
  let qr = null;
  let reason = null;
  let client = null;

  const halted = () => state === "stopped";

  function writeMarker(why) {
    try {
      fs.mkdirSync(dataDir, { recursive: true });
      fs.writeFileSync(path.join(dataDir, HALT_FILE), JSON.stringify({ reason: why, at: new Date().toISOString() }));
    } catch (err) {
      log.error("não consegui gravar o marcador de parada:", err.message);
    }
  }

  // `persist`: grava o marcador (um reinício do contêiner não reabre o WhatsApp até o dono apagá-lo).
  function halt(why, { persist = true } = {}) {
    if (halted()) return;
    state = "stopped";
    qr = null;
    reason = why;
    log.error(`robô PARADO: ${why}. Não vai reconectar sozinho; ${RESUME_HINT}`);
    if (persist) writeMarker(why);
    if (client) Promise.resolve().then(() => client.destroy()).catch((err) => log.warn("fechando o navegador:", err?.message || err));
  }

  // Marcador de uma parada anterior: não abre o navegador (o chamador não cria o cliente).
  function startStopped(previous) {
    state = "stopped";
    reason = previous.reason || "parada anterior";
    log.error(`robô começou PARADO (${reason}${previous.at ? `, desde ${previous.at}` : ""}); ${RESUME_HINT}`);
  }

  function attach(cli) {
    client = cli;
    // Depois de parado, o que a biblioteca ainda emitir (ela re-injeta a página ao sair) não pode mudar o estado.
    cli.on("loading_screen", (percent, message) => {
      if (halted()) return;
      log.log(`carregando o WhatsApp Web: ${percent}% ${message || ""}`);
      // A tela de carregamento só aparece depois do pareamento: dali em diante não é mais QR.
      if (state === "starting" || state === "qr") {
        state = "authenticated";
        qr = null;
      }
    });
    cli.on("change_state", (s) => !halted() && log.log(`estado do WhatsApp Web: ${s}`));
    cli.on("qr", async (raw) => {
      if (halted()) return;
      try {
        const image = await renderQr(raw);
        if (halted() || state === "authenticated" || state === "ready") return;
        state = "qr";
        qr = image;
        log.log("QR code novo: abra o painel (aba Envio) e leia com o WhatsApp do número do robô");
      } catch (err) {
        log.error("não consegui desenhar o QR:", err.message);
      }
    });
    cli.on("authenticated", () => {
      if (halted()) return;
      state = "authenticated";
      qr = null;
      log.log("autenticado: sincronizando");
    });
    cli.on("ready", () => {
      if (halted()) return;
      state = "ready";
      qr = null;
      log.log("WhatsApp conectado");
    });
    cli.on("auth_failure", (msg) => halt(`falha de autenticação: ${msg}`));
    cli.on("disconnected", (why) => {
      log.error("desconectado:", why);
      if (why === QR_TIMEOUT) return halt("ninguém leu o QR a tempo", { persist: false });
      halt(why === "LOGOUT" ? "o WhatsApp encerrou a sessão deste aparelho (LOGOUT)" : `desconectado (${why})`);
    });
  }

  // Erro que ninguém tratou (a biblioteca lança rejeições soltas ao re-injetar a página depois de um LOGOUT).
  // Já parado: só ruído. Já conectado: não derruba o envio por causa dele. Antes disso: para.
  function fatal(err) {
    const text = String(err?.message || err);
    if (halted()) return log.warn("(já parado) erro ignorado:", text);
    if (state === "ready") return log.error("erro não tratado com o WhatsApp conectado (ignorado):", text);
    halt(`erro interno antes de conectar: ${text}`);
  }

  return {
    attach,
    halt,
    fatal,
    startStopped,
    status: () => ({ state, qr, reason }),
    isReady: () => state === "ready",
    isStopped: halted,
    getState: () => state,
  };
}

module.exports = { createLifecycle, readHalt, HALT_FILE, QR_TIMEOUT, RESUME_HINT };
