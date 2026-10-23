import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, type Offer, type OffersResponse, type Status } from "../api";
import { CopyButton, ErrorBox, ScoreBadge, Spinner, StatusBadge } from "../components";
import { STATUS_HINT, STATUS_LABEL, dateTimeLabel, money } from "../format";

const STATUSES: Status[] = ["offer", "learning", "no_offer", "irrelevant"];

function isStatus(value: string | null): value is Status {
  return STATUSES.includes(value as Status);
}

function OfferCard({ offer }: { offer: Offer }) {
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
          {offer.affiliate_url && <span className="text-xs text-emerald-700">link de afiliado ✓</span>}
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
        <div className="mt-2 flex flex-wrap gap-2">
          <CopyButton text={offer.message} />
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
  const status: Status = isStatus(params.get("status")) ? (params.get("status") as Status) : "offer";
  const q = params.get("q") ?? "";
  const minScore = Number(params.get("min") ?? 0) || 0;

  const [search, setSearch] = useState(q);
  const [data, setData] = useState<OffersResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

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
      .offers({ status, q, minScore })
      .then((body) => !cancelled && (setData(body), setError("")))
      .catch((exc: Error) => !cancelled && setError(exc.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [status, q, minScore]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2" role="tablist">
        {STATUSES.map((s) => (
          <button
            key={s}
            role="tab"
            aria-selected={s === status}
            title={STATUS_HINT[s]}
            onClick={() => update({ status: s === "offer" ? "" : s })}
            className={`rounded-full px-3 py-1 text-sm font-medium ring-1 ring-inset ${
              s === status ? "bg-slate-900 text-white ring-slate-900" : "bg-white text-slate-700 ring-slate-300 hover:bg-slate-50"
            }`}
          >
            {STATUS_LABEL[s]} <span className="opacity-70">{data?.counts[s] ?? ""}</span>
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
          {status === "offer" && data.counts.learning > 0
            ? `Nenhuma oferta ainda: ${data.counts.learning} produtos estão aprendendo (precisam de uns 7 dias de histórico).`
            : "Nada por aqui com esses filtros."}
        </p>
      ) : (
        <ul className={`space-y-3 ${loading ? "opacity-60" : ""}`}>{data?.items.map((offer) => <OfferCard key={offer.id} offer={offer} />)}</ul>
      )}
    </div>
  );
}
