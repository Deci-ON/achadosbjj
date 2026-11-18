const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { createLifecycle, readHalt, HALT_FILE, QR_TIMEOUT } = require("./lifecycle");

const quiet = { log() {}, warn() {}, error() {} };

function setup() {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "sender-"));
  const client = Object.assign(new EventEmitter(), { destroyed: 0, destroy: async function () { this.destroyed += 1; } });
  const life = createLifecycle({ dataDir, renderQr: async (raw) => `data:${raw}`, log: quiet });
  life.attach(client);
  return { dataDir, client, life };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("fluxo normal: QR, pareamento, pronto", async () => {
  const { client, life } = setup();
  assert.equal(life.getState(), "starting");
  client.emit("qr", "abc");
  await tick();
  assert.deepEqual(life.status(), { state: "qr", qr: "data:abc", pairingCode: null, reason: null });
  client.emit("loading_screen", 10, "WhatsApp");
  assert.deepEqual(life.status(), { state: "authenticated", qr: null, pairingCode: null, reason: null });
  client.emit("ready");
  assert.equal(life.isReady(), true);
});

test("LOGOUT para de vez: grava o marcador, fecha o navegador e ignora o que a biblioteca ainda emite", async () => {
  const { dataDir, client, life } = setup();
  client.emit("ready");
  client.emit("disconnected", "LOGOUT");
  await tick();

  assert.equal(life.isStopped(), true);
  assert.match(life.status().reason, /LOGOUT/);
  assert.equal(client.destroyed, 1);
  assert.match(readHalt(dataDir).reason, /LOGOUT/);

  // a biblioteca re-injeta a página e emite QR / carregamento / pronto: nada disso muda o estado
  client.emit("qr", "novo");
  client.emit("loading_screen", 5, "x");
  client.emit("authenticated");
  client.emit("ready");
  await tick();
  assert.deepEqual(life.status().state, "stopped");
  assert.equal(life.status().qr, null);

  client.emit("disconnected", "LOGOUT"); // de novo: não fecha duas vezes
  await tick();
  assert.equal(client.destroyed, 1);
});

test("QR que ninguém leu para o robô, mas sem marcador (um reinício pode tentar de novo)", async () => {
  const { dataDir, client, life } = setup();
  client.emit("qr", "abc");
  await tick();
  client.emit("disconnected", QR_TIMEOUT);
  await tick();
  assert.equal(life.isStopped(), true);
  assert.match(life.status().reason, /QR/);
  assert.equal(fs.existsSync(path.join(dataDir, HALT_FILE)), false);
});

test("falha de autenticação e desconexão por outro motivo também param com marcador", async () => {
  for (const [event, arg] of [["auth_failure", "sessão inválida"], ["disconnected", "CONFLICT"]]) {
    const { dataDir, client, life } = setup();
    client.emit(event, arg);
    await tick();
    assert.equal(life.isStopped(), true);
    assert.notEqual(readHalt(dataDir), null);
  }
});

test("erro solto: antes de conectar para, conectado só registra, parado ignora", async () => {
  const antes = setup();
  antes.life.fatal(new Error("Execution context was destroyed"));
  await tick();
  assert.equal(antes.life.isStopped(), true);
  assert.match(antes.life.status().reason, /Execution context/);
  assert.notEqual(readHalt(antes.dataDir), null);
  antes.life.fatal(new Error("outro")); // já parado: não lança
  assert.equal(antes.client.destroyed, 1);

  const pronto = setup();
  pronto.client.emit("ready");
  pronto.life.fatal("rejeição qualquer"); // string, como a biblioteca às vezes lança
  assert.equal(pronto.life.isReady(), true);
  assert.equal(readHalt(pronto.dataDir), null);
});

test("começar com marcador: estado parado e sem cliente", () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "sender-"));
  assert.equal(readHalt(dataDir), null);
  fs.writeFileSync(path.join(dataDir, HALT_FILE), JSON.stringify({ reason: "LOGOUT", at: "2026-11-17T10:00:00Z" }));
  const life = createLifecycle({ dataDir, renderQr: async () => "", log: quiet });
  life.startStopped(readHalt(dataDir));
  assert.deepEqual(life.status(), { state: "stopped", qr: null, pairingCode: null, reason: "LOGOUT" });
  assert.equal(life.isReady(), false);
});

test("o destroy que falha não vira erro solto", async () => {
  const { client, life } = setup();
  client.destroy = async () => {
    throw new Error("Target closed");
  };
  life.halt("teste");
  await tick();
  assert.equal(life.isStopped(), true);
});

test("código de pareamento aparece no lugar do QR e some ao autenticar", () => {
  const { life, client } = setup();
  life.showPairingCode("ABCD-1234");
  assert.deepEqual(life.status(), { state: "qr", qr: null, pairingCode: "ABCD-1234", reason: null });
  client.emit("authenticated");
  assert.equal(life.status().pairingCode, null);
  life.showPairingCode("ZZZZ-0000"); // já autenticado: ignora
  assert.equal(life.getState(), "authenticated");
});
