// Diagnóstico de dentro do robô: enquanto a conexão não fica pronta, registra no log (a cada minuto) o estado da
// página do WhatsApp Web, a idade dela e a memória do contêiner. Sondas de fora (puppeteer.connect) recarregam a
// página e gastam memória, o que estragava o diagnóstico; esta lê pelo mesmo navegador que a biblioteca já usa.

const fs = require("fs");

function containerMemoryMiB() {
  for (const file of ["/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"]) {
    try {
      return Math.round(Number(fs.readFileSync(file, "utf8")) / 1048576);
    } catch {
      // próximo caminho (cgroup v1 ou v2)
    }
  }
  return null;
}

// Roda dentro da página. Só lê.
function readPageState() {
  const r = { readyState: document.readyState, ageSeconds: Math.round(performance.now() / 1000) };
  try {
    r.hasDebug = typeof window.Debug !== "undefined";
    r.injected = typeof window.WWebJS !== "undefined";
  } catch {
    // ignora
  }
  try {
    const socket = window.require("WAWebSocketModel").Socket;
    r.socketState = socket.state;
    r.hasSynced = socket.hasSynced;
  } catch (err) {
    r.socketModule = "indisponível";
  }
  return r;
}

function watchPage(client, life, { intervalMs = 60000, maxRuns = 25, log = console, readMemory = containerMemoryMiB } = {}) {
  let runs = 0;
  let hooked = null;

  const hook = (page) => {
    hooked = page;
    page.on("framenavigated", (frame) => {
      if (frame.parentFrame() !== null) return;
      let url = frame.url();
      try {
        const u = new URL(url);
        url = u.host + u.pathname;
      } catch {
        // deixa como está
      }
      log.log(`página do WhatsApp Web navegou para ${url}`);
    });
    page.on("pageerror", (err) => log.warn("erro dentro da página do WhatsApp Web:", String(err?.message || err).slice(0, 200)));
  };

  const timer = setInterval(async () => {
    if (life.isReady() || life.isStopped() || ++runs > maxRuns) return clearInterval(timer);
    const page = client.pupPage;
    if (!page) return log.log(`página do WhatsApp Web: navegador ainda sem página (estado ${life.getState()})`);
    if (hooked !== page) hook(page);
    const mem = readMemory();
    try {
      const info = await Promise.race([
        page.evaluate(readPageState),
        new Promise((_, reject) => setTimeout(() => reject(new Error("a página não respondeu em 10 s")), 10000)),
      ]);
      log.log(`página do WhatsApp Web: ${JSON.stringify(info)} | memória do contêiner: ${mem ?? "?"} MiB | estado do robô: ${life.getState()}`);
    } catch (err) {
      log.log(`página do WhatsApp Web: sem leitura (${err.message}) | memória do contêiner: ${mem ?? "?"} MiB | estado do robô: ${life.getState()}`);
    }
  }, intervalMs);
  timer.unref();
  return () => clearInterval(timer);
}

module.exports = { watchPage, readPageState, containerMemoryMiB };
