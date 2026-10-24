// ==UserScript==
// @name         achadosbjj - links de afiliado do ML
// @namespace    https://github.com/Deci-ON/achadosbjj
// @version      0.1.0
// @description  Preenche o gerador de links do Mercado Livre com o lote do achadosbjj e devolve os links gerados ao painel. Quem clica em Gerar é você.
// @match        https://www.mercadolivre.com.br/afiliados/linkbuilder*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      localhost
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

/*
 * O que faz, e o que NÃO faz:
 *  - busca no painel a fila de ofertas sem link (GET /api/affiliate/pending) e cola as URLs no gerador;
 *  - NÃO clica em Gerar e não mexe no reCAPTCHA: o clique em Gerar é seu;
 *  - depois que os links aparecem, lê os `https://meli.la/...` na ordem em que o gerador os mostra e,
 *    se a quantidade bate com o lote, mostra a tabela produto <-> link e espera o seu clique em Salvar
 *    (POST /api/affiliate/links; o servidor ainda recusa o lote inteiro se algo não fechar).
 *  - o token do painel fica no armazenamento do Tampermonkey, que a página do ML não lê.
 * Se o ML pedir verificação ou o layout mudar, o script simplesmente não acha os links e para: o fluxo
 * manual do painel (tela "Gerar links") continua valendo.
 */

const SHORT_LINK = /https:\/\/meli\.la\/[A-Za-z0-9_-]+/g;
const MAX_LOTE = 20;

/** Os links curtos na ordem em que aparecem, sem repetir. */
function extractShortLinks(text) {
  const seen = new Set();
  const links = [];
  for (const match of text.match(SHORT_LINK) ?? []) {
    if (!seen.has(match)) {
      seen.add(match);
      links.push(match);
    }
  }
  return links;
}

/** Só pareia se a quantidade bate: o pareamento é pela ordem, então qualquer diferença é desalinhamento. */
function pairBatch(batch, links) {
  if (links.length !== batch.length) {
    return { ok: false, reason: `o gerador mostrou ${links.length} link(s) curto(s) e o lote tem ${batch.length}` };
  }
  return { ok: true, pairs: batch.map((item, i) => ({ id: item.id, url: links[i] })) };
}

function normalizeBase(value) {
  return value.trim().replace(/\/+$/, "");
}

if (typeof module !== "undefined") {
  module.exports = { extractShortLinks, pairBatch, normalizeBase, MAX_LOTE };
} else {
  main();
}

