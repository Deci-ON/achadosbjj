const test = require("node:test");
const assert = require("node:assert/strict");
const { createServer } = require("./server");

function fakeBot() {
  const sent = [];
  return {
    sent,
    status: () => ({ state: "ready", qr: null }),
    groups: () => [{ id: "1@g.us", name: "G", participants: 3 }],
    send: async (job) => {
      if (job.chatId === "gone@g.us") throw Object.assign(new Error("grupo não encontrado"), { status: 404 });
      sent.push(job);
    },
  };
}

async function withServer(bot, fn) {
  const server = createServer(bot, "segredo");
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn((path, init = {}) =>
      fetch(base + path, { ...init, headers: { Authorization: "Bearer segredo", "Content-Type": "application/json", ...init.headers } }),
    base);
  } finally {
    server.close();
  }
}

test("exige o token (menos em /health)", async () => {
  await withServer(fakeBot(), async (call, base) => {
    assert.equal((await fetch(`${base}/status`)).status, 401);
    assert.equal((await fetch(`${base}/status`, { headers: { Authorization: "Bearer errado" } })).status, 401);
    assert.equal((await fetch(`${base}/health`)).status, 200);
    assert.deepEqual(await (await call("/status")).json(), { state: "ready", qr: null });
  });
});

test("lista os grupos", async () => {
  await withServer(fakeBot(), async (call) => {
    assert.deepEqual(await (await call("/groups")).json(), [{ id: "1@g.us", name: "G", participants: 3 }]);
  });
});

test("manda para um grupo", async () => {
  const bot = fakeBot();
  await withServer(bot, async (call) => {
    const res = await call("/send", { method: "POST", body: JSON.stringify({ chat_id: "1@g.us", text: "oi", image_url: "https://img" }) });
    assert.equal(res.status, 200);
    assert.deepEqual(bot.sent, [{ chatId: "1@g.us", text: "oi", imageUrl: "https://img" }]);
  });
});

test("recusa quem não é grupo, texto vazio e JSON ruim", async () => {
  const bot = fakeBot();
  await withServer(bot, async (call) => {
    assert.equal((await call("/send", { method: "POST", body: JSON.stringify({ chat_id: "5511999999999@c.us", text: "oi" }) })).status, 422);
    assert.equal((await call("/send", { method: "POST", body: JSON.stringify({ chat_id: "1@g.us", text: "  " }) })).status, 422);
    assert.equal((await call("/send", { method: "POST", body: "{ruim" })).status, 400);
    assert.deepEqual(bot.sent, []);
  });
});

test("repassa o status de erro do robô (grupo sumiu = 404)", async () => {
  await withServer(fakeBot(), async (call) => {
    const res = await call("/send", { method: "POST", body: JSON.stringify({ chat_id: "gone@g.us", text: "oi" }) });
    assert.equal(res.status, 404);
    assert.match((await res.json()).error, /grupo/);
  });
});
