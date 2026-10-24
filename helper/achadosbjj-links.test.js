// node --test helper/
const test = require("node:test");
const assert = require("node:assert/strict");
const { extractShortLinks, pairBatch, normalizeBase } = require("./achadosbjj-links.user.js");

test("extrai os links curtos na ordem em que aparecem, sem repetir", () => {
  const text = "Produto A https://meli.la/AAA111 copiar\nProduto B https://meli.la/BBB222 copiar\nlink de A de novo: https://meli.la/AAA111";
  assert.deepEqual(extractShortLinks(text), ["https://meli.la/AAA111", "https://meli.la/BBB222"]);
});

test("ignora links que não são curtos do ML", () => {
  const text = "https://www.mercadolivre.com.br/social/on_lancer?ref=abc http://meli.la/HTTP https://outro.com/meli.la/x";
  assert.deepEqual(extractShortLinks(text), []);
});

test("pareia pela ordem quando a quantidade bate", () => {
  const batch = [{ id: 7 }, { id: 3 }];
  const result = pairBatch(batch, ["https://meli.la/A", "https://meli.la/B"]);
  assert.deepEqual(result, {
    ok: true,
    pairs: [
      { id: 7, url: "https://meli.la/A" },
      { id: 3, url: "https://meli.la/B" },
    ],
  });
});

test("recusa quando a quantidade não bate (nem a mais, nem a menos)", () => {
  const batch = [{ id: 1 }, { id: 2 }];
  assert.equal(pairBatch(batch, ["https://meli.la/A"]).ok, false);
  assert.equal(pairBatch(batch, ["https://meli.la/A", "https://meli.la/B", "https://meli.la/C"]).ok, false);
  assert.equal(pairBatch(batch, []).ok, false);
});

test("endereço do painel sem barra no fim", () => {
  assert.equal(normalizeBase(" https://achados.tibiascope.com/// "), "https://achados.tibiascope.com");
});