function main() {
  const KEY = { base: "base", token: "token", batch: "batch" };
  let batch = readJson(GM_getValue(KEY.batch, ""));
  let found = [];
  let busy = false;
  let message = "";

  function readJson(raw) {
    try {
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function configure() {
    const base = prompt("Endereço do painel achadosbjj (com o túnel SSH aberto: http://localhost:8001):", GM_getValue(KEY.base, "http://localhost:8001"));
    if (base === null) return;
    const token = prompt("ADMIN_TOKEN do painel (fica só no Tampermonkey):", "");
    if (token === null) return;
    GM_setValue(KEY.base, normalizeBase(base));
    if (token.trim()) GM_setValue(KEY.token, token.trim());
    say("Configurado.");
  }
  GM_registerMenuCommand("achadosbjj: configurar painel e token", configure);

  function api(method, path, body) {
    const base = GM_getValue(KEY.base, "");
    const token = GM_getValue(KEY.token, "");
    if (!base || !token) return Promise.reject(new Error("configure o painel e o token (menu do Tampermonkey > achadosbjj)"));
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: base + path,
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        data: body ? JSON.stringify(body) : undefined,
        timeout: 20000,
        onload: (res) => {
          let parsed = null;
          try {
            parsed = JSON.parse(res.responseText);
          } catch {
            /* corpo não é JSON */
          }
          if (res.status >= 200 && res.status < 300) resolve(parsed);
          else reject(new Error(typeof parsed?.detail === "string" ? parsed.detail : `erro ${res.status}`));
        },
        onerror: () => reject(new Error("não consegui falar com o painel")),
        ontimeout: () => reject(new Error("o painel demorou demais")),
      });
    });
  }

  function textarea() {
    return document.querySelector("#url-0") ?? document.querySelector("textarea.andes-form-control__field");
  }

  function fill(value) {
    const field = textarea();
    if (!field) return false;
    // O campo é controlado pelo framework da página: o setter nativo + evento "input" é o que ele enxerga.
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  }

  function pageLinks() {
    const hrefs = [...document.querySelectorAll("a[href]")].map((a) => a.href).join("\n");
    return extractShortLinks(document.body.innerText + "\n" + hrefs);
  }

  async function fetchBatch() {
    busy = true;
    say("Buscando a fila no painel…");
    try {
      const pending = await api("GET", `/api/affiliate/pending?limit=${MAX_LOTE}`);
      if (!pending.items.length) {
        batch = null;
        GM_setValue(KEY.batch, "");
        say(pending.below_cut ? `Fila vazia. ${pending.below_cut} oferta(s) ficaram abaixo do corte de pontuação (${pending.min_score}).` : "Nenhuma oferta aguarda link.");
        return;
      }
      batch = pending.items.map(({ id, title, url, price }) => ({ id, title, url, price }));
      GM_setValue(KEY.batch, JSON.stringify(batch));
      found = [];
      if (!fill(batch.map((item) => item.url).join("\n"))) {
        say("Não achei o campo de URLs do gerador (o layout mudou?). Use a tela Gerar links do painel.");
        return;
      }
      say(`${batch.length} URL(s) coladas. Agora clique em Gerar (deixe em Links curtos).`);
    } catch (exc) {
      say(`Falhou: ${exc.message}`);
    } finally {
      busy = false;
      render();
    }
  }

  async function save() {
    const result = pairBatch(batch, found);
    if (!result.ok) {
      say(result.reason);
      return;
    }
    busy = true;
    say("Salvando no painel…");
    try {
      const saved = await api("POST", "/api/affiliate/links", { items: result.pairs });
      batch = null;
      found = [];
      GM_setValue(KEY.batch, "");
      say(`${saved.saved} link(s) salvos. As ofertas já estão prontas para enviar no painel.`);
    } catch (exc) {
      say(`O painel recusou o lote: ${exc.message}`);
    } finally {
      busy = false;
      render();
    }
  }

  function cancel() {
    batch = null;
    found = [];
    GM_setValue(KEY.batch, "");
    say("Lote descartado.");
  }

  // --- interface (shadow DOM: o CSS do ML não mexe nela e a página não lê o que há dentro) -----------------

  const host = document.createElement("div");
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
  const root = host.attachShadow({ mode: "closed" });
  document.body.appendChild(host);

  function say(text) {
    message = text;
    render();
  }

  function el(tag, props = {}, ...children) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  }

  function render() {
    const result = batch && found.length ? pairBatch(batch, found) : null;
    const box = el("div", {});
    box.style.cssText =
      "font:13px/1.4 system-ui,sans-serif;background:#0f172a;color:#f8fafc;border-radius:10px;padding:12px;width:340px;box-shadow:0 8px 24px rgba(0,0,0,.35);max-height:70vh;overflow:auto;";
    box.append(el("div", { textContent: "achadosbjj" }));
    box.firstChild.style.cssText = "font-weight:700;margin-bottom:6px;";
    if (message) {
      const note = el("div", { textContent: message });
      note.style.cssText = "margin-bottom:8px;color:#cbd5e1;";
      box.append(note);
    }

    if (batch && found.length && result && !result.ok) {
      box.append(el("div", { textContent: `Atenção: ${result.reason}. Confira se está em "Links curtos" e se todos foram gerados.` }));
      box.lastChild.style.cssText = "margin-bottom:8px;color:#fda4af;";
    }
    if (batch && result?.ok) {
      const list = el("ol", {});
      list.style.cssText = "margin:0 0 8px 18px;padding:0;font-size:12px;";
      batch.forEach((item, i) => {
        const line = el("li", {}, el("b", { textContent: item.title }), el("div", { textContent: found[i] }));
        line.lastChild.style.cssText = "color:#94a3b8;word-break:break-all;";
        list.append(line);
      });
      box.append(list);
    }

    const row = el("div", {});
    row.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;";
    const button = (label, onClick, disabled, color = "#334155") => {
      const b = el("button", { textContent: label, onclick: onClick, disabled });
      b.style.cssText = `background:${color};color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer;opacity:${disabled ? 0.4 : 1};`;
      return b;
    };
    if (!batch) row.append(button("Buscar lote", fetchBatch, busy));
    if (batch && result?.ok) row.append(button(`Salvar ${batch.length} link(s)`, save, busy, "#047857"));
    if (batch) row.append(button("Descartar lote", cancel, busy));
    box.append(row);
    root.replaceChildren(box);
  }

  // O gerador mostra os links só depois do seu clique em Gerar: observa a página e confere o lote.
  let timer = null;
  new MutationObserver(() => {
    if (!batch) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      const links = pageLinks();
      if (links.length !== found.length || links.some((link, i) => link !== found[i])) {
        found = links;
        render();
      }
    }, 400);
  }).observe(document.body, { childList: true, subtree: true });

  if (batch) say(`Lote de ${batch.length} em andamento: clique em Gerar se ainda não gerou.`);
  else render();
}
