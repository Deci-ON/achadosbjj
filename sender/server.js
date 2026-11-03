// API HTTP do robô: GET /status, GET /groups, POST /send. Tudo com `Authorization: Bearer <SENDER_TOKEN>`.
// A lógica de HTTP fica aqui, separada do WhatsApp, para testar sem navegador (ver server.test.js).

const http = require("node:http");
const crypto = require("node:crypto");

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function tokenOk(header, token) {
  const [scheme, value = ""] = (header || "").split(" ");
  if (scheme.toLowerCase() !== "bearer" || !token) return false;
  const a = Buffer.from(value);
  const b = Buffer.from(token);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function readJson(req, limit = 64 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new HttpError(413, "corpo grande demais"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {});
      } catch {
        reject(new HttpError(400, "JSON inválido"));
      }
    });
    req.on("error", reject);
  });
}

// `bot` precisa de: status() -> {state, qr}, groups() -> [{id,name,participants}], send({chatId, text, imageUrl}).
function createServer(bot, token) {
  return http.createServer(async (req, res) => {
    const reply = (status, body) => {
      res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(body));
    };
    try {
      const path = new URL(req.url, "http://x").pathname;
      if (path === "/health") return reply(200, { ok: true });
      if (!tokenOk(req.headers.authorization, token)) throw new HttpError(401, "token inválido");

      if (req.method === "GET" && path === "/status") return reply(200, await bot.status());
      if (req.method === "GET" && path === "/groups") return reply(200, await bot.groups());
      if (req.method === "POST" && path === "/send") {
        const body = await readJson(req);
        if (typeof body.chat_id !== "string" || !body.chat_id.endsWith("@g.us")) throw new HttpError(422, "chat_id deve ser um grupo (@g.us)");
        if (typeof body.text !== "string" || !body.text.trim()) throw new HttpError(422, "text vazio");
        await bot.send({ chatId: body.chat_id, text: body.text, imageUrl: body.image_url || null });
        return reply(200, { ok: true });
      }
      throw new HttpError(404, "rota desconhecida");
    } catch (err) {
      const status = err instanceof HttpError ? err.status : err.status || 502;
      reply(status, { error: err.message });
    }
  });
}

module.exports = { createServer, HttpError, tokenOk };
