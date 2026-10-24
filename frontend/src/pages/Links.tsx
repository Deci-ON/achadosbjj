import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { ApiError, api, type PendingItem, type PendingResponse } from "../api";
import { CopyButton, ErrorBox, Spinner } from "../components";
import { money } from "../format";

const LINK_BUILDER = "https://www.mercadolivre.com.br/afiliados/linkbuilder";
const BATCH_KEY = "achadosbjj.batch";
const BATCH_SIZE = 20;

// O lote fica guardado na aba: se você recarregar entre copiar as URLs e colar os links, o pareamento
// continua sendo com as URLs que foram de fato para o gerador, não com a fila de agora.
function loadBatch(): PendingItem[] | null {
  try {
    const raw = sessionStorage.getItem(BATCH_KEY);
    return raw ? (JSON.parse(raw) as PendingItem[]) : null;
  } catch {
    return null;
  }
}

function saveBatch(batch: PendingItem[] | null): void {
  try {
    if (batch) sessionStorage.setItem(BATCH_KEY, JSON.stringify(batch));
    else sessionStorage.removeItem(BATCH_KEY);
  } catch {
    /* sem armazenamento: vale só enquanto a página estiver aberta */
  }
}

export function Links() {
  const [pending, setPending] = useState<PendingResponse | null>(null);
  const [batch, setBatch] = useState<PendingItem[] | null>(loadBatch);
  const [pasted, setPasted] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState<number | null>(null);

  useEffect(() => {
    api
      .pendingLinks(BATCH_SIZE)
      .then(setPending)
      .catch((exc: Error) => setError(exc.message));
  }, [done]);

  const lines = useMemo(
    () =>
      pasted
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean),
    [pasted],
  );
  const looksLikeLinks = lines.every((line) => /^https:\/\/\S+$/.test(line));
  const countsMatch = batch !== null && lines.length === batch.length;
  const canSave = batch !== null && lines.length > 0 && countsMatch && looksLikeLinks && !saving;

  function startBatch() {
    if (!pending) return;
    setBatch(pending.items);
    saveBatch(pending.items);
    setPasted("");
    setDone(null);
    setError("");
  }

  function reset() {
    setBatch(null);
    saveBatch(null);
    setPasted("");
    setError("");
  }

  async function save() {
    if (!batch) return;
    setSaving(true);
    setError("");
    try {
      const result = await api.saveLinks(batch.map((item, i) => ({ id: item.id, url: lines[i] })));
      setDone(result.saved);
      reset();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar os links.");
    } finally {
      setSaving(false);
    }
  }

  if (!pending && !error) return <Spinner />;

  const urlsText = (batch ?? pending?.items ?? []).map((item) => item.url).join("\n");

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Gerar links de afiliado</h1>

      {done !== null && (
        <p className="rounded-md bg-emerald-50 p-3 text-sm text-emerald-800 ring-1 ring-emerald-200">
          {done} {done === 1 ? "link salvo" : "links salvos"}. As ofertas já estão prontas para enviar.{" "}
          <Link to="/ofertas" className="font-medium underline">
            Ver prontas
          </Link>
        </p>
      )}
      {error && <ErrorBox message={error} />}

      {pending && pending.total === 0 && batch === null ? (
        <p className="rounded-md bg-white p-6 text-center text-sm text-slate-600 ring-1 ring-slate-200">
          Nenhuma oferta aguarda link agora. Quando o sistema aprender que um produto está em oferta de verdade, ele aparece aqui.
          {pending.below_cut > 0 && (
            <>
              <br />
              {pending.below_cut} {pending.below_cut === 1 ? "oferta ficou" : "ofertas ficaram"} abaixo do corte de pontuação ({pending.min_score}).
            </>
          )}
        </p>
      ) : (
        <>
          <p className="text-sm text-slate-600">
            O ML protege a criação de links com reCAPTCHA, então esta parte é sua, mas é <b>uma colagem para o lote inteiro</b>:
          </p>

          <section className="rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">1. Copie as URLs</h2>
            {batch === null ? (
              <>
                <p className="mb-3 text-sm text-slate-600">
                  {pending?.total} {pending?.total === 1 ? "oferta aguarda" : "ofertas aguardam"} link. Cada lote leva até {BATCH_SIZE}, da melhor para a pior.
                </p>
                {pending && pending.below_cut > 0 && (
                  <p className="mb-3 text-xs text-slate-500">
                    Só entram ofertas com pontuação {pending.min_score} ou mais. {pending.below_cut}{" "}
                    {pending.below_cut === 1 ? "oferta ficou" : "ofertas ficaram"} de fora: dá para colar o link à mão na página do produto.
                  </p>
                )}
                <button
                  type="button"
                  onClick={startBatch}
                  className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
                >
                  Montar lote de {Math.min(pending?.total ?? 0, BATCH_SIZE)}
                </button>
              </>
            ) : (
              <>
                <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-sm">
                  {batch.map((item) => (
                    <li key={item.id}>
                      <span className="font-medium">{item.title}</span> <span className="text-slate-500">{money(item.price)}</span>
                    </li>
                  ))}
                </ol>
                <div className="flex flex-wrap items-center gap-2">
                  <CopyButton text={urlsText} label={`Copiar ${batch.length} URLs`} />
                  <button type="button" onClick={reset} className="text-sm text-slate-600 underline">
                    Recomeçar
                  </button>
                </div>
              </>
            )}
          </section>

          <section className={`rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200 ${batch === null ? "opacity-50" : ""}`}>
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">2. No gerador do ML</h2>
            <p className="text-sm text-slate-600">
              Abra o{" "}
              <a className="font-medium text-slate-900 underline" href={LINK_BUILDER} target="_blank" rel="noreferrer">
                gerador de links
              </a>
              , cole as URLs (uma por linha), clique em <b>Gerar</b>, deixe em <b>Links curtos</b> e clique em <b>Copiar todos</b>.
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Os links voltam na mesma ordem das URLs. Cada link gerado entra na sua lista pública "Minhas recomendações".
            </p>
          </section>

          <section className={`rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200 ${batch === null ? "opacity-50" : ""}`}>
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">3. Cole os links aqui</h2>
            <textarea
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              disabled={batch === null}
              rows={Math.min(Math.max(lines.length, 3), 10)}
              placeholder={batch === null ? "Monte o lote primeiro" : "https://meli.la/…"}
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-xs outline-none focus:border-slate-900 disabled:bg-slate-50"
            />

            {batch !== null && lines.length > 0 && (
              <>
                {!countsMatch && (
                  <p className="mt-2 text-sm text-rose-700">
                    Você colou {lines.length} {lines.length === 1 ? "link" : "links"}, mas o lote tem {batch.length}. Confira antes de salvar: o
                    pareamento é pela ordem.
                  </p>
                )}
                {!looksLikeLinks && <p className="mt-2 text-sm text-rose-700">Tem linha que não é um link https.</p>}
                {countsMatch && looksLikeLinks && (
                  <div className="mt-3 overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <thead className="uppercase text-slate-500">
                        <tr>
                          <th className="py-1 pr-3">#</th>
                          <th className="py-1 pr-3">Produto</th>
                          <th className="py-1">Link</th>
                        </tr>
                      </thead>
                      <tbody>
                        {batch.map((item, i) => (
                          <tr key={item.id} className="border-t border-slate-100">
                            <td className="py-1 pr-3">{i + 1}</td>
                            <td className="py-1 pr-3">{item.title}</td>
                            <td className="py-1 font-mono">{lines[i]}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}

            <button
              type="button"
              onClick={save}
              disabled={!canSave}
              className="mt-3 rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-40"
            >
              {saving ? "Salvando…" : batch ? `Salvar ${batch.length} ${batch.length === 1 ? "link" : "links"}` : "Salvar"}
            </button>
          </section>
        </>
      )}
    </div>
  );
}
