import { useState, type FormEvent } from "react";
import { ApiError, api, type ProductDetail } from "./api";
import { CopyButton } from "./components";

const ORIGIN_LABEL: Record<NonNullable<ProductDetail["affiliate_origin"]>, string> = {
  manual: "colado por você",
  api: "veio da API da loja",
  template: "montado pelo modelo ML_AFFILIATE_TEMPLATE (não confirmado)",
};

export function AffiliateCard({ product, onChange }: { product: ProductDetail; onChange: (p: ProductDetail) => void }) {
  // Um link montado pelo modelo não vira texto do campo: o que se salva é o que o painel do ML gerou.
  const [value, setValue] = useState(product.affiliate_origin === "template" ? "" : (product.affiliate_url ?? ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  async function save(next: string | null) {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const updated = await api.setAffiliateLink(product.id, next);
      onChange(updated);
      setValue(updated.affiliate_origin === "template" ? "" : (updated.affiliate_url ?? ""));
      setSaved(true);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar o link.");
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void save(value.trim() || null);
  }

  const isMl = product.source === "mercadolivre";
  return (
    <section className="rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Link de afiliado</h2>

      {isMl ? (
        <ol className="mb-3 list-decimal space-y-1 pl-5 text-sm text-slate-600">
          <li>
            Copie a URL do produto:{" "}
            <span className="inline-flex items-center gap-2">
              <code className="max-w-[16rem] truncate rounded bg-slate-100 px-1.5 py-0.5 text-xs">{product.url}</code>
              <CopyButton text={product.url} label="Copiar URL" />
            </span>
          </li>
          <li>No painel de afiliados do Mercado Livre (Ferramentas, Gerador de link), gere o link curto.</li>
          <li>Cole o link abaixo e salve. Ele vai na mensagem e a coleta não o apaga.</li>
        </ol>
      ) : (
        <p className="mb-3 text-sm text-slate-600">Nesta loja o link de afiliado vem da API; você também pode colar um por aqui.</p>
      )}

      <form onSubmit={onSubmit} className="flex flex-wrap gap-2">
        <input
          type="url"
          value={value}
          onChange={(e) => (setValue(e.target.value), setSaved(false))}
          placeholder="https://mercadolivre.com/sec/…"
          className="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-slate-900"
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
        >
          {busy ? "Salvando…" : "Salvar"}
        </button>
        {product.affiliate_origin === "manual" && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void save(null)}
            className="rounded-md px-3 py-1.5 text-sm font-medium text-rose-700 ring-1 ring-rose-200 hover:bg-rose-50 disabled:opacity-40"
          >
            Remover
          </button>
        )}
      </form>

      {error && <p className="mt-2 text-sm text-rose-700">{error}</p>}
      {saved && !error && <p className="mt-2 text-sm text-emerald-700">Salvo. A mensagem abaixo já usa o novo link.</p>}

      <p className="mt-3 text-sm text-slate-600">
        {product.affiliate_url && product.affiliate_origin ? (
          <>
            Link na mensagem: <span className="break-all underline">{product.affiliate_url}</span>{" "}
            <span className="text-slate-500">({ORIGIN_LABEL[product.affiliate_origin]})</span>
          </>
        ) : (
          <span className="text-amber-700">Ainda sem link de afiliado: a mensagem usa o link normal do produto, sem comissão.</span>
        )}
      </p>
    </section>
  );
}
