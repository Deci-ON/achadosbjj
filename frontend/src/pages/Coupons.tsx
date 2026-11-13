import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, api, type Coupon, type CouponKind, type CouponStore } from "../api";
import { ErrorBox, Spinner } from "../components";
import { COUPON_KIND_LABEL, COUPON_STORE_LABEL, dateLabel } from "../format";

const STORES: CouponStore[] = ["mercadolivre", "shopee", "all"];
const KINDS: CouponKind[] = ["code", "page", "shipping"];

const inputClass = "w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-slate-900";

function CouponForm({ onCreated }: { onCreated: () => void }) {
  const [store, setStore] = useState<CouponStore>("mercadolivre");
  const [kind, setKind] = useState<CouponKind>("code");
  const [title, setTitle] = useState("");
  const [rule, setRule] = useState("");
  const [date, setDate] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const canSave = title.trim() !== "" && date !== "" && !busy;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSave) return;
    setBusy(true);
    setError("");
    try {
      await api.createCoupon({
        store,
        kind,
        title: title.trim(),
        rule: rule.trim(),
        expires_at: `${date}T23:59:59-03:00`, // vale até o fim do dia, horário de Brasília
        is_default: isDefault,
      });
      setTitle("");
      setRule("");
      setDate("");
      setIsDefault(false);
      onCreated();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar o cupom.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg bg-white p-4 ring-1 ring-slate-200">
      <h2 className="font-semibold">Novo cupom</h2>
      {error && <ErrorBox message={error} />}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm text-slate-600">
          Loja
          <select value={store} onChange={(e) => setStore(e.target.value as CouponStore)} className={`mt-1 ${inputClass}`}>
            {STORES.map((s) => (
              <option key={s} value={s}>
                {s === "all" ? "Ambas" : COUPON_STORE_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-slate-600">
          Tipo
          <select value={kind} onChange={(e) => setKind(e.target.value as CouponKind)} className={`mt-1 ${inputClass}`}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {COUPON_KIND_LABEL[k]}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-slate-600">
          Nome ou código
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={120} placeholder="ex.: JIU10" className={`mt-1 ${inputClass}`} />
        </label>
        <label className="block text-sm text-slate-600">
          Validade
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} required className={`mt-1 ${inputClass}`} />
        </label>
      </div>
      <label className="block text-sm text-slate-600">
        Regra (opcional)
        <input
          value={rule}
          onChange={(e) => setRule(e.target.value)}
          maxLength={200}
          placeholder="ex.: compra mínima R$ 100, até R$ 30 off"
          className={`mt-1 ${inputClass}`}
        />
      </label>
      <label className="flex items-center gap-2 text-sm text-slate-700">
        <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
        Usar como padrão da loja
      </label>
      <button
        type="submit"
        disabled={!canSave}
        className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {busy ? "Salvando…" : "Salvar cupom"}
      </button>
    </form>
  );
}

function CouponRow({ coupon, onChanged, onError }: { coupon: Coupon; onChanged: () => void; onError: (message: string) => void }) {
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<unknown>, fallback: string) {
    setBusy(true);
    onError("");
    try {
      await action();
      onChanged();
    } catch (exc) {
      onError(exc instanceof ApiError ? exc.message : fallback);
    } finally {
      setBusy(false);
    }
  }

  function remove() {
    if (!window.confirm(`Excluir o cupom "${coupon.title}"? Ele sai de todas as ofertas.`)) return;
    void run(() => api.deleteCoupon(coupon.id), "Não consegui excluir.");
  }

  return (
    <li className={`flex flex-wrap items-start justify-between gap-3 rounded-lg bg-white p-3 ring-1 ring-slate-200 ${coupon.active ? "" : "opacity-60"}`}>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{coupon.title}</span>
          {coupon.is_default && (
            <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800 ring-1 ring-inset ring-emerald-200">padrão</span>
          )}
          {!coupon.active && (
            <span className="rounded-full bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-inset ring-rose-200">vencido</span>
          )}
        </div>
        <p className="text-xs text-slate-500">
          {COUPON_STORE_LABEL[coupon.store] ?? coupon.store} · {COUPON_KIND_LABEL[coupon.kind] ?? coupon.kind} · vale até {dateLabel(coupon.expires_at)}
        </p>
        {coupon.rule && <p className="mt-0.5 text-sm text-slate-700">{coupon.rule}</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void run(() => api.updateCoupon(coupon.id, { is_default: !coupon.is_default }), "Não consegui salvar.")}
          className="rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50 disabled:opacity-50"
        >
          {coupon.is_default ? "Tirar padrão" : "Tornar padrão"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={remove}
          className="rounded-md px-3 py-1.5 text-sm font-medium text-rose-700 ring-1 ring-rose-200 hover:bg-rose-50 disabled:opacity-50"
        >
          Excluir
        </button>
      </div>
    </li>
  );
}

export function Coupons() {
  const [coupons, setCoupons] = useState<Coupon[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .coupons()
      .then((list) => (setCoupons(list), setError("")))
      .catch((exc: Error) => setError(exc.message));
  }, []);

  useEffect(load, [load]);

  return (
    <div className="space-y-4">
      <section className="space-y-1 rounded-lg bg-white p-4 text-sm text-slate-700 ring-1 ring-slate-200">
        <h1 className="font-semibold">Cupons</h1>
        <p>
          O cupom é cadastrado à mão: nem o Mercado Livre nem a Shopee têm API de cupom. Quem aplica é o comprador, no app ou no checkout; o cupom não vai
          dentro do link. Ele aparece como uma linha na mensagem da oferta, antes do link, e <b>não muda preço nem pontuação</b>.
        </p>
        <p>
          Os cupons marcados como <b>padrão</b> de uma loja (pode haver mais de um) entram em toda mensagem daquela loja até vencerem. Os outros você anexa oferta por oferta, na aba
          Ofertas.
        </p>
      </section>

      <CouponForm onCreated={load} />

      {error && <ErrorBox message={error} />}
      {!coupons && !error && <Spinner />}
      {coupons && coupons.length === 0 && (
        <p className="rounded-md bg-white p-6 text-center text-sm text-slate-600 ring-1 ring-slate-200">Nenhum cupom cadastrado ainda.</p>
      )}
      {coupons && coupons.length > 0 && (
        <ul className="space-y-2">
          {coupons.map((c) => (
            <CouponRow key={c.id} coupon={c} onChanged={load} onError={setError} />
          ))}
        </ul>
      )}
    </div>
  );
}
