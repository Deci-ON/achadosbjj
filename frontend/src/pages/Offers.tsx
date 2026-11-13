import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ApiError, api, type Coupon, type Offer, type OffersResponse, type Status } from "../api";
import { CopyButton, ErrorBox, PendingLinksBanner, ScoreBadge, SendToBotButton, SentToggle, SizeChips, Spinner, StatusBadge } from "../components";
import { COUPON_KIND_LABEL, SOURCE_LABEL, STATUS_HINT, STATUS_LABEL, dateLabel, dateTimeLabel, money, relative } from "../format";

type Tab = Status | "ready";

const TABS: Tab[] = ["ready", "offer", "learning", "no_offer", "irrelevant"];
const TAB_LABEL: Record<Tab, string> = { ready: "Prontas para enviar", ...STATUS_LABEL };
const TAB_HINT: Record<Tab, string> = {
  ready: "Oferta de verdade, com link de afiliado e ainda não enviada",
  ...STATUS_HINT,
};

function isTab(value: string | null): value is Tab {
  return TABS.includes(value as Tab);
}

const SOURCES = Object.keys(SOURCE_LABEL);

/** Anexa/tira cupons da oferta. Só entram os ativos da loja da oferta (ou "Ambas"); sem nenhum, convida a cadastrar. */
function CouponPicker({ offer, coupons, onChanged }: { offer: Offer; coupons: Coupon[]; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const options = coupons.filter((c) => c.active && (c.store === offer.source || c.store === "all"));
  const attached = offer.attached_coupon_ids ?? [];

  if (options.length === 0) {
    return (
      <Link to="/cupons" className="self-center text-xs text-slate-500 underline hover:text-slate-800">
        Cadastrar cupom
      </Link>
    );
  }

  async function toggle(id: number, checked: boolean) {
    setBusy(true);
    setError("");
    try {
      const next = checked ? [...attached, id] : attached.filter((x) => x !== id);
      await api.setProductCoupons(offer.id, next);
      onChanged();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar o cupom.");
    } finally {
      setBusy(false);
    }
  }

  const count = options.filter((c) => attached.includes(c.id)).length;
  return (
    <details className="relative">
      <summary className="cursor-pointer list-none rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50">
        Cupom{count > 0 ? ` (${count})` : ""} ▾
      </summary>
      <div className="absolute left-0 z-10 mt-1 w-72 space-y-1 rounded-md bg-white p-2 shadow-lg ring-1 ring-slate-200">
        {options.map((c) => (
          <label key={c.id} className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={attached.includes(c.id)}
              disabled={busy}
              onChange={(e) => void toggle(c.id, e.target.checked)}
            />
            <span>
              {c.title}
              {c.is_default && <span className="ml-1 text-xs text-emerald-700">padrão</span>}
              <span className="block text-xs text-slate-500">
                {COUPON_KIND_LABEL[c.kind] ?? c.kind} · até {dateLabel(c.expires_at)}
              </span>
            </span>
          </label>
        ))}
        {error && <p className="text-xs text-rose-700">{error}</p>}
      </div>
    </details>
  );
}

function OfferCard({ offer, canSend, coupons, onChanged }: { offer: Offer; canSend: boolean; coupons: Coupon[]; onChanged: () => void }) {
  const drop = offer.real_discount_pct ?? 0;
  return (
    <li className="flex gap-3 rounded-lg bg-white p-3 shadow-sm ring-1 ring-slate-200">
      {offer.image_url ? (
        <img src={offer.image_url} alt="" loading="lazy" className="size-20 shrink-0 rounded-md object-contain" />
      ) : (
        <div className="size-20 shrink-0 rounded-md bg-slate-100" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={offer.status} label={STATUS_LABEL[offer.status]} />
          <ScoreBadge score={offer.score} />
          <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-300">
            {SOURCE_LABEL[offer.source] ?? offer.source}
          </span>
          {offer.affiliate_url ? (
            <span className="text-xs text-emerald-700">link de afiliado ✓</span>
          ) : (
            offer.needs_link && <span className="text-xs text-amber-700">aguardando link de afiliado</span>
          )}
          {offer.sent_at && <span className="text-xs text-slate-500">enviada {relative(offer.sent_at)}</span>}
        </div>
        <Link to={`/produto/${offer.id}`} className="mt-1 block truncate font-medium hover:underline" title={offer.title}>
          {offer.title}
        </Link>
        <p className="mt-0.5 text-sm">
          <span className="text-lg font-semibold">{money(offer.price)}</span>
          {offer.status === "offer" && offer.reference_price != null && (
            <span className="ml-2 text-slate-600">
              <b className="text-emerald-700">−{drop}%</b> sobre {money(offer.reference_price)} (preço normal)
            </span>
          )}
          {offer.claimed_discount_pct != null && (
            <span className="ml-2 text-xs text-slate-500" title="Desconto que o vendedor declara; não é confiável">
              vendedor diz −{offer.claimed_discount_pct}%
            </span>
          )}
        </p>
        <p className="text-xs text-slate-500">
          {offer.reasons[0]} · {offer.observed_days} dias de histórico · visto {dateTimeLabel(offer.last_seen_at)}
        </p>
        <SizeChips variants={offer.variants} />
        {offer.coupons && offer.coupons.length > 0 && (
          <ul className="mt-1.5 flex flex-wrap gap-1.5" aria-label="Cupons na mensagem">
            {offer.coupons.map((c) => (
              <li
                key={c.id}
                title={COUPON_KIND_LABEL[c.kind] ?? c.kind}
                className="inline-flex items-center rounded-md bg-violet-50 px-1.5 py-0.5 text-xs font-medium text-violet-800 ring-1 ring-inset ring-violet-200"
              >
                🏷 {c.title}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-2 flex flex-wrap items-start gap-2">
          {canSend && offer.ready && <SendToBotButton offer={offer} onChanged={onChanged} />}
          <CopyButton text={offer.message} />
          <CouponPicker offer={offer} coupons={coupons} onChanged={onChanged} />
          {offer.status === "offer" && <SentToggle offer={offer} onChanged={onChanged} />}
          <a
            href={offer.affiliate_url ?? offer.url}
            target="_blank"
            rel="noreferrer"
            className="rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50"
          >
            Abrir no site
          </a>
          <Link to={`/produto/${offer.id}`} className="rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50">
            Histórico
          </Link>
        </div>
      </div>
    </li>
  );
}

export function Offers() {
  const [params, setParams] = useSearchParams();
  const tab: Tab = isTab(params.get("status")) ? (params.get("status") as Tab) : "ready";
  const q = params.get("q") ?? "";
  const source = SOURCES.includes(params.get("source") ?? "") ? (params.get("source") as string) : "";
  const minScore = Number(params.get("min") ?? 0) || 0;

  const [search, setSearch] = useState(q);
  const [data, setData] = useState<OffersResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [canSend, setCanSend] = useState(false); // robô de envio configurado (SENDER_URL)
  const [coupons, setCoupons] = useState<Coupon[]>([]); // carregados uma vez; os cards só filtram

  useEffect(() => {
    api
      .coupons()
      .then(setCoupons)
      .catch(() => setCoupons([])); // sem cupons a lista de ofertas funciona igual
  }, []);

  useEffect(() => {
    api
      .sender()
      .then((s) => setCanSend(s.configured))
      .catch(() => setCanSend(false));
  }, []);

  function update(next: Record<string, string>) {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value) merged.set(key, value);
      else merged.delete(key);
    }
    setParams(merged, { replace: true });
  }

  // A busca espera o usuário parar de digitar antes de ir ao servidor.
  useEffect(() => {
    const timer = setTimeout(() => search !== q && update({ q: search }), 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .offers({ status: tab, source, q, minScore })
      .then((body) => !cancelled && (setData(body), setError("")))
      .catch((exc: Error) => !cancelled && setError(exc.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [tab, source, q, minScore, reloadKey]);

  const count = (t: Tab) => (t === "ready" ? data?.ready : data?.counts[t]) ?? "";

  return (
    <div className="space-y-4">
      {data && <PendingLinksBanner pending={data.pending_link} />}

      <div className="flex flex-wrap gap-2" role="tablist">
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={t === tab}
            title={TAB_HINT[t]}
            onClick={() => update({ status: t === "ready" ? "" : t })}
            className={`rounded-full px-3 py-1 text-sm font-medium ring-1 ring-inset ${
              t === tab ? "bg-slate-900 text-white ring-slate-900" : "bg-white text-slate-700 ring-slate-300 hover:bg-slate-50"
            }`}
          >
            {TAB_LABEL[t]} <span className="opacity-70">{count(t)}</span>
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Buscar no título (ex.: koral azul)"
          className="w-full max-w-sm rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-slate-900"
        />
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Loja
          <select
            value={source}
            onChange={(e) => update({ source: e.target.value })}
            className="rounded-md border border-slate-300 bg-white px-2 py-1"
          >
            <option value="">Todas</option>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {SOURCE_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Score mínimo
          <select
            value={minScore}
            onChange={(e) => update({ min: e.target.value === "0" ? "" : e.target.value })}
            className="rounded-md border border-slate-300 bg-white px-2 py-1"
          >
            {[0, 40, 60, 80].map((n) => (
              <option key={n} value={n}>
                {n === 0 ? "qualquer" : `${n}+`}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && !data ? (
        <Spinner />
      ) : data && data.items.length === 0 ? (
        <p className="rounded-md bg-white p-6 text-center text-sm text-slate-600 ring-1 ring-slate-200">
          {tab === "ready" && data.pending_link > 0
            ? `Nenhuma pronta ainda: ${data.pending_link} oferta(s) esperam o link de afiliado (use "Gerar links").`
            : tab === "ready" && data.counts.learning > 0
              ? `Nenhuma oferta ainda: ${data.counts.learning} produtos estão aprendendo (precisam de uns 7 dias de histórico).`
              : tab === "offer" && data.counts.learning > 0
                ? `Nenhuma oferta ainda: ${data.counts.learning} produtos estão aprendendo (precisam de uns 7 dias de histórico).`
                : "Nada por aqui com esses filtros."}
        </p>
      ) : (
        <ul className={`space-y-3 ${loading ? "opacity-60" : ""}`}>
          {data?.items.map((offer) => <OfferCard key={offer.id} offer={offer} canSend={canSend} coupons={coupons} onChanged={() => setReloadKey((k) => k + 1)} />)}
        </ul>
      )}
    </div>
  );
}
