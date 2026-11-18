const test = require("node:test");
const assert = require("node:assert/strict");
const { watchPage, readPageState } = require("./pagewatch");

function logger() {
  const lines = [];
  return { lines, log: (...a) => lines.push(a.join(" ")), warn: (...a) => lines.push(a.join(" ")), error: (...a) => lines.push(a.join(" ")) };
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function fakePage(evaluate) {
  const handlers = {};
  return { handlers, on: (ev, fn) => (handlers[ev] = fn), evaluate };
}

test("registra o estado da página e a memória, e para quando o robô fica pronto", async () => {
  const log = logger();
  let ready = false;
  const page = fakePage(async () => ({ readyState: "complete", ageSeconds: 5, hasSynced: false }));
  const client = { pupPage: page };
  const life = { isReady: () => ready, isStopped: () => false, getState: () => "authenticated" };
  const stop = watchPage(client, life, { intervalMs: 5, log, readMemory: () => 431 });
  await wait(30);
  ready = true;
  await wait(15);
  const count = log.lines.length;
  await wait(30);
  stop();

  assert.ok(count >= 2);
  assert.equal(log.lines.length, count); // parou
  assert.match(log.lines[0], /"hasSynced":false/);
  assert.match(log.lines[0], /431 MiB/);
  assert.match(log.lines[0], /estado do robô: authenticated/);
  page.handlers.framenavigated({ parentFrame: () => null, url: () => "https://web.whatsapp.com/?post=1&x=2" });
  assert.match(log.lines.at(-1), /web\.whatsapp\.com\/$/);
});

test("página que não responde e navegador sem página viram linha de log, não erro", async () => {
  const log = logger();
  const client = { pupPage: null };
  const life = { isReady: () => false, isStopped: () => false, getState: () => "starting" };
  const stop = watchPage(client, life, { intervalMs: 5, log, readMemory: () => null });
  await wait(20);
  assert.match(log.lines[0], /sem página/);
  client.pupPage = fakePage(async () => {
    throw new Error("Execution context was destroyed");
  });
  await wait(20);
  stop();
  assert.ok(log.lines.some((l) => /sem leitura \(Execution context was destroyed\).*\? MiB/.test(l)));
});

test("readPageState sem WhatsApp na página diz indisponível em vez de lançar", () => {
  global.document = { readyState: "loading" };
  global.performance = { now: () => 3000 };
  global.window = { require: () => null };
  const state = readPageState();
  delete global.document;
  delete global.performance;
  delete global.window;
  assert.equal(state.readyState, "loading");
  assert.equal(state.ageSeconds, 3);
  assert.equal(state.socketModule, "indisponível");
});
